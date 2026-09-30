#!/usr/bin/env python3
"""Actual authenticated downloads through every private acceptance client."""
import json
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1])
results = []
for entry in json.loads((root / 'clients.json').read_text()):
    config = root / entry['file']
    subprocess.run(['/usr/local/bin/xray', 'run', '-test', '-config', str(config)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    core = subprocess.Popen(['/usr/local/bin/xray', 'run', '-config', str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.8)
        size = 65536 * (entry['user'] + 1)
        request = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '35', '--socks5-hostname', '127.0.0.1:18701', '-o', '/dev/null', '-w', '%{http_code} %{size_download}', 'https://speed.cloudflare.com/__down?bytes=' + str(size)], capture_output=True, text=True, timeout=40)
        parts = request.stdout.split()
        ok = request.returncode == 0 and parts == ['200', str(size)]
        result = {'route': entry['name'], 'testUser': entry['user'], 'expected': size, 'ok': ok}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        core.terminate()
        try:
            core.wait(timeout=5)
        except subprocess.TimeoutExpired:
            core.kill()
            core.wait()
(root / 'result.json').write_text(json.dumps(results))
sys.exit(0 if all(r['ok'] for r in results) else 1)
