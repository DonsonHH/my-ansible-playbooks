# Collector synchronization repair — 2026-10-28

## Problem and reproduction

The portal showed every enabled user's traffic collector as waiting for synchronization. All six physical collectors had recent native and flow reports, but none reported the current desired revision. The running service and maintenance reader agreed on the desired revision and payload, excluding a frontend cache or revision-hash mismatch.

The collector service used `ProtectSystem=strict` without a write exception for `/var/log/kenxu`. Activity-log preparation and Xray configuration validation need to write the existing private log. Validation outside that sandbox succeeded. The same candidate inside a strict transient service failed with a read-only-filesystem error; adding only the private log directory exception made validation pass. A separate network-namespace fixture also confirmed that hot client updates work with the accepted core.

Existing users continued to upload usage using the last accepted configuration. This was a real desired-state failure, rather than evidence that all byte collection had stopped. It also prevented newly added clients from reaching the running core. Historical connection coverage cannot be reconstructed from this repair, and no estimated bytes were inserted.

## Change

- Add the conditional activity-log write exception to the meter service template and flow deployment. Preserve the strict sandbox and existing state/configuration exceptions. Reject unsupported log destinations before a core deployment stops services.
- Add a guarded existing-host repair that only changes this service permission and gracefully restarts the collector, retaining the core process and all private identities/queues.
- Strengthen the independent operational review: current portal revision, cached revision and actual runtime user identities must agree, in addition to counter, queue and service checks.
- Stage the shared sandbox helper alongside the flow deployment script so remote execution does not depend on the developer's checkout.

## Live verification

US3 was repaired first and reviewed before proceeding serially through the five Azure hosts. SG2's UK route and US3's residential route retain their separate accounting identities; they do not require separate physical collectors.

All six physical collectors applied the current desired configuration. Every active account's assigned managed routes then reported both online and synchronized. Each repaired host retained its Xray PID and private collector registration, with native and flow delivery queues drained. Independent native/observer cumulative counters reconciled; loopback API binding, private observer socket, service activity and queue integrity passed.

The portal comparison preserved account metadata, grants, credentials, rule profiles, private source, signing key, the existing native/flow history prefix and all 21 generated ready-user configurations across Clash, Stash and Shadowrocket. Checks derive the current account/export counts rather than treating them as a permanent inventory. No portal source change or display-only healthy override was needed.

## Regression checks

- Linux unit tests: 33 passed, none skipped. Coverage includes private log ownership/content, rejected links and special files, disabled activity logging, narrow sandbox paths, stale desired revision, missing runtime users, and durable queue handling.
- Rendered the service template with logging enabled and disabled; strict sandbox and existing write paths remain in both cases.
- Portal unit/API suite: 84 passed. The monitor repository's JavaScript regression suite also passed; it is separate from per-user metering.

No CI workflow exists in this deployment repository; local and live checks above are the evidence. A healthy current snapshot does not prove lossless historical collection or replace a future host's independent calibration.

## Follow-up review

A later independent review found two maintenance/acceptance gaps, reproduced them in isolation and corrected them:

1. A failure after starting the repaired collector restored the original service fragment but left the process in the new mount namespace. Reloading systemd and starting an already active unit does not recreate that process. Rollback now gracefully stops collection before restoring/reloading the fragment and starting under its original sandbox. Tests cover both an absent and a pre-existing fragment, preserving private configuration and queue bytes while never restarting Xray.
2. The operational review accepted matching cached revision and runtime users without comparing persisted users. A fixture with a missing managed client on disk incorrectly passed. The review now compares persisted managed identities too, while excluding legacy clients from collector ownership.

The two regressions failed against the preceding code and passed after correction. The complete Linux suite now has **35 passing tests with no skips**. The Windows run has four explicitly skipped POSIX checks; it is not substituted for Linux acceptance.

All six physical hosts subsequently passed the strengthened operational review and additional non-disruptive persistence checks: enabled boot services, the persistent narrow log exception and the running process's writable log mount, private log ownership/modes, saved configuration validation as the Xray service account, and unchanged core PID during review. Reapplying the collector's managed configuration preparation to the saved configuration produced no difference. Queues were empty and the preceding 15 minutes contained no desired-sync deferrals at the sampled checks.

The portal still reported all assigned managed routes synchronized for every active account; its 84 unit/API tests passed. Account permissions, all 21 ready-user exports and the retained ledger prefix again matched the protected baseline. No production rollback was triggered to test the failure path, and no core or collector restart was required by this follow-up review.
