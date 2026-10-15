#!/usr/bin/env python3
"""Vision/TLS or Reality loopback calibration against independent wire bytes."""
import argparse
import contextlib
import json
import hashlib
import os
import pathlib
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import traceback
import concurrent.futures
from calibrate_xray import Origin, Handler, exact, port, counters, observer


class TLSOrigin(Origin):
    def get_request(self):
        stream,address=super().get_request()
        return self.tls.wrap_socket(stream,server_side=True),address


class HoldHandler(Handler):
    def handle(self):
        super().handle()
        self.server.release.wait(10)


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        outbound=socket.create_connection(self.server.target,timeout=20)
        def transfer(source,target,direction):
            try:
                while True:
                    data=source.recv(65536)
                    if not data:break
                    target.sendall(data)
                    with self.server.guard:self.server.count[direction]+=len(data)
            except OSError:pass
            finally:
                with contextlib.suppress(OSError):target.shutdown(socket.SHUT_WR)
        thread=threading.Thread(target=transfer,args=(self.request,outbound,'up'),daemon=True);thread.start()
        transfer(outbound,self.request,'down');thread.join(3);outbound.close()


def socks(entry,target):
    stream=socket.create_connection(('127.0.0.1',entry),timeout=20);stream.sendall(b'\x05\x01\x00');assert exact(stream,2)==b'\x05\x00'
    stream.sendall(b'\x05\x01\x00\x01'+socket.inet_aton(target[0])+struct.pack('!H',target[1]));head=exact(stream,4)
    if head[1]!=0:raise RuntimeError('Fixture SOCKS failed')
    exact(stream,4 if head[3]==1 else 16);exact(stream,2);return stream


