"""Owned simulator development sessions; all mutations run through the worker queue."""
from __future__ import annotations
import copy, hashlib, http.client, json, os, plistlib, re, signal, socket, subprocess, time, uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
from worker import Problem, ident, within, atomic_json, sha256_file

BUNDLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{1,250}')
ELEMENT_KEY = 'element-6066-11e4-a52e-4f735466cecf'

def status_overrides_empty(output):
    lines=[line.strip() for line in output.splitlines() if line.strip()]
    lines=[line for line in lines if line!='Current Status Bar Overrides:' and set(line)!={'='}]
    return not lines or '\n'.join(lines) in {'{}','(null)','No overrides are set.'}

class Development:
    def __init__(self, worker):
        self.w = worker
        self.children = {}
        self.w.state.setdefault('sessions', {})
        # Persist reservations across service restarts. Explicit recovery/close is required.
        for session in self.w.state['sessions'].values():
            if session['status'] == 'active':
                session['status'] = 'interrupted'
                self.stop_controller(session)

    def sessions(self):
        return [{k:v for k,v in s.items() if k not in {'wda','environment'}} for s in self.w.snapshot()['sessions'].values()]

    def session(self, sid, active=True):
        s = self.w.state['sessions'].get(ident(sid))
        if not s or (active and s['status'] != 'active'):
            raise Problem('Session is absent or not active',409)
        return s

    def reservation(self, udid):
        return next((s for s in self.w.state['sessions'].values() if s['simulator_id']==udid and s['status'] in {'active','interrupted','opening'}),None)

    def check_device(self, req):
        udid=req.get('simulator_id',self.w.cfg.get('default_simulator'))
        sid=req.get('session_id')
        if sid:
            session=self.session(sid)
            if req.get('simulator_id') and udid!=session['simulator_id']:raise Problem('Session owns a different simulator',409)
            if req.get('project_id') and req['project_id']!=session['project_id']:raise Problem('Session owns a different project',409)
            req['simulator_id']=session['simulator_id'];udid=session['simulator_id']
            if req.get('bundle_id') and req['bundle_id']!=session['bundle_id']:raise Problem('Only the session app may be controlled',403)
        owner=self.reservation(udid)
        if owner and owner['id']!=sid:raise Problem('Simulator reserved by another development session',409)
        return udid

    def stop_controller(self,s):
        rec=s.pop('wda',None)
        if not rec:return
        pid=rec.get('pid')
        if pid:
            current=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True).stdout.strip()
            if current and current==rec.get('identity'):
                try:os.killpg(pid,signal.SIGTERM)
                except ProcessLookupError:pass
                child=self.children.pop(pid,None)
                if child:
                    try:child.wait(timeout=12)
                    except subprocess.TimeoutExpired:os.killpg(pid,signal.SIGKILL);child.wait(timeout=5)

    def close(self):
        for s in self.w.state['sessions'].values():
            self.stop_controller(s)
            if s['status']=='active':s['status']='interrupted'
        self.w.save()

    def request(self,s,method,path,body=None,raw=False):
        rec=s.get('wda')
        if not rec:raise Problem('UI controller is not started',409)
        conn=http.client.HTTPConnection('127.0.0.1',rec['port'],timeout=65)
        try:
            prefix='' if raw else '/session/'+rec['session_id']
            conn.request(method,prefix+path,body=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
            response=conn.getresponse();data=response.read(8*1024*1024)
            value=json.loads(data)
            result=value.get('value')
            if response.status>=300 or (isinstance(result,dict) and result.get('error')):
                raise Problem('UI: '+str(result.get('message',result) if isinstance(result,dict) else result)[:2000],422)
            return value if raw else result
        except (OSError,ValueError) as exc:raise Problem('UI controller unavailable: '+str(exc),503) from exc
        finally:conn.close()

    def start_controller(self,jid,s):
        if s.get('wda'):
            try:self.request(s,'GET','/status',raw=True);return
            except Problem:self.stop_controller(s)
        for other in self.w.state['sessions'].values():
            if other['id']!=s['id'] and other.get('wda'):self.stop_controller(other)
        products=self.w.root/'Service/automation/DerivedData/Build/Products'
        runs=list(products.glob('*.xctestrun'))
        if len(runs)!=1:raise Problem('Prebuilt UI controller not found; run Mac toolchain preparation',503)
        ports=[]
        for _ in range(2):
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));ports.append(sock.getsockname()[1])
        original=plistlib.loads(runs[0].read_bytes())
        controller_env={'USE_IP':'127.0.0.1','USE_PORT':str(ports[0]),'MJPEG_SERVER_PORT':str(ports[1]),'MJPEG_SERVER_FRAMERATE':'1'}
        def visit(value):
            if isinstance(value,dict):
                if 'TestBundlePath' in value:
                    value.setdefault('EnvironmentVariables',{}).update(controller_env)
                    value.setdefault('TestingEnvironmentVariables',{}).update(controller_env)
                    value['TestHostPath']=value.get('TestHostPath','').replace('__TESTROOT__',str(products))
                    value['TestBundlePath']=value['TestBundlePath'].replace('__TESTROOT__',str(products))
                for key,item in list(value.items()):
                    if isinstance(item,str):value[key]=item.replace('__TESTROOT__',str(products))
                    else:visit(item)
            elif isinstance(value,list):
                for index,item in enumerate(value):
                    if isinstance(item,str):value[index]=item.replace('__TESTROOT__',str(products))
                    else:visit(item)
        visit(original)
        folder=self.w.managed_dir('session_logs',s['id'])
        runfile=folder/'controller.xctestrun';runfile.write_bytes(plistlib.dumps(original))
        env=self.w.env(jid);env.update(controller_env)
        log=open(folder/'controller.log','ab',buffering=0)
        try:
            child=subprocess.Popen(['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(runfile),'-destination','platform=iOS Simulator,id='+s['simulator_id'],'-derivedDataPath',str(self.w.root/'Service/automation/DerivedData'),'-resultBundlePath',str(folder/('controller-'+uuid.uuid4().hex+'.xcresult')),'-parallel-testing-enabled','NO','-maximum-concurrent-test-simulator-destinations','1'],env=env,cwd=products,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
        finally:log.close()
        identity=subprocess.run(['/bin/ps','-p',str(child.pid),'-o','lstart='],capture_output=True,text=True).stdout.strip()
        self.children[child.pid]=child
        s['wda']={'pid':child.pid,'identity':identity,'port':ports[0],'mjpeg_port':ports[1]};self.w.save()
        deadline=time.monotonic()+120
        try:
            while time.monotonic()<deadline:
                if child.poll() is not None:raise Problem('UI controller exited; inspect controller diagnostic log',503)
                if jid in self.w.cancelled or self.w.stop_event.is_set():raise Problem('cancelled',409)
                self.w.guard()
                try:
                    self.request(s,'GET','/status',raw=True);break
                except Problem:time.sleep(1)
            else:raise Problem('UI controller startup timed out',504)
            response=self.request(s,'POST','/session',{'capabilities':{'alwaysMatch':{'bundleId':s['bundle_id'],'shouldTerminateApp':False,'shouldWaitForQuiescence':False,'waitForIdleTimeout':1,'forceAppLaunch':False,'arguments':s.get('arguments',[]),'environment':s.get('environment',{})}}},raw=True)
            s['wda']['session_id']=response.get('sessionId') or response.get('value',{}).get('sessionId')
            if not s['wda']['session_id']:raise Problem('UI controller did not return a session',503)
            self.request(s,'POST','/appium/settings',{'settings':{'defaultActiveApplication':s['bundle_id']}})
            self.w.save()
        except BaseException:self.stop_controller(s);self.w.save();raise

    def app_from_build(self,jid,build_id,project_id,app_name=None):
        b=self.w.get_job(build_id)
        if b['status']!='succeeded' or b['request'].get('project_id')!=project_id:raise Problem('Use a successful build of this project')
        apps=[Path(p) for p in b.get('result',{}).get('apps',[]) if not app_name or Path(p).name==app_name]
        if len(apps)!=1:raise Problem('Specify one app_name from build result')
        app=within(self.w.root/'Results',apps[0]);info=plistlib.loads((app/'Info.plist').read_bytes())
        return app,info

    def open(self,jid,req):
        project=ident(req['project_id']);udid=req.get('simulator_id',self.w.cfg['default_simulator'])
        self.check_device({'simulator_id':udid});self.w.simulator(udid)
        if sum(s['status'] in {'active','interrupted','opening'} for s in self.w.state['sessions'].values())>=3:raise Problem('Close an existing session before opening another',409)
        app,info=self.app_from_build(jid,req['build_job_id'],project,req.get('app_name'))
        bundle=info['CFBundleIdentifier']
        s={'id':uuid.uuid4().hex,'project_id':project,'bundle_id':bundle,'executable':info.get('CFBundleExecutable'),'simulator_id':udid,'label':str(req.get('label','development'))[:120],'status':'opening','created_at':time.time(),'booted_by_worker':False,'build_job_id':req['build_job_id'],'arguments':req.get('arguments',[]),'environment':req.get('environment',{})}
        self.validate_launch(s)
        self.w.state['sessions'][s['id']]=s;self.w.save()
        try:
            self.w.boot_simulator(jid,udid,require_shutdown=True)
            s['booted_by_worker']=True
            self.w.run(jid,['/usr/bin/xcrun','simctl','install',udid,str(app)],timeout=120)
            s['status']='active';self.w.save()
            self.launch(jid,s,req)
            if req.get('ui',True):self.start_controller(jid,s)
            return {k:v for k,v in s.items() if k not in {'wda','environment'}}
        except BaseException:
            self.stop_controller(s)
            if s['booted_by_worker']:self.w.run(jid,['/usr/bin/xcrun','simctl','shutdown',udid],timeout=60,check=False)
            s['status']='failed';self.w.save();raise

    @staticmethod
    def validate_launch(s):
        args=s.get('arguments',[]);env=s.get('environment',{})
        if not isinstance(args,list) or len(args)>100 or any(not isinstance(a,str) or len(a)>2000 for a in args):raise Problem('Invalid launch arguments')
        if not isinstance(env,dict) or len(env)>50 or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k) or not isinstance(v,str) or len(v)>4000 for k,v in env.items()):raise Problem('Invalid app environment')

    def launch(self,jid,s,req):
        self.stop_controller(s)
        for k in ['arguments','environment']:
            if k in req:s[k]=req[k]
        self.validate_launch(s)
        folder=self.w.managed_dir('session_logs',s['id'])
        output,_=self.w.run(jid,['/usr/bin/xcrun','simctl','launch','--terminate-running-process','--stdout='+str(folder/'stdout.log'),'--stderr='+str(folder/'stderr.log'),s['simulator_id'],s['bundle_id'],*s['arguments']],timeout=60,capture=True,extra_env={'SIMCTL_CHILD_'+k:v for k,v in s['environment'].items()})
        match=re.search(r':\s*(\d+)\s*$',output)
        s['app_pid']=int(match[1]) if match else None
        s['app_pid_identity']=subprocess.run(['/bin/ps','-p',str(s['app_pid']),'-o','lstart='],capture_output=True,text=True).stdout.strip() if match else None
        s['last_launch']=time.time();self.w.save()
        return {'launched':s['bundle_id'],'pid':s['app_pid']}

    def end(self,jid,s):
        if s.get('original_orientation') and s.get('wda'):
            try:self.request(s,'POST','/orientation',{'orientation':s['original_orientation']})
            except Problem:pass
        self.stop_controller(s)
        if s.get('original_appearance'):
            self.w.run(jid,['/usr/bin/xcrun','simctl','ui',s['simulator_id'],'appearance',s['original_appearance']],check=False,timeout=30)
        if s.get('status_override'):
            self.w.run(jid,['/usr/bin/xcrun','simctl','status_bar',s['simulator_id'],'clear'],check=False,timeout=30)
        if s.get('booted_by_worker'):
            self.w.run(jid,['/usr/bin/xcrun','simctl','shutdown',s['simulator_id']],timeout=90,check=False)
        s.update(status='closed',finished_at=time.time());self.w.save()
        return {'session_id':s['id'],'status':'closed','shutdown_own_device':bool(s.get('booted_by_worker'))}

    def find(self,s,req):
        selector=req.get('selector',{})
        using=selector.get('using','accessibility id');value=selector.get('value')
        if using not in {'accessibility id','predicate string','class name','xpath','class chain'} or not isinstance(value,str) or len(value)>2000:raise Problem('Use selector {using,value}')
        result=self.request(s,'POST','/elements',{'using':using,'value':value})
        return [{'element_id':x.get(ELEMENT_KEY,x.get('ELEMENT'))} for x in result]

    def ui(self,jid,s,req):
        self.start_controller(jid,s)
        op=req.get('operation')
        if op=='tree':
            xml=self.request(s,'GET','/source?format=xml');tree=ET.fromstring(xml)
            out=self.w.managed_dir('results',jid)/'page.xml';out.write_text(xml);self.w.add_artifact(jid,out,'ui_tree')
            nodes=[];limit=max(1,min(int(req.get('limit',100)),300))
            for element in tree.iter():
                a=element.attrib
                if req.get('visible_only',True) and a.get('visible')=='false':continue
                if a.get('type','') in {'XCUIElementTypeOther','XCUIElementTypeWindow'} and not a.get('name'):continue
                nodes.append({k:v for k,v in a.items() if k in {'type','name','label','value','enabled','visible','x','y','width','height'}})
            return {'elements':nodes[:limit],'total':len(nodes),'truncated':len(nodes)>limit,'full_tree_artifact':'page.xml'}
        if op in {'find','wait'}:
            until=time.monotonic()+max(0,min(float(req.get('timeout_seconds',10)),45))
            while True:
                found=self.find(s,req)
                if found or op=='find':return {'elements':found[:100],'count':len(found)}
                if time.monotonic()>until:raise Problem('Timed out waiting for element',408)
                if jid in self.w.cancelled:raise Problem('cancelled',409)
                time.sleep(.5)
        if op=='orientation':
            orientation=req.get('orientation')
            if orientation not in {'PORTRAIT','LANDSCAPE','LANDSCAPELEFT','LANDSCAPERIGHT','PORTRAIT_UPSIDEDOWN'}:raise Problem('Invalid orientation')
            if not s.get('original_orientation'):
                s['original_orientation']=self.request(s,'GET','/orientation');self.w.save()
            return {'orientation':self.request(s,'POST','/orientation',{'orientation':orientation})}
        if op=='tap' and 'x' in req:
            return {'value':self.request(s,'POST','/wda/tap',{'x':float(req['x']),'y':float(req['y'])})}
        element=req.get('element_id')
        if not element and req.get('selector'):
            elements=self.find(s,req)
            if len(elements)!=1:raise Problem('Selector must match exactly one element; got '+str(len(elements)),409)
            element=elements[0]['element_id']
        encoded=quote(str(element),safe='') if element else None
        if op in {'tap','clear','type'}:
            if not encoded:raise Problem('Specify element_id or selector')
            if op=='type':
                value=req.get('text','')
                if not isinstance(value,str) or len(value)>10000:raise Problem('Invalid input text')
                if req.get('clear',False):self.request(s,'POST','/element/'+encoded+'/clear',{})
                result=self.request(s,'POST','/element/'+encoded+'/value',{'value':[value]})
            else:result=self.request(s,'POST','/element/'+encoded+('/click' if op=='tap' else '/clear'),{})
            return {'operation':op,'value':result}
        if op in {'swipe','scroll'}:
            direction=req.get('direction','up')
            if direction not in {'up','down','left','right'}:raise Problem('Invalid direction')
            path='/wda/'+('element/'+encoded+'/' if encoded else '')+op
            return {'operation':op,'value':self.request(s,'POST',path,{'direction':direction})}
        raise Problem('Unsupported UI operation')

    def screenshot(self,jid,s,req):
        from PIL import Image
        folder=self.w.managed_dir('results',jid)
        name=ident(req.get('name','screenshot'))
        out=folder/(name+'.png')
        self.w.run(jid,['/usr/bin/xcrun','simctl','io',s['simulator_id'],'screenshot',str(out)],timeout=60)
        with Image.open(out) as im:
            original_mode=im.mode;width,height=im.size
            # Explicit opaque export; RGB PNG contains no alpha channel.
            if req.get('store',False):
                rgb=Image.new('RGB',im.size,'white');rgb.paste(im,mask=im.getchannel('A') if 'A' in im.getbands() else None);rgb.save(out,'PNG')
        with Image.open(out) as check:mode=check.mode
        artifact=self.w.add_artifact(jid,out,'store_screenshot' if req.get('store') else 'screenshot')
        accepted={(1320,2868),(1398,2034),(2007,2853),(1260,2736),(1290,2796),(1284,2778),(1242,2688),(1206,2622),(1170,2532),(1284,2778),(1179,2556),(1242,2208),(1125,2436),(1080,2340),(750,1334),(640,1136),(2064,2752),(2048,2732),(1668,2388),(1668,2420),(1640,2360),(1488,2266),(1512,2316),(1668,2224),(1536,2048)}
        return {'artifact':artifact,'width':width,'height':height,'mode':mode,'alpha_removed':original_mode!=mode,'accepted_dimension':(min(width,height),max(width,height)) in accepted,'store_ready_format':mode=='RGB' and (min(width,height),max(width,height)) in accepted,'note':'Format/dimensions only; required device class, content and localization still need review.'}

    def diagnostics(self,jid,s,req):
        folder=self.w.managed_dir('results',jid);op=req.get('operation','logs');session_dir=self.w.root/'Sessions'/s['id']
        if op=='logs':
            seconds=max(1,min(int(req.get('seconds',120)),3600));name=s['executable']
            if not name or not re.fullmatch(r'[A-Za-z0-9_. -]{1,150}',name):raise Problem('App executable name cannot be used as a log filter')
            output,code=self.w.run(jid,['/usr/bin/xcrun','simctl','spawn',s['simulator_id'],'log','show','--style','compact','--last',str(seconds)+'s','--predicate','process == '+json.dumps(name),'--info','--debug'],timeout=60,capture=True,check=False)
            for file in ['stdout.log','stderr.log']:
                p=session_dir/file
                if p.exists():
                    with open(p,'rb') as stream:stream.seek(max(0,p.stat().st_size-128000));output+='\n'+file+'\n'+stream.read().decode(errors='replace')
            out=folder/'app.log';out.write_text(output);self.w.add_artifact(jid,out,'app_log')
            return {'tail':output[-16000:],'truncated':len(output)>16000,'exit_code':code}
        if op=='controller_log':
            p=session_dir/'controller.log'
            if not p.exists():return {'tail':''}
            with open(p,'rb') as stream:stream.seek(max(0,p.stat().st_size-16000));return {'tail':stream.read().decode(errors='replace')}
        if op=='sample':
            pid=s.get('app_pid');identity=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True).stdout.strip()
            if not pid or identity!=s.get('app_pid_identity'):raise Problem('App PID changed; relaunch through this session before sampling',409)
            out=folder/'app-sample.txt'
            _,code=self.w.run(jid,['/usr/bin/sample',str(pid),str(max(1,min(int(req.get('seconds',3)),10))),'-file',str(out)],timeout=30,check=False)
            if not out.exists():raise Problem('OS denied stack sampling; no permissions were changed',403)
            self.w.add_artifact(jid,out,'stack_sample');return {'sampled_pid':pid,'exit_code':code,'tail':out.read_text(errors='replace')[-4000:]}
        if op=='crashes':
            matches=[]
            roots=[Path.home()/'Library/Logs/DiagnosticReports',self.w.root/'Simulators'/s['simulator_id']/'data/Library/Logs/CrashReporter']
            for root in roots:
                if not root.exists():continue
                for p in sorted(root.glob('*'),key=lambda x:x.stat().st_mtime,reverse=True):
                    if len(matches)>=10:break
                    if p.is_symlink() or not p.is_file() or p.suffix not in {'.ips','.crash'} or p.stat().st_mtime<s['created_at']-5:continue
                    content=p.read_text(errors='replace')[:4*1024*1024]
                    # A simulator/device or app PID must match as well as this bundle/executable.
                    if s['bundle_id'] not in content:continue
                    if s['simulator_id'] not in content and not re.search(r'"pid"\s*:\s*'+str(s.get('app_pid',-1))+r'\b',content):continue
                    out=folder/('crash-'+str(len(matches))+p.suffix);out.write_text(content)
                    matches.append(self.w.add_artifact(jid,out,'crash'))
            return {'reports':matches,'note':'Only this session app and device/PID, since session start. Empty means no matching report found.'}
        raise Problem('Unsupported diagnostic operation')

    def execute(self,jid,req):
        op=req.get('operation');action=req['action']
        if action=='session' and op=='open':return self.open(jid,req)
        s=self.session(req.get('session_id'),active=op!='close')
        if action=='ui':return self.ui(jid,s,req)
        if action=='diagnostics':return self.diagnostics(jid,s,req)
        if action=='capture':return self.screenshot(jid,s,req)
        if op=='close':return self.end(jid,s)
        if op=='launch':return self.launch(jid,s,req)
        if op=='configure':return self.configure(jid,s,req)
        if op=='install':
            app,info=self.app_from_build(jid,req['build_job_id'],s['project_id'],req.get('app_name'))
            if info['CFBundleIdentifier']!=s['bundle_id']:raise Problem('Build has a different bundle ID',403)
            self.stop_controller(s);self.w.run(jid,['/usr/bin/xcrun','simctl','install',s['simulator_id'],str(app)],timeout=120)
            s['build_job_id']=req['build_job_id'];self.w.save()
            return self.launch(jid,s,req)
        raise Problem('Unsupported session operation')

    def configure(self,jid,s,req):
        if 'appearance' in req:
            style=req['appearance']
            if style not in {'light','dark'}:raise Problem('Appearance must be light or dark')
            if not s.get('original_appearance'):
                previous,_=self.w.run(jid,['/usr/bin/xcrun','simctl','ui',s['simulator_id'],'appearance'],capture=True,timeout=30)
                if previous.strip() not in {'light','dark'}:raise Problem('Cannot determine original appearance')
                s['original_appearance']=previous.strip();self.w.save()
            self.w.run(jid,['/usr/bin/xcrun','simctl','ui',s['simulator_id'],'appearance',style],timeout=30)
        if req.get('status_bar',False):
            if not s.get('status_override'):
                previous,_=self.w.run(jid,['/usr/bin/xcrun','simctl','status_bar',s['simulator_id'],'list'],capture=True,timeout=30)
                # Preserve somebody else's configured overrides by refusing to replace them.
                if not status_overrides_empty(previous):raise Problem('Existing status-bar overrides found; leave them unchanged or omit status_bar',409)
                s['status_override']=True;self.w.save()
            self.w.run(jid,['/usr/bin/xcrun','simctl','status_bar',s['simulator_id'],'override','--time','9:41','--batteryState','charged','--batteryLevel','100','--wifiMode','active','--wifiBars','3','--cellularMode','active','--cellularBars','4'],timeout=30)
        return {'configured':True}

    def matrix(self,jid,req):
        devices=req.get('devices',[]);variants=req.get('variants',[])
        if not isinstance(devices,list) or not 1<=len(devices)<=4 or len(set(devices))!=len(devices):raise Problem('Use 1–4 distinct existing devices')
        if not isinstance(variants,list) or not 1<=len(variants)<=10:raise Problem('Use 1–10 screenshot variants')
        inventory=self.w.simulator_inventory()
        for udid in devices:
            self.w.simulator(udid);self.check_device({'simulator_id':udid})
            if udid in inventory['booted_ids']:raise Problem('Screenshot device is occupied',409)
        results=[]
        for udid in devices:
            record=self.open(jid,dict(req,action='session',operation='open',simulator_id=udid,ui=False))
            s=self.session(record['id'])
            try:
                for n,variant in enumerate(variants):
                    if jid in self.w.cancelled:raise Problem('cancelled',409)
                    language=variant.get('language','en');locale=variant.get('locale','en_US')
                    if not all(isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,30}',x) for x in [language,locale]):raise Problem('Invalid language/locale')
                    arguments=list(variant.get('arguments',[]))+['-AppleLanguages','('+language+')','-AppleLocale',locale]
                    self.launch(jid,s,{'arguments':arguments,'environment':variant.get('environment',{})})
                    self.configure(jid,s,variant)
                    if variant.get('orientation'):
                        self.ui(jid,s,{'operation':'orientation','orientation':variant['orientation']})
                    steps=variant.get('steps',[])
                    if not isinstance(steps,list) or len(steps)>50:raise Problem('At most 50 UI steps per screenshot')
                    for step in steps:self.ui(jid,s,step)
                    if variant.get('wait_for'):self.ui(jid,s,{'operation':'wait','selector':variant['wait_for'],'timeout_seconds':30})
                    time.sleep(max(0,min(float(variant.get('settle_seconds',1)),5)))
                    name=ident(variant.get('name','screen'))[:20]+'-'+str(n)+'-'+udid[:8].lower()+'-'+language.lower()
                    result=self.screenshot(jid,s,{'name':name,'store':True})
                    results.append(dict(result,simulator_id=udid,language=language,locale=locale,variant=n))
            finally:self.end(jid,s)
        folder=self.w.managed_dir('results',jid)
        atomic_json(folder/'capture-manifest.json',{'captures':results,'dimension_spec_checked':'2026-11-14','spec':'https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications'})
        self.w.add_artifact(jid,folder/'capture-manifest.json','capture_manifest')
        import zipfile
        with zipfile.ZipFile(folder/'screenshots.zip','x',zipfile.ZIP_DEFLATED) as z:
            z.write(folder/'capture-manifest.json','capture-manifest.json')
            for result in results:
                name=result['artifact']['name'];z.write(folder/name,name)
        self.w.add_artifact(jid,folder/'screenshots.zip','screenshots')
        return {'captures':results,'count':len(results),'devices_reused':len(devices)}

    def register(self,jid,req):
        udid=str(uuid.UUID(req['simulator_id'])).upper();self.w.guard(admission=True)
        if udid in self.w.state['simulators']:return dict(self.w.state['simulators'][udid],already_registered=True)
        inventory=self.w.simulator_inventory();device=next((d for d in inventory['devices'] if d['udid']==udid),None)
        if not device or device['state']!='Shutdown' or udid in inventory['booted_ids']:raise Problem('Register an existing idle simulator only',409)
        ref=Path.home()/'Library/Developer/CoreSimulator/Devices'/udid/'data'
        backup=ref.with_name('data.before-ios-worker-v2');external=self.w.root/'Simulators'/udid/'data'
        if not ref.is_dir() or ref.is_symlink() or backup.exists() or external.exists():raise Problem('Device data layout needs manual review',409)
        external.parent.mkdir(parents=True,mode=0o700)
        self.w.run(jid,['/usr/bin/ditto',str(ref),str(external)],timeout=600)
        def fingerprint(root):
            h=hashlib.sha256();count=0
            for current,dirs,names in os.walk(root,followlinks=False):
                dirs.sort()
                for name in sorted(dirs+names):
                    p=Path(current)/name;rel=p.relative_to(root).as_posix()
                    value=['link',os.readlink(p)] if p.is_symlink() else (['file',p.stat().st_size,sha256_file(p)] if p.is_file() else ['directory'])
                    h.update(json.dumps([rel,value],ensure_ascii=False).encode());count+=1
            return h.hexdigest(),count
        original=fingerprint(ref)
        if original!=fingerprint(external):raise Problem('Device copy verification failed; original remains unchanged',409)
        # Recheck after copying, before replacing the data reference.
        inventory=self.w.simulator_inventory()
        if udid in inventory['booted_ids']:raise Problem('Device became occupied during registration',409)
        ref.rename(backup)
        try:ref.symlink_to(external,target_is_directory=True)
        except BaseException:backup.rename(ref);raise
        log_ref=Path.home()/'Library/Logs/CoreSimulator'/udid;log_dest=external.parent/'Logs'
        log_dest.mkdir(mode=0o700)
        if not log_ref.exists() and not log_ref.is_symlink():log_ref.parent.mkdir(parents=True,exist_ok=True);log_ref.symlink_to(log_dest,target_is_directory=True)
        record={'udid':udid,'name':device['name'],'runtime':device['runtime'],'data':str(external),'reference':str(ref),'original_backup':str(backup),'copy_sha256':original[0],'verified_entries':original[1],'registered_existing':True}
        self.w.state['simulators'][udid]=record;self.w.save();return record
