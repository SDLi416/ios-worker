"""Portable Python 3.9+ client. No third-party dependencies, no proxy, strict TLS."""
import argparse
import hashlib
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

class Client:
    def __init__(self, config):
        self.path = Path(config).resolve()
        self.cfg = json.loads(self.path.read_text(encoding='utf-8-sig'))
        self.url = urllib.parse.urlsplit(self.cfg['url'])
        if self.url.scheme != 'https':raise ValueError('HTTPS required')
        self.context = ssl.create_default_context(cafile=str(self.path.parent/self.cfg['ca_file']))
    def request(self, method, path, data=None, upload=None, output=None):
        if not path.startswith('/v1/') or '?' in path or '#' in path:raise ValueError('Use a /v1/ API path')
        conn = http.client.HTTPSConnection(self.url.hostname,self.url.port or 443,context=self.context,timeout=180)
        headers={'Authorization':'Bearer '+self.cfg['token'],'User-Agent':'ios-worker-python/2.0'}
        stream=None
        try:
            if upload:
                stream=open(upload,'rb');body=stream
                headers.update({'Content-Type':'application/zip','Content-Length':str(Path(upload).stat().st_size)})
            else:
                body=json.dumps(data).encode() if data is not None else (b'' if method=='POST' else None)
                if body is not None:headers.update({'Content-Type':'application/json','Content-Length':str(len(body))})
            conn.connect()
            pin=hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if pin.lower()!=self.cfg['certificate_sha256'].lower():raise ValueError('Certificate pin changed')
            conn.request(method,path,body=body,headers=headers)
            r=conn.getresponse()
            if r.status>=300:raise RuntimeError(f'HTTP {r.status}: '+r.read(8192).decode(errors='replace'))
            if output:
                dest=Path(output);dest.parent.mkdir(parents=True,exist_ok=True)
                with open(dest,'xb') as f:
                    while block:=r.read(1024*1024):f.write(block)
                return {'saved':str(dest)}
            raw=r.read().decode('utf-8')
            return json.loads(raw) if 'json' in r.getheader('Content-Type','') else raw
        finally:
            if stream:stream.close()
            conn.close()
    def wait(self,jid):
        while True:
            result=self.request('GET','/v1/jobs/'+jid)
            if result['status'] in ['succeeded','failed','cancelled','interrupted']:return result
            time.sleep(3)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default=str(Path(__file__).with_name('connection.json')))
    p.add_argument('--method',default='GET',choices=['GET','POST'])
    p.add_argument('--path',default='/v1/health')
    p.add_argument('--json',dest='json_text');p.add_argument('--json-file');p.add_argument('--upload');p.add_argument('--output')
    p.add_argument('--wait',action='store_true');p.add_argument('--verify',action='store_true')
    a=p.parse_args();client=Client(a.config)
    if a.verify:
        result={'health':client.request('GET','/v1/health'),
                'connection':client.request('POST','/v1/hello',{'client_id':socket.gethostname()}),
                'simulators':client.request('GET','/v1/simulators')}
    else:
        data=json.loads(Path(a.json_file).read_text(encoding='utf-8-sig')) if a.json_file else (json.loads(a.json_text) if a.json_text else None)
        result=client.request(a.method,a.path,data,a.upload,a.output)
        if a.wait:result=client.wait(result['id'])
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if isinstance(result,dict) and result.get('status') in ['failed','cancelled','interrupted']:sys.exit(1)

if __name__=='__main__':main()
