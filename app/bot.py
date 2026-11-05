import asyncio
import io
import json
import logging
import re
import shlex

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.workers import quiet

log = logging.getLogger(__name__)
HELP = """GenieACS Control Center
/dashboard — fleet summary
/devices, /online, /offline, /stale, /new
/search text — find ID, serial, name or model
/device ID — inspect and manage
/parameters ID [search] — reported parameters
/monitor ID — cached measurements and auto-refresh
/traffic, /network, /wifi, /fiber, /cellular ID
/tasks [ID], /faults ID, /history ID, /alerts
/groups — groups; /tags ID [tag] — list/add tag
/diagnostics ID — verified supported controls
/bulk ID,ID — preview a bounded refresh batch
/prepare ID JSON — preview advanced task
/confirm TOKEN CONFIRM N — confirm exact targets
/cancel TASK — cancel locally queued work
/retry TASK — retry a known ACS fault
/name ID label; /group ID label; /mute ID on|off
/report json|csv|pdf|png; /export; /backup
/stats, /status, /health, /settings
/chart ID PARAMETER_PATH — measured history PNG
/exportparams ID — redacted parameter snapshot
/select NAME model=... — save an exact filtered selection
/batch NAME JSON — preview a saved selection task
/role USER_ID VIEWER|OPERATOR|ADMIN (owner)
/revoke USER_ID (owner)

Task JSON example:
/prepare ID {"operation":"refresh","object_name":"Device.DeviceInfo"}
All status is inferred from Informs. Refreshing the display does not request new CPE telemetry.
"""


class API:
    def __init__(self, cfg, transport=None):
        url = cfg.core_api_url if cfg.deployment_mode == "hybrid" else f"http://127.0.0.1:{cfg.app_port}"
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/",
            timeout=30,
            transport=transport,
            headers={"Authorization": "Bearer " + cfg.bot_api_token.get_secret_value()},
        )

    async def call(self, uid, path, method="GET", data=None, params=None, binary=False):
        r = await self.http.request(
            method, path, headers={"X-Telegram-User": str(uid)}, json=data, params=params
        )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", "Request failed")
            except ValueError:
                detail = "Backend unavailable"
            raise ValueError(str(detail)[:250])
        return r.content if binary else r.json()


