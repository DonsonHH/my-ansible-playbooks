#!/usr/bin/env python3
"""Isolated, loopback-only Xray calibration using known raw TCP bytes.

Never reads live config, real credentials or production API ports. Output is
sanitized JSON. --require-flow makes the observer contract red-capable.
"""
import argparse
import concurrent.futures
import contextlib
import json
import http.client
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
import urllib.request
import traceback


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=10)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(str(self.socket_path))


def observer(path, method='GET', value=None):
    connection = UnixHTTP(path)
    try:
        connection.request(method, '/snapshot' if method=='GET' else '/ack', body=json.dumps(value) if value else None, headers={'Content-Type':'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('Isolated observer API failed')
        return json.loads(response.read(2*1024*1024))
    finally:
        connection.close()


def exact(stream, size):
    result = bytearray()
    while len(result) < size:
        value = stream.recv(min(65536, size-len(result)))
        if not value:
            raise RuntimeError('Premature fixture EOF')
        result.extend(value)
    return bytes(result)


class Origin(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    request_queue_size = 128
    daemon_threads = True


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        header = exact(self.request, 16)
        upload, download = struct.unpack('!QQ', header)
        if max(upload, download) > 16*1024*1024:
            return
        remaining = upload
        while remaining:
            data = self.request.recv(min(65536, remaining))
            if not data:
                return
            remaining -= len(data)
        remaining = download
        while remaining:
            value = b'x' * min(65536, remaining)
            self.request.sendall(value)
            remaining -= len(value)


def port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def counters(binary, address):
    p = subprocess.run([binary, 'api', 'statsquery', '-s', address, '-pattern', 'kenxu:fixture'], capture_output=True, text=True, timeout=10)
    if p.returncode:
        raise RuntimeError('Fixture API not ready')
    rows = {}
    for item in json.loads(p.stdout).get('stat', []):
        bits = item['name'].split('>>>')
        if len(bits) == 4 and bits[0] == 'user':
            rows.setdefault(bits[1], {'up':0,'down':0})['up' if bits[3]=='uplink' else 'down'] = int(item.get('value', 0))
    return rows


def send(entry, identity, destination, upload, download):
    started=time.monotonic()
    with socket.create_connection(('127.0.0.1', entry), timeout=20) as stream:
        target, target_port = destination
        # VLESS v0, no flow, TCP, IPv4. Authentication is a fresh fixture UUID.
        request = b'\x00' + uuid.UUID(identity).bytes + b'\x00\x01' + struct.pack('!H', target_port) + b'\x01' + socket.inet_aton(target)
        stream.sendall(request + struct.pack('!QQ', upload, download) + b'u'*upload)
        response = exact(stream, 2)
        first_byte_ms=(time.monotonic()-started)*1000
        if response != b'\x00\x00':
            raise RuntimeError('Invalid fixture VLESS response')
        remaining=download
        while remaining:
            payload=exact(stream,min(65536,remaining))
            if payload != b'x'*len(payload):
                raise RuntimeError('Fixture payload mismatch')
            remaining-=len(payload)
        return first_byte_ms


def resources(pid):
    fields=pathlib.Path('/proc/'+str(pid)+'/stat').read_text().split()
    status=pathlib.Path('/proc/'+str(pid)+'/status').read_text().splitlines()
    return {'cpu':(int(fields[13])+int(fields[14]))/os.sysconf('SC_CLK_TCK'),'rss':next(int(line.split()[1]) for line in status if line.startswith('VmHWM:'))*1024}


def run(binary, require_flow=False, count=8, benchmark=False):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='kenxu-calibration-') as temporary, contextlib.ExitStack() as stack:
        directory = pathlib.Path(temporary)
        origins = []
        for address in ['127.0.0.1', '127.0.0.2']:
            server = Origin((address, 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            origins.append(server.server_address)
        entry, api = port(), port()
        users = [(str(uuid.uuid4()), 'kenxu:fixture:alice'), (str(uuid.uuid4()), 'kenxu:fixture:bob')]
        cfg = {'log': {'loglevel':'none'}, 'api':{'tag':'lab-api','listen':'127.0.0.1:'+str(api),'services':['StatsService']}, 'stats':{}, 'policy':{'levels':{'88':{'statsUserUplink':True,'statsUserDownlink':True}}}, 'inbounds':[{'tag':'lab-in','listen':'127.0.0.1','port':entry,'protocol':'vless','settings':{'decryption':'none','clients':[{'id':identity,'email':email,'level':88} for identity,email in users]}}], 'outbounds':[{'tag':'lab-direct','protocol':'freedom','settings':{}}]}
        config = directory/'server.json'
        config.write_text(json.dumps(cfg))
        with (directory/'core.log').open('w') as log:
            environment = dict(os.environ)
            if require_flow:
                environment['KENXU_FLOW_SOCKET'] = str(directory/'flow.sock')
            process = subprocess.Popen([binary, 'run', '-config', str(config)], stdout=log, stderr=log, env=environment)
            stack.callback(lambda: process.wait(timeout=5) if process.poll() is not None else (process.terminate(), process.wait(timeout=5)))
            for attempt in range(60):
                try:
                    counters(binary, '127.0.0.1:'+str(api))
                    break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError('Isolated core exited before readiness') from None
                    time.sleep(.1)
            else:
                raise RuntimeError('Isolated API readiness timeout')
            expected = {email:{'up':0,'down':0} for _,email in users}
            if benchmark:
                # Performance excludes first-registration races in the stock
                # core. The cold-start race has its own deterministic Go test.
                for identity,email in users:
                    send(entry,identity,origins[0],0,1)
                    expected[email]['up']+=16;expected[email]['down']+=1
            started = time.monotonic()
            before=resources(process.pid)
            jobs = []
            for i in range(count):
                identity,email = users[i%2]
                up,down = (1024,16*1024*1024) if benchmark else (1024*(i%8+1),8192*(i%8+1))
                expected[email]['up'] += up+16
                expected[email]['down'] += down
                jobs.append((identity,origins[i%2],up,down))
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                timings=list(pool.map(lambda j: send(entry, *j), jobs))
            transfer=time.monotonic()-started;after=resources(process.pid)
            # Response delivery and the server's final counter update are not
            # an atomic operation. Bound the wait, never hide excess bytes.
            for _ in range(60):
                actual = counters(binary, '127.0.0.1:'+str(api))
                if actual == expected or any(actual.get(e,{}).get(d,0)>v[d] for e,v in expected.items() for d in ('up','down')):
                    break
                time.sleep(.05)
            if actual != expected:
                print(json.dumps({'stage':'raw-tcp','passed':False,'expected':expected,'actual':actual}))
                raise AssertionError('Actual user counters differ from independent fixture bytes')
            print(json.dumps({'stage':'raw-tcp','passed':True,'users':2,'origins':2,'connections':count,'exactBytes':True,'seconds':round(time.monotonic()-started,3)}), flush=True)
            if require_flow:
                # Activated only for the patched observer. A stock core must not
                # pass because its process merely started successfully.
                output = directory/'flow.sock'
                if not output.exists():
                    raise AssertionError('Per-flow observer not present; do not claim domain-byte calibration')
                observed = {email:{'up':0,'down':0} for _,email in users}
                batches = 0
                while True:
                    first = observer(output)
                    if not first['records']:
                        break
                    again = observer(output)
                    if again != first:
                        raise AssertionError('Unacknowledged snapshot changed across retries')
                    for row in first['records']:
                        observed[row['email']]['up'] += row['up']
                        observed[row['email']]['down'] += row['down']
                    observer(output, 'POST', {'epoch':first['epoch'],'seq':first['seq']})
                    observer(output, 'POST', {'epoch':first['epoch'],'seq':first['seq']})
                    batches += 1
                if observed != expected:
                    raise AssertionError('Per-flow bytes do not match independent raw TCP fixtures')
                print(json.dumps({'stage':'per-flow','passed':True,'exactBytes':True,'stableRetry':True,'idempotentAck':True,'batches':batches}), flush=True)
            return {'seconds':transfer,'cpu':after['cpu']-before['cpu'],'rss':after['rss'],'firstByteP95':sorted(timings)[int((len(timings)-1)*.95)],'firstBytes':timings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', default='/usr/local/bin/xray')
    parser.add_argument('--require-flow', action='store_true')
    parser.add_argument('--count', type=int, default=8)
    args = parser.parse_args()
    try:
        run(args.binary, args.require_flow, args.count)
    except Exception as exc:
        trace=[{'file':pathlib.Path(f.filename).name,'line':f.lineno,'call':f.name} for f in traceback.extract_tb(exc.__traceback__)]
        print(json.dumps({'ok':False,'errorClass':type(exc).__name__,'reason':str(exc) if isinstance(exc, AssertionError) else 'Isolated fixture failed; no production service was touched','trace':trace}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
