# Opt-in per-user Xray metering

This playbook adds a local collector and managed clients to an existing native Xray deployment. It preserves legacy clients, REALITY settings, WARP and existing outbound routing. The collector's API listens on loopback only. Infrastructure network totals and per-user proxy counters remain separate metrics.

## Prerequisites

- Kenxu's metering backend is deployed and a node has been registered against a VLESS source entry.
- The server has Python 3, systemd and an Xray version with StatsService, HandlerService and RoutingService command support.
- The target configuration has exactly one VLESS inbound on the selected port and a `block` outbound. A different topology needs an explicit adapter.
- Meter registration uses a dedicated random token. Do not use a login password, monitoring token or Xray UUID as the collector token.
- Use exactly one collector per Xray process. Multiple logical routes belong to its physical agent registration. Each route has its own per-user UUID and accounting identity. For SG2, attach the UK source route to the SG2 agent with outbound tag `uk-gusecure2`; do not deploy a second collector.

## Deployment

Put these values in Ansible Vault or a private host_vars file excluded from Git:

On the Kenxu host, register an ordinary source node using `scripts/register-meter.mjs EXACT_SOURCE_NODE_NAME PRIVATE_OUTPUT_FILE` with `DATA_DIR` and `PUBLIC_ORIGIN` set. The registration file is private and must not be committed. The fleet registration adapter enrolls currently authorized users; subsequent account creation/access updates automatically enroll granted managed routes. Users must refresh subscriptions when switching from legacy shared credentials.

```yaml
kenxu_meter_enabled: true
kenxu_meter_node_id: REPLACE_WITH_REGISTERED_NODE_ID
kenxu_meter_token: REPLACE_WITH_PRIVATE_METER_TOKEN
kenxu_meter_origin: https://YOUR_PRIVATE_PORTAL_DOMAIN
kenxu_meter_inbound_port: 443
kenxu_meter_api_port: 10085
kenxu_meter_xray_service: xray
kenxu_meter_xray_config: /usr/local/etc/xray/config.json
kenxu_meter_activity_enabled: true
```

For a normal update on an already bootstrapped host (including activity logging infrastructure), limit execution to the selected host:

```sh
ansible-playbook kenxu_meter.yml -l YOUR_PILOT_HOST --ask-vault-pass
```

The first bootstrap adds local Xray APIs and enables managed-user counters. It validates the candidate configuration before replacement, takes a root-only backup and restarts Xray once. Request it explicitly:

```sh
ansible-playbook kenxu_meter.yml -l YOUR_PILOT_HOST -e kenxu_meter_bootstrap=true --ask-vault-pass
```

Normal synchronization persists client changes and applies add/remove users and routing through Xray APIs. Add-user requests contain a complete inbound configuration: minimal settings can fail construction while the Xray CLI still exits zero. The collector validates the acknowledged count and reads live users back before accepting synchronization. It also repairs a persisted-versus-runtime mismatch. A failed apply restores the prior config and restarts Xray. Do not expose API port 10085 in a cloud security group or firewall.

Only allowlisted users receive managed UUIDs. Granted managed routes awaiting enrollment are omitted from subscriptions; they do not fall back to legacy shared credentials. The separately authorized direct/unmetered delivery mode is an explicit exception, not a default. Enrolled users must refresh subscriptions. Disabled, password-change-pending or unassigned users are removed on the next successful desired-state sync, normally within 30 seconds. Deleting a user from Xray prevents new connections; existing long-lived sessions may persist until closed. Legacy personal credentials are preserved and cannot be attributed retroactively.

## Configuration ownership

The existing `software.yml` now reconciles managed clients after rendering `templates/xray.j2` when `kenxu_meter_enabled` is true. Install/register the collector before enabling that variable. Otherwise normal upstream behavior is unchanged.

`software.yml` now refuses implicit replacement when custom outbounds or additional personal clients exist. Use `kenxu_meter.yml` for these hosts. The guard runs before stopping collection or writing the template, protecting UK routing from accidental removal. Plan an explicit migration separately if the underlying topology must change. Private registration and bootstrap use `no_log`.

The collector owns `kenxu:` clients, their private-network block rule and `kenxu-route-*` user routing rules. Do not edit these while it runs. Legacy clients/rules remain outside its ownership. A nonblocking process lock rejects concurrent manual and daemon collection. Stop the service before `--once` or `--bootstrap` maintenance.

The meter playbook restarts collection after updating code or registration; an already-started daemon must not keep using an old token/module indefinitely. Base-template configuration is readable only by root and the actual Xray service group (`0640`), not every local user.

## Third-party relay

A separate loopback VLESS/WebSocket core forwards each route through its existing third-party VLESS/REALITY outbound. All managed clients use newly generated portal UUIDs; source provider UUIDs stay private. Per-user routing selects the appropriate provider, and the default outbound is blackhole to prevent an unmapped client from obtaining direct Internet access.

The portal bridges only a fixed WebSocket path to a fixed loopback port. It strips cookies and HTTP authorization and exposes neither Xray API nor private administration. Public WSS reaches the portal through the existing HTTP Tunnel; no additional public Jetson TCP port is opened. Client flow/REALITY fields are removed from relay subscriptions; the upstream still uses its original REALITY transport. Xray v26.3.27 was tested; WebSocket transport is deprecated upstream, so future Xray upgrades require a compatibility review.

