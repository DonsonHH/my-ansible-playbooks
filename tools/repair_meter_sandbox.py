#!/usr/bin/env python3
"""Repair only the missing activity-log write exception; never restart Xray.

Run as root after reviewing the host. Existing meter reports/observer queues and
all identity material stay in place. The collector is gracefully restarted and
runtime clients must match the portal desired state before success is reported.
"""
import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote
from meter_service import activity_log_directory


def command(*args, timeout=45):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Maintenance command failed: ' + pathlib.Path(args[0]).name)
    return result.stdout.strip()


def atomic(path, content, mode=0o644):
    temporary = path.with_name(path.name + '.next')
    with temporary.open('wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def queue_status(settings):
    file = pathlib.Path(settings.get('state_dir', '/var/lib/kenxu-meter')) / 'queue.sqlite'
    database = sqlite3.connect('file:' + quote(str(file)) + '?mode=ro', uri=True)
    try:
        return {table: database.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in ['queue', 'flow_queue']}
    finally:
        database.close()


def repair():
    if os.geteuid() != 0:
        raise ValueError('Run as root')
    os.umask(0o077)
    settings_file = pathlib.Path('/etc/kenxu-meter/config.json')
    settings_bytes = settings_file.read_bytes()
    settings = json.loads(settings_bytes)
    if settings.get('xray_service', 'xray') != 'xray':
        raise ValueError('Review nonstandard service explicitly')
    directory = activity_log_directory(settings)
    if directory is None:
        raise ValueError('This repair is only for enabled activity logging')
    log = pathlib.Path(directory)
    if log.is_symlink() or not log.is_dir() or log.resolve() != log:
        raise ValueError('Expected the existing private activity log directory')
    if command('systemctl', 'show', 'kenxu-meter', '-p', 'ProtectSystem', '--value') != 'strict':
        raise ValueError('Review nonstandard sandbox explicitly')
    pid = command('systemctl', 'show', 'xray', '-p', 'MainPID', '--value')
    if int(pid) <= 0:
        raise RuntimeError('Core is not running')
    allowed = command('systemctl', 'show', 'kenxu-meter', '-p', 'ReadWritePaths', '--value').split()
    if directory in allowed:
        return {'alreadyConfigured': True, 'corePidUnchanged': True, 'protectSystem': 'strict'}
    if any(queue_status(settings).values()):
        raise RuntimeError('Drain existing delivery queues before maintenance')
    drop = pathlib.Path('/etc/systemd/system/kenxu-meter.service.d/80-kenxu-activity-log.conf')
    previous = drop.read_bytes() if drop.exists() else None
    saved = pathlib.Path('/var/lib/kenxu-meter') / ('before-sandbox-repair-' + str(time.time_ns()))
    saved.mkdir(mode=0o700)
    (saved / 'config.json').write_bytes(pathlib.Path(settings.get('xray_config', '/usr/local/etc/xray/config.json')).read_bytes())
    if previous is not None:
        (saved / 'previous-dropin.conf').write_bytes(previous)
    changed = False
    try:
        drop.parent.mkdir(parents=True, exist_ok=True)
        atomic(drop, ('[Service]\nReadWritePaths=' + directory + '\n').encode())
        changed = True
        command('systemctl', 'daemon-reload')
        command('systemctl', 'stop', 'kenxu-meter', timeout=90)
        if any(queue_status(settings).values()):
            raise RuntimeError('Graceful stop did not drain existing delivery queues')
        command('systemctl', 'start', 'kenxu-meter')
        sys.path.insert(0, '/usr/local/lib/kenxu-meter')
        from kenxu_meter import Meter, client_identity
        reader = Meter.__new__(Meter)
        reader.settings = settings
        matched = False
        for _ in range(45):
            time.sleep(1)
            if command('systemctl', 'show', 'xray', '-p', 'MainPID', '--value') != pid:
                raise RuntimeError('Core unexpectedly restarted; stop rollout')
            desired = reader.request('/api/meter/desired')
            cached = json.loads((pathlib.Path(settings.get('state_dir', '/var/lib/kenxu-meter')) / 'desired.json').read_text())
            target = json.loads(pathlib.Path(settings.get('xray_config', '/usr/local/etc/xray/config.json')).read_text())
            inbound = next(row for row in target['inbounds'] if row.get('protocol') == 'vless' and row.get('port') == settings.get('inbound_port', 443))
            runtime = reader.runtime_users(inbound['tag'])
            expected = {row['email']: client_identity(row) for row in desired['clients']}
            if cached.get('revision') == desired['revision'] and runtime == expected:
                matched = True
                break
        if not matched:
            raise RuntimeError('Collector did not apply desired users; stop rollout')
        if settings_file.read_bytes() != settings_bytes:
            raise RuntimeError('Private meter registration unexpectedly changed')
        allowed = command('systemctl', 'show', 'kenxu-meter', '-p', 'ReadWritePaths', '--value').split()
        if directory not in allowed or command('systemctl', 'show', 'kenxu-meter', '-p', 'ProtectSystem', '--value') != 'strict':
            raise RuntimeError('Least-privilege sandbox readback failed')
        command('systemctl', 'is-active', '--quiet', 'xray', 'kenxu-meter')
        deadline = time.monotonic() + 30
        while any(queue_status(settings).values()):
            if time.monotonic() >= deadline:
                raise RuntimeError('Collector delivery backlog did not drain after restart')
            time.sleep(.5)
        if command('systemctl', 'show', 'xray', '-p', 'MainPID', '--value') != pid:
            raise RuntimeError('Core unexpectedly restarted during delivery drain; stop rollout')
        command('systemctl', 'is-active', '--quiet', 'xray', 'kenxu-meter')
        result = {'repaired': True, 'corePidUnchanged': True, 'runtimeMatchesDesired': True, 'desiredClientCount': len(expected), 'privateRegistrationUnchanged': True, 'protectSystem': 'strict', 'onlyAddedWritePath': directory, 'queues': queue_status(settings)}
        atomic(saved / 'result.json', json.dumps(result).encode(), 0o600)
        return result
    except Exception:
        if changed:
            # daemon-reload changes the next process, not the mount namespace
            # of a collector already started above. Gracefully stop it before
            # restoring the fragment, then start under the original sandbox.
            command('systemctl', 'stop', 'kenxu-meter', timeout=90)
            if previous is None:
                drop.unlink(missing_ok=True)
            else:
                atomic(drop, previous)
            command('systemctl', 'daemon-reload')
            command('systemctl', 'start', 'kenxu-meter')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    try:
        print(json.dumps(repair()))
    except Exception as error:
        print(json.dumps({'ok': False, 'errorClass': type(error).__name__, 'reason': 'Guarded collector sandbox repair failed; inspect private host state before proceeding'}))
        raise SystemExit(1)
