import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch
from pathlib import Path
from fastapi.testclient import TestClient
from worker import Worker, Problem, extract_source, CATEGORIES, atomic_json, acquire_service_lock
from api import create_app

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ios-worker-safety-')
        self.mount = Path(self.tmp.name).resolve()
        self.root = self.mount / 'worker'
        real_is_mount = Path.is_mount
        fixture_mount = self.mount
        self.mount_patch = patch.object(Path, 'is_mount', lambda p: p == fixture_mount or real_is_mount(p))
        self.mount_patch.start()
        for name in [*CATEGORIES.values(),'Repos','Archives','Logs','Simulators','Service/state']:
            (self.root/name).mkdir(parents=True,exist_ok=True)
        self.cfg = dict(root=str(self.root),mount=str(self.mount),volume_uuid='test-volume',owner='test-owner',
                        min_internal_free_gb=0,min_external_free_gb=0,hard_internal_free_gb=0,hard_external_free_gb=0,
                        developer_dir='/Applications/Xcode.app/Contents/Developer',build_token='test-build',admin_token='test-admin',
                        allowed_networks=['127.0.0.1/32'])
        atomic_json(self.root/'.worker-root.json',{'owner':'test-owner'})
        self.worker = Worker(self.cfg,lambda _: {'VolumeUUID':'test-volume','MountPoint':str(self.mount)})
    def tearDown(self):
        self.mount_patch.stop()
        self.tmp.cleanup()
    def cache(self,name='fixture'):
        p=self.worker.managed_dir('derived_data',name)
        (p/'cache.bin').write_bytes(b'test-cache')
        return p
    def test_wrong_volume_fails_closed(self):
        self.worker.reader=lambda _: {'VolumeUUID':'wrong','MountPoint':str(self.mount)}
        with self.assertRaises(Problem):self.worker.guard()
    def test_duplicate_service_cannot_recover_running_jobs(self):
        fd=acquire_service_lock(self.cfg,self.worker.reader)
        self.worker.state['jobs']['sentinel']={'id':'sentinel','status':'running'}
        self.worker.save()
        before=self.worker.state_file.read_bytes()
        script='''import json,sys
from pathlib import Path
from worker import acquire_service_lock,Worker,Problem
cfg=json.loads(sys.argv[1])
Path.is_mount=lambda p: p == Path(cfg['mount'])
reader=lambda _: {'VolumeUUID':'test-volume','MountPoint':cfg['mount']}
try:
    fd=acquire_service_lock(cfg,reader)
except Problem as exc:
    print(str(exc))
    sys.exit(0 if exc.status==409 else 2)
Worker(cfg,reader)
sys.exit(3)
'''
        try:
            result=subprocess.run([sys.executable,'-B','-c',script,json.dumps(self.cfg)],
                                  capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('already running',result.stdout)
            self.assertEqual(before,self.worker.state_file.read_bytes())
        finally:os.close(fd)
        fd=acquire_service_lock(self.cfg,self.worker.reader)
        os.close(fd)
    def test_service_lock_validates_volume_before_creating_file(self):
        with self.assertRaises(Problem):
            acquire_service_lock(self.cfg,lambda _: {'VolumeUUID':'wrong','MountPoint':str(self.mount)})
        self.assertFalse((self.root/'Service/state/server.lock').exists())
    def test_archive_and_keep_are_protected(self):
        p=self.cache();(p/'History.xcarchive').mkdir()
        q=self.cache('preserve');(q/'.keep').touch()
        self.assertEqual(self.worker.plan_cleanup(['derived_data'],0)['entries'],[])
        with self.assertRaises(Problem):self.worker.plan_cleanup(['Archives'],0)
        with self.assertRaises(Problem):self.worker.plan_cleanup(['Repos'],0)
    def test_plan_requires_matching_confirmation_and_unchanged_tree(self):
        p=self.cache();plan=self.worker.plan_cleanup(['derived_data'],0)
        with self.assertRaises(Problem):self.worker.execute_cleanup(plan['id'],'wrong')
        (p/'new-file').touch()
        with self.assertRaises(Problem):self.worker.execute_cleanup(plan['id'],plan['id'])
        self.assertTrue(p.exists())
    def test_delete_only_preview_and_prevent_replay(self):
        p=self.cache();outside=self.root/'Archives/precious.xcarchive';outside.mkdir()
        (outside/'keep').write_text('precious')
        (p/'link').symlink_to(outside,target_is_directory=True)
        plan=self.worker.plan_cleanup(['derived_data'],0)
        self.assertEqual(len(plan['entries']),1)
        self.worker.execute_cleanup(plan['id'],plan['id'])
        self.assertFalse(p.exists());self.assertEqual((outside/'keep').read_text(),'precious')
        with self.assertRaises(Problem):self.worker.execute_cleanup(plan['id'],plan['id'])
    def test_category_symlink_swap_rejected(self):
        p=self.cache();plan=self.worker.plan_cleanup(['derived_data'],0)
        parent=p.parent;renamed=parent.with_name('Original');parent.rename(renamed);parent.symlink_to(renamed,target_is_directory=True)
        with self.assertRaises(Problem):self.worker.execute_cleanup(plan['id'],plan['id'])
        with self.assertRaises(Problem):self.worker.plan_cleanup(['derived_data'],0)
    def test_busy_jobs_and_uploads_lock_cleanup(self):
        self.cache();plan=self.worker.plan_cleanup(['derived_data'],0)
        self.worker.active='busy'
        with self.assertRaises(Problem):self.worker.execute_cleanup(plan['id'],plan['id'])
        self.worker.active=None;self.worker.uploads=1
        with self.assertRaises(Problem):self.worker.plan_cleanup(['derived_data'],0)
    def test_zip_traversal_links_and_case_collisions_rejected(self):
        for index,entries in enumerate([['../escape'],['/absolute'],['a','A'],['.'],['history.xcarchive/file']]):
            archive=self.root/f'{index}.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for name in entries:z.writestr(name,'data')
            with self.assertRaises(Problem):extract_source(archive,self.root/f'out{index}')
        archive=self.root/'link.zip'
        with zipfile.ZipFile(archive,'w') as z:
            info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(info,'/etc')
        with self.assertRaises(Problem):extract_source(archive,self.root/'link-out')
    def test_bearer_ip_and_admin_boundaries(self):
        app=create_app(self.cfg,self.worker)
        client=TestClient(app,client=('127.0.0.1',1234))
        self.assertEqual(client.get('/v1/health').status_code,401)
        headers={'Authorization':'Bearer test-build'}
        self.assertEqual(client.get('/v1/health',headers=headers).status_code,200)
        self.assertEqual(client.post('/v1/storage/execute',headers=headers,json={}).status_code,403)
        outsider=TestClient(app,client=('203.0.113.5',1234))
        self.assertEqual(outsider.get('/v1/health',headers=headers).status_code,403)
    def test_existing_simulators_rejected(self):
        with self.assertRaises(Problem):self.worker.simulator('someone-elses-device')
    def test_testing_never_takes_over_a_booted_simulator(self):
        self.worker.simulator=Mock()
        self.worker.simulator_inventory=Mock(return_value={'devices':[{'udid':'shared','state':'Booted'}],'booted_ids':['shared']})
        self.worker.run=Mock()
        with self.assertRaises(Problem):self.worker.boot_simulator('job','shared',require_shutdown=True)
        self.worker.run.assert_not_called()
    def test_testing_requires_successful_owned_boot(self):
        self.worker.simulator=Mock()
        self.worker.simulator_inventory=Mock(return_value={'devices':[{'udid':'idle','state':'Shutdown'}],'booted_ids':[]})
        self.worker.run=Mock(side_effect=Problem('boot raced with another process'))
        with self.assertRaises(Problem):self.worker.boot_simulator('job','idle',require_shutdown=True)
        self.assertTrue(self.worker.run.call_args.kwargs['check'])
        self.assertEqual(self.worker.run.call_count,1)
    def test_queue_limit_and_cancellation(self):
        request={'action':'simulator','operation':'shutdown'}
        jobs=[self.worker.submit(request) for _ in range(20)]
        with self.assertRaises(Problem):self.worker.submit(request)
        self.assertTrue(self.worker.cancel(jobs[0]['id'])['cancellation_requested'])

if __name__=='__main__':unittest.main()
