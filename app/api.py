import asyncio
import hmac
import json
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.domain import ROLES, now
from app.reports import render
from app.service import TaskInput

router = APIRouter(prefix="/api")


async def actor(
    request: Request, authorization: str = Header(""), x_telegram_user: int | None = Header(None)
):
    cfg, s = request.app.state.cfg, request.app.state.service
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if hmac.compare_digest(token, cfg.api_token.get_secret_value()):
        uid = cfg.telegram_owner_id
    elif hmac.compare_digest(token, cfg.bot_api_token.get_secret_value()) and x_telegram_user:
        uid = x_telegram_user
    else:
        raise PermissionError("Invalid API credential")
    await s.authorize(uid)
    await s.limit(
        str(uid) + ":worker"
        if request.url.path in {"/api/bot/notifications/claim", "/api/bot/sessions"}
        or request.url.path.endswith("/ack")
        else uid
    )
    return uid


UID = Annotated[int, Depends(actor)]


def service(request):
    return request.app.state.service


def bot_only(request):
    expected = "Bearer " + request.app.state.cfg.bot_api_token.get_secret_value()
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise PermissionError("Bot service credential required")


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[str] = Field(min_length=1, max_length=500)
    task: TaskInput


class Confirm(BaseModel):
    phrase: str = Field(max_length=100)


@router.get("/me")
async def me(request: Request, uid: UID):
    return await service(request).authorize(uid)


@router.get("/devices")
async def devices(
    request: Request,
    uid: UID,
    q: str = "",
    status: str = "",
    group: str = "",
    model: str = "",
    manufacturer: str = "",
    firmware: str = "",
    tag: str = "",
    enrollment: str = "",
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(20, ge=1, le=100),
):
    return await service(request).devices(
        q, status, group, model, manufacturer, firmware, tag, enrollment, offset, limit
    )


@router.get("/devices/{device}")
async def device(request: Request, uid: UID, device: str):
    row = await service(request).device(device)
    row.pop("parameters", None)
    return row


