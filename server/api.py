from __future__ import annotations
import hmac
import ipaddress
import os
import time
import uuid
import hashlib
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from worker import Worker, Problem, VERSION, GB, ident, within

def create_app(config, worker=None):
    engine = worker or Worker(config)
    networks = [ipaddress.ip_network(n) for n in config["allowed_networks"]]
    @asynccontextmanager
    async def lifespan(app):
        engine.start()
        yield
        engine.close()
    app = FastAPI(title="Private iOS Build Worker", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.worker = engine
    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        try:
            address = ipaddress.ip_address(request.client.host)
        except ValueError:
            return JSONResponse({"error": "Invalid client address"}, status_code=403)
        if not any(address in n for n in networks):
            return JSONResponse({"error": "Client address is not allowed"}, status_code=403)
        token = request.headers.get("authorization", "")
        admin = hmac.compare_digest(token, "Bearer " + config["admin_token"])
        build = hmac.compare_digest(token, "Bearer " + config["build_token"])
        if not (admin or build):
            return JSONResponse({"error": "Bearer token required"}, status_code=401)
        request.state.admin = admin
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
        limit = 2 * GB if request.url.path.endswith("/sources") or '/sources/blobs/' in request.url.path else (32*1024*1024 if request.url.path.endswith('/sync/plan') else 1024*1024)
        if length < 0 or length > limit:
            return JSONResponse({"error": "Request body too large"}, status_code=413)
        if request.method in {"POST", "PUT"} and not request.url.path.endswith("/sources"):
            if "content-length" not in request.headers:
                return JSONResponse({"error": "Content-Length required"}, status_code=411)
        return await call_next(request)
    def admin(request):
        if not request.state.admin:
            raise Problem("Admin token required for storage deletion", 403)
    @app.get("/v1/health")
    def health():
        engine.guard()
        return {"service": "ios-worker", "version": VERSION, "time": time.time(),
                "root": str(engine.root), "active_job": engine.active,
                "default_simulator": config.get("default_simulator"),
                "capabilities": ["simulator_inventory", "test_requires_idle_simulator", "development_sessions", "ui_control", "incremental_sync", "stable_workspace", "capture_matrix", "runtime_diagnostics", "compact_status"],
                "external_volume_uuid": config["volume_uuid"]}
    @app.get('/v1/status')
    def compact_status():
        inventory=engine.simulator_inventory()
        return {'health':health(),'simulators':{'devices':[{k:v for k,v in d.items() if k in {'udid','name','state','runtime','isAvailable','worker_managed'}} for d in inventory['devices'] if d['worker_managed'] or d['udid'] in inventory['booted_ids']], 'booted_ids':inventory['booted_ids'],'total_devices':len(inventory['devices'])},'sessions':[s for s in engine.development.sessions() if s['status'] in {'active','opening','interrupted'}]}
    @app.get('/v1/sessions')
    def sessions():return engine.development.sessions()
    @app.get('/v1/toolchain')
    def toolchain():
        engine.guard()
        version=subprocess.run(['/usr/bin/xcodebuild','-version'],capture_output=True,text=True,timeout=30).stdout.strip()
        sdks=subprocess.run(['/usr/bin/xcodebuild','-showsdks'],capture_output=True,text=True,timeout=30).stdout.strip()
        return {'xcode':version,'sdks':sdks,'tools':{k:shutil.which(k,path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin') for k in ['git','xcodebuild','xcrun','xcodegen','pod','swift']},'ui_controller_prebuilt':bool(list((engine.root/'Service/automation/DerivedData/Build/Products').glob('*.xctestrun'))),'dependency_policy':'SwiftPM supported. Other package managers require explicit project preparation.'}
    @app.post('/v1/projects/{project_id}/sync/plan')
    def sync_plan(project_id:str,body:dict):return engine.sources.plan(project_id,body.get('files'))
    @app.post('/v1/projects/{project_id}/sync/commit')
    def sync_commit(project_id:str,body:dict):return engine.sources.commit(project_id,body.get('plan_id'))
    @app.post('/v1/sources/retention/plan')
    def source_retention_plan(body:dict):return engine.sources.retention_plan(body.get('project_id'),body.get('keep_last',10),body.get('older_than_days',14))
    @app.post('/v1/sources/retention/execute')
    def source_retention_execute(body:dict,request:Request):
        admin(request)
        return engine.sources.retention_execute(body.get('plan_id'),body.get('confirmation'))
    @app.post('/v1/sources/blobs/{digest}')
    async def blob(digest:str,request:Request):
        target=engine.sources.blob(digest)
        with engine.lock:engine.guard(admission=True);engine.uploads+=1
        temp=None
        try:
            folder=engine.managed_dir('temporary','blob-'+uuid.uuid4().hex);temp=folder/'content'
            h=hashlib.sha256();total=0
            with open(temp,'xb') as stream:
                async for chunk in request.stream():
                    total+=len(chunk)
                    if total>2*GB:raise Problem('Blob exceeds 2 GB',413)
                    h.update(chunk);await run_in_threadpool(stream.write,chunk)
                    if total//(8*1024*1024)!=(total-len(chunk))//(8*1024*1024):await run_in_threadpool(engine.guard,True)
            if h.hexdigest()!=digest:raise Problem('Source blob checksum mismatch',422)
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with engine.lock:
                if not target.exists():os.replace(temp,target);target.chmod(0o600)
            return {'sha256':digest,'bytes':total}
        finally:
            if temp and temp.exists():temp.unlink()
            with engine.lock:engine.uploads-=1
    @app.post("/v1/hello")
    async def hello(request: Request):
        body = await request.json()
        await run_in_threadpool(engine.client_seen, body.get("client_id", "unnamed"),
                                request.client.host, request.headers.get("user-agent", ""))
        return {"received_from": request.client.host, "client_id": body.get("client_id"), "time": time.time()}
    @app.get("/v1/projects")
    def projects():
        engine.guard()
        return engine.snapshot()["projects"]
    @app.post("/v1/projects/{project_id}/sources")
    async def upload(project_id: str, request: Request):
        ident(project_id)
        with engine.lock:
            engine.guard(admission=True)
            if engine.uploads:
                raise Problem("Another source upload is active", 409)
            engine.uploads += 1
        temp = None
        try:
            folder = engine.managed_dir("temporary", "upload-" + uuid.uuid4().hex)
            temp = folder / "sources.zip"
            total = 0
            with open(temp, "xb") as f:
                os.chmod(temp, 0o600)
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 2 * GB:
                        raise Problem("Source ZIP exceeds 2 GB limit", 413)
                    if total // (8 * 1024 * 1024) != (total - len(chunk)) // (8 * 1024 * 1024):
                        await run_in_threadpool(engine.guard, True)
                    await run_in_threadpool(f.write, chunk)
            if not total:
                raise Problem("Send a ZIP as the raw request body")
            try:
                return await run_in_threadpool(engine.import_zip, project_id, temp)
            except (ValueError, OSError) as exc:
                raise Problem("Invalid source ZIP: " + str(exc)) from exc
        finally:
            if temp and temp.exists():
                temp.unlink()
            with engine.lock:
                engine.uploads -= 1
    @app.post("/v1/jobs")
    def submit(body: dict, request: Request):
        if body.get('action')=='register_device':admin(request)
        for key in ["scheme", "configuration", "container", "test_plan"]:
            if key in body and (not isinstance(body[key], str) or len(body[key]) > 500 or body[key].startswith("-")):
                raise Problem("Invalid " + key)
        for key in ["signing", "locked_packages", "keep_simulator_running"]:
            if key in body and not isinstance(body[key], bool):
                raise Problem(key + " must be boolean")
        if "timeout_seconds" in body and (not isinstance(body["timeout_seconds"], int) or not 30 <= body["timeout_seconds"] <= 7200):
            raise Problem("timeout_seconds must be 30–7200")
        return engine.submit(body)
    @app.get("/v1/jobs")
    def jobs():
        return list(engine.snapshot()["jobs"].values())[-100:]
    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str):
        return engine.get_job(job_id)
    @app.get('/v1/jobs/{job_id}/summary')
    def job_summary(job_id:str):
        j=engine.get_job(job_id)
        result={k:j[k] for k in ['id','status','created_at','started_at','finished_at','error','result','artifacts'] if k in j}
        result['action']=j['request'].get('action')
        if j.get('finished_at') and j.get('started_at'):result['duration_seconds']=round(j['finished_at']-j['started_at'],2)
        return result
    @app.get('/v1/jobs/{job_id}/log-summary')
    def log_summary(job_id:str):
        engine.get_job(job_id);p=engine.root/'Logs'/ident(job_id)/'build.log'
        if not p.exists():return {'tail':'','diagnostics':[]}
        with open(p,'rb') as stream:stream.seek(max(0,p.stat().st_size-256000));tail=stream.read().decode(errors='replace')
        errors=[line[:1000] for line in tail.splitlines() if re.search(r'error:|warning:|failed|FAIL|Assertion',line)]
        return {'tail':'\n'.join(tail.splitlines()[-60:])[-12000:],'diagnostics':errors[-20:],'full_log_bytes':p.stat().st_size,'full_log_path':'/v1/jobs/'+job_id+'/log'}
    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return engine.cancel(job_id)
    @app.get("/v1/jobs/{job_id}/log")
    def log(job_id: str):
        engine.get_job(job_id)
        engine.guard()
        p = engine.root / "Logs" / ident(job_id) / "build.log"
        if not p.exists():
            return PlainTextResponse("")
        return FileResponse(within(engine.root / "Logs", p), media_type="text/plain")
    @app.get("/v1/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: str, name: str):
        p = engine.artifact(job_id, name)
        return FileResponse(p, filename=p.name)
    @app.get("/v1/simulators")
    def simulators():
        return engine.simulator_list()
    @app.get("/v1/simulators/inventory")
    def inventory():
        return engine.simulator_inventory()
    @app.get("/v1/storage")
    def storage():
        return engine.storage()
    @app.post("/v1/storage/plan")
    def plan(body: dict):
        return engine.plan_cleanup(body.get("categories"), body.get("older_than_days", 14))
    @app.post("/v1/storage/execute")
    def execute_cleanup(body: dict, request: Request):
        admin(request)
        return engine.execute_cleanup(body.get("plan_id"), body.get("confirmation"))
    return app
