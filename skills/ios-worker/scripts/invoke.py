"""Cross-platform skill entrypoint. Python 3.9+, standard library only."""
import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
from transport import Client

ROOT = Path(__file__).resolve().parents[1]

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Invalid project/job identifier')
    return value

def configuration(admin=False):
    env = 'IOS_WORKER_ADMIN_CONFIG' if admin else 'IOS_WORKER_CONFIG'
    path = Path(os.environ.get(env) or ROOT/'private'/('admin-connection.json' if admin else 'connection.json'))
    if not path.is_file():raise ValueError('Connection profile missing: '+str(path))
    return path

def prepare_test(client, job):
    job = copy.deepcopy(job)
    if job.get('action') != 'test':return job
    health = client.request('GET', '/v1/health')
    if 'test_requires_idle_simulator' not in health.get('capabilities', []):
        raise ValueError('Worker needs the simulator ownership update before running tests')
    inventory = client.request('GET', '/v1/simulators/inventory')
    session=None
    if job.get('session_id'):
        session=next((x for x in client.request('GET','/v1/sessions') if x['id']==job['session_id'] and x['status']=='active'),None)
        if not session:raise ValueError('Session is not active')
    udid = (session['simulator_id'] if session else None) or job.get('simulator_id') or health.get('default_simulator')
    target = next((d for d in inventory['devices'] if d['udid']==udid), None)
    if not target or not target.get('worker_managed'):
        raise ValueError('Choose an existing Worker-managed simulator; other devices are read-only')
    if not session and (target.get('state')!='Shutdown' or udid in inventory['booted_ids']):
        raise ValueError('Simulator is occupied; select another existing idle device or wait. Do not create a replacement automatically.')
    job['simulator_id'] = udid
    return job

def invoke(request, base, client=None):
    operation = request.get('operation', 'status')
    client = client or Client(configuration(operation in {'cleanup_execute','retention_execute','register_device'}))
    if operation=='status':
        return client.request('GET','/v1/status')
    routes = {'projects':'/v1/projects','jobs':'/v1/jobs','storage':'/v1/storage','simulators':'/v1/simulators/inventory','sessions':'/v1/sessions','toolchain':'/v1/toolchain'}
    if operation in routes:
        result=client.request('GET',routes[operation])
        if operation=='jobs' and not request.get('full'):return [{k:j.get(k) for k in ['id','status','request','started_at','finished_at','error']} for j in result[-20:]]
        return result
    if operation=='sync':
        from sync import sync
        source=Path(request['source'])
        if not source.is_absolute():source=base/source
        return sync(source,identifier(request['project_id']),client)
    aliases={'session_open':('session','open'),'session_close':('session','close'),'session_install':('session','install'),'session_launch':('session','launch'),'session_configure':('session','configure')}
    if operation in aliases:
        action,op=aliases[operation];job={k:v for k,v in request.items() if k not in {'operation','wait','wait_seconds'}};job.update(action=action,operation=op)
        return client.request('POST','/v1/jobs',job)
    if operation in {'ui','diagnostics','capture','capture_matrix','attachments','prepare','register_device'}:
        job={k:v for k,v in request.items() if k not in {'operation','wait','wait_seconds'}};job['action']=operation
        if 'command' in job:job['operation']=job.pop('command')
        return client.request('POST','/v1/jobs',job)
    if operation=='upload':
        source = Path(request['zip_path'])
        if not source.is_absolute():source=base/source
        if not source.is_file():raise ValueError('Source ZIP is missing')
        if source.stat().st_size > 2_000_000_000:raise ValueError('Source ZIP exceeds 2 GB')
        return client.request('POST','/v1/projects/'+identifier(request['project_id'])+'/sources',upload=source)
    if operation=='submit':
        job = prepare_test(client,request['job'])
        if job.get('action')=='simulator' and job.get('operation') in {'create','erase','delete'}:
            raise ValueError('Skill does not create/erase/delete simulators. Reuse existing devices.')
        return client.request('POST','/v1/jobs',job)
    if operation in {'job','wait','log','cancel','download'}:
        path='/v1/jobs/'+identifier(request['job_id'])
        if operation in {'job','wait'}:return client.request('GET',path+('' if request.get('full') else '/summary'))
        if operation=='log':return client.request('GET',path+('/log' if request.get('full') else '/log-summary'))
        if operation=='cancel':return client.request('POST',path+'/cancel',{})
        from urllib.parse import quote
        out=Path(request['output_path'])
        if not out.is_absolute():out=base/out
        record=client.request('GET',path)
        item=next((a for a in record['artifacts'] if a['name']==request['name']),None)
        if not item:raise ValueError('Artifact not listed by this job')
        result=client.request('GET',path+'/artifacts/'+quote(item['name'],safe=''),output=out)
        import hashlib
        h=hashlib.sha256()
        with open(out,'rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        if h.hexdigest()!=item['sha256']:raise ValueError('Downloaded artifact checksum mismatch; retain it for inspection')
        return dict(result,sha256=h.hexdigest(),verified=True)
    if operation=='retention_preview':
        return client.request('POST','/v1/sources/retention/plan',{'project_id':identifier(request['project_id']),'keep_last':request.get('keep_last',10),'older_than_days':request.get('older_than_days',14)})
    if operation=='retention_execute':
        if not request.get('plan_id') or request.get('confirmation')!=request['plan_id']:raise ValueError('confirmation must equal the reviewed plan_id')
        return client.request('POST','/v1/sources/retention/execute',{'plan_id':request['plan_id'],'confirmation':request['confirmation']})
    if operation=='cleanup_preview':
        return client.request('POST','/v1/storage/plan',{'categories':request['categories'],'older_than_days':request.get('older_than_days',14)})
    if operation=='cleanup_execute':
        if request.get('confirmation') != request.get('plan_id') or not request.get('plan_id'):
            raise ValueError('confirmation must equal the reviewed plan_id')
        return client.request('POST','/v1/storage/execute',{'plan_id':request['plan_id'],'confirmation':request['confirmation']})
    raise ValueError('Unknown operation: '+str(operation))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',help='UTF-8 JSON request file; omit for status')
    args=parser.parse_args()
    try:
        path=Path(args.request).resolve() if args.request else None
        request=json.loads(path.read_text(encoding='utf-8-sig')) if path else {'operation':'status'}
        result=invoke(request,path.parent if path else Path.cwd())
        if isinstance(result,dict) and result.get('status') in {'queued','running'} and request.get('wait',request.get('operation')!='job'):
            client=Client(configuration());deadline=time.monotonic()+min(7200,max(1,int(request.get('wait_seconds',900))))
            print('Waiting for job '+result['id'],file=sys.stderr,flush=True)
            delay=.5
            while result['status'] in {'queued','running'} and time.monotonic()<deadline:
                time.sleep(delay);delay=min(5,delay*1.4)
                result=client.request('GET','/v1/jobs/'+identifier(result['id'])+'/summary')
            if result['status'] in {'queued','running'}:result['wait_timed_out']=True
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if isinstance(result,dict) and result.get('status') in {'failed','cancelled','interrupted'}:return 2
        return 0
    except Exception as exc:
        # Do not dump profiles, tokens or a traceback into the agent context.
        print(json.dumps({'error':str(exc),'hint':'For timeout/refused: check Mac service and LAN route. For 401/403: check profile/IP allowlist. Never disable TLS validation.'},ensure_ascii=False))
        return 1

if __name__=='__main__':sys.exit(main())
