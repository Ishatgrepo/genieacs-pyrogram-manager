---
title: Advanced GenieACS Control Center
emoji: 📡
sdk: docker
app_port: 7860
---

# GenieACS Pyrogram Manager

A Telegram control center for **authorized, enrolled TR-069 devices**. Uses Python 3.12,
Kurigram (maintained Pyrogram-compatible APIs), TgCrypto, FastAPI, PyMongo Async and Docker.
Supports an all-VPS stack or a Hugging Face bot/gateway with a durable VPS core.

## Full VPS through Cloudflare Tunnel (no public IPv4)

This branch adds a **single-hostname** deployment for `api.cat6.in` using
Docker Compose, an internal Nginx gateway, and an outbound Cloudflare Tunnel.
**No public inbound ports, host TLS certificates, or extra subdomains are needed.**
It runs the entire GenieACS / Telegram / MongoDB stack on one server.

**Start here:** [Cloudflare deployment guide](deploy/cloudflare/README.md)
using `docker-compose.cloudflare.yml`. The standard Compose instructions
below are for the separate, direct-public-IP deployment and do not apply to
this Cloudflare variant.

## What it does

* Discovers devices, queues first-seen alerts, and requires enrollment approval.
* Lists/searches/filters devices and browses redacted, timestamped TR-181/TR-098 parameters.
* Displays reported hardware, Wi-Fi, fiber, cellular and interface data where available.
* Calculates traffic rates from distinct counter samples; handles resets/reboots/wraps.
* Offers private Telegram menus, pagination, refresh, expiring auto-monitor sessions and roles.
* Previews and confirms bounded tasks, maintains task history, and handles 202 as pending.
* Supports exact allowlisted settings, verified model RPCs and reported ping/traceroute diagnostics.
* Stores historical snapshots/metrics, groups, threshold alerts, selections and templates.
* Provides authenticated APIs, CSV/JSON/PDF/PNG fleet exports and scheduled summary alerts.

This is an initial implementation, not a claim that every vendor or the full requested
enterprise feature list has been production-certified. Read [architecture and limits](docs/architecture.md)
and [acceptance checks](docs/acceptance.md) before relying on it.

## Deploy on your VPS

Requires Docker Engine with Compose v2, DNS pointing to your VPS, valid certificates for
your ACS hostname, Telegram API ID/hash from my.telegram.org and a BotFather bot token.

```bash
git clone https://github.com/Ishatgrepo/genieacs-pyrogram-manager.git
cd genieacs-pyrogram-manager
cp .env.example .env
```

Edit `.env` with the Telegram credentials, numeric owner ID, hostnames, CPE credentials,
and independent secrets. Generate **each** API token, Mongo password and JWT secret with
`openssl rand -hex 32`. Mongo passwords must be hex so connection URIs remain valid.
Never commit `.env`, TLS private keys, sessions or database dumps.

Put your legitimate ACS hostname's certificate chain and private key in:

```text
secrets/tls/fullchain.pem
secrets/tls/privkey.pem
```

Make them readable by UID 1000 inside the ACS container using appropriate owner/group
permissions (do not make the private key world-readable). Renew them and restart the
CWMP/FS services when certificates change. Caddy obtains the manager hostname certificate.

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs -f manager
```

Open the Telegram bot and send `/start`, then `/dashboard`. Enroll CPEs using supported
configuration with `https://YOUR_CWMP_HOST:7547/` and the configured CPE credentials. The
Compose bootstrap rejects unauthenticated CPEs. It creates a shared bootstrap credential;
for larger deployments configure per-device authentication in GenieACS before enrollment.
Manager approval is a second control, separate from CPE-to-ACS authentication.

Open the GenieACS UI via an SSH tunnel to VPS port 3000 and complete initial UI setup:

```bash
ssh -L 3000:127.0.0.1:3000 USER@YOUR_VPS
```

Visit `http://127.0.0.1:3000` locally. Do not publish the initial setup UI to the Internet.

| Service | Exposure |
| --- | --- |
| Manager | HTTPS 443 through Caddy; local 127.0.0.1:7860 |
| CWMP | TLS 7547, legitimate operator-owned ACS certificate |
| File server | TLS 7567 for approved firmware files |
| GenieACS UI | Loopback 3000, SSH tunnel |
| NBI | Private container network only, 7557 |
| MongoDB | Authenticated private container network only, 27017 |

To use an **existing ACS**, deploy the root Dockerfile separately with a durable MongoDB
URI and authenticated HTTPS NBI URL. `GENIEACS_NBI_AUTH_TOKEN` is sent as Bearer auth to
your NBI reverse proxy; GenieACS itself does not automatically enforce that token.
The supplied full-stack Compose overrides NBI/Mongo URLs for its private services.

## Hugging Face

