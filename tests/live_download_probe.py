"""Run only on the explicitly selected pilot host, with private test configs."""
import json
import pathlib
import subprocess
import sys
import time

directory = pathlib.Path(sys.argv[1])
clients = []
try:
    for i in range(2):
        path = directory / ('client-' + str(i) + '.json')
        check = subprocess.run(['/usr/local/bin/xray', 'run', '-test', '-config', str(path)], capture_output=True)
        if check.returncode:
            raise RuntimeError('Private test client config rejected')
        clients.append(subprocess.Popen(['/usr/local/bin/xray', 'run', '-config', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(1)
    index = int(sys.argv[2])
    denied = '--expect-denied' in sys.argv
    size = 2097152 if index == 0 else 4194304
    probe = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '5' if denied else '30', '--socks5-hostname', '127.0.0.1:' + str(18701 + index), '--output', '/dev/null', '--write-out', '%{http_code} %{size_download}', 'https://speed.cloudflare.com/__down?bytes=' + str(size)], capture_output=True, text=True, timeout=35)
    parts = probe.stdout.strip().split()
    if denied:
        if probe.returncode == 0 or parts != ['000', '0']:
            raise RuntimeError('Disabled pilot user still connected')
        print(json.dumps({'user': 'A' if index == 0 else 'B', 'connectionDenied': True}))
    elif probe.returncode or len(parts) != 2 or parts[0] != '200' or int(parts[1]) != size:
        raise RuntimeError('Controlled download failed; no connection data printed')
    else:
        print(json.dumps({'user': 'A' if index == 0 else 'B', 'downloaded_bytes': size, 'http': 200}))
finally:
    for client in clients:
        client.terminate()
    for client in clients:
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
