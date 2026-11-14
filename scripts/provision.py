"""Create a new external-volume deployment and an owner-only client bundle.

No existing deployment is overwritten. This script does not start a service,
change login items, or create/boot/register a simulator.
"""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, 'x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2)


def external_volume_for(root):
    parent = root.parent
    while not parent.exists():
        parent = parent.parent
    mount = parent.resolve()
    while not mount.is_mount():
        if mount == mount.parent:
            raise ValueError('Cannot identify the external volume')
        mount = mount.parent
    result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(mount)],
                            check=True, capture_output=True, timeout=15)
    info = plistlib.loads(result.stdout)
    if info.get('Internal') is not False or info.get('MountPoint') != str(mount) or not info.get('VolumeUUID'):
        raise ValueError('Choose a mounted external volume with a VolumeUUID')
    return mount, info['VolumeUUID']


def validate_options(root, url, networks, listen):
    root = Path(root).expanduser().absolute()
    if root.is_symlink() or root.resolve().is_relative_to(REPO):
        raise ValueError('Use a separate external deployment directory, outside this checkout')
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('Destination is not empty; existing files were left unchanged')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTPS origin without credentials, path, query or fragment')
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError('Invalid certificate hostname')
    port = parsed.port or 443
    allowed = ['127.0.0.1/32', '::1/128']
    for value in networks:
        network = ipaddress.ip_network(value, strict=False)
        if network.prefixlen == 0:
            raise ValueError('Do not allow an entire address family; use a client IP or a trusted narrow subnet')
        if str(network) not in allowed:
            allowed.append(str(network))
    ipaddress.ip_address(listen)
    return root, url.rstrip('/'), host, port, allowed


def provision(root, url, networks=(), listen='127.0.0.1', default_simulator=None):
    root, url, hostname, port, allowed = validate_options(root, url, networks, listen)
    mount, volume_uuid = external_volume_for(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in ['Repos', 'Caches', 'Results', 'Archives', 'Temporary', 'Simulators', 'Logs',
                   'SourceStore', 'Workspaces', 'Sessions', 'Service/config', 'Service/state',
                   'Service/logs', 'Service/work']:
        (root/folder).mkdir(parents=True, exist_ok=True, mode=0o700)
    private_json(root/'.worker-root.json', {'owner': (owner := str(uuid.uuid4()))})
    template = json.loads((REPO/'config/server.example.json').read_text())
    cfg = dict(template, root=str(root), mount=str(mount), volume_uuid=volume_uuid, owner=owner,
               host=listen, port=port, allowed_networks=allowed, default_simulator=default_simulator,
               build_token=secrets.token_urlsafe(32), admin_token=secrets.token_urlsafe(32),
               tls_cert=str(root/'Service/config/server.crt'), tls_key=str(root/'Service/config/server.key'))
    try:
        ipaddress.ip_address(hostname)
        san = 'IP:' + hostname
    except ValueError:
        san = 'DNS:' + hostname
    tls_config = root/'Service/config/openssl.cnf'
    tls_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=server\n'
                          '[dn]\nCN=iOS Worker\n[server]\nbasicConstraints=critical,CA:TRUE\n'
                          'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
                          'extendedKeyUsage=serverAuth\nsubjectAltName='+san+'\n')
    tls_config.chmod(0o600)
    subprocess.run(['/usr/bin/openssl', 'req', '-new', '-x509', '-newkey', 'rsa:3072', '-nodes',
                    '-days', '365', '-config', str(tls_config), '-keyout', cfg['tls_key'],
                    '-out', cfg['tls_cert']], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    Path(cfg['tls_key']).chmod(0o600)
    Path(cfg['tls_cert']).chmod(0o600)
    private_json(root/'Service/config/config.json', cfg)
    shutil.copytree(REPO/'server', root/'Service/src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(REPO/'requirements.lock', root/'Service/requirements.lock')
    skill = root/'Client/ios-worker'
    shutil.copytree(REPO/'skills/ios-worker', skill, ignore=shutil.ignore_patterns('private', '__pycache__', '*.pyc'))
    private = skill/'private'
    private.mkdir(mode=0o700)
    shutil.copy2(cfg['tls_cert'], private/'server.crt')
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(cfg['tls_cert']).read_text())).hexdigest()
    for name, token in [('connection.json', cfg['build_token']), ('admin-connection.json', cfg['admin_token'])]:
        private_json(private/name, {'url':url, 'token':token, 'ca_file':'server.crt', 'certificate_sha256':pin})
    for item in root.rglob('*'):
        if item.is_file():
            item.chmod(0o600)
    return {'deployment': str(root), 'client_bundle': str(skill), 'listen_address': listen,
            'next': 'Install Service/requirements.lock into Service/venv, then run the server or build the menu-bar launcher. Keep Client private.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--url', required=True, help='HTTPS origin the client can reach; included in the certificate SAN')
    parser.add_argument('--allow-client', action='append', default=[], help='Repeat for each trusted client IP/CIDR')
    parser.add_argument('--listen', default='127.0.0.1', help='Use an explicit LAN address or 0.0.0.0 for remote access')
    parser.add_argument('--default-simulator', help='Optional existing device UDID; still requires registration')
    args = parser.parse_args()
    if sys.platform != 'darwin' or sys.version_info < (3,11):
        parser.error('Provisioning requires macOS and Python 3.11+')
    os.umask(0o077)
    try:
        print(json.dumps(provision(args.root, args.url, args.allow_client, args.listen, args.default_simulator), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
