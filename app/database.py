from datetime import timedelta

from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain import now


async def initialize(db, cfg):
    for key in ("status", "enrollment", "group", "model", "manufacturer", "firmware", "tags", "last_inform"):
        await db.devices.create_index(key)
    await db.devices.create_index("acs_id", unique=True)
    await db.devices.create_index([("enrollment", 1), ("next_sample_at", 1)])
    await db.cooldowns.create_index("until", expireAfterSeconds=0)
    await db.events.create_index("outboxed")
    await db.actions.create_index([("state", 1), ("materialized", 1)])
    await db.tasks.create_index([("state", 1), ("run_at", 1), ("priority", -1)])
    await db.tasks.create_index([("device", 1), ("created_at", -1)])
    await db.notifications.create_index([("state", 1), ("next_at", 1)])
    for name in ("events", "snapshots", "metrics"):
        await db[name].create_index([("device", 1), ("at", -1)])
        await db[name].create_index("at", expireAfterSeconds=cfg.retention_days * 86400)
    await db.audit.create_index("at", expireAfterSeconds=cfg.audit_retention_days * 86400)
    for name in ("actions", "sessions", "limits", "callbacks"):
        await db[name].create_index("expires_at", expireAfterSeconds=0)
    await db.notifications.create_index("finished_at", expireAfterSeconds=cfg.retention_days * 86400)
    await db.tasks.create_index("finished_at", expireAfterSeconds=cfg.retention_days * 86400)
    await db.users.update_one({"_id": cfg.telegram_owner_id}, {"$set": {"role": "OWNER"}}, upsert=True)
    for uid in cfg.admin_ids - {cfg.telegram_owner_id}:
        await db.users.update_one({"_id": uid}, {"$setOnInsert": {"role": "ADMIN"}}, upsert=True)


def connect(cfg):
    return AsyncMongoClient(
        cfg.mongodb_uri.get_secret_value(), tz_aware=True, serverSelectionTimeoutMS=5000, maxPoolSize=50
    )


async def lease(db, key, owner, seconds=90):
    try:
        row = await db.leases.find_one_and_update(
            {"_id": key, "$or": [{"until": {"$lte": now()}}, {"owner": owner}]},
            {"$set": {"owner": owner, "until": now() + timedelta(seconds=seconds)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return bool(row and row["owner"] == owner)
    except DuplicateKeyError:
        return False
