# Architecture and operational semantics

This is an original implementation of authorized GenieACS administration. It does
not import or run code, certificates, keys, APKs or JARs from Housekeeping-PoC.

## Components

* `app/config.py`: validated environment settings, secret fields and HTTPS boundaries.
* `app/acs.py`: async NBI client, percent-encoded IDs, bounded HTTP pool, no unsafe POST retries.
* `app/domain.py`: parameter normalization/redaction, statuses, counter math, value validation.
* `app/database.py`: PyMongo Async client, indexes, retention and worker leases.
* `app/service.py`: shared access controls, enrollment, previews, durable actions/tasks/outbox.
* `app/workers.py`: discovery, cached telemetry, status changes, reconciliation, reports.
* `app/api.py`: authenticated API, reusable selections/templates, exports and bot state.
* `app/bot.py`: Pyrogram/Kurigram MTProto bot, menus, private-chat checks, callbacks and delivery.
* `app/reports.py`: CSV/JSON/PDF/PNG from stored fleet records.
* `app/main.py`: process lifecycle, health, request limits, trace IDs and hybrid proxy.

Closely related features share modules instead of creating dozens of empty handlers.
No USP adapter is included. Vendor measurements are displayed only when reported.

## Data path

CPE -> GenieACS CWMP -> GenieACS MongoDB -> NBI -> core discovery/collection -> manager
MongoDB -> persistent outbox -> Pyrogram -> authorized private Telegram chats.

Default discovery polls projected identity fields every 10 seconds with a persistent
checkpoint and a 60-second overlap. Hourly full keyset scans catch older/missing events.
First-connect notification latency is normally one polling interval plus queue delivery;
this is not a synchronous Inform webhook. Informs are not independently ingested/logged
as full CWMP sessions, so several Informs between polls can coalesce.

Approved devices have their **existing ACS cache** sampled every five minutes; stale
telemetry remains visibly timestamped. Offline devices are sampled less often. A user
can explicitly request an object refresh. No periodic full-tree CPE refresh is forced.
Firmware/IP/boot changes and status transitions are durable events. Numeric samples use
CPE/ACS parameter timestamps. Missing/invalid readings remain unavailable, not zero.
Counter wraps require a configured 32/64-bit width near the modulus; resets, reboots,
repeated timestamps and large sample gaps do not produce a rate.

## Task state machine

`QUEUED -> DISPATCHING -> PENDING_INFORM/EXECUTING -> COMPLETED/FAILED/TIMED_OUT`.
`QUEUED -> CANCELLED` is the only reliable cancellation path exposed by the bot.

A task is persisted before submission. The same confirmation produces deterministic
per-target task IDs. Replay does not duplicate an operation. Per-device locks prevent
concurrent tasks. All dispatches recheck the actor's current role, enrollment and allowlist.
Bulk actions require an explicit bounded list and a typed confirmation of the exact count.

HTTP 202 means queued, never completed. HTTP 200 confirms RPC execution; firmware,
diagnostics and reboot additionally wait for fresh observable postconditions. A task
that vanishes from the ACS without a fault is not automatically called successful:
refresh/set/reboot/diagnostic/firmware require their respective fresh postcondition.
Factory-reset success can only be reported from an immediate NBI 200; deferred reset
without a verifiable postcondition times out rather than inventing success.

A timeout during submission creates an **unknown outcome**. A crash after the ACS accepted
a POST but before its response was durably recorded creates the same uncertainty.
The worker never resubmits that operation automatically. It remains DISPATCHING until
its deadline. Inspect ACS task/fault records before taking further action. NBI does not
provide a general exactly-once idempotency contract.

Explicit `/retry` reuses a known faulted **refresh** task, with at most three attempts.
Disruptive operations require a fresh preview after the administrator inspects the fault.
A command's successful RPC does not guarantee that the intended physical outcome occurred.

## Notifications and recovery

