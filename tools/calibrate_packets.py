#!/usr/bin/env python3
"""Loopback fixtures for VLESS UDP, TCP MUX and multi-destination XUDP.

Run inside a fresh network namespace. No production configuration is read.
"""
import argparse
import contextlib
import json
import os
import pathlib
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from calibrate_xray import Origin, Handler, exact, port, counters, observer


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data,stream=self.request
        stream.sendto(data,self.client_address)


class UDP(socketserver.ThreadingUDPServer):
    daemon_threads=True


def address(target):
    return struct.pack('!H',target[1])+b'\x01'+socket.inet_aton(target[0])


def frame(session,status,data=None,target=None,udp=False,global_id=None):
    meta=struct.pack('!HBB',session,status,int(data is not None))
    if target:meta+=bytes([2 if udp else 1])+address(target)
    if global_id:meta+=global_id
    return struct.pack('!H',len(meta))+meta+(struct.pack('!H',len(data))+data if data is not None else b'')


def connect(entry,identity,command,target=None,source='127.0.0.1'):
    stream=socket.create_connection(('127.0.0.1',entry),timeout=10,source_address=(source,0))
    stream.sendall(b'\x00'+uuid.UUID(identity).bytes+b'\x00'+bytes([command])+(address(target) if target else b''))
    return stream


def read_frame(stream):
    meta=exact(stream,struct.unpack('!H',exact(stream,2))[0])
    sid,status,option=struct.unpack('!HBB',meta[:4])
    data=exact(stream,struct.unpack('!H',exact(stream,2))[0]) if option&1 else b''
    return sid,status,data