See [hybrid deployment](deploy/huggingface/README.md). Set the VPS to `DEPLOYMENT_MODE=core`,
`BOT_ENABLED=false`; set the Space to `DEPLOYMENT_MODE=hybrid` with the HTTPS core URL
and Telegram/API secrets. MongoDB and durable workers remain on the VPS. Never assume
that direct Atlas port 27017 or arbitrary CWMP ports are available from Spaces.

## Telegram usage

`/devices` -> select a device -> **Approve** -> **Parameters** or **Monitor**.
Use **Request Refresh** to preview a CPE refresh. **Refresh view** only redraws cached data.
Only a device's actual reported values appear; stale readings show their original sample time.

The full command help is in `/help`. Advanced examples (use the manager device ID shown
in the device panel, not the raw ACS ID):

```text
/parameters DEVICE_ID Device.WiFi
/bulk DEVICE_ID_1,DEVICE_ID_2
/prepare DEVICE_ID {"operation":"refresh","object_name":"Device.DeviceInfo"}
/prepare DEVICE_ID {"operation":"set","values":{"Device.WiFi.SSID.1.SSID":"My WiFi"}}
/prepare DEVICE_ID {"operation":"reboot"}
/prepare DEVICE_ID {"operation":"diagnostic","diagnostic":"Device.IP.Diagnostics.IPPing","host":"example.com"}
/confirm TOKEN CONFIRM 2
/report pdf
/role TELEGRAM_USER_ID VIEWER
```

For configuration changes, enable `ALLOW_HIGH_RISK_ACTIONS=true` and add exact permitted
paths to `PARAMETER_ALLOWLIST`. A path must also be reported writable with a supported
data type. Reboot/firmware/factory-reset RPCs additionally need a verified entry in
`MODEL_CAPABILITIES`. Default settings expose no unverified disruptive RPCs.

Firmware images must be uploaded to GenieACS by an administrator, hash-verified, matched
to the device model and listed in `FIRMWARE_CATALOG`. The catalog records the expected
SHA256; this manager does not itself verify the bytes at the device or sign firmware.
Do not replace an approved file under the same name. Verify installation on a spare device.

## Protected API

`Authorization: Bearer API_TOKEN` grants owner access. Keep this token private.
`BOT_API_TOKEN` is only for the trusted bot service and additionally requires
`X-Telegram-User`; it is not a limited public-client token.

```bash
curl -fsS https://YOUR_MANAGER_HOST/api/devices \
  -H "Authorization: Bearer $API_TOKEN"
```

Routes: `/api/devices`, `/api/devices/{id}`, `/parameters`, `/tree`, `/compare`,
`/history`, `/metrics`, `/snapshots`, `/capabilities`, `/diagnostics`, `/tasks`;
`/api/groups`, `/alerts`, `/tasks`, `/statistics`, `/system`, `/reports/{json|csv|pdf|png}`,
`/selections`, `/templates`, `/users` and `/backup`.

Create a preview using `POST /api/actions` with `{"targets":["ID"],"task":{"operation":"refresh"}}`.
Confirm using `POST /api/actions/TOKEN/confirm` with `{"phrase":"CONFIRM 1"}`.
Fetch per-device results with `GET /api/actions/TOKEN`; cancel queued items with
`POST /api/actions/TOKEN/cancel`. Task requests optionally accept ISO `run_at` and priority 0–10.

Group thresholds use `PUT /api/groups/NAME`, e.g. `{"thresholds":{"Device.DeviceInfo.TemperatureStatus.TemperatureSensor.1.Value":{"max":75}}}`.
Assign a device using `PATCH /api/devices/ID` with `{"group":"NAME"}`. Parameter paths
and units vary by device; do not apply the example threshold blindly.

Saved selections and templates have separate `PUT`/`GET` endpoints. Applying either uses
its `/prepare` endpoint and the same confirmation path. See source route schemas for the
complete typed request bodies. Public interactive API docs are disabled intentionally.

## Test and operate

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock -e '.[test]'
pytest -q
ruff check app tests
```

TgCrypto may need a C compiler and Python development headers when no wheel is available;
the Docker build stage provides them. Run only one bot process/replica for a token.
Core workers use MongoDB leases, bounded concurrency and persistent checkpoints.

`/backup` is an owner-only sanitized portable export, not a complete database backup.
Use [backup/restore instructions](deploy/vps/README.md) for a consistent operational backup.
`/healthz` is a non-sensitive process/DB readiness check. Monitor it externally for outages.

Normal repeated Informs do not duplicate registration alerts. A crash after Telegram sends
but before acknowledgement can duplicate a notification: delivery is at least once.
An ambiguous ACS POST is **never blindly retried**. See the task semantics documentation.

[Source/dependency review](docs/source-review.md) · [Security policy](SECURITY.md) · [MIT license](LICENSE)
