#!/usr/bin/env python3
"""Read-only planning preflight. Never writes config or starts/stops services."""
import json
import pathlib
import re
import subprocess
import sys


def describe(config, settings):
    clients = [c for inbound in config.get('inbounds', []) for c in inbound.get('settings', {}).get('clients', []) if c.get('email', '').startswith('kenxu:')]
    listen = config.get('api', {}).get('listen', '')
    return {
        'managedClients': len(clients),
        'apiLoopback': bool(re.fullmatch(r'127\.0\.0\.1:[0-9]+', listen)),
        'userByteCounters': all(config.get('policy', {}).get('levels', {}).get(str(c.get('level', 0)), {}).get('statsUserUplink') and config.get('policy', {}).get('levels', {}).get(str(c.get('level', 0)), {}).get('statsUserDownlink') for c in clients) and bool(clients),
        'visionClients': sum(c.get('flow') == 'xtls-rprx-vision' for c in clients),
        'ukRoutePresent': any(o.get('tag') == 'uk-gusecure2' for o in config.get('outbounds', [])),
        'activityEnabled': settings.get('activity_enabled') is True,
        'flowRolloutEnabled': settings.get('flow_enabled', False) is True,
        'mutationsPerformed': False,
    }


def main():
    try:
        settings = json.loads(pathlib.Path('/etc/kenxu-meter/config.json').read_text())
        config = json.loads(pathlib.Path(settings.get('xray_config', '/usr/local/etc/xray/config.json')).read_text())
        result = subprocess.run([settings.get('xray_binary', '/usr/local/bin/xray'), 'version'], capture_output=True, text=True, timeout=5)
        match = re.search(r'Xray\s+([0-9]+\.[0-9]+\.[0-9]+)', result.stdout)
        summary = describe(config, settings)
        summary.update(coreVersion=match.group(1) if match else 'unknown', flowCapability='not_probed')
        for service in ['xray', 'kenxu-meter']:
            active = subprocess.run(['systemctl', 'is-active', '--quiet', service], capture_output=True, timeout=5).returncode == 0
            summary[service + 'Active'] = active
        print(json.dumps(summary))
    except Exception as exc:
        print(json.dumps({'ok': False, 'errorClass': type(exc).__name__, 'mutationsPerformed': False}))
        sys.exit(1)


if __name__ == '__main__':
    main()
