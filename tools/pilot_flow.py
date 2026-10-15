#!/usr/bin/env python3
"""Small real-credential pilot through the existing production Reality inlet.

Independent SSL MemoryBIO counts inner TLS wire bytes, not file sizes. Secrets
remain in private files/RAM. The test adds a small amount of genuine user usage.
"""
import argparse
import concurrent.futures
import contextlib
import json
import os
import pathlib
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import re
import sys
sys.path.insert(0,'/usr/local/lib/kenxu-meter')


def exact(stream,size):
    result=bytearray()
    while len(result)<size:
        data=stream.recv(size-len(result))
        if not data:raise RuntimeError('SOCKS fixture closed')
        result.extend(data)
    return result


def tls_request(proxy,target='www.cloudflare.com',path='/cdn-cgi/trace',target_port=443,tls_context=None,observed=None):
    wire=observed if observed is not None else {'up':0,'down':0}
    with socket.create_connection(('127.0.0.1',proxy),timeout=30) as stream:
        stream.sendall(b'\x05\x01\x00');assert exact(stream,2)==b'\x05\x00';host=target.encode()
        stream.sendall(b'\x05\x01\x00\x03'+bytes([len(host)])+host+struct.pack('!H',target_port));head=exact(stream,4);assert head[1]==0
        exact(stream,4 if head[3]==1 else 16);exact(stream,2)
        incoming,outgoing=ssl.MemoryBIO(),ssl.MemoryBIO();context=tls_context or ssl.create_default_context();context.set_alpn_protocols(['http/1.1']);secure=context.wrap_bio(incoming,outgoing,server_hostname=target)
        def flush():
            while outgoing.pending:
                data=outgoing.read();stream.sendall(data);wire['up']+=len(data)
        def receive():
            flush();data=stream.recv(65536)
            if not data:incoming.write_eof();return False
            wire['down']+=len(data);incoming.write(data);return True
        while True:
            try:secure.do_handshake();flush();break
            except ssl.SSLWantReadError:
                if not receive():raise RuntimeError('TLS handshake closed')
        wire['tlsVersion']=secure.version()
        request=('GET '+path+' HTTP/1.1\r\nHost: '+target+'\r\nConnection: close\r\nUser-Agent: Kenxu-Calibrate/1\r\n\r\n').encode();secure.write(request);flush();response=bytearray()
        while True:
            try:
                data=secure.read(65536)
                if not data:break
                response.extend(data)
                if len(response)>2*1024*1024:raise RuntimeError('Pilot response oversized')
                if b'\r\n\r\n' in response:
                    header,body=bytes(response).split(b'\r\n\r\n',1)
                    sizes=[line.split(b':',1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')]
                    if sizes and sizes[0].isdigit() and len(body)>=int(sizes[0]):break
            except ssl.SSLWantReadError:
                if not receive():
                    with contextlib.suppress(ssl.SSLEOFError,ssl.SSLZeroReturnError):response.extend(secure.read(65536))
                    break
            except ssl.SSLZeroReturnError:break
        if not response.startswith(b'HTTP/1.1 200 '):raise RuntimeError('Pilot target did not return HTTP 200')
        country=re.search(rb'(?:^|[\r\n])loc=([A-Z]{2})(?:[\r\n]|$)',response)
        wire['country']=country[1].decode() if country else None
    return wire


def local_status():
    from kenxu_flow import UnixHTTP
    connection=UnixHTTP('/var/lib/kenxu-flow/observer.sock')
    try:
        connection.request('GET','/status');response=connection.getresponse();assert response.status==200;return json.loads(response.read())
    finally:connection.close()


def native(binary,api,email):
    raw=subprocess.run([binary,'api','statsquery','-s',api,'-pattern',email],capture_output=True,text=True,check=True,timeout=10).stdout
    return {('up' if item['name'].endswith('uplink') else 'down'):int(item.get('value',0)) for item in json.loads(raw).get('stat',[]) if item['name'].startswith('user>>>') and '>>>traffic>>>' in item['name']}


def run(probe,count=8,client_binary='/usr/local/bin/xray'):
    os.umask(0o077);settings=json.loads(pathlib.Path('/etc/kenxu-meter/config.json').read_text());config=json.loads(pathlib.Path(settings.get('xray_config','/usr/local/etc/xray/config.json')).read_text());inbound=next(i for i in config['inbounds'] if i.get('protocol')=='vless' and i.get('port')==443)
    identity=next(c for c in inbound['settings']['clients'] if c.get('email')==probe['email']);reality=inbound['streamSettings']['realitySettings'];binary='/usr/local/bin/xray'
    if local_status()['scope']=='selected' and probe['email'] not in local_status()['users']:raise RuntimeError('Pilot identity is not observed')
    with tempfile.TemporaryDirectory(prefix='kenxu-real-pilot-') as temporary:
        with socket.socket() as reserved:reserved.bind(('127.0.0.1',0));proxy=reserved.getsockname()[1]
        client={'log':{'loglevel':'none'},'inbounds':[{'listen':'127.0.0.1','port':proxy,'protocol':'socks','settings':{'auth':'noauth'}}],'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':'127.0.0.1','port':443,'users':[{'id':identity['id'],'encryption':'none','flow':'xtls-rprx-vision'}]}]},'streamSettings':{'network':'tcp','security':'reality','realitySettings':{'serverName':reality['serverNames'][0],'fingerprint':'chrome','password':probe['publicKey'],'shortId':reality['shortIds'][0]}}}]}
        file=pathlib.Path(temporary)/'client.json';file.write_text(json.dumps(client));env={k:v for k,v in os.environ.items() if not k.startswith('KENXU_FLOW_')};env.update(GOMAXPROCS='1',GOMEMLIMIT='32MiB')
        with (pathlib.Path(temporary)/'client.log').open('w') as log:
            process=subprocess.Popen([client_binary,'run','-config',str(file)],stdout=log,stderr=log,env=env)
            try:
                for _ in range(60):
                    try:
                        with socket.create_connection(('127.0.0.1',proxy),timeout=.2):break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('Pilot client not ready')
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:tls_request(proxy),range(count)))
                time.sleep(1.3);api='127.0.0.1:'+str(settings.get('api_port',10085));matched=False
                for _ in range(20):
                    before=native(binary,api,probe['email']);status=local_status();observed=next(({'up':v['up'],'down':v['down']} for v in status['totals'] if v['email']==probe['email']),None);after=native(binary,api,probe['email'])
                    if observed and all(before.get(d,0)<=observed[d]<=after.get(d,0) for d in ['up','down']):matched=True;break
                    time.sleep(.1)
                if not matched:raise AssertionError('Native and observed cumulative counters do not reconcile within a bracketed live read')
                countries=sorted(set(v['country'] for v in results if v.get('country')))
                if probe.get('expectedCountry') and countries!=[probe['expectedCountry']]:raise AssertionError('Forwarded exit country does not match the expected route')
                result={'ok':True,'productionReality':True,'requests':count,'concurrency':4,'independentWire':{d:sum(v[d] for v in results) for d in ['up','down']},'tlsVersions':sorted(set(v['tlsVersion'] for v in results)),'countries':countries,'nativeObserverReconciled':True,'target':'www.cloudflare.com','scope':status['scope']}
                pathlib.Path(args.probe_file).with_name('pilot-result.json').write_text(json.dumps(result));return result
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--probe-file',required=True);p.add_argument('--count',type=int,default=8);p.add_argument('--client-binary',default='/usr/local/bin/xray');args=p.parse_args()
    try:print(json.dumps(run(json.loads(pathlib.Path(args.probe_file).read_text()),args.count,args.client_binary)),flush=True)
    except Exception as error:print(json.dumps({'ok':False,'errorClass':type(error).__name__,'reason':str(error) if isinstance(error,AssertionError) else 'Production pilot failed; stop promotion and review private fixture state'}));raise SystemExit(1)
