import json
import re
import secrets
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain import ROLES, SECRET, TERMINAL, digest, now, parameters_matching, validate_value


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    object_name: str = ""
    values: dict = Field(default_factory=dict, max_length=20)
    firmware: str = ""
    diagnostic: str = ""
    host: str = ""
    run_at: str | None = None
    priority: int = Field(0, ge=0, le=10)


class Service:
    def __init__(self, db, cfg, acs):
        self.db, self.cfg, self.acs = db, cfg, acs

    async def authorize(self, uid, minimum="VIEWER"):
        user = await self.db.users.find_one({"_id": uid})
        if not user or user.get("disabled") or ROLES.get(user.get("role"), -1) < ROLES[minimum]:
            raise PermissionError("Access denied")
        return user

    async def audit(self, uid, operation, target, detail=None):
        await self.db.audit.insert_one(
            {"at": now(), "actor": uid, "operation": operation, "target": target, "detail": detail or {}}
        )

    async def limit(self, uid):
        minute = int(now().timestamp() // 60)
        row = await self.db.limits.find_one_and_update(
            {"_id": f"{uid}:{minute}"},
            {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": now() + timedelta(minutes=2)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if row["count"] > 120:
            raise ValueError("Rate limit reached; retry next minute")

    async def device(self, device):
        row = await self.db.devices.find_one({"_id": device})
        if not row:
            raise ValueError("Unknown device ID; use the manager ID from /devices")
        return row

    async def devices(
        self,
        q="",
        status="",
        group="",
        model="",
        manufacturer="",
        firmware="",
        tag="",
        enrollment="",
        offset=0,
        limit=20,
    ):
        query = {
            k: v
            for k, v in {
                "status": status,
                "group": group,
                "model": model,
                "manufacturer": manufacturer,
                "firmware": firmware,
                "tags": tag,
                "enrollment": enrollment,
            }.items()
            if v
        }
        if q:
            query["$or"] = [
                {k: {"$regex": re.escape(q[:100]), "$options": "i"}}
                for k in ("acs_id", "name", "serial", "model")
            ]
        rows = (
            await self.db.devices.find(query, {"parameters": 0})
            .sort("_id", 1)
            .skip(offset)
            .limit(limit)
            .to_list(limit)
        )
        return {"items": rows, "total": await self.db.devices.count_documents(query), "offset": offset}

    async def parameters(self, device, q="", category="", data_type="", offset=0, limit=20):
        d = await self.device(device)
        rows = parameters_matching(d.get("parameters", []), q, category, data_type)
        return {"items": rows[offset : offset + limit], "total": len(rows), "cached_at": d.get("sampled_at")}

    async def statistics(self):
        result = {"total": await self.db.devices.count_documents({}), "statuses": {}, "tasks": {}}
        for status in ("ACTIVE", "STALE", "OFFLINE", "UNKNOWN", "FAULT"):
            result["statuses"][status] = await self.db.devices.count_documents({"status": status})
        for status in ("QUEUED", "DISPATCHING", "PENDING_INFORM", "EXECUTING", *sorted(TERMINAL)):
            result["tasks"][status] = await self.db.tasks.count_documents({"state": status})
        result["new_today"] = await self.db.devices.count_documents(
            {"first_seen": {"$gte": now().replace(hour=0, minute=0, second=0, microsecond=0)}}
        )
        result["pending_approval"] = await self.db.devices.count_documents({"enrollment": "PENDING"})
        result["at"] = now()
        result["system"] = await self.db.system.find_one({"_id": "health"}) or {}
        return result

    async def enrollment(self, uid, device, decision):
        await self.authorize(uid, "ADMIN")
        if decision not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError("Invalid enrollment state")
        await self.device(device)
        await self.audit(uid, "enrollment", device, {"decision": decision})
        await self.db.devices.update_one({"_id": device}, {"$set": {"enrollment": decision}})
        return {"enrollment": decision}

    async def edit_device(self, uid, device, data):
        await self.authorize(uid, "ADMIN")
        await self.device(device)
        if set(data) - {"name", "group", "muted", "active_threshold", "offline_threshold", "thresholds"}:
            raise ValueError("Unsupported setting")
        if "thresholds" in data and (
            not isinstance(data["thresholds"], dict) or len(data["thresholds"]) > 30
        ):
            raise ValueError("Invalid thresholds")
        for key in ("active_threshold", "offline_threshold"):
            if key in data and (type(data[key]) is not int or data[key] < 30):
                raise ValueError("Invalid status threshold")
        for key in ("name", "group"):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 100):
                raise ValueError("Invalid label")
        if "muted" in data and type(data["muted"]) is not bool:
            raise ValueError("muted must be boolean")
        await self.audit(uid, "edit_device", device, data)
        await self.db.devices.update_one({"_id": device}, {"$set": data})
        return {"updated": True}

    async def capabilities(self, device):
        d = await self.device(device)
        params = d.get("parameters", [])
        paths = {p["path"] for p in params}
        diagnostics = sorted(
            p[: -len(".DiagnosticsState")]
            for p in paths
            if p.endswith(".DiagnosticsState") and any(k in p for k in ("IPPing", "TraceRoute"))
        )
        configured = self.cfg.model_capabilities.get(d.get("model", ""), [])
        return {
            "operations": ["refresh"]
            + [x for x in ("reboot", "factory_reset", "firmware") if x in configured],
            "writable": [
                p["path"]
                for p in params
                if p["path"] in self.cfg.parameter_allowlist
                and p.get("writable")
                and not SECRET.search(p["path"])
            ],
            "diagnostics": diagnostics,
            "firmware": [k for k, v in self.cfg.firmware_catalog.items() if v.get("model") == d.get("model")],
        }

    async def payload(self, device, request):
        d = await self.device(device)
        if d.get("enrollment") != "APPROVED":
            raise PermissionError("Device requires enrollment approval")
        caps = await self.capabilities(device)
        op = request.operation
        if op == "refresh":
            path = request.object_name.rstrip(".")
            if path and not any(
                p["path"] == path or p["path"].startswith(path + ".") for p in d.get("parameters", [])
            ):
                raise ValueError("Object is not in the reported data model")
            return {"name": "refreshObject", "objectName": path}
        if not self.cfg.allow_high_risk_actions:
            raise PermissionError("High-risk operations are disabled")
        if op in ("reboot", "factory_reset"):
            if op not in caps["operations"]:
                raise ValueError("RPC not verified for this model")
            return {"name": "reboot" if op == "reboot" else "factoryReset"}
        if op == "set":
            if not request.values:
                raise ValueError("No values supplied")
            entries = []
            for path, value in request.values.items():
                if path not in caps["writable"]:
                    raise PermissionError("Parameter is not allowlisted and reported writable")
                p = next(p for p in d["parameters"] if p["path"] == path)
                validate_value(value, p.get("type"))
                entries.append([path, value, p["type"]])
            return {"name": "setParameterValues", "parameterValues": entries}
        if op == "diagnostic":
            if request.diagnostic not in caps["diagnostics"] or not re.fullmatch(
                r"[A-Za-z0-9.:-]{1,253}", request.host
            ):
                raise ValueError("Unsupported diagnostic or invalid host")
            paths = {p["path"]: p for p in d["parameters"]}
            for suffix in ("Host", "DiagnosticsState"):
                if not paths.get(request.diagnostic + "." + suffix, {}).get("writable"):
                    raise ValueError("Diagnostic is not reported writable")
            return {
                "name": "setParameterValues",
                "parameterValues": [
                    [request.diagnostic + ".Host", request.host, "xsd:string"],
                    [request.diagnostic + ".DiagnosticsState", "Requested", "xsd:string"],
                ],
            }
        if op == "firmware":
            if op not in caps["operations"] or request.firmware not in caps["firmware"]:
                raise ValueError("Firmware not approved for this model")
            entry = self.cfg.firmware_catalog[request.firmware]
            if not re.fullmatch(r"[a-fA-F0-9]{64}", entry.get("sha256", "")) or not entry.get("version"):
                raise ValueError("Firmware catalog requires verified SHA256 and target version")
            return {"name": "download", "file": entry["file"], "fileType": "1 Firmware Upgrade Image"}
        raise ValueError("Unsupported operation")

    async def prepare(self, uid, targets, request):
        await self.authorize(uid, "OPERATOR" if request.operation == "refresh" else "ADMIN")
        targets = list(dict.fromkeys(targets))
        if not 1 <= len(targets) <= self.cfg.max_bulk_devices:
            raise ValueError("Explicit bounded target list required")
        preview = []
        for target in targets:
            payload = await self.payload(target, request)
            d = await self.device(target)
            preview.append({"device": target, "acs_id": d["acs_id"], "payload": payload})
        token = secrets.token_urlsafe(18)
        row = {
            "_id": token,
            "actor": uid,
            "targets": targets,
            "request": request.model_dump(),
            "preview": preview,
            "state": "PREPARED",
            "expires_at": now() + timedelta(minutes=5),
        }
        await self.db.actions.insert_one(row)
        await self.audit(uid, "prepare", token, {"targets": targets, "operation": request.operation})
        return {
            "token": token,
            "preview": preview,
            "expires_at": row["expires_at"],
            "phrase": f"CONFIRM {len(targets)}",
            "impact": "Refresh requests device data"
            if request.operation == "refresh"
            else "May interrupt service or change configuration; verify every target",
        }

    async def confirm(self, uid, token, phrase):
        action = await self.db.actions.find_one({"_id": token, "actor": uid})
        if not action or action["expires_at"] <= now():
            raise ValueError("Confirmation expired or invalid")
        if phrase != f"CONFIRM {len(action['targets'])}":
            raise ValueError("Confirmation phrase does not match")
        req = TaskInput(**action["request"])
        await self.authorize(uid, "OPERATOR" if req.operation == "refresh" else "ADMIN")
        # Revalidate enrollment, current role, allowlist, and model at execution time.
        for target in action["targets"]:
            await self.payload(target, req)
        from app.domain import stamp

        run_at = stamp(req.run_at) if req.run_at else now()
        if not run_at:
            raise ValueError("Invalid schedule; use an ISO timestamp")
        await self.audit(uid, "confirm", token, {"targets": action["targets"]})
        if action["state"] == "PREPARED":
            await self.db.actions.update_one(
                {"_id": token, "state": "PREPARED"},
                {
                    "$set": {
                        "state": "CONFIRMED",
                        "run_at": run_at,
                        "materialized": False,
                        "expires_at": now() + timedelta(days=self.cfg.retention_days),
                    }
                },
            )
        action = await self.db.actions.find_one({"_id": token})
        return await self.enqueue_action(action)

    async def enqueue_action(self, action):
        req = TaskInput(**action["request"])
        ids = []
        for target in action["targets"]:
            task_id = digest([action["_id"], target])[:32]
            await self.db.tasks.update_one(
                {"_id": task_id},
                {
                    "$setOnInsert": {
                        "device": target,
                        "actor": action["actor"],
                        "action": action["_id"],
                        "request": req.model_dump(),
                        "state": "QUEUED",
                        "created_at": now(),
                        "run_at": action["run_at"],
                        "priority": req.priority,
                        "attempts": 0,
                        "deadline": action["run_at"] + timedelta(seconds=self.cfg.task_timeout),
                    }
                },
                upsert=True,
            )
            ids.append(task_id)
        await self.db.actions.update_one({"_id": action["_id"]}, {"$set": {"materialized": True}})
        return {"tasks": ids}

    async def cancel(self, uid, task_id):
        await self.authorize(uid, "OPERATOR")
        task = await self.db.tasks.find_one({"_id": task_id})
        if not task:
            raise ValueError("Unknown task")
        if task["state"] != "QUEUED":
            raise ValueError(
                "Only locally queued work can be cancelled reliably; dispatched work may already execute"
            )
        await self.audit(uid, "cancel", task_id)
        result = await self.db.tasks.update_one(
            {"_id": task_id, "state": "QUEUED"}, {"$set": {"state": "CANCELLED", "finished_at": now()}}
        )
        return {"cancelled": result.modified_count == 1}

    async def retry(self, uid, task_id):
        await self.authorize(uid, "ADMIN")
        task = await self.db.tasks.find_one({"_id": task_id})
        if not task or task["state"] != "FAILED" or not task.get("acs_task") or task.get("attempts", 0) >= 3:
            raise ValueError("Only known faulted ACS tasks may be retried, at most 3 attempts")
        if task["request"]["operation"] != "refresh":
            raise ValueError(
                "Disruptive retries require a new preview and confirmation after inspecting the ACS fault"
            )
        await self.payload(task["device"], TaskInput(**task["request"]))
        try:
            await self.db.device_locks.insert_one({"_id": task["device"], "task": task_id})
        except DuplicateKeyError:
            raise ValueError("Another operation holds the device lock") from None
        await self.audit(uid, "retry", task_id)
        # Explicit retry reuses the ACS task, never creates a second operation.
        # Mark before POST; a network error must not permit a second retry submission.
        await self.db.tasks.update_one(
            {"_id": task_id, "state": "FAILED"},
            {
                "$set": {
                    "state": "DISPATCHING",
                    "submitted_at": now(),
                    "deadline": now() + timedelta(seconds=self.cfg.task_timeout),
                },
                "$unset": {"finished_at": ""},
            },
        )
        await self.acs.retry(task["acs_task"])
        await self.db.tasks.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "state": "PENDING_INFORM",
                    "deadline": now() + timedelta(seconds=self.cfg.task_timeout),
                },
                "$unset": {"finished_at": ""},
                "$inc": {"attempts": 1},
            },
        )
        return {"state": "PENDING_INFORM"}

    async def event(self, key, device, kind, detail, notify=True):
        await self.db.events.update_one(
            {"_id": key},
            {
                "$setOnInsert": {
                    "device": device,
                    "kind": kind,
                    "detail": detail,
                    "at": now(),
                    "notify": notify,
                    "outboxed": False,
                }
            },
            upsert=True,
        )

    async def outbox(self):
        async for action in self.db.actions.find({"state": "CONFIRMED", "materialized": False}).limit(20):
            await self.enqueue_action(action)
        async for event in self.db.events.find({"outboxed": False}).limit(200):
            d = await self.db.devices.find_one({"_id": event["device"]}) or {}
            if event.get("notify") and not d.get("muted"):
                async for user in self.db.users.find(
                    {"role": {"$in": ["OWNER", "ADMIN"]}, "disabled": {"$ne": True}}
                ):
                    cooldown_key = digest([event["device"], event["kind"], user["_id"]])
                    last = await self.db.cooldowns.find_one({"_id": cooldown_key})
                    if last and last.get("event") != event["_id"] and last["until"] > now():
                        continue
                    await self.db.cooldowns.update_one(
                        {"_id": cooldown_key},
                        {
                            "$set": {
                                "event": event["_id"],
                                "until": now() + timedelta(seconds=self.cfg.alert_cooldown),
                            }
                        },
                        upsert=True,
                    )
                    key = digest([event["_id"], user["_id"]])
                    await self.db.notifications.update_one(
                        {"_id": key},
                        {
                            "$setOnInsert": {
                                "user": user["_id"],
                                "device": event["device"],
                                "kind": event["kind"],
                                "text": f"{event['kind']}\n{d.get('acs_id', event['device'])}\n"
                                + json.dumps(event["detail"], default=str, ensure_ascii=False)[:2200],
                                "state": "QUEUED",
                                "next_at": now(),
                                "attempts": 0,
                            }
                        },
                        upsert=True,
                    )
            await self.db.events.update_one({"_id": event["_id"]}, {"$set": {"outboxed": True}})

    async def claim_notification(self):
        token = secrets.token_hex(16)
        row = await self.db.notifications.find_one_and_update(
            {
                "$or": [
                    {"state": "QUEUED", "next_at": {"$lte": now()}},
                    {"state": "SENDING", "lease_until": {"$lte": now()}},
                ]
            },
            {
                "$set": {"state": "SENDING", "lease_until": now() + timedelta(seconds=120), "claim": token},
                "$inc": {"attempts": 1},
            },
            sort=[("next_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return row

    async def ack_notification(self, notification, claim, message_id=None, delay=0):
        update = (
            {"state": "QUEUED", "next_at": now() + timedelta(seconds=max(5, delay))}
            if delay
            else {"state": "SENT", "message_id": message_id, "finished_at": now()}
        )
        await self.db.notifications.update_one(
            {"_id": notification, "state": "SENDING", "claim": claim}, {"$set": update}
        )

    async def callback(self, uid, data):
        token = secrets.token_urlsafe(16)
        await self.db.callbacks.insert_one(
            {"_id": token, "actor": uid, "data": data, "expires_at": now() + timedelta(hours=24)}
        )
        return token

    async def get_callback(self, uid, token):
        row = await self.db.callbacks.find_one({"_id": token, "actor": uid, "expires_at": {"$gt": now()}})
        if not row:
            raise PermissionError("Expired or invalid button")
        return row["data"]

    async def session(self, uid, device, chat, message):
        await self.authorize(uid)
        await self.device(device)
        if chat != uid:
            raise PermissionError("Private chats only")
        existing = await self.db.sessions.find_one({"_id": uid})
        if (
            not existing
            and await self.db.sessions.count_documents({"expires_at": {"$gt": now()}})
            >= self.cfg.max_monitor_sessions
        ):
            raise ValueError("Monitor session limit reached")
        await self.db.sessions.replace_one(
            {"_id": uid},
            {
                "_id": uid,
                "device": device,
                "chat": chat,
                "message": message,
                "next_at": now(),
                "expires_at": now() + timedelta(seconds=self.cfg.monitor_ttl),
            },
            upsert=True,
        )
        return {"monitoring": True}
