"""Install or upgrade the portable skill, preserving existing private profiles."""
import argparse,csv,datetime,json,os,shutil,subprocess,sys,uuid
from pathlib import Path

def validate_tree(root):
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob('*')):
        raise ValueError('Skill package and private profiles must not contain symbolic links')

def install(destination,upgrade=False):
    source=Path(__file__).resolve().parents[1]
    destination=Path(destination).expanduser().absolute()
    if destination==source or destination.resolve().is_relative_to(source):raise ValueError('Install from a separate extracted package')
    validate_tree(source)
    exists=destination.exists() or destination.is_symlink()
    if exists:
        if not upgrade:raise ValueError('Destination exists; use --upgrade to preserve credentials and back up the previous skill')
        if destination.is_symlink() or not (destination/'SKILL.md').is_file() or 'name: ios-worker' not in (destination/'SKILL.md').read_text():raise ValueError('Upgrade destination is not an ios-worker skill')
        if (destination/'private').exists():validate_tree(destination/'private')
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=destination.with_name('.ios-worker-stage-'+uuid.uuid4().hex)
    stage.mkdir(mode=0o700);private=stage/'private';private.mkdir(mode=0o700)
    backup=None
    try:
        if os.name=='nt':
            rows=csv.reader(subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True).splitlines())
            sid=next(cell for row in rows for cell in row if cell.startswith('S-1-'))
            subprocess.run(['icacls',str(private),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F','/Q'],check=True,stdout=subprocess.DEVNULL)
        for p in source.rglob('*'):
            if '__pycache__' in p.parts or p.suffix=='.pyc':continue
            target=stage/p.relative_to(source)
            if p.is_dir():target.mkdir(exist_ok=True,mode=0o700)
            else:
                shutil.copyfile(p,target)
                if os.name!='nt':target.chmod(0o600)
        if exists and (destination/'private').exists():
            for p in (destination/'private').rglob('*'):
                target=private/p.relative_to(destination/'private')
                if p.is_dir():target.mkdir(exist_ok=True,mode=0o700)
                else:
                    shutil.copyfile(p,target)
                    if os.name!='nt':target.chmod(0o600)
        if exists:
            backup=destination.parent.parent/'skill-backups'/('ios-worker-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
            backup.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            destination.rename(backup)
        try:stage.rename(destination)
        except BaseException:
            if backup:backup.rename(destination)
            raise
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {'installed':str(destination),'version':'2.0.0','backup':str(backup) if backup else None,'private_profiles_preserved':exists,'next':'Start a new agent session and ask ios-worker for status. No MCP setup is required.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent',choices=['codex','claude'],default='codex')
    parser.add_argument('--destination',help='Exact destination skill directory for another agent')
    parser.add_argument('--upgrade',action='store_true')
    args=parser.parse_args()
    path=args.destination or Path.home()/('.agents' if args.agent=='codex' else '.claude')/'skills/ios-worker'
    try:print(json.dumps(install(path,args.upgrade),ensure_ascii=False,indent=2))
    except Exception as exc:print(json.dumps({'error':str(exc)},ensure_ascii=False));sys.exit(1)
