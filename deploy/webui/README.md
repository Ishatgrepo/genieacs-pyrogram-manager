# One-command GenieACS WebUI on acs.cat6.in

**Web-only, JFC-style setup.** No Telegram bot. No public IPv4. No MongoDB
password to enter. No TR-069/CPE username or password to enter. One central
GenieACS deployment for multiple PCs/sites. The only inputs are:

1. Your **WebUI password**, entered twice for confirmation.
2. Your **Cloudflare Tunnel token**, copied from Cloudflare.

The installer automatically creates a random GenieACS UI JWT signing secret.
MongoDB runs **without authentication**, like the JFC-Group PoC, but is
restricted to an internal Docker-only network. The CWMP endpoint is also
unauthenticated, as in the JFC PoC; this makes registration easier but is
**unsafe for an unrestricted public ACS**. Restrict CWMP access to your
authorized source IPs or controlled site gateways when possible, and use
authenticated CPE enrollment for a production deployment.

The public UI has HTTP Basic Auth (bcrypt hash) in front of the native
GenieACS login/setup. Your WebUI username is always `admin`.

## 1. Create the Cloudflare Tunnel

1. In Cloudflare, make sure `cat6.in` is managed as a Cloudflare DNS zone.
2. Go to **Networking > Tunnels**, create a **remotely managed Cloudflared
   tunnel** (suggested name `genieacs-webui`).
3. Copy the **tunnel token**; do not install a separate cloudflared service
   on the VPS, since Docker Compose includes its own connector.
4. Add a single published application:

   | Field | Value |
   | --- | --- |
   | Public hostname | `acs.cat6.in` |
   | Service | `HTTP` |
   | Service URL | `http://gateway:8080` |

   The internal hostname `gateway` is resolved by Docker from
   the cloudflared container. Do not use `localhost` or your VPS IP.
   If an old record already uses `acs.cat6.in`, resolve the conflict
   intentionally.

Cloudflare handles public HTTPS (443), and cloudflared makes outbound
connections. No public IPv4 or inbound firewall ports are required.
Avoid Cloudflare Access login/CAPTCHA/browser challenges on `/cwmp/*`
or `/files/*` because standard embedded TR-069 clients cannot
complete browser challenges. The UI has its own Basic Auth and native
GenieACS login. Consider Cloudflare IP allowlisting for CWMP.

## 2. Install Docker and download the web-only branch

Install Docker Engine and Docker Compose v2 on the VPS first.

```bash
git clone -b feature/acs-webui-cloudflare \
  https://github.com/Ishatgrepo/genieacs-pyrogram-manager.git
cd genieacs-pyrogram-manager
```

The repository may be private and may require GitHub authentication.

**Run this single command:**

```bash
bash deploy/webui/setup.sh
```

It will ask:

```text
WebUI password:
Confirm WebUI password:
Cloudflare Tunnel token:
```

Nothing else is required from you. It automatically generates
`.env`, `.env.tunnel`, `secrets/ui.htpasswd`, validates the Compose file,
builds the GenieACS image and launches the full web-only stack.

Re-running the script reuses existing generated credentials and password
hash; it doesn't replace or delete the MongoDB volume.

## 3. Open the web UI

Browse to:

**https://acs.cat6.in/**

Log in to Nginx Basic Auth with:

- **Username:** `admin`
- **Password:** the WebUI password you entered during setup.

On the first visit, GenieACS itself may ask you to finish setting up
its **native** administrator account; this is a separate app-level
login/setup, not an additional VPS deployment secret.

Once logged into GenieACS, you can view registered devices,
browse parameters, queue tasks, manage presets/provisions,
and inspect faults/measurements supported by each device.

## 4. Addresses and ports

| Address | Purpose | Security |
| --- | --- | --- |
| `https://acs.cat6.in/` | Native GenieACS WebUI | Basic Auth + GenieACS login |
| `https://acs.cat6.in/cwmp/` | Standard TR-069 device endpoint | No CPE auth (JFC-style testing) |
| `https://acs.cat6.in/files/` | GenieACS firmware/file service | Public CPE download path |
| VPS `127.0.0.1:7547` | CWMP for private site relay | Localhost only |
| VPS `127.0.0.1:7557` | GenieACS NBI for private site relay | Localhost only |
| `mongo:27017` | MongoDB | Docker-internal only |

**Never expose 7557 or 27017 to the Internet.** Raw NBI does not
have the same login protection as the WebUI.

## 5. JFC-Group Housekeeping-PoC: what changes, what doesn't

Read the upstream [JFC README](https://github.com/JFC-Group/Housekeeping-PoC)
and [stage1.sh](https://github.com/JFC-Group/Housekeeping-PoC/blob/main/stage1.sh).

**This branch replaces the per-PC GenieACS server**, not every part
of the JFC device-specific connection process.

- An ordinary TR-069 device **you control** that permits changing its
  ACS URL can connect to `https://acs.cat6.in/cwmp/` over HTTPS 443.
- A stock JFC-targeted Jio STB is different: upstream `stage1.sh`
  expects its original ACS hostname on port **8443** (sometimes 9443),
  a local DNS redirect, and the specialized `tls/pass_front.py`
  TLS/session-handling proxy. **A Cloudflare HTTP tunnel on 443
  cannot automatically substitute for those requirements.**
- To avoid installing MongoDB and GenieACS on every PC, a trusted
  gateway/site PC can instead use a **secure VPN/SSH port-forward**
  to this single VPS's localhost CWMP (7547) and NBI (7557) ports.
  The site's original JFC front-end requirements still apply.
  The domain tunnel on `acs.cat6.in` does not itself provide an SSH
  endpoint; set up SSH/VPN separately.
- The upstream full `stage1.sh` workflow disables the Jio TR-069
  client after its one-time task. Those boxes won't remain live-managed
  by GenieACS after the TR-069 client is disabled.
- JFC's stock client requirements, trust chain, proprietary network
  checks and port expectations vary by firmware; verify one authorized
  device/site before scaling.

**Security warning:** Because this mirrors JFC's lack of CPE authentication,
any device that can reach the public `/cwmp/` route may attempt to
register or inject device reports. Use Cloudflare source-IP restrictions
for authorized gateways and monitor device enrollment. If you need
long-term Internet-facing fleet management, adopt per-device CPE auth.

## 6. Troubleshooting and commands

```bash
docker compose -f docker-compose.webui.yml ps
docker compose -f docker-compose.webui.yml logs -f cloudflared gateway ui cwmp
curl -I https://acs.cat6.in/
```

The last command should return HTTP **401 Unauthorized** from Nginx
for anonymous access to the WebUI. After successful Basic Auth,
the native GenieACS UI should load.

To check which CWMP auth config is stored (especially important if
reusing an *existing* database volume from an earlier deployment):

```bash
docker compose -f docker-compose.webui.yml exec -T mongo \
  mongosh --quiet genieacs --eval 'db.config.findOne({_id:"cwmp.auth"})'
```

**A fresh installation does not insert `cwmp.auth`.** An existing volume
may still contain a previously configured auth rule; the installer does
**not** silently delete existing authentication settings.

To stop without deleting the database:

```bash
docker compose -f docker-compose.webui.yml down
```

**Never use `down -v`** unless you intentionally want to erase the MongoDB
database. Store backups and credentials securely.

## Deployment verification

This is an initial integration. Automated CI checks lint the installer,
validate Compose and Nginx configuration, and confirm that only
localhost CWMP/NBI ports are published. It has not been verified
against your particular live Cloudflare tunnel, VPS, or STB.
