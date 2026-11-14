"""Snapshot the current Git working tree, including non-ignored untracked files."""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

def collect_files(source):
    source=Path(source).resolve()
    def git(*args):return subprocess.check_output(['git',*args],cwd=source)
    root=Path(os.fsdecode(git('rev-parse','--show-toplevel')).strip()).resolve()
    if root!=source:raise ValueError('Source must be the repository root')
    modes={}
    for entry in git('ls-files','--stage','-z').split(b'\0'):
        if not entry:continue
        meta,name=entry.split(b'\t',1);mode,_,stage=meta.split()
        if stage!=b'0':raise ValueError('Resolve merge conflicts before building')
        if mode==b'160000':raise ValueError('Submodule detected: prepare a complete ZIP explicitly')
        if mode==b'120000':raise ValueError('Git symlink detected: prepare a reviewed source snapshot explicitly')
        modes[os.fsdecode(name)]=int(mode,8)
    names=sorted(set(os.fsdecode(n) for n in git('ls-files','--cached','--others','--exclude-standard','-z').split(b'\0') if n))
    files=[]
    for name in names:
        p=source/name
        if not p.exists() and not p.is_symlink():continue  # deleted working-tree file
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(source):
            raise ValueError('Snapshot cannot include links or special files: '+name)
        if any(x.lower().endswith('.xcarchive') for x in p.relative_to(source).parts):
            raise ValueError('Archive found among source files: '+name)
        if p.suffix.lower() in {'.p12','.pfx','.p8','.key','.pem'} or (p.name.startswith('.env') and p.name not in {'.env.example','.env.sample','.env.template'}):
            raise ValueError('Potential credential file found; prepare a reviewed ZIP explicitly: '+name)
        with open(p,'rb') as stream:prefix=stream.read(100)
        if prefix.startswith(b'version https://git-lfs.github.com/spec/v1'):
            raise ValueError('Unresolved Git LFS pointer: '+name)
        mode = (stat.S_IFREG | (p.stat().st_mode&0o777)) if os.name!='nt' else modes.get(name,stat.S_IFREG|0o644)
        files.append((name,p,mode))
    if not files or len(files)>100000:raise ValueError('Source snapshot is empty or has over 100,000 files')
    if sum(p.stat().st_size for _,p,_ in files)>8_000_000_000:raise ValueError('Expanded source exceeds 8 GB')
    return files

def snapshot(source, output):
    source=Path(source).resolve();output=Path(output).resolve()
    if output.is_relative_to(source):raise ValueError('Put the ZIP outside the source repository')
    files=collect_files(source)
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as z:
        for name,p,mode in files:
            info=zipfile.ZipInfo.from_file(p,name);info.external_attr=mode<<16;info.compress_type=zipfile.ZIP_DEFLATED
            with open(p,'rb') as src,z.open(info,'w') as dst:
                import shutil
                shutil.copyfileobj(src,dst,1024*1024)
    digest=hashlib.sha256()
    with open(output,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):digest.update(b)
    return {'zip_path':str(output),'file_count':len(files),'sha256':digest.hexdigest(),'includes_working_tree_changes':True}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True)
    a=p.parse_args()
    try:print(json.dumps(snapshot(a.source,a.output),ensure_ascii=False,indent=2))
    except Exception as e:print(json.dumps({'error':str(e)},ensure_ascii=False));raise SystemExit(1)
