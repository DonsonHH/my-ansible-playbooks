#!/usr/bin/env python3
"""Guarded core/observer rollout on an existing registered host (run as root).

Never regenerates credentials or routing. Errors restore service/code/config,
never an old queue database. A short node reconnect is part of this upgrade.
"""
import argparse
import hashlib
import json
import os
import pathlib
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback


def command(*args,timeout=30):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Maintenance command failed: '+pathlib.Path(args[0]).name)
    return result.stdout


def digest(file):
    return hashlib.sha256(pathlib.Path(file).read_bytes()).hexdigest()


def atomic(file,data,mode=0o600):
    temporary=file.with_name(file.name+'.next')
    with temporary.open('wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.chmod(temporary,mode);os.replace(temporary,file)


def deploy(stage,expected,users):
    if os.geteuid()!=0:raise ValueError('Run maintenance as root')
    os.umask(0o077);stage=stage.resolve();binary=stage/'xray-kenxu-amd64'
    if not expected or digest(binary)!=expected:raise ValueError('Candidate SHA-256 mismatch')
    os.chmod(binary,0o700)
    registration=pathlib.Path('/etc/kenxu-meter/config.json');settings=json.loads(registration.read_text())
    config=pathlib.Path(settings.get('xray_config','/usr/local/etc/xray/config.json'));old_config=config.read_bytes();parsed=json.loads(old_config)
    if settings.get('xray_service','xray')!='xray' or settings.get('inbound_port',443)!=443:raise ValueError('Review nonstandard service before deployment')
    inbound=[i for i in parsed['inbounds'] if i.get('protocol')=='vless' and i.get('port')==443]
    if len(inbound)!=1 or not inbound[0].get('tag'):raise ValueError('Expected one tagged production inbound')
    emails={c.get('email') for c in inbound[0]['settings']['clients'] if c.get('email','').startswith('kenxu:')}
    if users and not set(users).issubset(emails):raise ValueError('Pilot identity not registered on this core')
    if command('systemctl','show','xray','-p','User','--value').strip()!='xray':raise ValueError('Review nonstandard service user')
    if not command('/usr/local/bin/xray','version').startswith('Xray 26.3.27 '):raise ValueError('Review core version migration before installing this pinned patch')
    current=command('systemctl','show','xray','-p','ExecStart','--value')
    if 'run -config '+str(config) not in current:raise ValueError('Unexpected core launch arguments')
    pid=command('systemctl','show','xray','-p','MainPID','--value').strip();listeners=command('ss','-Hlnpt','sport','=',':443').splitlines()
    if not listeners or any(('pid='+pid+',') not in row for row in listeners):raise ValueError('Proxy port is not exclusively owned by this core')
    for name in ['kenxu_meter.py','kenxu_activity.py','kenxu_flow.py']:
        if not (stage/name).is_file():raise ValueError('Collector source missing')
    command(str(binary),'run','-test','-config',str(config))
    # Portal readiness and credentials are checked before disconnecting anything.
    sys.path.insert(0,'/usr/local/lib/kenxu-meter')
    from kenxu_meter import Meter
    backup=pathlib.Path('/var/lib/kenxu-meter')/('before-flow-'+str(time.time_ns()));backup.mkdir(mode=0o700)
    collector=pathlib.Path('/usr/local/lib/kenxu-meter');drop=pathlib.Path('/etc/systemd/system/xray.service.d/70-kenxu-flow.conf');previous_drop=drop.read_bytes() if drop.exists() else None
    meter_drop=pathlib.Path('/etc/systemd/system/kenxu-meter.service.d/70-kenxu-flow.conf');previous_meter_drop=meter_drop.read_bytes() if meter_drop.exists() else None
    registration_copy=registration.read_bytes();(backup/'registration.json').write_bytes(registration_copy);(backup/'config.json').write_bytes(old_config)
    if previous_drop is not None:(backup/'flow-dropin.conf').write_bytes(previous_drop)
    if previous_meter_drop is not None:(backup/'meter-flow-dropin.conf').write_bytes(previous_meter_drop)
    for name in ['kenxu_meter.py','kenxu_activity.py','kenxu_flow.py']:
        if (collector/name).exists():shutil.copy2(collector/name,backup/name)
    # Recoverable byte-only queue snapshot: no backed-up source/target metadata.
    source=sqlite3.connect('/var/lib/kenxu-meter/queue.sqlite');saved=sqlite3.connect(str(backup/'queue.sqlite'));source.backup(saved);source.close()
    saved.execute('PRAGMA secure_delete=ON');saved.execute("UPDATE queue SET payload=json_set(payload,'$.activity',json('[]')) WHERE json_type(payload,'$.activity') IS NOT NULL")
    if saved.execute("SELECT 1 FROM sqlite_master WHERE name='flow_queue'").fetchone():saved.execute('DELETE FROM flow_queue')
    saved.commit();saved.execute('VACUUM');saved.close()
    changed=False;meter=None
    try:
        command('systemctl','stop','kenxu-meter')
        meter=Meter(str(registration));meter.sync();meter.sample();meter.flush();getattr(meter,'flow_cycle',lambda **kwargs:None)(strict=True)
        if config.read_bytes()!=old_config:raise RuntimeError('Desired configuration changed; review before core switch')
        if meter.db.execute('SELECT count(*) FROM queue').fetchone()[0]:raise RuntimeError('Native queue must drain before core switch')
        # Stop accepting new VLESS streams, then close only this verified local
        # proxy port while the old StatsService remains alive for a final drain.
        changed=True;api='127.0.0.1:'+str(settings.get('api_port',10085));command('/usr/local/bin/xray','api','rmi','-s',api,inbound[0]['tag'])
        command('ss','-K','state','established','(','sport','=',':443',')');time.sleep(1.3)
        meter.sample();meter.flush();getattr(meter,'flow_cycle',lambda **kwargs:None)(strict=True);meter.db.close();meter.lock.close()
        command('systemctl','stop','xray')
        # Executable directories are public code, not the private state tree.
        # os.umask(077) must not leave root-only parent traversal for xray.
        libexec=pathlib.Path('/usr/local/libexec')
        if not libexec.exists():libexec.mkdir(mode=0o755);os.chmod(libexec,0o755)
        destination=libexec/'kenxu';destination.mkdir(exist_ok=True);os.chmod(destination,0o755)
        if not os.stat(libexec).st_mode&0o005:raise RuntimeError('Xray needs executable traversal of libexec')
        installed=destination/('xray-flow-'+expected[:16]);shutil.copy2(binary,installed);os.chmod(installed,0o755)
        if digest(installed)!=expected:raise RuntimeError('Installed core hash mismatch')
        account=pwd.getpwnam('xray');runtime=pathlib.Path('/var/lib/kenxu-flow');runtime.mkdir(exist_ok=True,mode=0o700);os.chown(runtime,account.pw_uid,account.pw_gid);os.chmod(runtime,0o700)
        drop.parent.mkdir(parents=True,exist_ok=True)
        content='[Service]\nEnvironment="KENXU_FLOW_SOCKET=/var/lib/kenxu-flow/observer.sock"\nEnvironment="KENXU_FLOW_USERS='+','.join(users)+'"\nExecStart=\nExecStart='+str(installed)+' run -config '+str(config)+'\nReadWritePaths=/var/lib/kenxu-flow\n'
        atomic(drop,content.encode(),0o644)
        meter_drop.parent.mkdir(parents=True,exist_ok=True);atomic(meter_drop,b'[Service]\nReadWritePaths=/var/lib/kenxu-flow\n',0o644)
        for name in ['kenxu_meter.py','kenxu_activity.py','kenxu_flow.py']:
            shutil.copy2(stage/name,collector/name);os.chmod(collector/name,0o700 if name=='kenxu_meter.py' else 0o600)
        settings.update(flow_enabled=True,flow_socket='/var/lib/kenxu-flow/observer.sock');atomic(registration,json.dumps(settings).encode())
        command('systemctl','daemon-reload');command('systemctl','start','xray')
        for _ in range(40):
            if (runtime/'observer.sock').exists():
                try:command('/usr/local/bin/xray','api','statsquery','-s',api,'-pattern','kenxu:');break
                except Exception:pass
            time.sleep(.25)
        else:raise RuntimeError('Observer readiness failed')
        if config.read_bytes()!=old_config:raise RuntimeError('Production configuration changed unexpectedly')
        command('python3',str(collector/'kenxu_meter.py'),'--once',timeout=90)
        command('systemctl','start','kenxu-meter');command('systemctl','is-active','--quiet','xray');command('systemctl','is-active','--quiet','kenxu-meter')
        state={'ok':True,'candidateSha256':expected,'scope':'selected' if users else 'all-managed','selectedUsers':len(users),'managedClients':len(emails),'configurationPreserved':True,'rollbackDirectory':str(backup)}
        atomic(backup/'rollout.json',json.dumps(state).encode());return state
    except Exception:
        if meter is not None:
            try:meter.db.close();meter.lock.close()
            except Exception:pass
        if changed:command('systemctl','stop','xray')
        atomic(registration,registration_copy)
        if previous_drop is None:
            if drop.exists():drop.unlink()
        else:atomic(drop,previous_drop,0o644)
        if previous_meter_drop is None:
            if meter_drop.exists():meter_drop.unlink()
        else:atomic(meter_drop,previous_meter_drop,0o644)
        for name in ['kenxu_meter.py','kenxu_activity.py','kenxu_flow.py']:
            if (backup/name).exists():shutil.copy2(backup/name,collector/name)
        command('systemctl','daemon-reload')
        if changed:command('systemctl','start','xray')
        command('systemctl','start','kenxu-meter')
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--stage',required=True);parser.add_argument('--sha256',required=True);parser.add_argument('--users-file');args=parser.parse_args()
    try:
        users=json.loads(pathlib.Path(args.users_file).read_text()) if args.users_file else []
        print(json.dumps(deploy(pathlib.Path(args.stage),args.sha256,users)))
    except Exception as error:
        trace=[{'file':pathlib.Path(f.filename).name,'line':f.lineno,'call':f.name} for f in traceback.extract_tb(error.__traceback__)]
        pathlib.Path(args.stage,'maintenance-error.txt').write_text(traceback.format_exc())
        print(json.dumps({'ok':False,'errorClass':type(error).__name__,'reason':'Guarded deployment failed; inspect private maintenance state before retrying','trace':trace}));raise SystemExit(1)
