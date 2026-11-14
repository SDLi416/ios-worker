"""Fetch the pinned upstream WebDriverAgent and build it on the external volume."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
UPSTREAM='https://github.com/appium/WebDriverAgent.git'
COMMIT='3d6a4e03cdd673e540289c610f7e189f8ffea434'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    args=parser.parse_args()
    root=Path(args.root).expanduser().resolve()
    sys.path.insert(0,str(REPO/'server'))
    from worker import storage_guard
    cfg=json.loads((root/'Service/config/config.json').read_text())
    storage_guard(cfg)
    statefile=root/'Service/state/state.json'
    if statefile.exists():
        state=json.loads(statefile.read_text())
        if any(j['status'] in {'queued','running'} for j in state['jobs'].values()) or any(s['status'] in {'active','opening','interrupted'} for s in state.get('sessions',{}).values()):
            raise SystemExit('Wait for jobs and close sessions before preparing the UI controller')
    base=root/'Service/automation';source=base/'WebDriverAgent';base.mkdir(exist_ok=True,mode=0o700)
    env=dict(os.environ,TMPDIR=str(root/'Service/work')+'/',DEVELOPER_DIR=cfg['developer_dir'],
             XDG_CACHE_HOME=str(base/'cache'),CLANG_MODULE_CACHE_PATH=str(base/'ModuleCache'),
             SWIFT_MODULE_CACHE_PATH=str(base/'ModuleCache'))
    def git(*arguments):
        return subprocess.check_output(['/usr/bin/git','-C',str(source),*arguments],text=True,env=env).strip()
    if not source.exists():
        subprocess.run(['/usr/bin/git','clone','--no-checkout',UPSTREAM,str(source)],env=env,check=True)
        git('checkout','--detach',COMMIT)
    if source.is_symlink() or git('rev-parse','HEAD')!=COMMIT or git('status','--porcelain'):
        raise SystemExit('UI source differs from the pinned clean checkout; preserve it before preparing a replacement')
    with open(base/'build.log','ab') as log:
        subprocess.run(['/usr/bin/xcodebuild','-project',str(source/'WebDriverAgent.xcodeproj'),'-scheme','WebDriverAgentRunner',
                        '-destination','generic/platform=iOS Simulator','-derivedDataPath',str(base/'DerivedData'),'-jobs',str(cfg.get('build_jobs',4)),
                        'build-for-testing','CODE_SIGNING_ALLOWED=NO','COMPILER_INDEX_STORE_ENABLE=NO','INDEX_ENABLE_DATA_STORE=NO',
                        'CLANG_MODULE_CACHE_PATH='+str(base/'ModuleCache'),'SWIFT_MODULE_CACHE_PATH='+str(base/'ModuleCache')],
                       env=env,check=True,stdout=log,stderr=subprocess.STDOUT)
    (base/'source.json').write_text(json.dumps({'repository':UPSTREAM,'commit':COMMIT,'version':'16.15.3','listen_policy':'HTTP and MJPEG on 127.0.0.1 only'},indent=2))
    print(json.dumps({'prepared':str(base),'commit':COMMIT,'log':str(base/'build.log')},indent=2))


if __name__=='__main__':main()
