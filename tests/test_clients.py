"""Portable client checks: no Worker connection, credentials or Apple SDK needed."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS=Path(__file__).resolve().parents[1]/'skills/ios-worker/scripts'
sys.path.insert(0,str(SCRIPTS))
import snapshot
spec=importlib.util.spec_from_file_location('skill_install',SCRIPTS/'install.py')
skill_install=importlib.util.module_from_spec(spec);spec.loader.exec_module(skill_install)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ios-worker-client-test-')
        self.base=Path(self.tmp.name);self.repo=self.base/'repository';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        (self.repo/'App.swift').write_text('original',encoding='utf-8')
        (self.repo/'.gitignore').write_text('ignored.txt\n',encoding='utf-8')
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        (self.repo/'App.swift').write_text('uncommitted change',encoding='utf-8')
        (self.repo/'new file.txt').write_text('untracked',encoding='utf-8')
        (self.repo/'ignored.txt').write_text('do not upload',encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_uses_dirty_worktree_and_nonignored_untracked_files(self):
        output=self.base/'source.zip'
        result=subprocess.run([sys.executable,'-B',str(SCRIPTS/'snapshot.py'),'--source',str(self.repo),'--output',str(output)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()),{'.gitignore','App.swift','new file.txt'})
            self.assertEqual(archive.read('App.swift'),b'uncommitted change')

    def test_snapshot_refuses_credential_like_inputs(self):
        (self.repo/'.env').write_text('fixture only',encoding='utf-8')
        with self.assertRaises(ValueError):snapshot.collect_files(self.repo)

    def test_upgrade_keeps_local_profiles_and_a_backup(self):
        destination=self.base/'agent/skills/ios-worker'
        skill_install.install(destination)
        local=destination/'private/local-settings.txt';local.write_text('local profile',encoding='utf-8')
        old=(destination/'SKILL.md').read_text();(destination/'SKILL.md').write_text(old+'\nPrevious installation marker\n',encoding='utf-8')
        with self.assertRaises(ValueError):skill_install.install(destination)
        result=skill_install.install(destination,upgrade=True)
        self.assertEqual(local.read_text(),'local profile')
        self.assertIn('Previous installation marker',(Path(result['backup'])/'SKILL.md').read_text())
        self.assertNotIn('Previous installation marker',(destination/'SKILL.md').read_text())

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'),'PowerShell not installed')
    def test_powershell_manifest_includes_hidden_and_dirty_files(self):
        shell=shutil.which('powershell') or shutil.which('pwsh')
        result=subprocess.run([shell,'-NoProfile','-File',str(SCRIPTS/'snapshot.ps1'),'-Source',str(self.repo),'-ManifestOnly'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        files=json.loads(result.stdout)['files']
        self.assertEqual({f['path'] for f in files},{'.gitignore','App.swift','new file.txt'})
        self.assertEqual(next(f['size'] for f in files if f['path']=='App.swift'),len(b'uncommitted change'))

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'),'PowerShell not installed')
    def test_powershell_accepts_a_repository_directory_alias(self):
        alias=self.base/'repository-alias'
        try:alias.symlink_to(self.repo,target_is_directory=True)
        except OSError as exc:self.skipTest('Directory links unavailable: '+str(exc))
        shell=shutil.which('powershell') or shutil.which('pwsh')
        result=subprocess.run([shell,'-NoProfile','-File',str(SCRIPTS/'snapshot.ps1'),'-Source',str(alias),'-ManifestOnly'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual({f['path'] for f in json.loads(result.stdout)['files']},{'.gitignore','App.swift','new file.txt'})

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'),'PowerShell not installed')
    def test_powershell_still_rejects_a_repository_subdirectory(self):
        nested=self.repo/'nested';nested.mkdir()
        shell=shutil.which('powershell') or shutil.which('pwsh')
        result=subprocess.run([shell,'-NoProfile','-File',str(SCRIPTS/'snapshot.ps1'),'-Source',str(nested),'-ManifestOnly'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Source must be the repository root',result.stdout)


if __name__=='__main__':unittest.main()
