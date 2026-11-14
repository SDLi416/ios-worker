import importlib.util
import json
import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts'))
from check_public import findings
import provision


class PublicExportTests(unittest.TestCase):
    def test_detects_private_data_without_reporting_values(self):
        address='192.'+'168.'+'1.9'
        self.assertIn('private-lan-address',findings('README.md',address.encode()))
        home='/Us'+'ers/someone/project'
        self.assertIn('personal-home-path',findings('README.md',home.encode()))
        self.assertIn('runtime-or-private-file',findings('skills/ios-worker/private/connection.json',b'{}'))
        key='-----BEGIN '+'OPENSSH'+' PRIVATE KEY-----'
        self.assertIn('credential-pattern',findings('example.txt',key.encode()))
        self.assertEqual(findings('config/connection.example.json',b'{"url":"https://mac-worker.example.test:9443"}'),[])
        self.assertEqual(findings('tests/example.py',b'host="203.0.113.10"'),[])

    def test_provision_refuses_overwrite_and_unsafe_origin(self):
        with tempfile.TemporaryDirectory(prefix='ios-worker-public-test-') as folder:
            existing=Path(folder)/'existing';existing.mkdir();(existing/'precious').write_text('keep')
            with self.assertRaises(ValueError):provision.validate_options(existing,'https://localhost:9443',[],'127.0.0.1')
            for url in ['http://localhost:9443','https://user:password@localhost:9443','https://localhost/path','https://<host>:9443']:
                with self.assertRaises(ValueError):provision.validate_options(Path(folder)/'new',url,[],'127.0.0.1')
            with self.assertRaises(ValueError):provision.validate_options(Path(folder)/'new','https://localhost:9443',['0.0.0.0/0'],'127.0.0.1')
            self.assertEqual((existing/'precious').read_text(),'keep')

    @unittest.skipUnless(sys.platform=='darwin','provisioner uses macOS openssl path')
    def test_new_deployment_generates_unique_private_profiles_and_valid_certificate(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix='ios-worker-provision-test-') as folder:
            mount=Path(folder).resolve();root=mount/'deployment'
            with patch.object(provision,'external_volume_for',return_value=(mount,'test-volume')):
                result=provision.provision(root,'https://127.0.0.1:9443',['203.0.113.10/32'])
            cfg=json.loads((root/'Service/config/config.json').read_text())
            profiles=root/'Client/ios-worker/private'
            build=json.loads((profiles/'connection.json').read_text());admin=json.loads((profiles/'admin-connection.json').read_text())
            self.assertEqual(build['token'],cfg['build_token']);self.assertEqual(admin['token'],cfg['admin_token'])
            self.assertNotEqual(build['token'],admin['token']);self.assertGreaterEqual(len(build['token']),40)
            cert=(profiles/'server.crt').read_text();ssl.create_default_context(cafile=str(profiles/'server.crt'))
            self.assertEqual(build['certificate_sha256'],hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest())
            self.assertEqual(cfg['host'],'127.0.0.1');self.assertEqual(cfg['default_simulator'],None)
            self.assertEqual(cfg['volume_uuid'],'test-volume')
            self.assertIn('203.0.113.10/32',cfg['allowed_networks'])
            self.assertEqual((profiles/'admin-connection.json').stat().st_mode&0o777,0o600)
            self.assertTrue((root/'Service/src/serve.py').is_file())
            self.assertFalse((root/'Service/state/state.json').exists())
            self.assertNotIn(build['token'],json.dumps(result));self.assertNotIn(admin['token'],json.dumps(result))
            self.assertEqual(json.loads((root/'.worker-root.json').read_text())['owner'],cfg['owner'])
            with patch.object(provision,'external_volume_for',return_value=(mount,'test-volume')):
                with self.assertRaises(ValueError):provision.provision(root,'https://127.0.0.1:9443')


if __name__=='__main__':unittest.main()
