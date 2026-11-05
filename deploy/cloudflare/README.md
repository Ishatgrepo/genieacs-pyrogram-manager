# Deploy the full GenieACS + Telegram manager using only api.cat6.in

This deployment runs **GenieACS CWMP/NBI/FS/UI, MongoDB, monitoring workers,
Pyrogram/Kurigram bot, Nginx, and cloudflared** on one Linux server. It needs
**no public IPv4, inbound port forwarding, or origin-side TLS certificate**.
It *does* need a working outbound Internet connection, Docker Engine with
Compose v2, a Cloudflare-controlled DNS zone for cat6.in, a Cloudflare Tunnel
token, valid Telegram credentials, and devices able to reach HTTPS/443.

The `main` branch's standard TLS/port-7547 Compose remains unchanged.
This branch adds a separate file: `docker-compose.cloudflare.yml`.

## Public URLs (single Cloudflare hostname)

| Public URL | Private upstream |
| --- | --- |
| https://api.cat6.in/ | manager:7860 |
| https://api.cat6.in/healthz | manager:7860 |
| https://api.cat6.in/api/* | manager:7860 |
| https://api.cat6.in/cwmp/ | cwmp:7547 (prefix stripped) |
| https://api.cat6.in/files/* | fs:7567 (prefix stripped) |

Do not use port 7547 in the public ACS URL: Cloudflare receives HTTPS on
443. Configure the devices' ACS URL **exactly** as
`https://api.cat6.in/cwmp/`. The external firmware URL prefix is
`https://api.cat6.in/files/`.

## 1. Cloudflare dashboard (one-time)

1. Put cat6.in on Cloudflare DNS. Ensure you are authorized to manage this zone.
2. Cloudflare dashboard > Networking > Tunnels > Create Tunnel > **cloudflared**,
   choose a **remotely managed** tunnel (suggested name: `genieacs-cat6`).
3. Copy the tunnel token from the install/run command. **Do not run a
   separate cloudflared process on the VPS**: Compose runs it.
4. Add one **Published application** route:

   | Field | Value |
   | --- | --- |
   | Hostname | api.cat6.in |
   | Service type | HTTP |
   | Service URL | http://gateway:8080 |

   The service URL is a **Docker service name**, not a public IP or a URL
   reachable from your PC. The tunnel container and gateway share a Docker network.
   Cloudflare creates/routes DNS to the tunnel; if a conflicting DNS record for
   api.cat6.in already exists, resolve it intentionally before switching traffic.

5. Do **not** put a Cloudflare Access browser login in front of the entire
   hostname: TR-069 devices cannot complete browser login. If using WAF/bot
   protections, avoid JS challenges and CAPTCHA on `/cwmp/*` and `/files/*`.
   Consider cache-bypass on those paths. Keep the manager API bearer-protected.
6. Cloudflare handles the public HTTPS certificate; CWMP/FS traffic from
   the local Nginx gateway to the corresponding containers is HTTP inside
   Docker-only networks.

## 2. Configure the server

Clone the Cloudflare-specific branch:

    git clone --branch feature/cloudflare-tunnel-no-public-ipv4 \
      https://github.com/Ishatgrepo/genieacs-pyrogram-manager.git
    cd genieacs-pyrogram-manager

If this is a private GitHub repository, authenticate with a credential that
has repository read access.

Set the manager/ACS secrets:

    cp .env.example .env
    chmod 600 .env
    nano .env

Fill at least these required values:

* `DEPLOYMENT_MODE=full` and `BOT_ENABLED=true`
* `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_OWNER_ID` (numeric)
* `API_TOKEN`, `BOT_API_TOKEN` (different random 32+ character strings)
* `MONGO_ROOT_PASSWORD`, `MONGO_APP_PASSWORD`, `MONGO_ACS_PASSWORD`
  (each a different **random hex** string of 32+ characters)
* `GENIEACS_UI_JWT_SECRET` (random)
* `CPE_USERNAME`, `CPE_PASSWORD` (your authorized device credentials)
* `PUBLIC_HOST=api.cat6.in`

Generate each token independently with:

    openssl rand -hex 32

