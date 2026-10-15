#!/usr/bin/env python3
"""Fail-fast calibration gate. Run with sudo unshare --net after enabling lo."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import pathlib
import subprocess
import time
import calibrate_xray
import calibrate_vision
import calibrate_packets


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--original',default='/usr/local/bin/xray');parser.add_argument('--candidate',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    if os.readlink('/proc/self/ns/net')==os.readlink('/proc/1/ns/net'):raise RuntimeError('Use an isolated network namespace; never fill production conntrack')
    result={'schema':1,'at':int(time.time()*1000),'candidateSha256':hashlib.sha256(pathlib.Path(args.candidate).read_bytes()).hexdigest(),'upstreamCommit':'d2758a023cd7f4174a5a5fa4ff66e487d4342ba0','steps':[],'passed':False}
    def step(name,callback):
        output=io.StringIO();started=time.monotonic()
        try:
            with contextlib.redirect_stdout(output):callback()
            entry={'name':name,'passed':True,'seconds':round(time.monotonic()-started,3),'evidence':[json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]}
        except Exception as error:
            entry={'name':name,'passed':False,'errorClass':type(error).__name__}
            result['steps'].append(entry);print(json.dumps(entry),flush=True);raise
        result['steps'].append(entry);print(json.dumps(entry),flush=True)
    try:
        step('original-raw',lambda:calibrate_xray.run(args.original,count=2))
        step('candidate-10000-short-connections',lambda:calibrate_xray.run(args.candidate,True,10000))
        step('original-udp-mux-xudp',lambda:calibrate_packets.run(args.original))
        step('candidate-udp-mux-xudp-peer-migration',lambda:calibrate_packets.run(args.candidate,True))
        for security in ['tls','reality']:
            step('original-'+security+'-vision',lambda s=security:calibrate_vision.run(args.original,s))
            step('candidate-'+security+'-vision',lambda s=security:calibrate_vision.run(args.candidate,s,True,True))
        step('candidate-graceful-tail',lambda:calibrate_vision.run(args.candidate,'reality',True,True,shutdown=True))
        step('original-short-https-tail',lambda:calibrate_vision.run(args.original,'reality',bio=True))
        step('candidate-short-https-tail',lambda:calibrate_vision.run(args.candidate,'reality',True,bio=True))
        def performance():
            test=subprocess.run(['python3',str(pathlib.Path(__file__).with_name('calibrate_performance.py')),'--original',args.original,'--candidate',args.candidate],capture_output=True,text=True,timeout=180)
            print(test.stdout,end='')
            if test.returncode:raise AssertionError('Performance promotion gate failed')
        step('performance-five-pairs',performance)
        result['passed']=True
    finally:
        pathlib.Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'allCalibrationGatesPassed':True,'candidateSha256':result['candidateSha256']}),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as error:print(json.dumps({'passed':False,'errorClass':type(error).__name__}));raise SystemExit(1)
