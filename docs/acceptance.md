# Acceptance and deployment checks

## Automated locally

Run `python -m pip install -r requirements.lock -e '.[test]'`, then `pytest -q` and
`ruff check app tests`. Tests use simulated ACS responses and an async Mongo test double
unless `TEST_MONGODB_URI` is supplied. No test contacts production CPEs or Telegram.

Covered: registration/outbox dedup across worker reconstruction; approval and parameter
redaction; actual sample timestamps; 120 simulated devices, pagination and search;
HTTP 202 vs 200 and fresh-postcondition reconciliation; inferred offline transitions;
callback/actor/role authorization; idempotent confirmation; per-device locking;
submission-timeout ambiguity; protected HTTP APIs; counter reset/wrap/gap behavior;
CSV formula safety; JSON/PDF/PNG report generation; hybrid credential forwarding;
Kurigram and TgCrypto imports.

## Must run with your services before production use

1. `docker compose config --quiet`, then `docker compose build` and `docker compose up -d`.
   Confirm all services start and `curl -f https://YOUR_MANAGER_HOST/healthz` succeeds.
   There is no local Docker engine in the code-generation environment, so the actual
   container build/startup is not claimed as tested there.
2. Open the bot in your configured owner account and send `/start`. Verify another
   Telegram account cannot access inventory or copied callback buttons.
3. Enroll one authorized CPE with your ACS URL, valid TLS trust and CPE credentials.
   Verify exactly one normal registration notification (without inducing the documented
   send-before-ack crash window). Approve it from its device panel.
4. Browse parameters, check model/firmware against the device, and verify measurement
   sample times. Missing optical/CPU/cellular values must remain unavailable.
5. Request one small object refresh. If NBI returns 202, confirm the bot shows pending.
   After the next Inform, confirm actual reported data and the recorded terminal state.
6. Stop the test CPE's Informs. Verify stale/offline transitions with configured thresholds;
   these are inferred activity, not active reachability probes.
7. Restart manager and Mongo containers without deleting volumes. Check device approvals,
   roles, history, pending work and outbox recovery.
8. With a spare device, explicitly enable and test one model RPC. Do not infer firmware
   installation from an HTTP acknowledgement alone. Check native TransferComplete/fault
   diagnostics in GenieACS where required.
9. For a Space, set hybrid mode and HTTPS core URL. Verify actual Pyrogram bot login,
   HTTPS API access, alert delivery, pause/resume behavior and queue recovery. Do not
   claim Hugging Face MTProto compatibility solely from port 443 being permitted.
10. Measure CPU, Mongo storage, NBI latency and Telegram FloodWait behavior with your
    target fleet. The 120-device simulated test is not a full CWMP load test.

The original acceptance scenarios A–I are only fully satisfied after these live checks.
No real Telegram login, real CPE action, full Docker deployment, real MongoDB restart,
or Hugging Face Space deployment was performed while generating this repository.
