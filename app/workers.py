import asyncio
import logging
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app.database import lease
from app.domain import TERMINAL, classify, digest, flatten, now, rate, stamp
from app.service import TaskInput

log = logging.getLogger(__name__)
IDENTITY_PROJECTION = ",".join(
    [
        "_id",
        "_deviceId",
        "_lastInform",
        "_registered",
        "_lastBoot",
        "_tags",
        "Device.DeviceInfo",
        "Device.ManagementServer.PeriodicInformInterval",
        "InternetGatewayDevice.DeviceInfo",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
    ]
)


class Workers:
    def __init__(self, service):
        self.s, self.db, self.cfg, self.acs = service, service.db, service.cfg, service.acs
        self.owner = uuid.uuid4().hex
        self.semaphore = asyncio.Semaphore(self.cfg.worker_concurrency)

    async def run(self):
        await asyncio.gather(
            self.loop("discovery", self.discover, self.cfg.device_discovery_interval),
            self.loop("status", self.statuses, self.cfg.device_status_interval),
            self.loop("metrics", self.collect, 5),
            self.loop("tasks", self.tasks, 3),
            self.loop("outbox", self.s.outbox, 2),
            self.loop("reports", self.scheduled_report, 60),
        )

    async def loop(self, name, fn, interval):
        while True:
            try:
                await self._loop(name, fn, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("worker_restarting", extra={"worker": name, "error_type": type(exc).__name__})
                await asyncio.sleep(10)

    async def _loop(self, name, fn, interval):
        while True:
            if await lease(self.db, name, self.owner, 90):
                # Renewal runs independently of long paginated syncs.
                async def renew():
                    while True:
                        await asyncio.sleep(20)
                        if not await lease(self.db, name, self.owner, 90):
                            raise RuntimeError("Worker lease lost")

                renewal = asyncio.create_task(renew())
                job = asyncio.create_task(fn())
                try:
                    done, _ = await asyncio.wait({job, renewal}, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        await t
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Do not log exception messages: HTTP URLs/DB errors can include credentials.
                    log.error("worker_failed", extra={"worker": name, "error_type": type(exc).__name__})
                    if name == "discovery":
                        await self.s.event(
                            "acs-failure:" + str(int(now().timestamp() // 300)),
                            "system",
                            "ACS_FAILURE",
                            {"error_type": type(exc).__name__},
                        )
                        await self.db.system.update_one(
                            {"_id": "health"}, {"$set": {"acs": "ERROR", "at": now()}}, upsert=True
                        )
                finally:
                    job.cancel()
                    renewal.cancel()
                    await asyncio.gather(job, renewal, return_exceptions=True)
            await asyncio.sleep(interval)

    async def discover(self):
        checkpoint = await self.db.system.find_one({"_id": "discovery"}) or {}
        start = now()
        query = {}
        if (
            checkpoint.get("at")
            and checkpoint.get("full_at")
            and start - checkpoint["full_at"] < timedelta(hours=1)
        ):
            query = {"_lastInform": {"$gte": (checkpoint["at"] - timedelta(seconds=60)).isoformat()}}
        after = None
        while True:
            rows = await self.acs.get("devices", query, IDENTITY_PROJECTION, limit=100, after=after)
            if not rows:
                break
            for row in rows:
                await self.ingest(row)
            after = rows[-1]["_id"]
            if len(rows) < 100:
                break
        old = await self.db.system.find_one({"_id": "health"}) or {}
        if old.get("acs") == "ERROR":
            await self.s.event("acs-recovery:" + start.isoformat(), "system", "ACS_RECOVERED", {})
        await self.db.system.update_one(
            {"_id": "health"}, {"$set": {"acs": "ONLINE", "at": now()}}, upsert=True
        )
        data = {"at": start}
        if not query:
            data["full_at"] = start
        await self.db.system.update_one({"_id": "discovery"}, {"$set": data}, upsert=True)

    async def ingest(self, raw, detailed=False):
        key = digest(raw["_id"])[:24]
        previous = await self.db.devices.find_one({"_id": key}) or {}
        parameters = flatten(raw)
        values = {p["path"]: p["value"] for p in parameters}

        def get(suffix, fallback=None):
            return values.get("Device." + suffix, values.get("InternetGatewayDevice." + suffix, fallback))

        identity = raw.get("_deviceId", {})
        data = {
            "acs_id": raw["_id"],
            "last_inform": stamp(raw.get("_lastInform")),
            "last_boot": stamp(raw.get("_lastBoot")),
            "tags": raw.get("_tags", []),
            "manufacturer": get("DeviceInfo.Manufacturer", identity.get("_Manufacturer", "Unknown")),
            "model": get("DeviceInfo.ModelName", identity.get("_ProductClass", "Unknown")),
            "serial": get("DeviceInfo.SerialNumber", identity.get("_SerialNumber", "Unknown")),
            "oui": identity.get("_OUI"),
            "product_class": identity.get("_ProductClass"),
            "hardware": get("DeviceInfo.HardwareVersion"),
            "firmware": get("DeviceInfo.SoftwareVersion"),
            "uptime": get("DeviceInfo.UpTime"),
            "inform_interval": get("ManagementServer.PeriodicInformInterval"),
            "synced_at": now(),
        }
        if detailed:
            data.update(
                parameters=parameters,
                sampled_at=now(),
                next_sample_at=now()
                + timedelta(
                    seconds=self.cfg.metrics_collection_interval
                    * (4 if classify(data.get("last_inform")) == "OFFLINE" else 1)
                ),
            )
            data["ips"] = sorted(
                str(p["value"])
                for p in parameters
                if p["path"].endswith(("ExternalIPAddress", ".IPAddress")) and p["value"]
            )
        first = {
            "first_seen": stamp(raw.get("_registered")) or now(),
            "discovered_at": now(),
            "name": raw["_id"],
            "enrollment": "PENDING" if self.cfg.require_device_approval else "APPROVED",
            "group": "",
            "status": "UNKNOWN",
        }
        # Durable event id is deterministic even if a crash occurs before/after upsert.
        if not previous:
            await self.s.event(
                "registration:" + key,
                key,
                "NEW_DEVICE",
                {"model": data["model"], "enrollment": first["enrollment"]},
                self.cfg.notify_new_devices,
            )
        for field in ("firmware", "ips", "last_boot"):
            if previous.get(field) and data.get(field) and data[field] != previous[field]:
                await self.s.event(
                    digest([key, field, data[field], data.get("last_inform")]),
                    key,
                    field.upper() + "_CHANGED",
                    {"before": previous[field], "after": data[field]},
                )
        await self.db.devices.update_one({"_id": key}, {"$set": data, "$setOnInsert": first}, upsert=True)
        if detailed:
            fingerprint = digest(parameters)
            if fingerprint != previous.get("parameter_hash"):
                await self.db.snapshots.update_one(
                    {"_id": digest([key, fingerprint])},
                    {"$setOnInsert": {"device": key, "at": now(), "parameters": parameters}},
                    upsert=True,
                )
                await self.db.devices.update_one({"_id": key}, {"$set": {"parameter_hash": fingerprint}})
            prior = {p["path"]: p for p in previous.get("parameters", [])}
            reboot = bool(previous.get("last_boot") and data.get("last_boot") != previous["last_boot"])
            measurements = []
            profile = self.cfg.metric_profiles.get(data["model"], {})
            for p in parameters:
                if (
                    isinstance(p["value"], (int, float))
                    and type(p["value"]) is not bool
                    and p.get("sample_at")
                ):
                    item = dict(p)
                    if p["path"].endswith(("BytesReceived", "BytesSent")):
                        item["bps"] = rate(
                            prior.get(p["path"]), p, profile.get(p["path"], {}).get("counter_bits"), reboot
                        )
                    if prior.get(p["path"], {}).get("sample_at") != p["sample_at"]:
                        measurements.append(item)
            if measurements:
                await self.db.metrics.update_one(
                    {"_id": digest([key, measurements])},
                    {"$setOnInsert": {"device": key, "at": now(), "measurements": measurements}},
                    upsert=True,
                )
            await self.thresholds(key, data, previous)
        return key

    async def thresholds(self, key, data, previous):
        group = await self.db.groups.find_one({"_id": previous.get("group", "")}) or {}
        thresholds = {**group.get("thresholds", {}), **previous.get("thresholds", {})}
        alarms = previous.get("alarms", {})
        for path, rule in thresholds.items():
            p = next((x for x in data.get("parameters", []) if x["path"] == path), None)
            if not p or type(p["value"]) not in (int, float) or not p["sample_at"]:
                continue
            if (now() - p["sample_at"]).total_seconds() > self.cfg.device_offline_threshold:
                continue
            if not isinstance(rule, dict):
                continue
            breached = (isinstance(rule.get("max"), (int, float)) and p["value"] > rule["max"]) or (
                isinstance(rule.get("min"), (int, float)) and p["value"] < rule["min"]
            )
            alert_key = digest(path)[:16]
            was = alarms.get(alert_key, False)
            if bool(breached) != was:
                await self.s.event(
                    digest([key, path, p["sample_at"], breached]),
                    key,
                    "THRESHOLD_ALARM" if breached else "THRESHOLD_RECOVERED",
                    {"parameter": path, "value": p["value"], "sample_at": p["sample_at"]},
                )
                alarms[alert_key] = bool(breached)
        await self.db.devices.update_one({"_id": key}, {"$set": {"alarms": alarms}})

    async def statuses(self):
        async for d in self.db.devices.find({}, {"parameters": 0}):
            group = await self.db.groups.find_one({"_id": d.get("group", "")}) or {}
            active = d.get(
                "active_threshold", group.get("active_threshold", self.cfg.device_active_threshold)
            )
            offline = d.get(
                "offline_threshold", group.get("offline_threshold", self.cfg.device_offline_threshold)
            )
            state = classify(d.get("last_inform"), active, max(offline, active + 1))
            if d.get("fault_count", 0):
                state = "FAULT"
            if state != d.get("status"):
                notify = self.cfg.notify_reconnected if state == "ACTIVE" else self.cfg.notify_offline
                await self.s.event(
                    digest([d["_id"], "status", d.get("status"), state, d.get("last_inform")]),
                    d["_id"],
                    "STATUS_" + state,
                    {"inferred": True, "last_inform": d.get("last_inform")},
                    notify,
                )
                await self.db.devices.update_one({"_id": d["_id"]}, {"$set": {"status": state}})

    async def collect(self):
        # Passive collection of ACS cache; does not poll every CPE or force full-tree RPCs.
        rows = (
            await self.db.devices.find(
                {
                    "enrollment": "APPROVED",
                    "$or": [{"next_sample_at": {"$exists": False}}, {"next_sample_at": {"$lte": now()}}],
                }
            )
            .limit(40)
            .to_list(40)
        )

        async def one(d):
            async with self.semaphore:
                try:
                    await self.ingest(await self.acs.device(d["acs_id"]), detailed=True)
                    faults = await self.acs.get("faults", {"device": d["acs_id"]}, limit=100)
                    await self.db.devices.update_one(
                        {"_id": d["_id"]},
                        {
                            "$set": {
                                "fault_count": len(faults),
                                "faults": [
                                    {
                                        "code": f.get("code"),
                                        "timestamp": f.get("timestamp"),
                                        "channel": f.get("channel"),
                                    }
                                    for f in faults
                                ],
                            }
                        },
                    )
                except Exception as exc:
                    await self.db.devices.update_one(
                        {"_id": d["_id"]},
                        {
                            "$set": {
                                "next_sample_at": now() + timedelta(seconds=60),
                                "sample_error": type(exc).__name__,
                            }
                        },
                    )

        await asyncio.gather(*(one(d) for d in rows))

    async def finish(self, task, state, reason):
        await self.s.event(
            "task:" + task["_id"] + ":" + state,
            task["device"],
            "TASK_" + state,
            {"task": task["_id"], "reason": reason},
            state == "COMPLETED" or self.cfg.notify_task_failures,
        )
        await self.db.tasks.update_one(
            {"_id": task["_id"]}, {"$set": {"state": state, "reason": reason, "finished_at": now()}}
        )
        await self.db.device_locks.delete_one({"_id": task["device"], "task": task["_id"]})

    async def tasks(self):
        # Recover stale device locks left between terminal update and release.
        async for lock in self.db.device_locks.find({}):
            t = await self.db.tasks.find_one({"_id": lock["task"]})
            if not t or t["state"] in TERMINAL:
                await self.db.device_locks.delete_one({"_id": lock["_id"], "task": lock["task"]})
        pending = (
            await self.db.tasks.find({"state": {"$nin": list(TERMINAL)}, "run_at": {"$lte": now()}})
            .sort([("priority", -1), ("created_at", 1)])
            .limit(100)
            .to_list(100)
        )

        async def one(t):
            async with self.semaphore:
                try:
                    await self.process_task(t)
                except Exception as exc:
                    # Read/reconciliation errors can be retried; submission errors are handled below.
                    log.warning("task_check_failed", extra={"error_type": type(exc).__name__})

        await asyncio.gather(*(one(t) for t in pending))

    async def process_task(self, task):
        if task["deadline"] <= now():
            return await self.finish(
                task,
                "TIMED_OUT",
                "Deadline exceeded; dispatch may have executed. Inspect ACS before retrying.",
            )
        if task["state"] == "QUEUED":
            try:
                await self.db.device_locks.insert_one({"_id": task["device"], "task": task["_id"]})
            except DuplicateKeyError:
                lock = await self.db.device_locks.find_one({"_id": task["device"]})
                if not lock or lock["task"] != task["_id"]:
                    return
            try:
                req = TaskInput(**task["request"])
                await self.s.authorize(task["actor"], "OPERATOR" if req.operation == "refresh" else "ADMIN")
                payload = await self.s.payload(task["device"], req)
                d = await self.s.device(task["device"])
            except (ValueError, PermissionError) as exc:
                return await self.finish(task, "FAILED", str(exc))
            result = await self.db.tasks.update_one(
                {"_id": task["_id"], "state": "QUEUED"},
                {
                    "$set": {"state": "DISPATCHING", "submitted_at": now(), "payload": payload},
                    "$inc": {"attempts": 1},
                },
            )
            if not result.modified_count:
                return
            try:
                await self.db.devices.update_one(
                    {"_id": task["device"]},
                    {"$set": {"last_connection_attempt": now(), "connection_request_result": "UNKNOWN"}},
                )
                code, response = await self.acs.submit(d["acs_id"], payload)
                await self.db.devices.update_one(
                    {"_id": task["device"]},
                    {
                        "$set": {
                            "connection_request_result": "RPC_COMPLETED"
                            if code == 200
                            else "QUEUED_NOT_VERIFIED",
                            "connection_result_at": now(),
                        }
                    },
                )
            except Exception as exc:
                # Critical ambiguity window: leave DISPATCHING and do not resend on restart.
                await self.db.tasks.update_one(
                    {"_id": task["_id"]},
                    {
                        "$set": {
                            "reason": "Submission outcome unknown: "
                            + type(exc).__name__
                            + ". No automatic resubmission."
                        }
                    },
                )
                return
            task_id = str(response.get("_id", ""))
            await self.db.tasks.update_one(
                {"_id": task["_id"]},
                {
                    "$set": {
                        "acs_task": task_id,
                        "state": "PENDING_INFORM" if code == 202 else "EXECUTING",
                        "response_code": code,
                    }
                },
            )
            if code == 200 and task["request"]["operation"] not in {"firmware", "diagnostic", "reboot"}:
                await self.finish(task, "COMPLETED", "GenieACS NBI returned HTTP 200 (RPC completed)")
            return
        if not task.get("acs_task"):
            return  # Unknown submit outcome: never guess completion or duplicate an operation.
        d = await self.s.device(task["device"])
        faults = await self.acs.get("faults", {"_id": d["acs_id"] + ":task_" + task["acs_task"]}, limit=1)
        if faults:
            return await self.finish(task, "FAILED", "ACS task fault code: " + str(faults[0].get("code")))
        remaining = await self.acs.get("tasks", {"_id": task["acs_task"]}, limit=1)
        if remaining:
            return
        # Task absence alone is not proof: someone might have deleted it from the ACS.
        raw = await self.acs.device(d["acs_id"])
        verified = self.verify(task, raw)
        if verified:
            await self.finish(
                task, "COMPLETED", "Task absent, no task fault, and fresh postcondition observed"
            )
        else:
            await self.db.tasks.update_one(
                {"_id": task["_id"]},
                {
                    "$set": {
                        "state": "EXECUTING",
                        "reason": "ACS task absent; awaiting observable postcondition (absence alone is inconclusive)",
                    }
                },
            )

    def verify(self, task, raw):
        submitted = task.get("submitted_at")
        if not submitted:
            return False
        params = flatten(raw)
        fresh = {p["path"]: p["value"] for p in params if p.get("sample_at") and p["sample_at"] >= submitted}
        op = task["request"]["operation"]
        if op == "set":
            return all(k in fresh and fresh[k] == v for k, v in task["request"]["values"].items())
        if op == "refresh":
            prefix = task["request"].get("object_name", "").rstrip(".")
            return any(not prefix or k == prefix or k.startswith(prefix + ".") for k in fresh)
        if op == "reboot":
            return bool(stamp(raw.get("_lastBoot")) and stamp(raw["_lastBoot"]) >= submitted)
        if op == "diagnostic":
            path = task["request"]["diagnostic"] + ".DiagnosticsState"
            return fresh.get(path) == "Complete"
        if op == "firmware":
            entry = self.cfg.firmware_catalog.get(task["request"]["firmware"], {})
            return any(
                k.endswith("DeviceInfo.SoftwareVersion") and v == entry.get("version")
                for k, v in fresh.items()
            )
        return False

    async def scheduled_report(self):
        if not self.cfg.enable_scheduled_reports:
            return
        local = now().astimezone(ZoneInfo(self.cfg.tz))
        bucket = {
            "daily": local.strftime("%Y-%m-%d"),
            "weekly": local.strftime("%G-W%V"),
            "monthly": local.strftime("%Y-%m"),
        }.get(
            self.cfg.report_schedule, str(int(now().timestamp() // (self.cfg.report_interval_hours * 3600)))
        )
        stats = await self.s.statistics()
        await self.s.event(f"report:{bucket}", "system", "FLEET_REPORT", stats)


def quiet(cfg):
    hour = now().astimezone(ZoneInfo(cfg.tz)).hour
    a, b = cfg.quiet_hours_start, cfg.quiet_hours_end
    return (a <= hour < b) if a < b else (hour >= a or hour < b) if a > b else False
