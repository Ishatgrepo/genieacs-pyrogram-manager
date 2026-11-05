# VPS operations

Use the root Compose file and README. New Mongo volumes initialize separate `manager`
and `acs` users and `cwmp.auth`; changing `.env` does not rotate users or CPE credentials
in an existing volume. Perform such rotations in Mongo/GenieACS and update `.env` together.
The UI is loopback-only. NBI and MongoDB must not be added to `ports:`.

## Back up and restore

The Telegram `/backup` export is deliberately sanitized and bounded to 10,000 rows per
collection. It excludes DB credentials, ACS data, notification delivery state and binary
firmware, so it is not suitable as the sole operational backup.

For a consistent cold backup, schedule a maintenance window, stop all writers, and dump
MongoDB. Existing devices may retry Informs while CWMP is stopped. Run from the repo root:

```bash
mkdir -p backups
chmod 700 backups
docker compose stop manager cwmp nbi fs ui
docker compose exec -T mongo sh -c 'exec mongodump --username root --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip' > backups/mongo.archive.gz
docker compose start manager cwmp nbi fs ui
chmod 600 backups/mongo.archive.gz
```

Encrypt the dump and store a copy off-host. Separately protect `.env`, your TLS keys and
Caddy volume backups. They must not be uploaded to GitHub or sent through Telegram.
A restored dump contains confidential device data and any ACS-stored credentials.

Restore first to an isolated test instance with the same MongoDB major version. Do not
run a restored bot against the live Telegram token during validation. `mongorestore --drop`
replaces data; use only when intentionally restoring the target instance:

```bash
docker compose stop manager cwmp nbi fs ui
docker compose exec -T mongo sh -c 'exec mongorestore --username root --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip --drop' < backups/mongo.archive.gz
docker compose start manager cwmp nbi fs ui
```

Ensure restored Mongo users match the connection credentials before restarting clients.
Account for the notification send/ack ambiguity when recovering from an old backup.

## TLS and CPE authentication

Use certificates for a hostname you control. The ACS container runs as UID 1000 and
needs read access to the mounted key. No upstream certificates/keys are shipped.
Keep the key directory protected and restart CWMP/FS after renewals.

The bootstrap CPE credential is a minimal starting point. For a real fleet configure
per-device credentials with a GenieACS `cwmp.auth` expression/extension and document
its enrollment process. Do not use credentials learned from an untrusted CPE as proof
of its authorization. The manager's pending-approval status is not ACS authentication.

## Scaling

Only one bot replica. A core-only deployment can be replicated behind a proxy; Mongo
leases assign each worker family to a process, and database-backed state survives loss
of a replica. Load-test on real MongoDB before production. Monitor worker logs, Mongo
storage/TTL behavior, NBI latency, queue sizes and `/healthz`. HTTP access logging is off
to avoid leaking sensitive query values; requests carry X-Request-ID.

## MongoDB host kernel compatibility

MongoDB 8.0 rejects Linux kernels 6.19 through 7.0.13 because of an allocator/kernel
incompatibility. Containers share the host kernel. Use a MongoDB-supported stock kernel
(e.g. the Ubuntu 22.04/24.04 supported kernel series), or a documented fixed kernel/release
combination. Do not suppress MongoDB's startup check. CI uses Ubuntu 22.04 instead of the
moving ubuntu-latest image after observing this exact startup failure.

Reference: https://www.mongodb.com/docs/manual/administration/production-notes/