class Bot:
    def __init__(self, cfg, api=None):
        self.cfg, self.api = cfg, api or API(cfg)
        self.client = Client(
            "genieacs_manager",
            api_id=cfg.telegram_api_id,
            api_hash=cfg.telegram_api_hash.get_secret_value(),
            bot_token=cfg.telegram_bot_token.get_secret_value(),
            in_memory=True,
            workers=8,
            parse_mode=ParseMode.DISABLED,
        )
        self.client.add_handler(MessageHandler(self.message, filters.private & filters.text & ~filters.bot))
        self.client.add_handler(CallbackQueryHandler(self.callback))
        self.send_lock = asyncio.Lock()

    async def run(self):
        async with self.client:
            await asyncio.gather(self.notifications(), self.monitors())

    async def keyboard(self, uid, rows):
        buttons = []
        for row in rows:
            line = []
            for label, data in row:
                result = await self.api.call(uid, "bot/callbacks", "POST", data)
                line.append(InlineKeyboardButton(label, callback_data="c:" + result["token"]))
            buttons.append(line)
        return InlineKeyboardMarkup(buttons)

    async def send(self, chat, text, markup=None, edit=None):
        # Serialize outbound UI sends and avoid unbounded FloodWait sleeps in callback handlers.
        async with self.send_lock:
            if edit:
                try:
                    return await self.client.edit_message_text(chat, edit, text[:4000], reply_markup=markup)
                except MessageNotModified:
                    return None
            return await self.client.send_message(chat, text[:4000], reply_markup=markup)

    async def show(self, uid, data, edit=None):
        view = data.get("view", "dashboard")
        device = data.get("device")
        offset = int(data.get("offset", 0))
        rows = []
        if view == "dashboard":
            stats = await self.api.call(uid, "statistics")
            text = "📡 ACS CONTROL CENTER\n" + json.dumps(stats, indent=2, ensure_ascii=False)
            rows = [
                [("Devices", {"view": "devices"}), ("New", {"view": "devices", "enrollment": "PENDING"})],
                [(x.title(), {"view": "devices", "status": x}) for x in ("ACTIVE", "STALE", "OFFLINE")],
                [("Tasks", {"view": "tasks"}), ("Alerts", {"view": "alerts"})],
            ]
        elif view == "devices":
            query = {
                k: data[k]
                for k in ("q", "status", "group", "model", "manufacturer", "firmware", "tag", "enrollment")
                if data.get(k)
            }
            result = await self.api.call(uid, "devices", params={**query, "offset": offset, "limit": 8})
            text = f"Devices: {result['total']} | page {offset // 8 + 1}\nActivity inferred from last Inform."
            rows = [
                [(f"{d['status']} · {d['name'][:45]}", {"view": "device", "device": d["_id"]})]
                for d in result["items"]
            ]
            nav = []
            if offset:
                nav.append(("Previous", {**data, "offset": max(0, offset - 8)}))
            if offset + 8 < result["total"]:
                nav.append(("Next", {**data, "offset": offset + 8}))
            if nav:
                rows.append(nav)
        elif view in ("device", "monitor"):
            d = await self.api.call(uid, f"devices/{device}")
            text = (
                f"📡 {d['name']}\nID: {d['_id']}\nACS ID: {d['acs_id']}\n"
                f"Model: {d.get('model')} | Firmware: {d.get('firmware')}\n"
                f"Status: {d['status']} (inferred)\nEnrollment: {d['enrollment']}\n"
                f"Last Inform: {d.get('last_inform')}\nCache collected: {d.get('sampled_at', 'not yet')}\n"
                f"Group: {d.get('group', '')} | Tags: {', '.join(d.get('tags', []))}\n"
            )
            if view == "monitor":
                metric = await self.api.call(uid, f"devices/{device}/metrics", params={"limit": 1})
                for m in (metric["items"][0]["measurements"] if metric["items"] else [])[:10]:
                    value = f"{m['bps'] / 1e6:.3f} Mbps" if m.get("bps") is not None else str(m["value"])
                    text += f"{m['path']}: {value} @ {m['sample_at']}\n"
                rows.append(
                    [
                        ("Auto Refresh", {"action": "monitor", "device": device}),
                        ("Stop Monitor", {"action": "stop"}),
                    ]
                )
            if d["enrollment"] != "APPROVED":
                rows.append(
                    [
                        ("Approve", {"action": "enroll", "device": device, "decision": "APPROVED"}),
                        ("Reject", {"action": "enroll", "device": device, "decision": "REJECTED"}),
                    ]
                )
            rows += [
                [
                    ("Parameters", {"view": "parameters", "device": device}),
                    ("Monitor", {"view": "monitor", "device": device}),
                ],
                [
                    (
                        "Request Refresh",
                        {"action": "prepare", "targets": [device], "task": {"operation": "refresh"}},
                    ),
                    ("Management", {"view": "controls", "device": device}),
                ],
                [
                    ("History", {"view": "history", "device": device}),
                    ("Tasks", {"view": "tasks", "device": device}),
                ],
            ]
        elif view == "controls":
            caps = await self.api.call(uid, f"devices/{device}/capabilities")
            text = (
                "Verified capabilities\n"
                + json.dumps(caps, indent=2)
                + "\nUse /prepare ID JSON for configuration, diagnostics and firmware."
            )
            rows = [
                [(op, {"action": "prepare", "targets": [device], "task": {"operation": op}})]
                for op in caps["operations"]
                if op in ("reboot", "factory_reset")
            ]
        elif view == "tree":
            prefix = data.get("prefix", "")
            result = await self.api.call(
                uid, f"devices/{device}/tree", params={"prefix": prefix, "offset": offset, "limit": 8}
            )
            text = "Parameter tree: " + (prefix or "root")
            for node in result["items"]:
                dest = (
                    {"view": "tree", "prefix": node["path"], "device": device}
                    if node["object"]
                    else {"view": "parameters", "q": node["path"], "device": device}
                )
                rows.append([(("📁 " if node["object"] else "📄 ") + node["path"].split(".")[-1], dest)])
            if prefix:
                rows.append(
                    [("Parent", {"view": "tree", "device": device, "prefix": prefix.rpartition(".")[0]})]
                )
            if offset:
                rows.append([("Previous", {**data, "offset": max(0, offset - 8)})])
            if offset + 8 < result["total"]:
                rows.append([("Next", {**data, "offset": offset + 8})])
        elif view == "parameters":
            result = await self.api.call(
                uid,
                f"devices/{device}/parameters",
                params={
                    "q": data.get("q", ""),
                    "category": data.get("category", ""),
                    "offset": offset,
                    "limit": 6,
                },
            )
            rows.append([("Browse tree", {"view": "tree", "device": device})])
            text = f"Reported parameters: {result['total']}\n"
            for p in result["items"]:
                text += f"\n{p['path']}\n{str(p['value'])[:150]} | {p.get('type')} | writable={p['writable']}\nSample: {p['sample_at']}\n"
            if not result["items"]:
                text += "No matching measurements reported. Approve the device, then wait for cache collection or request a supported refresh."
            nav = []
            if offset:
                nav.append(("Previous", {**data, "offset": max(0, offset - 6)}))
            if offset + 6 < result["total"]:
                nav.append(("Next", {**data, "offset": offset + 6}))
            if nav:
                rows.append(nav)
        elif view == "faults":
            d = await self.api.call(uid, f"devices/{device}")
            text = "ACS faults (cached)\n" + json.dumps(d.get("faults", []), indent=2)
        elif view in ("tasks", "alerts", "history", "groups"):
            path = f"devices/{device}/{view}" if device and view in ("tasks", "history") else view
            result = await self.api.call(uid, path, params={"offset": offset, "limit": 5})
            text = view.title() + "\n" + json.dumps(result, indent=2, default=str)[:3400]
            if view == "tasks":
                rows = [
                    [("Cancel " + t["_id"][:8], {"action": "cancel", "task": t["_id"]})]
                    for t in result["items"]
                    if t["state"] == "QUEUED"
                ]
            if result.get("total", 0) > offset + 5:
                rows.append([("Next", {**data, "offset": offset + 5})])
            if offset:
                rows.append([("Previous", {**data, "offset": max(0, offset - 5)})])
        else:
            raise ValueError("Unknown view")
        rows += [[("Refresh view", data), ("Dashboard", {"view": "dashboard"})]]
        markup = await self.keyboard(uid, rows)
        return await self.send(uid, text, markup, edit)

    async def preview(self, uid, targets, task):
        result = await self.api.call(uid, "actions", "POST", {"targets": targets, "task": task})
        preview = json.dumps(result["preview"], indent=2, ensure_ascii=False)
        if len(preview) > 2800:
            document = io.BytesIO(preview.encode())
            document.name = "exact-target-preview.json"
            await self.client.send_document(uid, document, caption="Review every target before confirming.")
            preview = f"Exact preview attached: {len(result['preview'])} targets."
        text = (
            result["impact"]
            + "\n"
            + preview
            + "\nExpires: "
            + result["expires_at"]
            + f"\n\nConfirm with:\n/confirm {result['token']} {result['phrase']}"
        )
        # Disruptive and bulk operations require a typed phrase, not a one-tap callback.
        markup = None
        if task.get("operation") == "refresh" and len(targets) == 1:
            markup = await self.keyboard(
                uid,
                [
                    [
                        (
                            "Confirm refresh",
                            {"action": "confirm", "token": result["token"], "phrase": result["phrase"]},
                        )
                    ]
                ],
            )
        await self.send(uid, text, markup)

    async def message(self, client: Client, message: Message):
        if (
            not message.from_user
            or message.chat.id != message.from_user.id
            or not message.text.startswith("/")
        ):
            return
        uid = message.from_user.id
        try:
            await self.api.call(uid, "me")
            command, _, arg = message.text.partition(" ")
            command = command.split("@")[0][1:].lower()
            arg = arg.strip()
            device, _, rest = arg.partition(" ")
            if command in ("start", "help"):
                await self.send(uid, HELP)
            elif command in ("dashboard", "status", "stats", "health"):
                await self.show(uid, {"view": "dashboard"})
            elif command in ("devices", "online", "offline", "stale", "new", "search"):
                data = {"view": "devices"}
                if command == "devices" and arg:
                    for term in shlex.split(arg):
                        key, sep, value = term.partition("=")
                        if not sep or key not in {
                            "group",
                            "model",
                            "manufacturer",
                            "firmware",
                            "tag",
                            "status",
                            "enrollment",
                        }:
                            raise ValueError("Use /devices model=... group=... tag=...")
                        data[key] = value
                if command in ("online", "offline", "stale"):
                    data["status"] = {"online": "ACTIVE", "offline": "OFFLINE", "stale": "STALE"}[command]
                if command == "new":
                    data["enrollment"] = "PENDING"
                if command == "search":
                    data["q"] = arg
                await self.show(uid, data)
            elif command in (
                "device",
                "monitor",
                "parameters",
                "traffic",
                "network",
                "wifi",
                "fiber",
                "cellular",
                "diagnostics",
                "faults",
                "history",
            ):
                if not re.fullmatch(r"[a-f0-9]{24}", device):
                    raise ValueError("Supply the manager device ID shown by /device")
                data = {"view": command, "device": device}
                if command in ("traffic", "network", "wifi", "fiber", "cellular"):
                    data.update(view="parameters", category=command)
                if command == "parameters":
                    data["q"] = rest
                if command == "diagnostics":
                    data["view"] = "controls"
                await self.show(uid, data)
            elif command in ("tasks", "alerts", "groups"):
                await self.show(
                    uid, {"view": command, **({"device": device} if device and command == "tasks" else {})}
                )
            elif command == "bulk":
                await self.preview(
                    uid, [x.strip() for x in arg.split(",") if x.strip()], {"operation": "refresh"}
                )
            elif command == "prepare":
                await self.preview(uid, [device], json.loads(rest))
            elif command == "confirm":
                result = await self.api.call(uid, f"actions/{device}/confirm", "POST", {"phrase": rest})
                await self.send(uid, "Queued task IDs:\n" + "\n".join(result["tasks"]))
            elif command in ("cancel", "retry"):
                await self.send(uid, str(await self.api.call(uid, f"tasks/{device}/{command}", "POST")))
            elif command in ("name", "group", "mute"):
                field = {"name": "name", "group": "group", "mute": "muted"}[command]
                if command == "mute" and rest not in ("on", "off"):
                    raise ValueError("Use /mute ID on|off")
                await self.api.call(
                    uid, f"devices/{device}", "PATCH", {field: rest == "on" if command == "mute" else rest}
                )
                await self.show(uid, {"view": "device", "device": device})
            elif command == "tags":
                if rest:
                    from urllib.parse import quote

                    await self.api.call(uid, f"devices/{device}/tags/{quote(rest, safe='')}", "POST")
                await self.show(uid, {"view": "device", "device": device})
            elif command in ("report", "export", "backup"):
                fmt = arg if arg in ("json", "csv", "pdf", "png") else "json"
                payload = await self.api.call(
                    uid, "backup" if command == "backup" else "reports/" + fmt, binary=True
                )
                doc = io.BytesIO(payload)
                doc.name = "manager-export.json" if command == "backup" else "fleet." + fmt
                await self.client.send_document(uid, doc)
            elif command == "exportparams":
                payload = await self.api.call(uid, f"devices/{device}/export", binary=True)
                doc = io.BytesIO(payload)
                doc.name = "parameters.json"
                await self.client.send_document(uid, doc)
            elif command == "chart":
                payload = await self.api.call(
                    uid, f"devices/{device}/chart", params={"path": rest}, binary=True
                )
                doc = io.BytesIO(payload)
                doc.name = "history.png"
                await self.client.send_document(uid, doc)
            elif command == "select":
                query = {}
                for term in shlex.split(rest):
                    key, sep, value = term.partition("=")
                    if not sep or key not in {"group", "model", "manufacturer", "firmware", "tag", "status"}:
                        raise ValueError("Selection needs explicit filters")
                    query[key] = value
                if not query:
                    raise ValueError("Selection needs at least one filter")
                selected = await self.api.call(uid, "devices", params={**query, "limit": 100})
                if selected["total"] > len(selected["items"]):
                    raise ValueError("Too many devices; narrow the filters")
                from urllib.parse import quote

                await self.api.call(
                    uid,
                    "selections/" + quote(device, safe=""),
                    "PUT",
                    {"targets": [d["_id"] for d in selected["items"]]},
                )
                await self.send(
                    uid,
                    f"Saved {selected['total']} exact targets as {device}. Use /batch NAME JSON to preview.",
                )
            elif command == "batch":
                result = await self.api.call(uid, "selections")
                selection = next((x for x in result["items"] if x["name"] == device), None)
                if not selection:
                    raise ValueError("Unknown selection")
                await self.preview(uid, selection["targets"], json.loads(rest))
            elif command in ("role", "revoke"):
                path = f"users/{int(device)}" + ("/" + rest.upper() if command == "role" else "")
                await self.api.call(uid, path, "PUT" if command == "role" else "DELETE")
                await self.send(uid, "User access updated.")
            elif command == "settings":
                await self.send(
                    uid,
                    "Settings are configured through environment variables and the protected API.\n"
                    "Use /name, /group, /mute and /role for device and user administration.\n"
                    "Telemetry is cached; no unsupported hardware readings are synthesized.",
                )
            else:
                await self.send(uid, HELP)
        except FloodWait as exc:
            log.warning("telegram_flood_wait", extra={"seconds": exc.value})
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            # Deliberately generic: don't echo credentials or malformed user input.
            try:
                await self.send(
                    uid,
                    "Request rejected or backend unavailable. Check arguments, role, enrollment and /help.",
                )
            except RPCError:
                pass

    async def callback(self, client: Client, query: CallbackQuery):
        if not query.from_user or not query.message or query.message.chat.id != query.from_user.id:
            await query.answer("Private administration only", show_alert=True)
            return
        uid = query.from_user.id
        try:
            if not re.fullmatch(r"c:[A-Za-z0-9_-]{22}", query.data or ""):
                raise ValueError("Invalid callback")
            data = await self.api.call(uid, "bot/callbacks/" + query.data[2:])
            await query.answer()
            action = data.get("action")
            if not action:
                await self.show(uid, data, query.message.id)
            elif action == "prepare":
                await self.preview(uid, data["targets"], data["task"])
            elif action == "confirm":
                r = await self.api.call(
                    uid, f"actions/{data['token']}/confirm", "POST", {"phrase": data["phrase"]}
                )
                await self.send(uid, "Queued: " + ", ".join(r["tasks"]))
            elif action == "enroll":
                await self.api.call(uid, f"devices/{data['device']}/enrollment/{data['decision']}", "POST")
                await self.show(uid, {"view": "device", "device": data["device"]}, query.message.id)
            elif action == "monitor":
                await self.api.call(
                    uid,
                    "bot/sessions",
                    "POST",
                    {"device": data["device"], "chat": uid, "message": query.message.id},
                )
                await self.send(uid, "Auto-refresh enabled for this cached view. It expires automatically.")
            elif action == "stop":
                await self.api.call(uid, "bot/sessions", "DELETE")
                await self.send(uid, "Monitor stopped.")
            elif action == "cancel":
                await self.api.call(uid, f"tasks/{data['task']}/cancel", "POST")
                await self.send(uid, "Queued task cancelled.")
        except (ValueError, KeyError, httpx.HTTPError, RPCError):
            try:
                await query.answer("Rejected, expired, rate-limited or unavailable", show_alert=True)
            except RPCError:
                pass

    async def notifications(self):
        while True:
            try:
                if not quiet(self.cfg):
                    n = await self.api.call(self.cfg.telegram_owner_id, "bot/notifications/claim", "POST")
                    if n:
                        # Recheck recipient authorization immediately before exposing any data.
                        await self.api.call(n["user"], "me")
                        markup = None
                        if re.fullmatch(r"[a-f0-9]{24}", n["device"]):
                            markup = await self.keyboard(
                                n["user"], [[("Device details", {"view": "device", "device": n["device"]})]]
                            )
                        delay, msg_id = 0, None
                        try:
                            msg = await self.send(n["user"], n["text"], markup)
                            msg_id = msg.id
                        except FloodWait as exc:
                            delay = exc.value + 1
                        except RPCError:
                            delay = min(3600, 30 * 2 ** min(n["attempts"], 7))
                        await self.api.call(
                            self.cfg.telegram_owner_id,
                            f"bot/notifications/{n['_id']}/ack",
                            "POST",
                            {"claim": n["claim"], "message_id": msg_id, "delay": delay},
                        )
            except (ValueError, httpx.HTTPError, RPCError):
                log.warning("notification_delivery_unavailable")
            await asyncio.sleep(2)

    async def monitors(self):
        while True:
            try:
                sessions = await self.api.call(self.cfg.telegram_owner_id, "bot/sessions")
                for row in sessions:
                    try:
                        await self.show(
                            row["_id"], {"view": "monitor", "device": row["device"]}, row["message"]
                        )
                    except (ValueError, httpx.HTTPError, RPCError):
                        # A removed message/session doesn't block other administrators.
                        pass
            except (ValueError, httpx.HTTPError):
                log.warning("monitor_backend_unavailable")
            await asyncio.sleep(10)
