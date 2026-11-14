"""Check the files about to be published for runtime data and common private identifiers.

This supplements a dedicated secret scanner; it cannot prove a file is non-sensitive.
Only filenames and rule names are printed when a check fails.
"""
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PARTS={'private','Service','Delivery','Repos','Caches','Results','Archives','Temporary',
                 'Simulators','Sessions','SourceStore','Workspaces','Client','backups','outputs','.venv','venv','__pycache__'}
FORBIDDEN_SUFFIXES={'.key','.pem','.crt','.cer','.der','.p12','.pfx','.p8','.mobileprovision',
                    '.ips','.crash','.log','.zip','.gz','.dmg','.pkg','.mp4','.pyc'}
PRIVATE_HOME=re.compile(r'(?:/Us' r'ers/[^/\s\'"<>]+|[A-Z]:[/\\]Users[/\\][^/\\\s\'"<>]+)')
IPV4=re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
MARKERS=['-----BEGIN '+'PRIVATE KEY-----']+['-----BEGIN '+kind+' PRIVATE KEY-----' for kind in ['RSA', 'EC', 'OPENSSH']]
CREDENTIALS=re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[A-Z0-9]{16})\b')


def findings(name, data):
    path=Path(name);problems=[]
    if set(path.parts)&FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in {'connection.json','admin-connection.json','state.json','.worker-root.json'}:
        problems.append('runtime-or-private-file')
    if b'\x00' in data:
        return problems+['unexpected-binary']
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:return problems+['non-utf8-file']
    if PRIVATE_HOME.search(text):problems.append('personal-home-path')
    if any(marker in text for marker in MARKERS) or CREDENTIALS.search(text):problems.append('credential-pattern')
    for match in IPV4.finditer(text):
        try:ip=ipaddress.ip_address(match[0])
        except ValueError:continue
        if ip in ipaddress.ip_network('10.0.0.0/8') or ip in ipaddress.ip_network('172.16.0.0/12') or ip in ipaddress.ip_network('192.168.0.0/16'):
            # The checker needs these three network definitions; they are public ranges.
            if name.replace('\\','/')=='scripts/check_public.py' and match[0] in {'10.0.0.0','172.16.0.0','192.168.0.0'}:continue
            problems.append('private-lan-address');break
    return list(dict.fromkeys(problems))


def main():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['git','-C',str(root),'ls-files','-z'],check=True,capture_output=True)
    names=[name for name in result.stdout.decode().split('\0') if name]
    if not names:raise SystemExit('Stage the source export before running the publication check')
    failed=[]
    for name in names:
        path=root/name
        if path.is_symlink():failed.append((name,['symbolic-link']));continue
        failed.extend((name,[rule]) for rule in findings(name,path.read_bytes()))
    if failed:
        for name,rules in failed:print(name+': '+', '.join(rules),file=sys.stderr)
        return 1
    print('Public export check passed: '+str(len(names))+' tracked UTF-8 source/documentation files')
    return 0


if __name__=='__main__':sys.exit(main())
