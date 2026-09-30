#!/usr/bin/env python3
"""Private Xray user metering. Never log UUIDs, tokens or raw Xray configuration."""
import argparse
import copy
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def atomic_json(path, value, owner=None, mode=0o600):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if owner:
            os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_config(original, clients, inbound_port=443, api_port=10085, routes=None):
    config = copy.deepcopy(original)
    matches = [i for i in config.get('inbounds', []) if i.get('protocol') == 'vless' and i.get('port') == inbound_port]
    if len(matches) != 1:
        raise ValueError('Expected exactly one target VLESS inbound')
    inbound = matches[0]
    inbound.setdefault('tag', 'kenxu-in-' + str(inbound_port))
    existing = inbound.setdefault('settings', {}).setdefault('clients', [])
    base_clients = [c for c in existing if not c.get('email', '').startswith('kenxu:')]
    for client in clients:
        if not client.get('email', '').startswith('kenxu:') or client.get('level') != 88:
            raise ValueError('Invalid managed client')
        if any(client.get('id') == c.get('id') for c in base_clients):
            raise ValueError('Managed UUID conflicts with legacy client')
    inbound['settings']['clients'] = base_clients + clients
    api = config.setdefault('api', {'tag': 'kenxu-api'})
    if api.get('listen') not in [None, '127.0.0.1:' + str(api_port)]:
        raise ValueError('Existing API needs explicit migration')
    api['listen'] = '127.0.0.1:' + str(api_port)
    api['services'] = sorted(set(api.get('services', []) + ['StatsService', 'HandlerService', 'RoutingService']))
    config.setdefault('stats', {})
    level = config.setdefault('policy', {}).setdefault('levels', {}).setdefault('88', {})
    level.update(statsUserUplink=True, statsUserDownlink=True)
    # New users must not bypass an old user-specific LAN block rule.
    rules = config.setdefault('routing', {}).setdefault('rules', [])
    rules[:] = [r for r in rules if not r.get('_kenxu_managed')]
    rules[:] = [r for r in rules if not r.get('ruleTag', '').startswith('kenxu-route-')]
    marker_emails = [c['email'] for c in clients]
    rules[:] = [r for r in rules if not (r.get('ip') == ['geoip:private'] and isinstance(r.get('user'), list) and all(e.startswith('kenxu:') for e in r['user']))]
    managed_rules = []
    for route in routes or []:
        outbound = route.get('outboundTag')
        emails = [c['email'] for c in route['clients']]
        if outbound:
            if not any(o.get('tag') == outbound for o in config.get('outbounds', [])):
                raise ValueError('Managed route outbound missing')
            if emails:
                managed_rules.append({'ruleTag': 'kenxu-route-' + route['nodeId'], 'type': 'field', 'user': emails, 'outboundTag': outbound})
    rules[0:0] = managed_rules
    if marker_emails:
        if not any(o.get('tag') == 'block' for o in config.get('outbounds', [])):
            raise ValueError('A block outbound is required for managed users')
        rules.insert(0, {'type': 'field', 'user': marker_emails, 'ip': ['geoip:private'], 'outboundTag': 'block'})
    return config, inbound['tag']


def parse_counters(response):
    values = {}
    for row in response.get('stat', []):
        parts = row.get('name', '').split('>>>')
        if len(parts) != 4 or parts[0] != 'user' or not parts[1].startswith('kenxu:') or parts[2] != 'traffic' or parts[3] not in ['uplink', 'downlink']:
            continue
        value = int(row.get('value', 0))
        if value < 0:
            raise ValueError('Negative counter')
        values.setdefault(parts[1], {'email': parts[1], 'up': 0, 'down': 0})['up' if parts[3] == 'uplink' else 'down'] = value
    return sorted(values.values(), key=lambda c: c['email'])


def validate_added(output, expected):
    # Xray adu prints build/API errors but may still return exit code zero.
    counts = re.findall(r'Added (\d+) user\(s\) in total\.', output)
    if counts != [str(expected)]:
        raise RuntimeError('Xray did not acknowledge all managed users')


def client_identity(client):
    return {'id': client['id'], 'level': client.get('level', 0), 'flow': client.get('flow', '')}


