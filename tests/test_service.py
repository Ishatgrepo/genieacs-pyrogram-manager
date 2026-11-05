import asyncio
from datetime import timedelta
import pytest
from app.domain import now
from app.service import Service, TaskInput
from app.workers import Workers
from conftest import sample


async def approved(setup):
    s, w, acs, db, cfg = setup
    device = await w.ingest(acs.raw, detailed=True)
    await s.enrollment(1, device, "APPROVED")
    return device


async def queued(setup, operation="refresh"):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    action = await s.prepare(1, [device], TaskInput(operation=operation))
    result = await s.confirm(1, action["token"], "CONFIRM 1")
    return await db.tasks.find_one({"_id": result["tasks"][0]})


async def test_new_device_dedup_restart(setup):
    s, w, acs, db, cfg = setup
    for _ in range(3):
        await w.ingest(acs.raw)
        await s.outbox()
    assert await db.events.count_documents({"kind": "NEW_DEVICE"}) == 1
    assert await db.notifications.count_documents({"kind": "NEW_DEVICE"}) == 2
    restarted = Service(db, cfg, acs)
    await Workers(restarted).ingest(acs.raw)
    await restarted.outbox()
    assert await db.notifications.count_documents({"kind": "NEW_DEVICE"}) == 2


async def test_100_devices_search_filter_pagination(setup):
    s, w, acs, db, cfg = setup
    acs.rows = [sample(str(i).zfill(3)) for i in range(120)]
    await w.discover()
    assert (await s.devices())["total"] == 120
    page1, page2 = await asyncio.gather(s.devices(limit=20), s.devices(offset=20, limit=20))
    assert not ({d["_id"] for d in page1["items"]} & {d["_id"] for d in page2["items"]})
    assert (await s.devices(q="Test-119"))["total"] == 1
    assert (await s.devices(model="Test", enrollment="PENDING"))["total"] == 120


async def test_authorization_enrollment_allowlist(setup):
    s, w, acs, db, cfg = setup
    device = await w.ingest(acs.raw, detailed=True)
    with pytest.raises(PermissionError):
        await s.prepare(999, [device], TaskInput(operation="refresh"))
    with pytest.raises(PermissionError):
        await s.prepare(1, [device], TaskInput(operation="refresh"))
    await s.enrollment(1, device, "APPROVED")
    with pytest.raises(PermissionError):
        await s.prepare(1, [device], TaskInput(operation="reboot"))
    cfg.allow_high_risk_actions = True
    with pytest.raises(PermissionError):
        await s.prepare(1, [device], TaskInput(operation="set", values={"Device.WiFi.SSID.1.Password": "x"}))
    a = await s.prepare(1, [device], TaskInput(operation="set", values={"Device.WiFi.SSID.1.SSID": "new"}))
    assert a["preview"][0]["payload"]["name"] == "setParameterValues"


async def test_confirmation_binding_replay_and_revocation(setup):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    action = await s.prepare(1, [device], TaskInput(operation="refresh"))
    with pytest.raises(ValueError):
        await s.confirm(2, action["token"], "CONFIRM 1")
    with pytest.raises(ValueError):
        await s.confirm(1, action["token"], "CONFIRM 2")
    first = await s.confirm(1, action["token"], "CONFIRM 1")
    assert first == await s.confirm(1, action["token"], "CONFIRM 1")
    assert await db.tasks.count_documents({}) == 1
    await s.enrollment(1, device, "REJECTED")
    await w.process_task(await db.tasks.find_one({"_id": first["tasks"][0]}))
    assert not acs.posted


async def test_202_is_pending_and_disappearance_not_success(setup):
    s, w, acs, db, cfg = setup
    t = await queued(setup)
    await w.process_task(t)
    t = await db.tasks.find_one({"_id": t["_id"]})
    assert t["state"] == "PENDING_INFORM"
    acs.pending = False
    await w.process_task(t)
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "EXECUTING"
    acs.raw = sample(at=now() + timedelta(seconds=1))
    await w.process_task(await db.tasks.find_one({"_id": t["_id"]}))
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "COMPLETED"


async def test_200_completion(setup):
    s, w, acs, db, cfg = setup
    t = await queued(setup)
    acs.code = 200
    await w.process_task(t)
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "COMPLETED"


async def test_submission_timeout_never_resends_after_restart(setup):
    s, w, acs, db, cfg = setup
    t = await queued(setup)
    acs.error = True
    await w.process_task(t)
    restarted = Workers(Service(db, cfg, acs))
    await restarted.process_task(await db.tasks.find_one({"_id": t["_id"]}))
    assert len(acs.posted) == 1
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "DISPATCHING"


async def test_fault_and_explicit_retry(setup):
    s, w, acs, db, cfg = setup
    t = await queued(setup)
    await w.process_task(t)
    acs.fault = True
    await w.process_task(await db.tasks.find_one({"_id": t["_id"]}))
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "FAILED"
    await s.retry(1, t["_id"])
    assert acs.retries == ["task1"]
    assert len(acs.posted) == 1


