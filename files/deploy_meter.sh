#!/bin/bash
# Run locally on an already configured Xray host, as root. Registration is secret.
set -Eeuo pipefail
preflight(){
    if [ "$(id -u)" -ne 0 ]; then
        echo 'Run this maintenance preflight as root so protected service settings can be checked.' >&2
        return 1
    fi
    if [ -e /etc/systemd/system/xray.service.d/70-kenxu-flow.conf ]; then
        echo 'Pinned per-flow core detected; use kenxu_meter.yml for collector maintenance or kenxu_flow_shadow.yml for a reviewed core upgrade.' >&2
        return 1
    fi
}
# Read-only check; never stops a collector or writes registration/configuration.
if [ "${1:-}" = --preflight-only ]; then
    preflight
    exit 0
fi
stage=${1:?private staging directory required}
preflight
for required in kenxu_meter.py kenxu_activity.py kenxu_flow.py registration.json kenxu-meter.service; do
    [ -r "$stage/$required" ] || { echo 'Incomplete private staging; existing collection was not stopped.' >&2; exit 1; }
done
resume_after_error(){ result=$?;systemctl start kenxu-meter 2>/dev/null || true;exit "$result"; }
trap resume_after_error ERR
systemctl stop kenxu-meter 2>/dev/null || true
if [ -f /usr/local/lib/kenxu-meter/kenxu_meter.py ]; then
    # A best-effort shutdown sample is insufficient before bootstrap may restart
    # the core. Refuse the upgrade until the old cumulative queue is delivered.
    /usr/bin/python3 /usr/local/lib/kenxu-meter/kenxu_meter.py --once
fi
install -d -m 700 /etc/kenxu-meter /var/lib/kenxu-meter /usr/local/lib/kenxu-meter
if [ ! -e /var/lib/kenxu-meter/pre-fleet-config.json ]; then
    install -m 600 /usr/local/etc/xray/config.json /var/lib/kenxu-meter/pre-fleet-config.json
fi
install -m 700 "$stage/kenxu_meter.py" /usr/local/lib/kenxu-meter/kenxu_meter.py
install -m 600 "$stage/kenxu_activity.py" /usr/local/lib/kenxu-meter/kenxu_activity.py
install -m 600 "$stage/kenxu_flow.py" /usr/local/lib/kenxu-meter/kenxu_flow.py
python3 - "$stage/registration.json" <<'PY'
import json,pathlib,os,sys
target=pathlib.Path('/etc/kenxu-meter/config.json')
value=json.loads(target.read_text()) if target.exists() else {}
value.update(json.loads(pathlib.Path(sys.argv[1]).read_text()))
temporary=target.with_name('config.json.next')
with temporary.open('w') as stream:
    json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
os.chmod(temporary,0o600);os.replace(temporary,target)
PY
install -m 644 "$stage/kenxu-meter.service" /etc/systemd/system/kenxu-meter.service
systemctl daemon-reload
if /usr/bin/python3 /usr/local/lib/kenxu-meter/kenxu_meter.py --bootstrap; then
    chmod 640 /usr/local/etc/xray/config.json
    chown root:"$(id -gn xray)" /usr/local/etc/xray/config.json
    systemctl enable --now kenxu-meter
    systemctl is-active --quiet xray
    systemctl is-active --quiet kenxu-meter
    echo 'Meter installed; core and collector active'
else
    systemctl start kenxu-meter 2>/dev/null || true
    echo 'Meter bootstrap failed; previous core config restored by collector' >&2
    exit 1
fi
