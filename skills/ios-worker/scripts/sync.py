"""Sync the Git working tree using content hashes; upload changed files only."""
import hashlib
from pathlib import Path
from snapshot import collect_files

def sync(source,project,client):
    files=[];paths={}
    for name,path,mode in collect_files(source):
        digest=hashlib.sha256()
        with open(path,'rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        sha=digest.hexdigest();paths[sha]=path
        files.append({'path':name,'sha256':sha,'size':path.stat().st_size,'executable':bool(mode&0o111)})
    plan=client.request('POST','/v1/projects/'+project+'/sync/plan',{'files':files})
    if plan.get('reused'):return {'project_id':project,'revision':plan['revision']['id'],'reused':True,'uploaded_files':0,'uploaded_bytes':0,'source_bytes':sum(f['size'] for f in files)}
    for item in plan['missing']:client.request('POST','/v1/sources/blobs/'+item['sha256'],upload=paths[item['sha256']])
    rec=client.request('POST','/v1/projects/'+project+'/sync/commit',{'plan_id':plan['plan_id']})
    return {'project_id':project,'revision':rec['id'],'reused':False,'uploaded_files':len(plan['missing']),'uploaded_bytes':plan['upload_bytes'],'source_bytes':plan['total_bytes'],'manifest_digest':rec['manifest_digest']}
