import hashlib,json,os,time,unittest
from pathlib import Path
from unittest.mock import Mock
import test_safety
from worker import Problem
from sources import validate_manifest,clone_file

class DevelopmentTests(unittest.TestCase):
    setUp=test_safety.SafetyTests.setUp
    tearDown=test_safety.SafetyTests.tearDown
    def test_status_bar_parser_does_not_replace_real_overrides(self):
        from development import status_overrides_empty
        self.assertTrue(status_overrides_empty('Current Status Bar Overrides:\n=============================\n'))
        self.assertFalse(status_overrides_empty('Current Status Bar Overrides:\n=============================\ntime: 10:15\n'))
        self.assertFalse(status_overrides_empty('Unknown output'))
    def test_session_reservation_rejects_other_tasks(self):
        self.worker.state['sessions']['owner']={'id':'owner','status':'active','project_id':'app','bundle_id':'test.app','simulator_id':'device'}
        with self.assertRaises(Problem):self.worker.development.check_device({'simulator_id':'device'})
        with self.assertRaises(Problem):self.worker.development.check_device({'session_id':'owner','project_id':'other'})
        request={'session_id':'owner','project_id':'app'}
        self.assertEqual(self.worker.development.check_device(request),'device')
        self.assertEqual(request['simulator_id'],'device')
    def test_source_sync_is_idempotent_and_workspace_preserves_unchanged_mtime(self):
        files=[]
        for name,value in [('App.swift',b'first'),('old.txt',b'old')]:
            h=hashlib.sha256(value).hexdigest();p=self.worker.sources.blob(h);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
            files.append({'path':name,'sha256':h,'size':len(value),'executable':False})
        plan=self.worker.sources.plan('app',files);rec=self.worker.sources.commit('app',plan['plan_id'])
        self.assertEqual(rec,self.worker.sources.commit('app',plan['plan_id']))
        root,info=self.worker.sources.workspace('app',rec['id']);mtime=(root/'App.swift').stat().st_mtime_ns
        (root/'Generated.xcodeproj').mkdir();(root/'Generated.xcodeproj/project.pbxproj').write_text('generated')
        self.assertTrue(self.worker.sources.plan('app',files)['reused'])
        root,info=self.worker.sources.workspace('app',rec['id']);self.assertEqual(info['changed_files'],0)
        self.assertEqual(mtime,(root/'App.swift').stat().st_mtime_ns)
        plan=self.worker.sources.plan('app',files[:1]);rec2=self.worker.sources.commit('app',plan['plan_id'])
        root,info=self.worker.sources.workspace('app',rec2['id']);self.assertFalse((root/'old.txt').exists())
        self.assertTrue((root/'Generated.xcodeproj/project.pbxproj').exists())
        self.assertTrue((self.root/rec['path']/'old.txt').exists())
        (root/'App.swift').write_text('mutated by build')
        self.worker.sources.workspace('app',rec2['id']);self.assertEqual((root/'App.swift').read_bytes(),b'first')
    def test_invalid_source_manifest_and_corrupt_blob_rejected(self):
        for name in ['../escape','/escape','.git/config','history.xcarchive/Info.plist','a\\b','a:b']:
            with self.assertRaises(Problem):validate_manifest([{'path':name,'sha256':'0'*64,'size':0}])
        with self.assertRaises(Problem):validate_manifest([{'path':x,'sha256':'0'*64,'size':0} for x in ['a','a/b']])
        h=hashlib.sha256(b'correct').hexdigest();plan=self.worker.sources.plan('app',[{'path':'file','sha256':h,'size':7}])
        blob=self.worker.sources.blob(h);blob.parent.mkdir(parents=True);blob.write_bytes(b'corrupt')
        with self.assertRaises(Problem):self.worker.sources.commit('app',plan['plan_id'])
        self.assertNotIn('app',self.worker.state['projects'])
    def test_close_does_not_shutdown_unowned_device(self):
        s={'id':'s','simulator_id':'d','status':'failed','booted_by_worker':False}
        self.worker.run=Mock();self.worker.development.end('job',s);self.worker.run.assert_not_called()
    def test_registration_requires_admin(self):
        from api import create_app
        from fastapi.testclient import TestClient
        client=TestClient(create_app(self.cfg,self.worker),client=('127.0.0.1',1234))
        r=client.post('/v1/jobs',headers={'Authorization':'Bearer test-build'},json={'action':'register_device','simulator_id':'device'})
        self.assertEqual(r.status_code,403)
        r=client.post('/v1/sources/retention/execute',headers={'Authorization':'Bearer test-build'},json={})
        self.assertEqual(r.status_code,403)

    def seed_revision(self,value):
        digest=hashlib.sha256(value).hexdigest();p=self.worker.sources.blob(digest)
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
        plan=self.worker.sources.plan('app',[{'path':'file','sha256':digest,'size':len(value)}])
        return self.worker.sources.commit('app',plan['plan_id'])

    def test_retention_protects_archive_sources_and_validates_preview(self):
        archive=self.seed_revision(b'archived');old=self.seed_revision(b'old');latest=self.seed_revision(b'latest')
        self.worker.state['jobs']['archive']={'status':'succeeded','request':{'action':'archive','project_id':'app','revision':archive['id']}}
        plan=self.worker.sources.retention_plan('app',1,0)
        self.assertEqual([e['revision'] for e in plan['entries'] if e['kind']=='revision'],[old['id']])
        with self.assertRaises(Problem):self.worker.sources.retention_execute(plan['id'],'wrong')
        (self.root/old['path']/'changed').touch()
        with self.assertRaises(Problem):self.worker.sources.retention_execute(plan['id'],plan['id'])
        plan=self.worker.sources.retention_plan('app',1,0)
        self.worker.sources.retention_execute(plan['id'],plan['id'])
        self.assertFalse((self.root/old['path']).exists())
        self.assertTrue((self.root/archive['path']).is_dir());self.assertTrue((self.root/latest['path']).is_dir())
        with self.assertRaises(Problem):self.worker.sources.retention_execute(plan['id'],plan['id'])

    def test_sessions_block_all_storage_deletion(self):
        self.seed_revision(b'first');self.seed_revision(b'second')
        cache_plan=self.worker.plan_cleanup(['derived_data'],0)
        source_plan=self.worker.sources.retention_plan('app',1,0)
        for status in ['active','opening','interrupted']:
            self.worker.state['sessions']['busy']={'status':status}
            with self.assertRaises(Problem):self.worker.plan_cleanup(['results'],0)
            with self.assertRaises(Problem):self.worker.execute_cleanup(cache_plan['id'],cache_plan['id'])
            with self.assertRaises(Problem):self.worker.sources.retention_plan('app',1,0)
            with self.assertRaises(Problem):self.worker.sources.retention_execute(source_plan['id'],source_plan['id'])

    def test_retention_keeps_live_uploads_and_rejects_symlink_revision(self):
        old=self.seed_revision(b'old');self.seed_revision(b'new')
        digest=hashlib.sha256(b'pending').hexdigest();blob=self.worker.sources.blob(digest);blob.parent.mkdir(parents=True,exist_ok=True);blob.write_bytes(b'pending')
        self.worker.sources.plan('app',[{'path':'pending','sha256':digest,'size':7}])
        plan=self.worker.sources.retention_plan('app',1,0)
        self.assertNotIn(digest,[x.get('sha256') for x in plan['entries']])
        p=self.root/old['path'];moved=p.with_name('renamed');p.rename(moved);p.symlink_to(moved,target_is_directory=True)
        with self.assertRaises(Problem):self.worker.sources.retention_execute(plan['id'],plan['id'])

if __name__=='__main__':unittest.main()
