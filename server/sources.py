"""Content-addressed source sync and a stable, worker-owned build workspace."""
from __future__ import annotations
import ctypes, hashlib, json, os, re, shutil, stat, subprocess, time, uuid
from pathlib import Path, PurePosixPath
from worker import Problem, ident, within, atomic_json, sha256_file, GB

SHA=re.compile(r'[0-9a-f]{64}')

def clone_file(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    clone=ctypes.CDLL(None,use_errno=True).clonefile
    clone.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_int];clone.restype=ctypes.c_int
    if clone(os.fsencode(src),os.fsencode(dst),0)!=0:shutil.copy2(src,dst)


def validate_manifest(files):
    if not isinstance(files,list) or not 1<=len(files)<=100000:raise Problem('Manifest must contain 1–100000 files')
    result=[];seen=set();total=0
    for f in files:
        name=f.get('path');p=PurePosixPath(name) if isinstance(name,str) else None
        if not p or not p.parts or p.is_absolute() or any(x in {'..','.git'} or x.endswith('.xcarchive') for x in p.parts) or '\\' in name or '\x00' in name or ':' in name or p.as_posix()!=name:raise Problem('Unsafe source path')
        if name.casefold() in seen:raise Problem('Case-colliding source paths')
        seen.add(name.casefold())
        digest=f.get('sha256','');size=f.get('size')
        if not isinstance(digest,str) or not SHA.fullmatch(digest) or not isinstance(size,int) or not 0<=size<=2*GB:raise Problem('Invalid file hash/size')
        total+=size
        if total>8*GB:raise Problem('Expanded source exceeds 8 GB')
        result.append({'path':name,'sha256':digest,'size':size,'executable':bool(f.get('executable',False))})
    for name in seen:
        parts=name.split('/')
        if any('/'.join(parts[:n]) in seen for n in range(1,len(parts))):raise Problem('File/directory path collision')
    return sorted(result,key=lambda x:x['path'])


def digest_manifest(files):return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

