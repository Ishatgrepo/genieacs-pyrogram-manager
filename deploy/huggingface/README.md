# Hugging Face hybrid deployment

The root README includes Docker Space metadata. Copy the repository files to a Docker Space.
Set `DEPLOYMENT_MODE=hybrid`, `CORE_API_URL=https://manager.your-domain.example`,
`BOT_ENABLED=true`, and the Telegram/API secrets from `.env.example` using Space Secrets.
Run the VPS stack with `DEPLOYMENT_MODE=core` and `BOT_ENABLED=false`.
Use exactly the same `BOT_API_TOKEN`, owner ID and admin ID configuration on both sides.
Do not run two bot replicas with the same Telegram token.

The Space does **not** connect to MongoDB, NBI port 7557, or CPEs directly. It connects to
our authenticated core API over HTTPS 443. Durable workers, queues, history, and MongoDB
stay on the VPS. This deliberately adjusts the original requested topology because
Spaces documents outbound networking on 80/443/8080, whereas MongoDB normally uses 27017.
Simply pointing MONGODB_URI at Atlas is not a verified workaround.

The Space exposes only port 7860 through the Hugging Face HTTPS ingress. The public
root and health endpoints reveal no inventory. API requests still require bearer auth.
Configure the Space as private if its availability/URL itself is sensitive.

Pyrogram MTProto uses Telegram connections on port 443. A port being permitted does
not prove that a particular Space permits MTProto traffic: verify actual bot login
and notification delivery in your Space before relying on it. If the platform blocks
that transport, keep the bot on the VPS (`full` mode). Do not switch silently to Bot API.

Sleeping or paused Spaces cannot deliver notifications. Core monitoring continues;
queued notifications are delivered on resume, with at-least-once delivery semantics.
Select appropriate always-on hosting if prompt alerts matter. No sleep-bypass pings
are included. No local Space disk is treated as durable storage.

Run the deployment checks in `docs/acceptance.md`. An actual Space deployment was not
performed during code generation: no Hugging Face credentials or Telegram credentials
were supplied. Platform pricing/plan eligibility may change; consult the current docs.

Sources: https://huggingface.co/docs/hub/spaces-overview and
https://huggingface.co/docs/hub/spaces-sdks-docker
