# Security policy

Use this software only for authorized enrolled equipment. It deliberately excludes
shell/ADB access, credential extraction, unlock workflows, certificate impersonation
and arbitrary GenieACS provision-script execution.

Never commit credentials, tokens, session files, TLS private keys or device dumps.
Keep NBI and MongoDB private; use TLS for external transport. API_TOKEN is an owner
credential and BOT_API_TOKEN delegates trusted Telegram identities. Do not give either
token to untrusted clients. API token rotation requires updating the configured services.

High-risk RPCs are disabled by default. Enable only model-tested capabilities and exact
writable paths; preview each target. Stage firmware on a spare device and verify its
source, checksum, model and version independently. Task deadlines cannot undo commands
already accepted by a CPE.

To report a security issue, contact the repository owner privately. Do not include live
secrets or confidential device details in public issues. No external security audit or
production certification is claimed.
