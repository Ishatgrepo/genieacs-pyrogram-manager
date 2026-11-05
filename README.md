# GenieACS WebUI — JFC-style central ACS on Cloudflare

**Dedicated branch:** `feature/acs-webui-cloudflare`

Run a **single GenieACS + MongoDB** server on your VPS without a public IPv4,
and manage devices in your browser at **https://acs.cat6.in/**.

This branch **does not run the Telegram bot**. It includes only GenieACS
CWMP, NBI, file server, built-in WebUI, MongoDB, Nginx Basic Auth,
and Cloudflare Tunnel.

## Only two inputs

1. **WebUI password** (username: `admin`).
2. **Cloudflare Tunnel token**.

The installer generates the GenieACS UI signing secret automatically.
MongoDB and CWMP registration are configured **like the JFC local PoC**:
no database password and no CPE username/password. MongoDB remains on a
private Docker network. Unauthenticated public CWMP is a **security tradeoff**;
restrict to trusted CPE gateways/source IPs for real deployments.

## One-command VPS installation

1. Create a remotely managed Cloudflare Tunnel with one published hostname:
   **`acs.cat6.in` → HTTP `http://gateway:8080`**.
2. On a server with Docker Engine and Compose v2:

```bash
git clone -b feature/acs-webui-cloudflare \
  https://github.com/Ishatgrepo/genieacs-pyrogram-manager.git
cd genieacs-pyrogram-manager
bash deploy/webui/setup.sh
```

The script prompts only for your **WebUI password** and
**Cloudflare Tunnel token**, creates its local configuration, builds the
containers, and starts the ACS.

**[Full installation and JFC-Group compatibility guide](deploy/webui/README.md)**

## Ports and services

| Path/port | Purpose |
| --- | --- |
| `https://acs.cat6.in/` | Password-protected native GenieACS WebUI |
| `https://acs.cat6.in/cwmp/` | CPE CWMP endpoint on HTTPS 443 |
| `https://acs.cat6.in/files/` | GenieACS file server |
| `127.0.0.1:7547` on VPS | Local-only CWMP (optional SSH/VPN relay) |
| `127.0.0.1:7557` on VPS | Local-only NBI (optional SSH/VPN relay) |
| Docker internal | MongoDB |

**Important for JFC Housekeeping PoC:** the upstream
[JFC-Group/Housekeeping-PoC](https://github.com/JFC-Group/Housekeeping-PoC)
uses an STB-specific TLS proxy at 8443/9443, local DNS routing and ADB.
Cloudflare HTTPS on 443 **does not replace** that local component, but
you can centralize **GenieACS itself** and securely forward its CWMP/NBI
ports to authorized site gateways. Read the full guide before trying
to register a stock STB.

Do not publish NBI (7557) or MongoDB (27017) to the Internet.
A successful live end-to-end test of the Cloudflare tunnel and your STB
has not yet been performed.