class Sources:
    def __init__(self,w):self.w=w
    def manifest(self,project,revision):
        rec=self.w.revision(project,revision)
        cache=self.w.root/'Service/state/manifests'/project/(rec['id']+'.json')
        if cache.exists():return json.loads(cache.read_text())
        source=within(self.w.root,self.w.root/rec['path']);files=[]
        for root,dirs,names in os.walk(source,followlinks=False):
            dirs[:]=[d for d in dirs if d!='.git']
            if any((Path(root)/d).is_symlink() for d in dirs):raise Problem('Incremental workspaces do not support symlinks; use a complete ZIP')
            for name in names:
                p=Path(root)/name
                if p.is_symlink() or not p.is_file():raise Problem('Incremental workspaces require regular files')
                files.append({'path':p.relative_to(source).as_posix(),'sha256':sha256_file(p),'size':p.stat().st_size,'executable':bool(p.stat().st_mode&0o111)})
        files=validate_manifest(files);value={'files':files,'digest':digest_manifest(files)};atomic_json(cache,value)
        return value
    def blob(self,digest):
        if not SHA.fullmatch(digest):raise Problem('Invalid SHA-256')
        return self.w.root/'SourceStore/objects'/digest[:2]/digest
    def plan(self,project,files):
        ident(project);self.w.guard(admission=True);files=validate_manifest(files);digest=digest_manifest(files)
        with self.w.lock:
            project_state=self.w.state['projects'].get(project,{})
            for rec in reversed(list(project_state.get('revisions',{}).values())):
                if rec.get('manifest_digest')==digest:return {'reused':True,'revision':rec,'missing':[],'upload_bytes':0}
            # Seed blobs from the latest revision, including older ZIP uploads.
            latest=project_state.get('latest_revision')
            if latest:
                rec=self.w.revision(project,latest);old=self.manifest(project,latest)
                if old['digest']==digest:return {'reused':True,'revision':rec,'missing':[],'upload_bytes':0}
                source=self.w.root/rec['path']
                for f in old['files']:
                    dest=self.blob(f['sha256'])
                    if not dest.exists():
                        clone_file(source/f['path'],dest);dest.chmod(0o600)
            sizes={f['sha256']:f['size'] for f in files};missing=[{'sha256':h,'size':size} for h,size in sizes.items() if not self.blob(h).is_file()]
            plan={'id':uuid.uuid4().hex,'project_id':project,'created_at':time.time(),'expires_at':time.time()+86400,'files':files,'digest':digest}
            atomic_json(self.w.root/'Service/state/sync-plans'/(plan['id']+'.json'),plan)
            return {'plan_id':plan['id'],'manifest_digest':digest,'missing':missing,'upload_bytes':sum(f['size'] for f in missing),'total_bytes':sum(f['size'] for f in files),'files':len(files)}
    def commit(self,project,pid):
        ident(project);ident(pid);self.w.guard(admission=True)
        path=self.w.root/'Service/state/sync-plans'/(pid+'.json')
        if not path.exists():raise Problem('Sync plan not found',404)
        plan=json.loads(path.read_text())
        if plan['project_id']!=project or time.time()>plan['expires_at']:raise Problem('Sync plan expired or project mismatch',409)
        with self.w.lock:
            if plan.get('revision'):return self.w.revision(project,plan['revision'])
            checked=set()
            for f in plan['files']:
                p=self.blob(f['sha256'])
                if f['sha256'] in checked:continue
                if not p.is_file() or p.is_symlink() or p.stat().st_size!=f['size'] or sha256_file(p)!=f['sha256']:raise Problem('Missing or invalid source blob '+f['sha256'],409)
                checked.add(f['sha256'])
            revision=uuid.uuid4().hex;dest=self.w.root/'Repos'/project/'revisions'/revision
            dest.mkdir(parents=True,mode=0o700)
            for f in plan['files']:
                p=dest/f['path'];clone_file(self.blob(f['sha256']),p);p.chmod(0o700 if f['executable'] else 0o600)
            rec={'id':revision,'source_type':'incremental','manifest_digest':plan['digest'],'created_at':time.time(),'path':str(dest.relative_to(self.w.root)),'expanded_bytes':sum(f['size'] for f in plan['files'])}
            entry=self.w.state['projects'].setdefault(project,{'id':project,'revisions':{}})
            entry['revisions'][revision]=rec;entry['latest_revision']=revision
            atomic_json(self.w.root/'Service/state/manifests'/project/(revision+'.json'),{'files':plan['files'],'digest':plan['digest']})
            plan['revision']=revision;atomic_json(path,plan);self.w.save();return rec
    def workspace(self,project,revision):
        """Only modify the managed working copy; immutable revisions remain intact."""
        rec=self.w.revision(project,revision);manifest=self.manifest(project,revision);source=self.w.root/rec['path']
        base=self.w.root/'Workspaces'/project;root=base/'source'
        if base.is_symlink() or root.is_symlink():raise Problem('Workspace cannot be a symlink',409)
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        marker=base/'.managed.json'
        if marker.exists() and json.loads(marker.read_text()).get('owner')!=self.w.cfg['owner']:raise Problem('Unowned workspace',403)
        if root.exists() and not marker.exists():raise Problem('Existing workspace has no ownership marker',403)
        atomic_json(marker,{'owner':self.w.cfg['owner'],'project_id':project})
        root.mkdir(exist_ok=True,mode=0o700);desired={f['path']:f for f in manifest['files']};changed=0;removed=0
        state_path=base/'revision.json'
        previous=set(json.loads(state_path.read_text()).get('tracked_files',[])) if state_path.exists() else set()
        # Remove stale source files only inside this worker-owned copy. Never follow links.
        for current,dirs,names in os.walk(root,topdown=False,followlinks=False):
            for name in names+dirs:
                p=Path(current)/name;rel=p.relative_to(root).as_posix()
                if p.is_symlink():p.unlink();removed+=1
                elif p.is_file() and rel in previous and rel not in desired:p.unlink();removed+=1
                elif p.is_dir() and not any(p.iterdir()):p.rmdir()
        for rel,f in desired.items():
            dst=root/rel;within(root,dst,must_exist=False)
            if dst.is_dir():shutil.rmtree(dst)
            if not dst.exists() or dst.stat().st_size!=f['size'] or sha256_file(dst)!=f['sha256']:
                temp=dst.with_name(dst.name+'.worker-'+uuid.uuid4().hex)
                clone_file(source/rel,temp);os.replace(temp,dst);changed+=1
            mode=0o700 if f['executable'] else 0o600
            if stat.S_IMODE(dst.stat().st_mode)!=mode:dst.chmod(mode)
        atomic_json(base/'revision.json',{'revision':revision,'manifest_digest':manifest['digest'],'updated_at':time.time(),'tracked_files':list(desired)})
        return root,{'changed_files':changed,'removed_files':removed,'total_files':len(desired),'stable_path':str(root)}

    def retention_candidates(self,project,keep,days):
        from worker import tree_fingerprint
        ident(project)
        if not isinstance(keep,int) or not 1<=keep<=1000 or not isinstance(days,(int,float)) or not 0<=days<=3650:raise Problem('Use keep_last 1–1000 and older_than_days 0–3650')
        if self.w.active or not self.w.tasks.empty() or self.w.uploads or any(s['status'] in {'active','opening','interrupted'} for s in self.w.state['sessions'].values()):raise Problem('Close development sessions and wait for jobs/uploads before source retention',409)
        p=self.w.state['projects'].get(project)
        if not p:raise Problem('Project not found',404)
        records=sorted(p['revisions'].values(),key=lambda x:x['created_at'],reverse=True)
        protected={x['id'] for x in records[:keep]}|{p.get('latest_revision')}
        for job in self.w.state['jobs'].values():
            req=job['request']
            if req.get('project_id')==project and (job['status'] in {'running','queued'} or req.get('action')=='archive'):
                protected.add(req.get('revision'))
        candidates=[];selected=set()
        for rec in records:
            if rec['id'] in protected or rec['created_at']>time.time()-days*86400:continue
            path=self.w.root/'Repos'/project/'revisions'/ident(rec['id'])
            if str(path.relative_to(self.w.root))!=rec['path'] or path.is_symlink():raise Problem('Unexpected revision layout',409)
            fp=tree_fingerprint(path)
            if fp['protected']:continue
            candidates.append({'kind':'revision','project_id':project,'revision':rec['id'],'path':str(path.relative_to(self.w.root)),**fp});selected.add(rec['id'])
        # Objects are retained while referenced by any surviving revision or live sync plan.
        used=set()
        for name,entry in self.w.state['projects'].items():
            for rec in entry['revisions'].values():
                if name==project and rec['id'] in selected:continue
                used.update(f['sha256'] for f in self.manifest(name,rec['id'])['files'])
        plans=self.w.root/'Service/state/sync-plans'
        if plans.exists():
            for path in plans.glob('*.json'):
                plan=json.loads(path.read_text())
                if not plan.get('revision') and plan['expires_at']>time.time():used.update(f['sha256'] for f in plan['files'])
        for path in (self.w.root/'SourceStore/objects').glob('*/*'):
            if path.is_symlink() or not path.is_file() or not SHA.fullmatch(path.name) or path.name in used:continue
            st=path.stat()
            if st.st_mtime>time.time()-days*86400:continue
            fingerprint=hashlib.sha256(f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}'.encode()).hexdigest()
            candidates.append({'kind':'blob','sha256':path.name,'path':str(path.relative_to(self.w.root)),'fingerprint':fingerprint,'bytes':st.st_blocks*512})
        return candidates

    def retention_plan(self,project,keep=10,days=14):
        self.w.guard()
        with self.w.lock:
            entries=self.retention_candidates(project,keep,days)
            plan={'id':uuid.uuid4().hex,'kind':'source_retention','project_id':project,'keep_last':keep,'older_than_days':days,'entries':entries,'created_at':time.time(),'expires_at':time.time()+900,'estimated_allocated_bytes_upper_bound':sum(e['bytes'] for e in entries),'note':'APFS clones may share blocks; actual reclaimed space may be smaller. Archives and archive-referenced sources are protected.'}
            self.w.state.setdefault('source_cleanup_plans',{})[plan['id']]=plan;self.w.save();return plan

    def retention_execute(self,pid,confirmation):
        self.w.guard()
        with self.w.lock:
            plan=self.w.state.get('source_cleanup_plans',{}).get(ident(pid))
            if not plan or confirmation!=pid or plan.get('executed_at') or plan['expires_at']<time.time():raise Problem('Invalid, expired or already executed source retention plan',409)
            current={e['path']:e for e in self.retention_candidates(plan['project_id'],plan['keep_last'],plan['older_than_days'])}
            for entry in plan['entries']:
                if entry['path'] not in current or current[entry['path']]['fingerprint']!=entry['fingerprint']:raise Problem('Source retention plan changed; preview again',409)
            if not shutil.rmtree.avoids_symlink_attacks:raise Problem('Safe directory deletion is unavailable',503)
            for entry in plan['entries']:
                path=within(self.w.root,entry['path'])
                if entry['kind']=='revision':
                    shutil.rmtree(path)
                    del self.w.state['projects'][entry['project_id']]['revisions'][entry['revision']]
                else:path.unlink()
            plan['executed_at']=time.time();self.w.save()
            return {'executed_plan':pid,'deleted_entries':len(plan['entries'])}