def run(binary,require_flow=False):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='kenxu-packets-') as temp,contextlib.ExitStack() as stack:
        directory=pathlib.Path(temp);origins=[];datagrams=[]
        for host in ['127.0.0.1','127.0.0.2']:
            for kind,handler,collection in [(Origin,Handler,origins),(UDP,Echo,datagrams)]:
                server=kind((host,0),handler);threading.Thread(target=server.serve_forever,daemon=True).start();stack.callback(server.server_close);stack.callback(server.shutdown);collection.append(server.server_address)
        entry,api=port(),port();users=[(str(uuid.uuid4()),'kenxu:fixture:alice'),(str(uuid.uuid4()),'kenxu:fixture:bob')]
        config={'log':{'loglevel':'none'},'api':{'tag':'lab-api','listen':'127.0.0.1:'+str(api),'services':['StatsService']},'stats':{},'policy':{'levels':{'88':{'statsUserUplink':True,'statsUserDownlink':True}}},'inbounds':[{'listen':'127.0.0.1','port':entry,'protocol':'vless','settings':{'decryption':'none','clients':[{'id':i,'email':e,'level':88} for i,e in users]}}],'outbounds':[{'tag':'direct','protocol':'freedom'}]}
        file=directory/'server.json';file.write_text(json.dumps(config));env=dict(os.environ)
        if require_flow:env['KENXU_FLOW_SOCKET']=str(directory/'flow.sock')
        log=stack.enter_context((directory/'core.log').open('w'));process=subprocess.Popen([binary,'run','-config',str(file)],stdout=log,stderr=log,env=env);stack.callback(lambda:(process.terminate(),process.wait(timeout=5)) if process.poll() is None else None)
        for _ in range(60):
            try:counters(binary,'127.0.0.1:'+str(api));break
            except Exception:time.sleep(.1)
        else:raise AssertionError('Packet fixture failed readiness')
        expected={e:{'up':0,'down':0} for _,e in users};targets={};peers={}
        def add(email,target,up,down,network,source='127.0.0.1'):
            expected[email]['up']+=up;expected[email]['down']+=down
            row=targets.setdefault((email,target[0],target[1],network),{'up':0,'down':0});row['up']+=up;row['down']+=down
            row=peers.setdefault((email,source),{'up':0,'down':0});row['up']+=up;row['down']+=down
        for identity,email in users:
            # Plain VLESS UDP length framing is not application payload.
            with connect(entry,identity,2,datagrams[0]) as stream:
                for index in range(12):
                    data=b'u'*(31+index*17);stream.sendall(struct.pack('!H',len(data))+data)
                    if index==0:assert exact(stream,2)==b'\x00\x00'
                    received=exact(stream,struct.unpack('!H',exact(stream,2))[0]);assert received==data;add(email,datagrams[0],len(data),len(data),'udp')
            # Two distinct TCP streams carried by a single authenticated MUX.
            with connect(entry,identity,3) as stream:
                received={1:bytearray(),2:bytearray()}
                for index,target in enumerate(origins,1):
                    data=struct.pack('!QQ',100*index,3000*index)+b'u'*(100*index);stream.sendall(frame(index,1,data,target));add(email,target,len(data),3000*index,'tcp')
                assert exact(stream,2)==b'\x00\x00'
                while any(len(received[i])<3000*i for i in received):
                    sid,status,data=read_frame(stream);received[sid].extend(data)
                for sid,data in received.items():assert bytes(data)==b'x'*(3000*sid)
            # XUDP keeps the logical flow but changes packet destinations.
            with connect(entry,identity,3) as stream:
                gid=os.urandom(8)
                for index in range(12):
                    target=datagrams[index%2];data=b'x'*(71+index*11)
                    stream.sendall(frame(11,1 if index==0 else 2,data,target,udp=True,global_id=gid if index==0 else None))
                    if index==0:assert exact(stream,2)==b'\x00\x00'
                    sid,status,received=read_frame(stream);assert sid==11 and received==data;add(email,target,len(data),len(data),'udp')
                # Keep the old carrier open while resuming from another peer.
                with connect(entry,identity,3,source='127.0.0.2') as resumed:
                    for index in range(4):
                        target=datagrams[index%2];data=b'r'*(211+index)
                        resumed.sendall(frame(12,1 if index==0 else 2,data,target,udp=True,global_id=gid if index==0 else None))
                        if index==0:assert exact(resumed,2)==b'\x00\x00'
                        sid,status,received=read_frame(resumed);assert sid==12 and received==data;add(email,target,len(data),len(data),'udp','127.0.0.2')
        for _ in range(60):
            actual=counters(binary,'127.0.0.1:'+str(api))
            if actual==expected:break
            time.sleep(.05)
        if actual!=expected:raise AssertionError('UDP/MUX native bytes differ from independent payload counters: '+json.dumps({'expected':expected,'actual':actual}))
        if require_flow:
            observed={};actual_peers={}
            while True:
                snap=observer(directory/'flow.sock')
                if not snap['records']:break
                for r in snap['records']:
                    if not r['up'] and not r['down']:continue
                    peer=actual_peers.setdefault((r['email'],r['source']),{'up':0,'down':0});peer['up']+=r['up'];peer['down']+=r['down']
                    row=observed.setdefault((r['email'],r['host'],r['port'],r['network']),{'up':0,'down':0});row['up']+=r['up'];row['down']+=r['down']
                observer(directory/'flow.sock','POST',{'epoch':snap['epoch'],'seq':snap['seq']})
            if observed!=targets:raise AssertionError('UDP/MUX target attribution mismatch: '+json.dumps({'expected':list(targets.values()),'actual':list(observed.values())}))
            if actual_peers!=peers:raise AssertionError('XUDP resume retained a stale transport source')
        print(json.dumps({'stage':'udp-mux-xudp','passed':True,'users':2,'udpPackets':56,'muxStreams':4,'exactBytes':True,'perTarget':require_flow,'transportPeer':require_flow,'peerMigration':require_flow}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--binary',default='/usr/local/bin/xray');p.add_argument('--require-flow',action='store_true');args=p.parse_args()
    try:run(args.binary,args.require_flow)
    except Exception as e:print(json.dumps({'ok':False,'errorClass':type(e).__name__,'reason':str(e) if isinstance(e,AssertionError) else 'Isolated packet fixture failed; production unchanged'}));raise SystemExit(1)