class Meter:
    def __init__(self, config_path):
        self.settings = json.loads(pathlib.Path(config_path).read_text())
        self.root = pathlib.Path(self.settings.get('state_dir', '/var/lib/kenxu-meter'))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.lock = (self.root / 'process.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Collector already running; stop its service before maintenance') from None
        self.db = sqlite3.connect(self.root / 'queue.sqlite')
        os.chmod(self.root / 'queue.sqlite', 0o600)
        self.db.executescript('CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS sequence(epoch TEXT PRIMARY KEY,value INTEGER NOT NULL);')
        self.desired = None
        self.last_sync = 0
        self.stop = False
        self.epoch = self.process_epoch()

    def run_command(self, *args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Command failed: ' + pathlib.Path(args[0]).name)
        return result.stdout

    def process_epoch(self):
        pid = int(subprocess.check_output(['systemctl', 'show', self.settings.get('xray_service', 'xray'), '-p', 'MainPID', '--value'], text=True).strip())
        if pid <= 0:
            raise RuntimeError('Xray is not running')
        start = pathlib.Path('/proc/' + str(pid) + '/stat').read_text().split(') ', 1)[1].split()[19]
        boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return hashlib.sha256((boot + ':' + str(pid) + ':' + start).encode()).hexdigest()[:40]

    def request(self, endpoint, body=None):
        origin = self.settings['portal_origin']
        parsed = urllib.parse.urlsplit(origin)
        local_http = parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
        if (parsed.scheme != 'https' and not local_http) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('HTTPS or explicit IPv4 loopback portal origin required')
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(origin + endpoint, data=data, headers={'Authorization': 'Bearer ' + self.settings['token'], 'Content-Type': 'application/json', 'User-Agent': 'Kenxu-Meter/1'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=10) as response:
            raw = response.read(524289)
            if len(raw) > 524288:
                raise ValueError('Oversized response')
            return json.loads(raw)

    def flush(self):
        for row_id, payload in self.db.execute('SELECT id,payload FROM queue ORDER BY id LIMIT 100').fetchall():
            result = self.request('/api/meter/report', json.loads(payload))
            if not result.get('ok'):
                raise RuntimeError('Report rejected')
            with self.db:
                self.db.execute('DELETE FROM queue WHERE id=?', (row_id,))

    def sample(self):
        if not self.desired:
            return
        epoch = self.process_epoch()
        raw = self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'statsquery', '-s', '127.0.0.1:' + str(self.settings.get('api_port', 10085)), '-pattern', 'kenxu:')
        if self.process_epoch() != epoch:
            raise RuntimeError('Core restarted during sample; retry next cycle')
        self.epoch = epoch
        counters = parse_counters(json.loads(raw))
        with self.db:
            last = self.db.execute('SELECT value FROM sequence WHERE epoch=?', (self.epoch,)).fetchone()
            seq = last[0] + 1 if last else 1
            payload = {'epoch': self.epoch, 'seq': seq, 'at': int(time.time() * 1000), 'revision': self.desired['revision'], 'counters': counters}
            self.db.execute('INSERT INTO sequence VALUES(?,?) ON CONFLICT(epoch) DO UPDATE SET value=excluded.value', (self.epoch, seq))
            self.db.execute('INSERT INTO queue(payload) VALUES(?)', (json.dumps(payload),))

    def sync(self, bootstrap=False):
        with (self.root / 'sync.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self.sync_locked(bootstrap)

    def sync_locked(self, bootstrap=False):
        desired = self.request('/api/meter/desired')
        if desired.get('nodeId') != self.settings['node_id'] or not isinstance(desired.get('clients'), list) or len(desired['clients']) > 500:
            raise ValueError('Invalid desired state')
        target = pathlib.Path(self.settings.get('xray_config', '/usr/local/etc/xray/config.json'))
        old = json.loads(target.read_text())
        updated, tag = prepare_config(old, desired['clients'], self.settings.get('inbound_port', 443), self.settings.get('api_port', 10085), desired.get('routes'))
        old_inbound = next(i for i in old['inbounds'] if i.get('protocol') == 'vless' and i.get('port') == self.settings.get('inbound_port', 443))
        old_managed = {c['email']: c for c in old_inbound['settings']['clients'] if c.get('email', '').startswith('kenxu:')}
        new_managed = {c['email']: c for c in desired['clients']}
        infrastructure_changed = old.get('api') != updated.get('api') or old.get('stats') != updated.get('stats') or old.get('policy') != updated.get('policy') or old_inbound.get('tag') != tag
        if infrastructure_changed and not bootstrap:
            raise RuntimeError('Bootstrap required before automated sync')
        identities = {email: client_identity(c) for email, c in new_managed.items()}
        runtime = self.runtime_users(tag) if not infrastructure_changed else {}
        mismatch = runtime != identities
        if updated != old or mismatch:
            owner_stat = target.stat()
            candidate = self.root / 'candidate.json'
            atomic_json(candidate, updated)
            self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'run', '-test', '-config', str(candidate))
            if bootstrap:
                backup = self.root / 'before-bootstrap.json'
                if not backup.exists():
                    atomic_json(backup, old)
            else:
                # Capture the last available counters before removing a managed user.
                self.sample()
                self.flush()
            atomic_json(target, updated, (owner_stat.st_uid, owner_stat.st_gid), stat.S_IMODE(owner_stat.st_mode))
            try:
                if bootstrap:
                    self.restart_core()
                else:
                    server = '127.0.0.1:' + str(self.settings.get('api_port', 10085))
                    for email, identity in runtime.items():
                        if identities.get(email) != identity:
                            self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'rmu', '-s', server, '-tag', tag, email)
                    added = [c for email, c in new_managed.items() if runtime.get(email) != identities[email]]
                    if added:
                        addition = self.root / 'add-users.json'
                        inbound_addition = copy.deepcopy(next(i for i in updated['inbounds'] if i.get('tag') == tag))
                        inbound_addition['settings']['clients'] = added
                        atomic_json(addition, {'inbounds': [inbound_addition]})
                        output = self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'adu', '-s', server, str(addition))
                        validate_added(output, len(added))
                    # User-specific private-network block list changed: persist it
                    # and reload rules using the API without restarting the core.
                    routing = self.root / 'routing.json'
                    atomic_json(routing, {'routing': updated['routing']})
                    self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'adrules', '-s', server, str(routing))
                if self.runtime_users(tag) != identities:
                    raise RuntimeError('Managed user runtime readback mismatch')
            except Exception:
                atomic_json(target, old, (owner_stat.st_uid, owner_stat.st_gid), stat.S_IMODE(owner_stat.st_mode))
                self.restart_core()
                raise RuntimeError('Sync failed; previous config restored')
        self.desired = desired
        atomic_json(self.root / 'desired.json', desired)
        self.last_sync = time.monotonic()

    def runtime_users(self, tag):
        output = self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'inbounduser', '-s', '127.0.0.1:' + str(self.settings.get('api_port', 10085)), '-tag', tag)
        return {u['email']: client_identity({'id': u['account']['id'], 'level': u.get('level', 0), 'flow': u['account'].get('flow', '')}) for u in json.loads(output).get('users', []) if u.get('email', '').startswith('kenxu:')}

    def restart_core(self):
        self.run_command('systemctl', 'restart', self.settings.get('xray_service', 'xray'))
        for attempt in range(10):
            try:
                self.process_epoch()
                self.run_command(self.settings.get('xray_binary', '/usr/local/bin/xray'), 'api', 'statsquery', '-s', '127.0.0.1:' + str(self.settings.get('api_port', 10085)), '-pattern', 'kenxu:')
                return
            except (RuntimeError, OSError, ValueError):
                time.sleep(0.5)
        raise RuntimeError('Core failed its readiness check')

    def run(self):
        cached = self.root / 'desired.json'
        if cached.exists():
            self.desired = json.loads(cached.read_text())
        while not self.stop:
            started = time.monotonic()
            try:
                if time.monotonic() - self.last_sync > 30:
                    self.sync()
            except Exception as exc:
                print('Desired sync deferred: ' + type(exc).__name__, flush=True)
            try:
                # Keep sampling into the durable queue during a portal outage.
                self.sample()
                self.flush()
            except Exception as exc:
                # Exception class only: urllib errors and raw commands may carry secrets.
                print('Meter cycle deferred: ' + type(exc).__name__, flush=True)
            remaining = max(1, 15 - (time.monotonic() - started))
            for _ in range(int(remaining)):
                if self.stop:
                    break
                time.sleep(1)
        try:
            self.sample()
            self.flush()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/kenxu-meter/config.json')
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    meter = Meter(args.config)
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: setattr(meter, 'stop', True))
    if args.bootstrap:
        meter.sync(bootstrap=True)
        meter.sample()
        meter.flush()
    elif args.once:
        meter.sync()
        meter.sample()
        meter.flush()
    else:
        meter.run()


if __name__ == '__main__':
    main()