The previous `CWMP_HOST` and `MANAGER_HOST` values in `.env.example`
are **not used by this Cloudflare Compose**. You can leave them untouched.
The manager's `MONGODB_URI`, `GENIEACS_NBI_URL`, `DEPLOYMENT_MODE`,
and `BOT_ENABLED` are overridden by the Compose file.

Store the Cloudflare **connector token separately**, so it is not injected
into the Telegram/manager container:

    cp deploy/cloudflare/tunnel.env.sample .env.tunnel
    chmod 600 .env.tunnel
    nano .env.tunnel

Replace the placeholder with the real `TUNNEL_TOKEN`. Both `.env` and
`.env.tunnel` are excluded by the repository's `.gitignore`.

### Check configuration before deploying

    docker compose -f docker-compose.cloudflare.yml config --quiet

## 3. Start and verify

    docker compose -f docker-compose.cloudflare.yml up -d --build
    docker compose -f docker-compose.cloudflare.yml ps
    docker compose -f docker-compose.cloudflare.yml logs --tail=100 cloudflared
    docker compose -f docker-compose.cloudflare.yml logs --tail=100 manager cwmp gateway

Then test:

    curl -fsS https://api.cat6.in/healthz

Expected: `{"status":"ok"}`. An unhealthy result, 502, or 503 means
you should investigate logs; successful tunnel connectivity alone does
not establish working GenieACS/device enrollment.

To call the protected API from a trusted client:

    curl -fsS 'https://api.cat6.in/api/devices' \
      -H "Authorization: Bearer YOUR_API_TOKEN"

A 401 without a bearer token is expected. The bot should accept `/start`
from the configured owner and expose `/dashboard`.

## 4. First device

Set an **authorized TR-069/CWMP-capable device** to:

    ACS URL:  https://api.cat6.in/cwmp/
    Username: the CPE_USERNAME in .env
    Password: the CPE_PASSWORD in .env

Its firmware and feature support determine which parameters and remote
actions are available. The bot discovers the device from GenieACS and
requires approval when `REQUIRE_DEVICE_APPROVAL=true`.
Validate a real Inform before assuming that Cloudflare accepts your
device's SOAP and HTTP authentication behavior.

Some legacy devices insist on `http://host:7547/`, a specific port,
end-to-end device-to-origin TLS behavior, or cannot use pathful ACS URLs.
These may **not** work with an HTTPS-on-443 Cloudflare Tunnel. A public
TCP endpoint or a device-side VPN would then be required.

Cloudflare may filter or challenge non-browser clients; inspect its
Security Events if the device's Informs do not arrive. Also verify
request timeouts and firmware sizes in live tests. Reverse device
Connection Requests from GenieACS will not automatically traverse a
CPE-side NAT; queued tasks may wait for the next Inform.

## 5. Native GenieACS UI, logs, and security

UI is intentionally **not** published through Cloudflare. Use an SSH tunnel
from your computer to the VPS:

    ssh -L 3000:127.0.0.1:3000 USER@YOUR_SERVER_SSH_ADDRESS

Open `http://127.0.0.1:3000` locally. Your SSH address can be a
private IPv4 reachable over VPN, IPv6, or an SSH-over-tunnel solution;
it does not have to be a public IPv4. If host port 3000 is already in use,
change the **left-hand** localhost port in the Compose file.

Only the gateway is connected to the cloudflared tunnel; MongoDB and NBI
are on an internal Docker network. No CWMP, FS, NBI, API, or database
ports are published on the host. The native GenieACS UI is bound to
127.0.0.1 only.

Keep high-risk actions disabled until explicitly verified on a spare
device, and do not store `.env`, `.env.tunnel`, database backups, or
private credentials in GitHub.

### Operational commands

    docker compose -f docker-compose.cloudflare.yml logs -f cwmp manager cloudflared
    docker compose -f docker-compose.cloudflare.yml restart cloudflared
    docker compose -f docker-compose.cloudflare.yml down

`down` leaves the named MongoDB volume intact unless you explicitly
use `-v`; **do not use `down -v`** for routine restarts.

For backup/restore and security cautions see `deploy/vps/README.md`
and `docs/acceptance.md`. This deployment variant is a new
configuration, **not yet verified on your live VPS or CPEs**.