@router.get("/devices/{device}/parameters")
async def parameters(
    request: Request,
    uid: UID,
    device: str,
    q: str = "",
    category: str = "",
    data_type: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    return await service(request).parameters(device, q, category, data_type, offset, limit)


@router.get("/devices/{device}/diagnostics")
@router.get("/devices/{device}/capabilities")
async def capabilities(request: Request, uid: UID, device: str):
    return await service(request).capabilities(device)


@router.post("/devices/{device}/enrollment/{decision}")
async def enrollment(request: Request, uid: UID, device: str, decision: str):
    return await service(request).enrollment(uid, device, decision)


@router.patch("/devices/{device}")
async def edit_device(request: Request, uid: UID, device: str, data: dict):
    return await service(request).edit_device(uid, device, data)


@router.post("/devices/{device}/tags/{tag}")
@router.delete("/devices/{device}/tags/{tag}")
async def tag(request: Request, uid: UID, device: str, tag: str):
    s = service(request)
    await s.authorize(uid, "ADMIN")
    d = await s.device(device)
    if d.get("enrollment") != "APPROVED" or not tag or len(tag) > 100:
        raise ValueError("Approved device and valid tag required")
    await s.audit(uid, "tag", device, {"tag": tag, "remove": request.method == "DELETE"})
    await s.acs.tag(d["acs_id"], tag, request.method == "DELETE")
    await s.db.devices.update_one(
        {"_id": device}, {"$pull" if request.method == "DELETE" else "$addToSet": {"tags": tag}}
    )
    return {"updated": True}


@router.post("/actions")
async def prepare(request: Request, uid: UID, body: Prepare):
    return await service(request).prepare(uid, body.targets, body.task)


@router.post("/actions/{token}/confirm")
async def confirm(request: Request, uid: UID, token: str, body: Confirm):
    return await service(request).confirm(uid, token, body.phrase)


@router.post("/tasks/{task}/cancel")
async def cancel(request: Request, uid: UID, task: str):
    return await service(request).cancel(uid, task)


@router.post("/tasks/{task}/retry")
async def retry(request: Request, uid: UID, task: str):
    return await service(request).retry(uid, task)


@router.get("/tasks")
@router.get("/alerts")
@router.get("/devices/{device}/tasks")
@router.get("/devices/{device}/history")
@router.get("/devices/{device}/metrics")
@router.get("/devices/{device}/snapshots")
async def records(
    request: Request,
    uid: UID,
    device: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    name = request.url.path.rsplit("/", 1)[-1]
    collection = {"alerts": "events", "history": "events"}.get(name, name)
    query = {"device": device} if device else {}
    s = service(request)
    if device:
        await s.device(device)
    rows = (
        await s.db[collection]
        .find(query, {"payload": 0})
        .sort("created_at" if collection == "tasks" else "at", -1)
        .skip(offset)
        .limit(limit)
        .to_list(limit)
    )
    return {"items": rows, "total": await s.db[collection].count_documents(query)}


@router.get("/statistics")
@router.get("/system")
async def statistics(request: Request, uid: UID):
    return await service(request).statistics()


@router.get("/groups")
async def groups(request: Request, uid: UID):
    s = service(request)
    return {"items": await s.db.groups.find({}).limit(100).to_list(100)}


@router.put("/groups/{group}")
async def put_group(request: Request, uid: UID, group: str, data: dict):
    s = service(request)
    await s.authorize(uid, "ADMIN")
    if len(group) > 100 or set(data) - {"thresholds", "active_threshold", "offline_threshold"}:
        raise ValueError("Invalid group settings")
    for key in ("active_threshold", "offline_threshold"):
        if key in data and (type(data[key]) is not int or data[key] < 30):
            raise ValueError("Invalid group threshold")
    if "thresholds" in data and not isinstance(data["thresholds"], dict):
        raise ValueError("thresholds must be a mapping of exact paths to min/max values")
    await s.audit(uid, "group", group, data)
    await s.db.groups.update_one({"_id": group}, {"$set": data}, upsert=True)
    return {"updated": True}


@router.put("/users/{user_id}/{role}")
async def put_user(request: Request, uid: UID, user_id: int, role: str):
    s = service(request)
    await s.authorize(uid, "OWNER")
    if role not in ROLES or role == "OWNER" or user_id == s.cfg.telegram_owner_id:
        raise ValueError("Invalid role update")
    await s.audit(uid, "role", str(user_id), {"role": role})
    await s.db.users.update_one({"_id": user_id}, {"$set": {"role": role, "disabled": False}}, upsert=True)
    return {"updated": True}


@router.delete("/users/{user_id}")
async def disable_user(request: Request, uid: UID, user_id: int):
    s = service(request)
    await s.authorize(uid, "OWNER")
    if user_id == s.cfg.telegram_owner_id:
        raise ValueError("Cannot disable configured owner")
    await s.audit(uid, "disable_user", str(user_id))
    await s.db.users.update_one({"_id": user_id}, {"$set": {"disabled": True}})
    return {"disabled": True}


@router.get("/reports/{format}")
async def report(request: Request, uid: UID, format: str):
    s = service(request)
    rows = await s.db.devices.find({}, {"parameters": 0}).limit(10000).to_list(10000)
    payload, mime = await asyncio.to_thread(render, rows, format)
    return Response(
        payload, media_type=mime, headers={"Content-Disposition": f'attachment; filename="fleet.{format}"'}
    )


@router.get("/backup")
async def backup(request: Request, uid: UID):
    s = service(request)
    await s.authorize(uid, "OWNER")
    await s.audit(uid, "export_backup", "manager")
    # Portable sanitized export; operational database backup instructions are separate.
    out = {}
    for name in ("devices", "groups", "users", "tasks", "events"):
        out[name] = await s.db[name].find({}).limit(10000).to_list(10000)
    return Response(
        json.dumps(out, default=str).encode(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="manager-export.json"'},
    )


@router.post("/bot/callbacks")
async def callback(request: Request, uid: UID, data: dict):
    bot_only(request)
    if len(json.dumps(data)) > 8000:
        raise ValueError("Callback too large")
    return {"token": await service(request).callback(uid, data)}


@router.get("/bot/callbacks/{token}")
async def get_callback(request: Request, uid: UID, token: str):
    bot_only(request)
    return await service(request).get_callback(uid, token)


@router.post("/bot/notifications/claim")
async def notification(request: Request, uid: UID):
    bot_only(request)
    await service(request).authorize(uid, "OWNER")
    return await service(request).claim_notification()


@router.post("/bot/notifications/{notification}/ack")
async def ack(request: Request, uid: UID, notification: str, data: dict):
    bot_only(request)
    await service(request).authorize(uid, "OWNER")
    await service(request).ack_notification(
        notification, data["claim"], data.get("message_id"), data.get("delay", 0)
    )
    return {"ok": True}


@router.post("/bot/sessions")
async def session(request: Request, uid: UID, data: dict):
    bot_only(request)
    return await service(request).session(uid, data["device"], data["chat"], data["message"])


@router.delete("/bot/sessions")
async def stop_session(request: Request, uid: UID):
    bot_only(request)
    await service(request).db.sessions.delete_one({"_id": uid})
    return {"stopped": True}


@router.get("/bot/sessions")
async def sessions(request: Request, uid: UID):
    bot_only(request)
    s = service(request)
    await s.authorize(uid, "OWNER")
    rows = (
        await s.db.sessions.find({"expires_at": {"$gt": now()}, "next_at": {"$lte": now()}})
        .limit(50)
        .to_list(50)
    )
    for row in rows:
        await s.db.sessions.update_one(
            {"_id": row["_id"]}, {"$set": {"next_at": now() + timedelta(seconds=s.cfg.monitor_interval)}}
        )
    return rows


@router.put("/selections/{name}")
async def save_selection(request: Request, uid: UID, name: str, body: dict):
    s = service(request)
    await s.authorize(uid, "OPERATOR")
    targets = body.get("targets", [])
    if not isinstance(targets, list) or not 1 <= len(targets) <= s.cfg.max_bulk_devices or len(name) > 100:
        raise ValueError("Supply an explicit bounded list of target IDs")
    for target in targets:
        await s.device(target)
    key = f"{uid}:{name}"
    await s.db.selections.replace_one(
        {"_id": key},
        {"_id": key, "actor": uid, "name": name, "targets": list(dict.fromkeys(targets))},
        upsert=True,
    )
    return {"saved": name}


@router.get("/selections")
async def selections(request: Request, uid: UID):
    return {"items": await service(request).db.selections.find({"actor": uid}).limit(100).to_list(100)}


@router.post("/selections/{name}/prepare")
async def prepare_selection(request: Request, uid: UID, name: str, task: TaskInput):
    s = service(request)
    row = await s.db.selections.find_one({"_id": f"{uid}:{name}"})
    if not row:
        raise ValueError("Unknown saved selection")
    return await s.prepare(uid, row["targets"], task)


@router.put("/templates/{name}")
async def save_template(request: Request, uid: UID, name: str, task: TaskInput):
    s = service(request)
    await s.authorize(uid, "ADMIN")
    if len(name) > 100 or task.operation not in {"refresh", "set"}:
        raise ValueError("Templates support refresh and allowlisted settings")
    from app.domain import SECRET

    if any(p not in s.cfg.parameter_allowlist or SECRET.search(p) for p in task.values):
        raise PermissionError("Template contains a forbidden parameter")
    await s.audit(uid, "template", name)
    await s.db.templates.replace_one({"_id": name}, {"_id": name, "task": task.model_dump()}, upsert=True)
    return {"saved": name}


@router.get("/templates")
async def templates(request: Request, uid: UID):
    return {"items": await service(request).db.templates.find({}).limit(100).to_list(100)}


@router.post("/templates/{name}/prepare")
async def prepare_template(request: Request, uid: UID, name: str, body: dict):
    s = service(request)
    row = await s.db.templates.find_one({"_id": name})
    if not row:
        raise ValueError("Unknown template")
    return await s.prepare(uid, body.get("targets", []), TaskInput(**row["task"]))


@router.get("/devices/{device}/compare")
async def compare_snapshots(request: Request, uid: UID, device: str):
    s = service(request)
    await s.device(device)
    snapshots = await s.db.snapshots.find({"device": device}).sort("at", -1).limit(2).to_list(2)
    if len(snapshots) < 2:
        return {"changes": [], "detail": "Two historical snapshots are required"}
    before = {p["path"]: p["value"] for p in snapshots[1]["parameters"]}
    after = {p["path"]: p["value"] for p in snapshots[0]["parameters"]}
    return {
        "before_at": snapshots[1]["at"],
        "after_at": snapshots[0]["at"],
        "changes": [
            {"path": p, "before": before.get(p), "after": after.get(p)}
            for p in sorted(set(before) | set(after))
            if before.get(p) != after.get(p)
        ],
    }


@router.get("/devices/{device}/tree")
async def parameter_tree(
    request: Request,
    uid: UID,
    device: str,
    prefix: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    d = await service(request).device(device)
    prefix = prefix.rstrip(".")
    root = prefix + "." if prefix else ""
    children = {}
    for p in d.get("parameters", []):
        if p["path"].startswith(root):
            tail = p["path"][len(root) :]
            component = tail.split(".")[0]
            children[root + component] = {"path": root + component, "object": "." in tail}
    rows = sorted(children.values(), key=lambda x: x["path"])
    return {"items": rows[offset : offset + limit], "total": len(rows)}


@router.get("/actions/{token}")
async def batch_status(request: Request, uid: UID, token: str):
    s = service(request)
    tasks = (
        await s.db.tasks.find({"action": token}).limit(s.cfg.max_bulk_devices).to_list(s.cfg.max_bulk_devices)
    )
    from collections import Counter

    return {
        "tasks": tasks,
        "counts": dict(Counter(t["state"] for t in tasks)),
        "completed": sum(t["state"] == "COMPLETED" for t in tasks),
        "total": len(tasks),
    }


@router.post("/actions/{token}/cancel")
async def batch_cancel(request: Request, uid: UID, token: str):
    s = service(request)
    await s.authorize(uid, "OPERATOR")
    tasks = (
        await s.db.tasks.find({"action": token, "state": "QUEUED"})
        .limit(s.cfg.max_bulk_devices)
        .to_list(s.cfg.max_bulk_devices)
    )
    results = []
    for t in tasks:
        try:
            results.append({"task": t["_id"], **await s.cancel(uid, t["_id"])})
        except ValueError:
            results.append({"task": t["_id"], "cancelled": False})
    return {"results": results, "note": "Already dispatched tasks are not cancelled"}


@router.get("/analytics")
async def analytics(request: Request, uid: UID, days: int = Query(7, ge=1, le=365)):
    s = service(request)
    since = now() - timedelta(days=days)
    from collections import Counter

    result = {}
    for field in ("model", "manufacturer", "firmware", "status"):
        # Aggregates stay on the database, not a full parameter scan in the API process.
        cursor = s.db.devices.aggregate(
            [
                {"$group": {"_id": "$" + field, "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 100},
            ]
        )
        import inspect

        if inspect.isawaitable(cursor):
            cursor = await cursor
        result[field] = await cursor.to_list(100)
    events = await s.db.events.find({"at": {"$gte": since}}, {"kind": 1}).limit(100000).to_list(100000)
    tasks = await s.db.tasks.find({"created_at": {"$gte": since}}, {"state": 1}).limit(100000).to_list(100000)
    counts = dict(Counter(t["state"] for t in tasks))
    result.update(
        events=dict(Counter(e["kind"] for e in events)),
        tasks=counts,
        days=days,
        failure_rate=(counts.get("FAILED", 0) / len(tasks)) if tasks else None,
        note="Retained records only; event/task analysis bounded to 100000 records",
    )
    return result


@router.get("/devices/{device}/export")
async def export_parameters(request: Request, uid: UID, device: str):
    d = await service(request).device(device)
    return Response(
        json.dumps(
            {"device": device, "collected_at": d.get("sampled_at"), "parameters": d.get("parameters", [])},
            default=str,
        ).encode(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="parameters-{device}.json"'},
    )


@router.get("/devices/{device}/chart")
async def traffic_chart(
    request: Request, uid: UID, device: str, path: str, days: int = Query(1, ge=1, le=30)
):
    from app.reports import chart

    s = service(request)
    await s.device(device)
    rows = (
        await s.db.metrics.find({"device": device, "at": {"$gte": now() - timedelta(days=days)}})
        .sort("at", 1)
        .limit(10000)
        .to_list(10000)
    )
    payload = await asyncio.to_thread(chart, rows, path)
    return Response(
        payload, media_type="image/png", headers={"Content-Disposition": 'attachment; filename="history.png"'}
    )