async def test_per_device_lock_and_cancellation(setup):
    s, w, acs, db, cfg = setup
    t1 = await queued(setup)
    t2 = await queued(setup)
    await w.process_task(t1)
    await w.process_task(t2)
    assert len(acs.posted) == 1
    await s.cancel(1, t2["_id"])
    with pytest.raises(ValueError):
        await s.cancel(1, t1["_id"])


async def test_offline_alert_dedup(setup):
    s, w, acs, db, cfg = setup
    await w.ingest(sample(at=now() - timedelta(hours=2)))
    await w.statuses()
    await w.statuses()
    await s.outbox()
    assert await db.events.count_documents({"kind": "STATUS_OFFLINE"}) == 1


async def test_notification_lease_and_callback_binding(setup):
    s, w, acs, db, cfg = setup
    await w.ingest(acs.raw)
    await s.outbox()
    n = await s.claim_notification()
    await s.ack_notification(n["_id"], "incorrect", message_id=3)
    assert (await db.notifications.find_one({"_id": n["_id"]}))["state"] == "SENDING"
    await s.ack_notification(n["_id"], n["claim"], message_id=3)
    assert (await db.notifications.find_one({"_id": n["_id"]}))["state"] == "SENT"
    token = await s.callback(1, {"view": "devices"})
    with pytest.raises(PermissionError):
        await s.get_callback(2, token)
    assert (await s.get_callback(1, token))["view"] == "devices"


async def test_sample_timestamp_and_counter_history(setup):
    s, w, acs, db, cfg = setup
    t = now() - timedelta(seconds=20)
    device = await w.ingest(sample(at=t, counter=100), detailed=True)
    await w.ingest(sample(at=t + timedelta(seconds=10), counter=200), detailed=True)
    metrics = await db.metrics.find({"device": device}).sort("at", -1).to_list(10)
    rates = [p["bps"] for m in metrics for p in m["measurements"] if p.get("bps") is not None]
    assert rates[0] == pytest.approx(80, rel=0.0001)
    assert "never-store-me" not in str(await db.devices.find_one({"_id": device}))


async def test_threshold_recovery(setup):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    await s.edit_device(1, device, {"thresholds": {"Device.DeviceInfo.UpTime": {"max": 50}}})
    await w.ingest(sample(), detailed=True)
    raw = sample()
    raw["Device"]["DeviceInfo"]["UpTime"]["_value"] = 20
    await w.ingest(raw, detailed=True)
    assert await db.events.count_documents({"kind": "THRESHOLD_ALARM"}) == 1
    assert await db.events.count_documents({"kind": "THRESHOLD_RECOVERED"}) == 1


async def test_confirmed_batch_materializes_after_crash(setup):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    action = await s.prepare(1, [device], TaskInput(operation="refresh"))
    # Simulate process death after confirmed intent but before inserting any tasks.
    await db.actions.update_one(
        {"_id": action["token"]}, {"$set": {"state": "CONFIRMED", "materialized": False, "run_at": now()}}
    )
    restarted = Service(db, cfg, acs)
    await restarted.outbox()
    assert await db.tasks.count_documents({"action": action["token"]}) == 1
    await restarted.outbox()
    assert await db.tasks.count_documents({"action": action["token"]}) == 1


async def test_cooldown_retains_history(setup):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    await s.event("alarm1", device, "CUSTOM", {"value": 1})
    await s.outbox()
    await s.event("alarm2", device, "CUSTOM", {"value": 2})
    await s.outbox()
    assert await db.events.count_documents({"kind": "CUSTOM"}) == 2
    assert await db.notifications.count_documents({"kind": "CUSTOM"}) == 2  # two admins, one each


async def test_expired_confirmation_and_callback(setup):
    s, w, acs, db, cfg = setup
    device = await approved(setup)
    action = await s.prepare(1, [device], TaskInput(operation="refresh"))
    await db.actions.update_one(
        {"_id": action["token"]}, {"$set": {"expires_at": now() - timedelta(seconds=1)}}
    )
    with pytest.raises(ValueError):
        await s.confirm(1, action["token"], "CONFIRM 1")
    token = await s.callback(1, {"view": "dashboard"})
    await db.callbacks.update_one({"_id": token}, {"$set": {"expires_at": now() - timedelta(seconds=1)}})
    with pytest.raises(PermissionError):
        await s.get_callback(1, token)


async def test_scheduled_monthly_report_dedup(setup):
    s, w, acs, db, cfg = setup
    cfg.enable_scheduled_reports = True
    cfg.report_schedule = "monthly"
    await w.scheduled_report()
    await w.scheduled_report()
    assert await db.events.count_documents({"kind": "FLEET_REPORT"}) == 1


async def test_task_deadline(setup):
    s, w, acs, db, cfg = setup
    t = await queued(setup)
    t["deadline"] = now() - timedelta(seconds=1)
    await w.process_task(t)
    assert (await db.tasks.find_one({"_id": t["_id"]}))["state"] == "TIMED_OUT"
    assert not acs.posted
