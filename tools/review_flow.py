#!/usr/bin/env python3
"""Read-only local post-rollout checks; never emits identities, IPs or tokens."""
import hashlib
import json
import pathlib
import sqlite3
import stat
import subprocess
import sys
import time
sys.path.insert(0,'/usr/local/lib/kenxu-meter')
from kenxu_flow import UnixHTTP


def native(binary,server):
    result=subprocess.run([binary,'api','statsquery','-s',server,'-pattern','kenxu:'],capture_output=True,text=True,check=True,timeout=10)
    rows={}
    for item in json.loads(result.stdout).get('stat',[]):
        parts=item['name'].split('>>>')
        if len(parts)==4 and parts[0]=='user' and parts[2]=='traffic':
            rows.setdefault(parts[1],{'up':0,'down':0})['up' if parts[3]=='uplink' else 'down']=int(item.get('value',0))
    return rows


def review():
    settings=json.loads(pathlib.Path('/etc/kenxu-meter/config.json').read_text());binary=settings.get('xray_binary','/usr/local/bin/xray');server='127.0.0.1:'+str(settings.get('api_port',10085));socket=pathlib.Path(settings.get('flow_socket','/var/lib/kenxu-flow/observer.sock'))
    for _ in range(20):
        before=native(binary,server);connection=UnixHTTP(str(socket))
        try:connection.request('GET','/status');response=connection.getresponse();assert response.status==200;observed=json.loads(response.read())
        finally:connection.close()
        after=native(binary,server)
        actual={row['email']:row for row in observed['totals']}
        emails=set(before)|set(actual)
        if observed['scope']=='selected':emails&=set(observed['users'])
        if all(before.get(email,{}).get(direction,0)<=actual.get(email,{}).get(direction,0)<=after.get(email,{}).get(direction,0) for email in emails for direction in ['up','down']):break
        time.sleep(.1)
    else:raise AssertionError('Native and observed cumulative counters do not reconcile')
    database=sqlite3.connect(str(pathlib.Path(settings.get('state_dir','/var/lib/kenxu-meter'))/'queue.sqlite'))
    try:
        pid=subprocess.check_output(['systemctl','show','xray','-p','MainPID','--value'],text=True).strip();executable=pathlib.Path('/proc/'+pid+'/exe').resolve();config=json.loads(pathlib.Path(settings.get('xray_config','/usr/local/etc/xray/config.json')).read_text())
        result={'ok':True,'nativeObserverReconciled':True,'scope':observed['scope'],'observedUsers':len(observed['totals']),'binarySha256':hashlib.sha256(executable.read_bytes()).hexdigest(),'apiLoopback':config.get('api',{}).get('listen')==server,'privateSocket':stat.S_IMODE(socket.stat().st_mode)==0o600,'nativeQueue':database.execute('SELECT count(*) FROM queue').fetchone()[0],'flowQueue':database.execute('SELECT count(*) FROM flow_queue').fetchone()[0],'queueIntegrity':database.execute('PRAGMA integrity_check').fetchone()[0]=='ok','services':all(subprocess.call(['systemctl','is-active','--quiet',s])==0 for s in ['xray','kenxu-meter'])}
        if not all(result[k] for k in ['apiLoopback','privateSocket','queueIntegrity','services']):raise AssertionError('Post-rollout operational guard failed')
        return result
    finally:database.close()


if __name__=='__main__':
    try:print(json.dumps(review()))
    except Exception as error:print(json.dumps({'ok':False,'errorClass':type(error).__name__,'reason':'Read-only review failed; inspect private host state'}));raise SystemExit(1)
