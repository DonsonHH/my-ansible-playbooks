# Opt-in per-user Xray metering

This playbook adds a local collector and managed clients to an existing native Xray deployment. It preserves legacy clients, REALITY settings, WARP and existing outbound routing. The collector's API listens on loopback only. Infrastructure network totals and per-user proxy counters remain separate metrics.

## Prerequisites

- Kenxu's metering backend is deployed and a node has been registered against a VLESS source entry.
- The server has Python 3, systemd and an Xray version with StatsService, HandlerService and RoutingService command support.
- The target configuration has exactly one VLESS inbound on the selected port and a `block` outbound. A different topology needs an explicit adapter.
- Meter registration uses a dedicated random token. Do not use a login password, monitoring token or Xray UUID as the collector token.
- This pilot supports one managed route per Xray process. Do not run two collectors against the same SG2 core for SG and UK routes; multi-route synchronization must be added first.

## Deployment

Put these values in Ansible Vault or a private host_vars file excluded from Git:

On the Kenxu host, register an ordinary source node using `scripts/register-meter.mjs EXACT_SOURCE_NODE_NAME PRIVATE_OUTPUT_FILE` with `DATA_DIR` and `PUBLIC_ORIGIN` set. The registration file is private and must not be committed. Then explicitly enroll permitted users in the private administrator page; registration alone does not switch existing users to new UUIDs.

```yaml
kenxu_meter_enabled: true
kenxu_meter_node_id: REPLACE_WITH_REGISTERED_NODE_ID
kenxu_meter_token: REPLACE_WITH_PRIVATE_METER_TOKEN
kenxu_meter_origin: https://YOUR_PRIVATE_PORTAL_DOMAIN
kenxu_meter_inbound_port: 443
kenxu_meter_api_port: 10085
```

Install only on the selected host:

```sh
ansible-playbook kenxu_meter.yml -l YOUR_PILOT_HOST --ask-vault-pass
```

The first bootstrap adds local Xray APIs and enables managed-user counters. It validates the candidate configuration before replacement, takes a root-only backup and restarts Xray once. Request it explicitly:

```sh
ansible-playbook kenxu_meter.yml -l YOUR_PILOT_HOST -e kenxu_meter_bootstrap=true --ask-vault-pass
```

Normal synchronization persists client changes and applies add/remove users and routing through Xray APIs. A failed apply restores the prior config and restarts Xray. Do not expose port 10085 in a cloud security group or firewall.

Only users explicitly enrolled in the node's metering allowlist receive managed UUIDs. Existing non-enrolled subscriptions retain their existing credentials. Enrolled users must update their client subscriptions. Disabled or unassigned users are removed on the next successful desired-state sync, normally within 30 seconds. Deleting a user from Xray prevents new connections; existing long-lived sessions may persist until closed.

## Configuration ownership

The existing `software.yml` now reconciles managed clients after rendering `templates/xray.j2` when `kenxu_meter_enabled` is true. Install/register the collector before enabling that variable. Otherwise normal upstream behavior is unchanged.

For a node with custom outbound or UK relay routing, the base template must already contain that routing before `software.yml` is run. This playbook does not reconstruct a customized relay that an unrelated base-template operation has overwritten. It never logs raw config contents or private variables (`no_log` is used for registration and bootstrap).

The collector owns clients whose email begins with `kenxu:` and their dedicated private-network block rule. Do not edit these manually while the collector is running. Legacy clients remain outside its ownership.

## Accounting

- Reads cumulative Xray user uplink/downlink with `reset=false` every 15 seconds.
- Uses boot ID, process ID and process start time as a counter epoch.
- Persists sequence numbers and an SQLite queue under `/var/lib/kenxu-meter` before posting. Reports are resent until acknowledged.
- Kenxu deduplicates samples and persists deltas. Reports for old sequences or altered duplicate bodies are rejected.
- A reset starts counting from the current value without subtracting old usage. Process restarts retain portal history.
- HTTP/TLS/proxy overhead can make the counter slightly larger than a file's payload size. It is not a provider billing statement.
- An abrupt process crash can lose bytes since the last sample; counters live in Xray memory. Ordinary polling does not guarantee zero-loss accounting.
- The first-version queue assumes a stable registration and portal schema. If the portal rejects a queued report, repair the mismatch instead of deleting the queue or resetting counters blindly.

## Operational checks

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
