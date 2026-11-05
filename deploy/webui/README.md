# Central GenieACS Web UI on acs.cat6.in (Cloudflare Tunnel)

**No Telegram bot. No manager container. No public IPv4. No ACS installed on each PC.**

This deployment runs one persistent GenieACS server (CWMP, NBI, file server
and **native browser-based GenieACS management UI**) plus MongoDB, Nginx,
and cloudflared on a Linux VPS or always-on home server.

> Important: This does **not** replace all the device-side requirements of
> the [JFC-Group Housekeeping PoC](https://github.com/JFC-Group/Housekeeping-PoC).
> Read the JFC compatibility section before using it with a Jio STB.

## Where everything lives

| URL / port | Function | Protection |
| --- | --- | --- |
| `https://acs.cat6.in/` | GenieACS **web UI** | Nginx Basic Auth **and** native GenieACS login |
| `https://acs.cat6.in/cwmp/` | TR-069/CWMP device endpoint | GenieACS CWMP auth; no browser login |
| `https://acs.cat6.in/files/` | Approved ACS file downloads | Never upload unknown firmware |
| `127.0.0.1:7547` on VPS | CWMP for optional SSH/VPN site relay | Host loopback only |
| `127.0.0.1:7557` on VPS | GenieACS NBI for optional SSH/VPN admin relay | Host loopback only |
| `mongo:27017` | Database | Internal Docker network |

**NBI is never published under acs.cat6.in.** It has no default
bearer-token authentication. Never expose it publicly or bind 7557 to
`0.0.0.0`. The UI is the supported remote control surface.

All device configurations, presets, tasks, parameters, and file uploads
are managed through **GenieACS's own web UI**, not the Pyrogram manager.

## Requirements

- Linux VPS or server with outbound Internet; Docker Engine and Compose v2.
- Domain **cat6.in** configured with Cloudflare DNS.
- A remotely managed Cloudflare Tunnel token.
- Device firmware able to connect to `https://acs.cat6.in/cwmp/` over TLS 443
  **and** configured with your ACS URL and CPE credentials.
- A browser to access the web UI. No VPS public IPv4, DNS A record pointing
  to your VPS, or inbound firewall ports are necessary.
- MongoDB 8.0 compatible CPU/kernel, adequate RAM and persistent disk.

## 1. Create a Cloudflare Tunnel

In the Cloudflare dashboard, go to **Networking > Tunnels**, create a
**remotely managed cloudflared** tunnel (e.g. `genieacs-webui`), and obtain
its **connector token**. Do not run its standalone installer on the VPS.

Add one **Published application** route:

| Field | Value |
| --- | --- |
| Public hostname | `acs.cat6.in` |
| Service type | `HTTP` |
| Service URL | `http://gateway:8080` |

`gateway` is the Docker Compose container name resolved **inside the
cloudflared container**. Do not enter `localhost` or `acs.cat6.in` in
the Service URL. Remove or adjust any conflicting existing Cloudflare DNS
record for `acs.cat6.in`; do not take down unrelated DNS records.

**Do not enable a domain-wide Cloudflare Access login or JS/CAPTCHA
challenge for the CPE paths** `/cwmp/*` and `/files/*`: embedded
TR-069 clients generally cannot solve those challenges. The UI has
two login layers (Basic Auth + GenieACS). Configure CF caching to bypass
CWMP/API responses and avoid security challenges on those paths.

Cloudflare Tunnel uses outbound connections; ensure Cloudflare egress is
allowed (including port 7844 where required by your environment). Public
HTTPS terminates at Cloudflare; internal gateway-to-GenieACS traffic uses
HTTP on private Docker networks.

## 2. Download the web-only branch

```bash
git clone --branch feature/acs-webui-cloudflare \
  https://github.com/Ishatgrepo/genieacs-pyrogram-manager.git
cd genieacs-pyrogram-manager
```

The repository may be private; authenticate to GitHub as an authorized
user if Git asks for credentials.

Use the *new web-only* configuration, **not** the main branch's Telegram
`.env.example`:

```bash
cp deploy/webui/env.example .env
cp deploy/webui/tunnel.env.example .env.tunnel
chmod 600 .env .env.tunnel
nano .env
```

Fill in:

```dotenv
ACS_HOST=acs.cat6.in
MONGO_ROOT_PASSWORD=EACH_A_DIFFERENT_RANDOM_HEX
MONGO_ACS_PASSWORD=EACH_A_DIFFERENT_RANDOM_HEX
GENIEACS_UI_JWT_SECRET=EACH_A_DIFFERENT_RANDOM_HEX
CPE_USERNAME=YOUR_DEVICE_USERNAME
CPE_PASSWORD=YOUR_DEVICE_PASSWORD
```

Generate each database password and UI signing secret separately:

```bash
openssl rand -hex 32
```

For a real fleet, prefer per-device CPE credentials rather than a shared
bootstrap credential. Changes to Mongo/ACS passwords **after MongoDB has
initialized** require database-side rotation; editing `.env` alone does
not change stored credentials.

Edit `.env.tunnel` and replace the token placeholder:

```dotenv
TUNNEL_TOKEN=YOUR_CLOUDFLARE_TUNNEL_TOKEN
```

Never commit or share these files.

## 3. Create the management login

A separate Basic Auth login protects the native GenieACS setup screen.
Choose a **strong unique password** at the prompt:

```bash
mkdir -p secrets
docker run --rm -it -v "$PWD/secrets:/auth" \
  httpd:2.4-alpine htpasswd -cB /auth/ui.htpasswd admin
chmod 644 secrets/ui.htpasswd
```

`htpasswd` stores a **bcrypt hash**, not the plaintext password.
The file is read-only inside the web gateway. The repo already ignores
`secrets/`. Use a long, unique password: hashes with read permission
still deserve protection from offline guessing.

If you want to rotate this Basic Auth credential, run the same
`htpasswd -B /auth/ui.htpasswd admin` command without `-c`, then restart
`gateway` if needed. Do not remove Basic Auth protection until native
GenieACS login is configured and additional restrictions are in place.

## 4. Deploy

```bash
docker compose -f docker-compose.webui.yml config --quiet
docker compose -f docker-compose.webui.yml up -d --build
docker compose -f docker-compose.webui.yml ps
docker compose -f docker-compose.webui.yml logs --tail=100 cloudflared gateway ui cwmp
```

Check that the **Cloudflare Tunnel is Healthy** in the dashboard.
Then open **https://acs.cat6.in/**. Enter the Nginx Basic Auth
credentials, then complete GenieACS's own first-time UI/account setup.
Configure GenieACS admin/users/roles securely.

To confirm that the browser UI is not anonymously exposed:

```bash
curl -I https://acs.cat6.in/
```

The expected initial result is **HTTP 401 Unauthorized** with
`WWW-Authenticate: Basic`, not a successful anonymous admin page.

To monitor CPE communications:

```bash
docker compose -f docker-compose.webui.yml logs -f cwmp
```

## 5. Enroll and manage compatible TR-069 devices

Configure an **authorized** TR-069 device with:

```text
ACS URL: https://acs.cat6.in/cwmp/
ACS username: CPE_USERNAME in .env
ACS password: CPE_PASSWORD in .env
```

Wait for the device to send its CWMP **Inform**. Open the GenieACS UI:
find the device in **Devices**, inspect reported parameters, check
**Tasks/Faults**, and create supported presets/provisions as appropriate.
The UI can queue remote parameter changes and other supported tasks.
Do not assume an accepted task has completed on-device until its
response and subsequent data confirm success.

Not every CPE accepts HTTPS port 443, a pathful ACS URL, the CA chain,
or the default credentials. Test with one device before scaling.
**Device configuration of the ACS endpoint is a separate step**.
Putting an ACS on the Internet does not change a device's built-in ACS
URL automatically.

## 6. JFC-Group Housekeeping-PoC compatibility: important limits

The upstream PoC's [stage1.sh](https://github.com/JFC-Group/Housekeeping-PoC/blob/main/stage1.sh)
expects a **site-local** `pass_front.py` TLS proxy at **8443/9443**, DNS
routing for the device's existing ACS host, and a local GenieACS NBI
endpoint at **127.0.0.1:7557**. The front end also implements special
device-protocol/session behavior. The separate `unlock.sh` uses local
ADB and is **not** an ACS server or a Web UI feature.

That means:

1. **Standard TR-069 devices already configured for your hostname:** use
   `https://acs.cat6.in/cwmp/` directly. The web UI controls them; no
   local PC needs GenieACS.
2. **Stock JFC-targeted Jio STBs using their existing ACS hostname/port:**
   **cannot be made compatible simply by replacing the hostname with
   acs.cat6.in or pointing DNS to a Cloudflare Tunnel**. The device
   expects its original host, nonstandard TCP port and the PoC-specific
   TLS/session behavior. Cloudflare's public HTTP tunnel does not simply
   proxy the device's port 8443. Do not treat this deployment as a
   drop-in replacement for `pass_front.py`.
3. **One local site adapter may still be required**, but you can keep
   the *ACS itself* centrally on the VPS. For a site gateway connected
   to the VPS using an authenticated SSH connection/VPN, the VPS exposes
   loopback-only CWMP **7547** and NBI **7557**; port-forward those
   privately to the site's local machine. Follow the upstream PoC's
   requirements for its local TLS/network behavior. You do **not**
   need to run its Docker ACS on every PC.
4. The default ACS `cwmp.auth` in this deployment **requires CPE
   credentials**. The stock JFC PoC used a permissive test ACS;
   unknown/unconfigured device credentials may cause registration
   failure. Do not turn off authentication on an Internet-facing ACS
   as a quick fix. Plan explicit, authorized enrollment instead.
5. **Important:** After the full `stage1.sh` workflow, the upstream
   script **disables the device's TR-069 client**. You cannot expect
   ongoing ACS management of that device unless it remains enrolled
   and its TR-069 agent is still running.

This repository does **not** copy or bundle JFC firmware keys,
certificates, TLS impersonation code, or exploits. The upstream PoC may
require separate LAN-side procedures and may be incompatible with your
device/build. No JFC exploit was executed or tested here.

### Optional local SSH/VPN port forwarding for a site adapter

If your gateway can already reach the VPS over authenticated SSH
(private VPN, reachable IPv6, or a separately configured Cloudflare
SSH Access setup), a long-lived secure tunnel to the **one central
server** can forward the internal ports:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:7547:127.0.0.1:7547 \
  -L 127.0.0.1:7557:127.0.0.1:7557 \
  USER@VPS_REACHABLE_SSH_ADDRESS
```

The local gateway then has `127.0.0.1:7547` for a compatible private
CWMP proxy and `127.0.0.1:7557` for trusted administrative clients.
Do **not** expose the NBI on the LAN or through a public Cloudflare
HTTP route. SSH itself is **not** made reachable just by the
`acs.cat6.in` web tunnel; you need a separate secure SSH/VPN path.

## Backups and useful commands

Database is a **named persistent Docker volume**. Keep MongoDB
backups and all secrets separate from GitHub. To stop:

```bash
docker compose -f docker-compose.webui.yml down
```

Do **not** use `down -v` unless you intend to erase your ACS database.

To restart:

```bash
docker compose -f docker-compose.webui.yml up -d
```

To inspect configuration:

```bash
docker compose -f docker-compose.webui.yml config --quiet
```

## Validation status

This is a new web-only deployment branch. The Compose and Nginx files
have configuration validation in CI, but a successful live connection
from **your** VPS through Cloudflare and a real device is **not claimed
until tested**.

Cloudflare reference:
https://developers.cloudflare.com/tunnel/get-started/
