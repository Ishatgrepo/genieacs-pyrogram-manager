#!/usr/bin/env bash
# One-time simple GenieACS WebUI installer for Cloudflare Tunnel (acs.cat6.in).
# Inputs: only the WebUI password and Cloudflare tunnel token.
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "Install Docker Engine and Compose v2 first."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 not found."
command -v openssl >/dev/null || die "openssl is required to generate a local signing secret."
[[ -r /dev/tty ]] || die "Run interactively in a terminal (SSH is fine)."

# Do not overwrite existing credentials or an existing authenticated MongoDB.
# A fresh install has a clean, unauthenticated, internal-only Mongo volume.
if [[ ! -f .env ]]; then
    jwt="$(openssl rand -hex 32)"
    printf 'GENIEACS_UI_JWT_SECRET=%s\n' "$jwt" > .env
    unset jwt
    chmod 600 .env
    echo "Generated the GenieACS UI signing secret automatically."
elif ! grep -Eq '^GENIEACS_UI_JWT_SECRET=[[:xdigit:]]{64}$' .env; then
    die "Existing .env has no valid signing secret. Back it up and remove it before a FRESH install; do not erase your MongoDB volume."
else
    echo "Using existing .env signing secret."
fi

mkdir -p secrets
if [[ ! -s secrets/ui.htpasswd ]]; then
    printf 'Create WebUI password for login name: admin\n'
    IFS= read -r -s -p 'WebUI password: ' pass1 </dev/tty
    printf '\n'
    IFS= read -r -s -p 'Confirm WebUI password: ' pass2 </dev/tty
    printf '\n'
    [[ -n "$pass1" && "${#pass1}" -ge 12 ]] || die "Use a WebUI password of at least 12 characters."
    [[ "$pass1" == "$pass2" ]] || die "WebUI passwords do not match."
    # -i reads password from stdin; it is never placed in a process argument.
    printf '%s\n' "$pass1" | docker run --rm -i -v "$ROOT/secrets:/auth" \
      httpd:2.4-alpine htpasswd -i -B -c /auth/ui.htpasswd admin
    unset pass1 pass2
    chmod 644 secrets/ui.htpasswd
    echo "WebUI password hash stored locally at secrets/ui.htpasswd."
else
    echo "Using existing WebUI password hash in secrets/ui.htpasswd."
fi

# Hugging Face / Telegram are NOT involved. The Cloudflare remotely-managed
# Tunnel token is the only Cloudflare credential required on the VPS.
if [[ -f .env.tunnel ]] && grep -Eq '^TUNNEL_TOKEN=[^[:space:]]{20,}$' .env.tunnel \
    && ! grep -q 'REPLACE_WITH' .env.tunnel; then
    echo "Using existing Cloudflare Tunnel token."
else
    IFS= read -r -s -p 'Cloudflare Tunnel token: ' token </dev/tty
    printf '\n'
    [[ "${#token}" -ge 20 && "$token" != *[[:space:]]* ]] || die "Paste the Tunnel token (not the whole cloudflared command)."
    printf 'TUNNEL_TOKEN=%s\n' "$token" > .env.tunnel
    chmod 600 .env.tunnel
    unset token
fi

echo "Checking Docker Compose configuration..."
docker compose -f docker-compose.webui.yml config --quiet

echo "Launching central GenieACS, MongoDB, WebUI and Cloudflare Tunnel..."
docker compose -f docker-compose.webui.yml up -d --build

printf '\n%s\n' '-------------------------------------------'
printf '%s\n' 'Browser:       https://acs.cat6.in/'
printf '%s\n' 'WebUI user:    admin'
printf '%s\n' 'ACS endpoint:  https://acs.cat6.in/cwmp/'
printf '%s\n' 'Check logs:    docker compose -f docker-compose.webui.yml logs -f cloudflared gateway ui cwmp'
printf '%s\n' 'Cloudflare route: acs.cat6.in -> http://gateway:8080'
printf '%s\n' '-------------------------------------------'
printf '%s\n' 'IMPORTANT: No CPE credentials are required (JFC-style compatibility).'
printf '%s\n' 'The public CWMP endpoint accepts unauthenticated devices: restrict access before production.'
printf '%s\n' 'Existing JFC stock STBs may still require their local port-8443 TLS adapter.'
