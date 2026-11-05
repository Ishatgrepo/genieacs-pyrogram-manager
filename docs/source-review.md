# Reference and dependency review

Reviewed 2026-11-05. Reference repository tree:
https://github.com/JFC-Group/Housekeeping-PoC/tree/394d1af98e18f191b5e7e41755b4cfe3c51f1d05

The repository tree was inventoried and the requested scripts/docs were retrieved for
architecture review. Binary JARs, private keys/certificates and captured logs were not
imported or executed. This is not a binary reverse-engineering or security audit.

| Reference | Finding and decision |
| --- | --- |
| `stage1.sh` | Uses NBI device discovery and queued tasks; distinction between 200/202 is relevant. Its ADB/security-changing actions are excluded. |
| `unlock.sh` | Device unlocking/boot security changes; outside this platform. |
| `tls/pass_front.py` | Custom TLS frontend with session-cookie to upstream connection mapping, sensitive body logging and device-specific impersonation behavior. Not reused. Standard GenieACS TLS is used. |
| `tls/docker-compose.yml` | Splits ACS and MongoDB but publishes management ports and includes a fixed JWT secret. This deployment keeps NBI/Mongo private, authenticates Mongo, and requires operator secrets. |
| `README.md` | Describes the unlock workflow and credits; not an authorized fleet-management implementation. |
| `Housekeeping-SDK.md` | Root-shell path; excluded. |
| `Housekeeping-UBoot.md` | Bootloader/security bypass workflow; excluded. |
| `src/`, JARs, `pull_boot.sh`, TLS assets/logs | Identified in the tree as supporting the device unlock tooling; no code/assets imported. |

No reference code is copied, so this project's original source has its own MIT license.
The original research remains credited to JFC-Group and its contributors, under its
upstream license. That attribution is not an endorsement of this manager.

## Current primary references

* GenieACS NBI endpoints, task JSON and 200/202 semantics:
  https://docs.genieacs.com/en/stable/api-reference.html
* Native TLS/environment and FS URL settings:
  https://docs.genieacs.com/en/stable/environment-variables.html
* CPE authentication (`cwmp.auth`, AUTH, EXT):
  https://docs.genieacs.com/en/stable/cpe-authentication.html
* Hugging Face network restrictions and lifecycle:
  https://huggingface.co/docs/hub/spaces-overview
* Docker Space ingress and runtime:
  https://huggingface.co/docs/hub/spaces-sdks-docker
* Original Pyrogram maintenance notice: https://docs.pyrogram.org/
* Kurigram, maintained Pyrogram-compatible fork (Python 3.12 supported):
  https://pypi.org/project/Kurigram/
* PyMongo Async migration and native AsyncMongoClient:
  https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/

Kurigram 2.2.26 imports as `pyrogram`; no aiogram/Telethon/Bot API substitution is used.
Python 3.12 imports and the TgCrypto C extension were tested locally. The test double
uses mongomock-motor for its async Mongo-compatible interface; production uses PyMongo
Async and does not import Motor. Exact resolved runtime dependencies are in requirements.lock.