Registration/event IDs and per-recipient outbox IDs are deterministic; repeated discovery
polls do not duplicate queued alerts. Outbox expansion is restart-safe and independent
of discovery. Delivery uses renewable claims and acknowledgement tokens. Quiet hours
and per-device muting apply before delivery. Persistent queue retry backs off on Telegram
errors and honors FloodWait. One bot replica is supported.

Delivery is **at least once**. If Telegram accepts a message and the process dies before
MongoDB receives the acknowledgement, it can be delivered twice. Exactly-once Telegram
delivery is not promised. Database outages prevent durable notifications; `/healthz`
returns degraded and workers retry. Use an independent uptime monitor for core/DB outages.

Threshold rules are exact parameter paths with `min`/`max`, using the reported units.
They produce breach/recovery transitions rather than an alert every sample. Repeated
notifications of the same kind/recipient/device have a configurable cooldown; the event
history is retained even when notification repeats are suppressed. Set thresholds
per device or group. Fault summaries omit raw fault bodies because they can contain secrets.

## Security

* Private chats only; Telegram ID must match chat ID. Every handler and callback is reauthorized.
* OWNER manages user roles; ADMIN approves devices and configurations; OPERATOR can refresh;
  VIEWER can inspect/export inventory. Owner-only portable backup includes user-role metadata.
* API_TOKEN is an owner service credential. BOT_API_TOKEN is a separate trusted service
  credential allowing Telegram-ID delegation; never distribute it to ordinary API clients.
* Callbacks use opaque user-bound DB tokens; confirmation tokens expire in five minutes.
* Sensitive parameter paths are redacted before storage/export. URL/username/password/key
  fields are not writable through this manager. Unknown vendor blobs should be reviewed
  before collection; path-based redaction is not a general content DLP system.
* Configuration writes use exact allowlists, reported writable flags and type/range checks.
* No shell execution, ADB enablement, security bypass, preset-script execution, or credential extraction.
* NBI and MongoDB have no published host ports; MongoDB uses separate least-privilege users.
* Public API ingress uses TLS; native CWMP/FS use operator-owned TLS certificates.
* Audit records are stored separately with a longer retention period. Database operators
  can modify them; this is not a tamper-proof external audit log.

## Capacity and retention

Default collection capacity is 40 devices per 5-second pass with eight concurrent reads.
For 1,000 devices at a 300-second collection interval, the steady average is about 3.3
cache reads/second plus fault lookups. Discovery reads projected deltas. Actual throughput
must be load-tested against your NBI and MongoDB; simulated tests are not a 1,000-CPE benchmark.

Storage estimate: 1,000 devices x 288 samples/day x 2 KB/sample = about 576 MB/day, or
17.3 GB over 30 days before indexes/replication. Parameter snapshots may be much larger.
Rate of change, number of parameters and BSON overhead materially change this estimate.
Metric history uses indexed regular collections with TTL; it is not a Mongo time-series
collection. Tasks/notifications expire after terminal completion; active work is retained.

The five-minute cache reader does not itself add CWMP traffic. Explicit object refreshes
and your GenieACS provisioning determine CPE traffic. At 1,000 devices, a 20 KB refresh
per device every five minutes would be about 5.76 GB/day, excluding protocol overhead.

## Limits of this release

Fleet status/distribution exports, current parameter exports, historical snapshots and
raw numeric history are implemented. Scheduled reports are JSON fleet summaries with daily, weekly, monthly or interval
buckets. Historical per-parameter PNG charts use actual sample timestamps and measured
values/rates. Availability percentages and cross-interface billing-grade traffic totals
are not implemented because sparse Informs/counters cannot establish them reliably.
Ping/traceroute are detected through reported writable diagnostic objects. Download/upload
and vendor DNS diagnostics need model-specific schemas and are not offered generically.
Model RPC capabilities are operator-verified configuration because parameter presence
alone cannot prove support for Reboot/Download/FactoryReset RPCs.