def run(binary,security='tls',require_flow=False,require_progress=False,size=8*1024*1024,shutdown=False,bio=False):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='kenxu-vision-') as tmp,contextlib.ExitStack() as stack:
        directory=pathlib.Path(tmp);key=directory/'key.pem';cert=directory/'cert.pem'
        subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=fixture.local'],check=True,capture_output=True)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_3;tls.load_cert_chain(cert,key)
        origin=TLSOrigin(('127.0.0.1',0),HoldHandler);origin.tls=tls;origin.release=threading.Event();threading.Thread(target=origin.serve_forever,daemon=True).start();stack.callback(origin.server_close);stack.callback(origin.shutdown);stack.callback(origin.release.set)
        relay=Origin(('127.0.0.1',0),RelayHandler);relay.target=origin.server_address;relay.guard=threading.Lock();relay.count={'up':0,'down':0};threading.Thread(target=relay.serve_forever,daemon=True).start();stack.callback(relay.server_close);stack.callback(relay.shutdown)
        entry,api,client_port=port(),port(),port();identity=str(uuid.uuid4());email='kenxu:fixture:vision'
        server_stream={'network':'tcp','security':'tls','tlsSettings':{'certificates':[{'certificateFile':str(cert),'keyFile':str(key)}]}}
        certificate=ssl.PEM_cert_to_DER_cert(cert.read_text())
        client_stream={'network':'tcp','security':'tls','tlsSettings':{'serverName':'fixture.local','pinnedPeerCertSha256':hashlib.sha256(certificate).hexdigest()}}
        if security=='reality':
            generated=subprocess.run([binary,'x25519'],capture_output=True,text=True,check=True)
            keys=generated.stdout+generated.stderr
            values={line.split(':',1)[0].strip():line.split(':',1)[1].strip() for line in keys.splitlines() if ':' in line}
            private=values['PrivateKey'];public=values.get('Password (PublicKey)') or values.get('Password') or values.get('PublicKey')
            if not public:raise RuntimeError('Fixture Reality key generation failed')
            server_stream={'network':'tcp','security':'reality','realitySettings':{'target':'127.0.0.1:'+str(origin.server_address[1]),'serverNames':['fixture.local'],'privateKey':private,'shortIds':['1234abcd']}}
            client_stream={'network':'tcp','security':'reality','realitySettings':{'serverName':'fixture.local','fingerprint':'chrome','password':public,'shortId':'1234abcd'}}
        server={'log':{'loglevel':'none'},'api':{'tag':'lab-api','listen':'127.0.0.1:'+str(api),'services':['StatsService']},'stats':{},'policy':{'levels':{'88':{'statsUserUplink':True,'statsUserDownlink':True}}},'inbounds':[{'listen':'127.0.0.1','port':entry,'protocol':'vless','settings':{'decryption':'none','clients':[{'id':identity,'email':email,'level':88,'flow':'xtls-rprx-vision'}]},'streamSettings':server_stream}],'outbounds':[{'protocol':'freedom','tag':'direct'}]}
        client={'log':{'loglevel':'none'},'inbounds':[{'listen':'127.0.0.1','port':client_port,'protocol':'socks','settings':{'auth':'noauth','udp':True}}],'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':'127.0.0.1','port':entry,'users':[{'id':identity,'encryption':'none','flow':'xtls-rprx-vision'}]}]},'streamSettings':client_stream}]}
        processes=[]
        for name,config in [('server',server),('client',client)]:
            file=directory/(name+'.json');file.write_text(json.dumps(config));environment=dict(os.environ)
            if require_flow and name=='server':environment['KENXU_FLOW_SOCKET']=str(directory/'flow.sock')
            log=stack.enter_context((directory/(name+'.log')).open('w'));process=subprocess.Popen([binary,'run','-config',str(file)],stdout=log,stderr=log,env=environment)
            processes.append((name,process))
            stack.callback(lambda p=process:p.wait(timeout=5) if p.poll() is not None else (p.terminate(),p.wait(timeout=5)))
        for _ in range(80):
            try:counters(binary,'127.0.0.1:'+str(api));stream=socks(client_port,relay.server_address);break
            except Exception:
                for name,process in processes:
                    if process.poll() is not None:
                        # All logs here belong to generated loopback fixtures.
                        text=(directory/(name+'.log')).read_text()
                        raise RuntimeError('Fixture '+name+' exited: '+text[-2000:]) from None
                time.sleep(.1)
        else:raise RuntimeError('Vision fixture startup failed')
        client_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);client_tls.check_hostname=False;client_tls.verify_mode=ssl.CERT_NONE;client_tls.minimum_version=ssl.TLSVersion.TLSv1_3
        if bio:
            stream.close()
            client_tls=ssl.create_default_context();client_tls.check_hostname=False;client_tls.verify_mode=ssl.CERT_NONE
            # Exercise the exact MemoryBIO client from the real pilot, against
            # a loopback HTTPS server with independent relay counters.
            from pilot_flow import tls_request
            class HTTPHandler(socketserver.BaseRequestHandler):
                def handle(self):
                    self.request.settimeout(5);data=b''
                    while b'\r\n\r\n' not in data:
                        chunk=self.request.recv(4096)
                        if not chunk or len(data)+len(chunk)>16384:return
                        data+=chunk
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\ntest')
                    # Let the client finish by Content-Length, not by racing
                    # outer TLS teardown after Vision switches to raw copy.
                    time.sleep(1)
                    with contextlib.suppress(OSError):self.request.recv(4096)
            origin.RequestHandlerClass=HTTPHandler
            independent={'up':0,'down':0};failure=None;version=None
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    results=list(pool.map(lambda _:tls_request(client_port,'127.0.0.1','/',relay.server_address[1],client_tls),range(16)))
                independent={direction:sum(r[direction] for r in results) for direction in ['up','down']};version=results[0]['tlsVersion']
            except Exception as error:failure=error
            time.sleep(1.3)
            final=counters(binary,'127.0.0.1:'+str(api))[email]
            with relay.guard:wire=dict(relay.count)
            print(json.dumps({'stage':'bio-https','client':independent,'origin':wire,'native':final,'tlsVersion':version}),flush=True)
            if failure:raise failure
            if final!=wire:raise AssertionError('HTTPS target socket and native byte counts differ')
            if independent!=wire:print(json.dumps({'stage':'bio-transport-tail','upExtraAtTarget':wire['up']-independent['up'],'downDifference':wire['down']-independent['down']}),flush=True)
            if require_flow:
                total={'up':0,'down':0}
                while True:
                    snap=observer(directory/'flow.sock')
                    if not snap['records']:break
                    for row in snap['records']:total['up']+=row['up'];total['down']+=row['down']
                    observer(directory/'flow.sock','POST',{'epoch':snap['epoch'],'seq':snap['seq']})
                if total!=wire:raise AssertionError('Short HTTPS per-flow bytes differ from independent origin wire')
                print(json.dumps({'stage':'bio-flow','passed':True,'targetWireExact':True}),flush=True)
            return
        secure=client_tls.wrap_socket(stream,server_hostname='fixture.local');started=time.monotonic();secure.sendall(struct.pack('!QQ',1024,size)+b'u'*1024);exact(secure,size);transfer_seconds=time.monotonic()-started
        time.sleep(1.3);mid=counters(binary,'127.0.0.1:'+str(api))[email]
        with relay.guard:wire_mid=dict(relay.count)
        live=mid['down']==wire_mid['down']
        print(json.dumps({'stage':security+'-vision-progress','liveBytesMatch':live,'observedDown':mid['down'],'wireDown':wire_mid['down'],'transferSeconds':round(transfer_seconds,4)}),flush=True)
        if require_progress and not live:raise AssertionError('Long-connection byte progress waits for close')
        if shutdown:
            if not require_flow:raise AssertionError('Shutdown fixture requires flow observer')
            process=processes[0][1];process.terminate();process.wait(timeout=10)
            total={'up':0,'down':0};files=list((directory/'spool').glob('*.json'))
            for file in files:
                snap=json.loads(file.read_text())
                for row in snap['records']:
                    total['up']+=row['up'];total['down']+=row['down']
            if total!=wire_mid:raise AssertionError('Graceful shutdown tail differs from independently observed bytes')
            secure.close();origin.release.set()
            print(json.dumps({'stage':security+'-vision-shutdown','passed':True,'gracefulTailExact':True,'checkpointBatches':len(files)}),flush=True)
            return
        origin.release.set();secure.close();time.sleep(.5);final=counters(binary,'127.0.0.1:'+str(api))[email]
        with relay.guard:wire=dict(relay.count)
        if final!=wire:raise AssertionError('Vision user totals differ from independent TLS wire counters')
        if require_flow:
            total={'up':0,'down':0};path=directory/'flow.sock'
            while True:
                snap=observer(path)
                if not snap['records']:break
                for r in snap['records']:total['up']+=r['up'];total['down']+=r['down']
                observer(path,'POST',{'epoch':snap['epoch'],'seq':snap['seq']})
            if total!=wire:raise AssertionError('Vision per-flow bytes differ from independent TLS wire counters')
        print(json.dumps({'stage':security+'-vision-final','passed':True,'wireExact':True,'perFlow':require_flow}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--binary',default='/usr/local/bin/xray');p.add_argument('--security',choices=['tls','reality'],default='tls');p.add_argument('--require-flow',action='store_true');p.add_argument('--require-progress',action='store_true');p.add_argument('--size',type=int,default=8*1024*1024);p.add_argument('--shutdown',action='store_true');p.add_argument('--bio',action='store_true');args=p.parse_args()
    try:run(args.binary,args.security,args.require_flow,args.require_progress,args.size,args.shutdown,args.bio)
    except Exception as error:print(json.dumps({'ok':False,'errorClass':type(error).__name__,'reason':str(error) if isinstance(error,(AssertionError,RuntimeError,ssl.SSLError)) else 'Isolated Vision fixture failed; production unchanged','trace':[{'file':pathlib.Path(f.filename).name,'line':f.lineno,'call':f.name} for f in traceback.extract_tb(error.__traceback__)]}),flush=True);raise SystemExit(1)


if __name__=='__main__':main()