Use `xray_service`, `xray_config`, `inbound_port` and `api_port` settings for the separate relay process. Its systemd unit must allow the configured directory in `ReadWritePaths`. A local portal origin may be `http://127.0.0.1:PORT` only; remote origins require HTTPS. Redirects and environment proxies are disabled, protecting the collector bearer token. No third-party provider key belongs in this repository.

The relay core configuration is private and must be refreshed when provider endpoints or credentials change. Do not overwrite it with a generated empty-client base while the service is live: stop collection, stage the new base, bootstrap/validate managed routes, and restart collection with a recoverable backup. Changing portal source YAML alone does not update a relay upstream.

The UK route through ocproxy/OpenConnect is TCP-only. Its generated subscription sets `udp: false`; do not advertise UDP support merely because its VLESS entry accepts UDP packets. Ordinary Azure routes support UDP, while third-party UDP support depends on the purchased provider. Test the full route, not just the first VLESS hop.

## Accounting

- Reads cumulative Xray user uplink/downlink with `reset=false` every 15 seconds.
- Uses boot ID, process ID and process start time as a counter epoch.
- Checks the process epoch both before and after querying counters; a restart during a sample causes a retry rather than mixing epochs.
- Persists sequence numbers and an SQLite queue under `/var/lib/kenxu-meter` before posting. Reports are resent until acknowledged.
- Kenxu deduplicates samples and persists deltas. Reports for old sequences or altered duplicate bodies are rejected.
- Delayed reports retain their sample timestamp. Old queued samples do not create a false current heartbeat; there is no arbitrary 30-day queue-expiry cutoff.
- A reset starts counting from the current value without subtracting old usage. Process restarts retain portal history.
- HTTP/TLS/proxy overhead can make the counter slightly larger than a file's payload size. It is not a provider billing statement.
- An abrupt process crash can lose bytes since the last sample; counters live in Xray memory. Ordinary polling does not guarantee zero-loss accounting.
- The first-version queue assumes a stable registration and portal schema. If the portal rejects a queued report, repair the mismatch instead of deleting the queue or resetting counters blindly.

## Private activity metadata (Kenxu 1.5.0+)

Upgrade the portal first. The dedicated meter playbook enables metadata by default; ordinary `common.yml` deployments still do not opt a server into metering. Existing meter registrations are reused: do not rotate tokens, UUIDs, logical routes or queue state for this upgrade.

Run the playbook with `kenxu_meter_bootstrap=true`, one physical server at a time. On an existing installation it first stops the old collector and runs its old installed version once to drain final counters **before replacing code or restarting Xray**; a failed drain resumes the old service and aborts. This adds `statsUserOnline` to managed level 88 and a private access log; enabling or changing log infrastructure requires bootstrap, not just hot user synchronization. A hot update refuses missing infrastructure before changing files. One core restart briefly interrupts that server's connections. On later normal updates leave bootstrap false.

The Xray service account must own `/var/log/kenxu` (default `xray:xray`, mode 0700); the root collector reads it. Install logrotate and ensure its daily timer/cron runs. The supplied stanza compresses and rotates by age (30 days) and checked size (10M); size is checked when logrotate runs, not a hard instantaneous disk cap. If the Xray service uses a different account, adapt directory ownership and `su` together before bootstrap. Raw logging is local and private, not journal output.

The parser accepts only authenticated `kenxu:` access records. It reads at most 1 MiB per sample, validates source IP and destination host/IP+port, limits targets, tracks a durable inode/offset cursor in the same transaction as queued reports, and bounds metadata to 128 KiB per report. Native online-IP queries supplement sources for ongoing connections. No URL path, payload, cookies, TLS decryption or external GeoIP calls are involved. Copy-truncate rotation and long-lived connections can omit target observations; these are best-effort metadata, not new accounting counters.

The portal joins observed new targets and sources to existing **minute/logical-route byte totals**. Stock Xray does not supply per-target bytes: a row with multiple targets is not attribution of that row's entire usage to each target. Delayed samples retain accounting; metadata older than 30 days is redacted even from retry queues, while byte reports continue retrying. Portal 1.5.0 hashes the accounting envelope independently of metadata so a redacted retry remains idempotent.

Only private administrators query these records. User profile data exposes only that user's latest observed source IP. Tell invited users about the logging scope. Unmanaged, direct/unmetered and bypass connections cannot be observed. An offline MMDB on the portal optionally supplies approximate country/region; a Country database does not establish a city.

## Operational checks

For the next per-connection byte-metering phase, see the [design and acceptance gates](kenxu-flow-design.md). The optional `kenxu_flow_preflight.yml` only inspects sanitized state; `tools/flow_contract.py` is an offline replay oracle, not a production collector or a core patch. Existing services and byte accounting remain unchanged.

```sh
systemctl is-active xray kenxu-meter
ss -lnt '( sport = :10085 )'
```

Run local tests before changes:

```sh
python3 -m unittest discover -s tests -v
ansible-playbook kenxu_meter.yml --syntax-check -i localhost,
```

Tests use fictional credentials. No actual host inventories, IP addresses, UUIDs, private keys, account credentials, service origins or registration tokens belong in this repository.

## Rollback

Stop `kenxu-meter`, validate `/var/lib/kenxu-meter/before-bootstrap.json` with `xray run -test`, restore it with the correct original owner/permissions, and restart Xray. Keep the portal usage database and collector queue for inspection. Restore a config backup before removing collectors if the node should stop accepting managed user credentials.

For ordinary maintenance, keep the collector enabled and manage the account through Kenxu rather than resetting server counters.
