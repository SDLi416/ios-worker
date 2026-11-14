"""Build an optional menu-bar launcher inside an existing external deployment."""
import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    args=parser.parse_args()
    root=Path(args.root).expanduser().resolve()
    sys.path.insert(0,str(REPO/'server'))
    from worker import storage_guard
    config=json.loads((root/'Service/config/config.json').read_text())
    storage_guard(config)
    app=root/'Service/iOS Worker.app'
    if app.exists():raise SystemExit('Launcher already exists; preserve or move it before rebuilding')
    binary=app/'Contents/MacOS/WorkerLauncher';binary.parent.mkdir(parents=True,mode=0o700)
    info={'CFBundleIdentifier':'dev.iosworker.launcher','CFBundleName':'iOS Worker',
          'CFBundleExecutable':'WorkerLauncher','CFBundlePackageType':'APPL','CFBundleVersion':'2.0.0',
          'LSUIElement':True,'IOSWorkerRoot':str(root)}
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    env=dict(os.environ,TMPDIR=str(root/'Service/work')+'/',CLANG_MODULE_CACHE_PATH=str(root/'Service/work/swift-module-cache'))
    subprocess.run(['/usr/bin/xcrun','swiftc','-O','-framework','AppKit','-module-cache-path',
                    str(root/'Service/work/swift-module-cache'),str(REPO/'server/Launcher.swift'),'-o',str(binary)],env=env,check=True)
    subprocess.run(['/usr/bin/codesign','--sign','-','--identifier','dev.iosworker.launcher',str(app)],check=True)
    print(json.dumps({'launcher':str(app),'next':'Open this app yourself and grant removable-volume access if macOS asks. Add it to Login Items only if desired.'},indent=2))


if __name__=='__main__':main()
