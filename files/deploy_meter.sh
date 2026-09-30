#!/bin/bash
# Run locally on an already configured Xray host, as root. Registration is secret.
set -euo pipefail
stage=${1:?private staging directory required}
systemctl stop kenxu-meter 2>/dev/null || true
install -d -m 700 /etc/kenxu-meter /var/lib/kenxu-meter /usr/local/lib/kenxu-meter
if [ ! -e /var/lib/kenxu-meter/pre-fleet-config.json ]; then
    install -m 600 /usr/local/etc/xray/config.json /var/lib/kenxu-meter/pre-fleet-config.json
fi
install -m 700 "$stage/kenxu_meter.py" /usr/local/lib/kenxu-meter/kenxu_meter.py
install -m 600 "$stage/registration.json" /etc/kenxu-meter/config.json
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
    echo 'Meter bootstrap failed; previous core config restored by collector' >&2
    exit 1
fi
