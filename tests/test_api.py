import httpx
import pytest
from app.main import create_app
from app.reports import render
from app.bot import API
from app.database import lease
from app.config import Settings


async def client_for(setup):
    s, w, acs, db, cfg = setup
    app = create_app(cfg, db, acs)
    app.state.cfg, app.state.service = cfg, s
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_api_auth_and_private_bot_routes(setup):
    s, w, acs, db, cfg = setup
    app, client = await client_for(setup)
    async with client:
        assert (await client.get("/api/devices")).status_code == 403
        headers = {"Authorization": "Bearer " + "b" * 40, "X-Telegram-User": "999"}
        assert (await client.get("/api/devices", headers=headers)).status_code == 403
        headers["X-Telegram-User"] = "1"
        assert (await client.get("/api/devices", headers=headers)).status_code == 200
        assert (
            await client.post("/api/bot/callbacks", headers=headers, json={"view": "dashboard"})
        ).status_code == 200
        assert (await client.get("/api/devices?limit=100000", headers=headers)).status_code == 422
        assert (await client.get("/api/devices", headers=headers)).headers["Cache-Control"] == "no-store"
        assert (await client.post("/api/actions", headers=headers, content=b"x" * 70000)).status_code == 413


async def test_api_roundtrip_task_and_role(setup):
    s, w, acs, db, cfg = setup
    device = await w.ingest(acs.raw, detailed=True)
    app, client = await client_for(setup)
    async with client:
        headers = {"Authorization": "Bearer " + "a" * 40}
        assert (
            await client.post(f"/api/devices/{device}/enrollment/APPROVED", headers=headers)
        ).status_code == 200
        action = await client.post(
            "/api/actions", headers=headers, json={"targets": [device], "task": {"operation": "refresh"}}
        )
        assert action.status_code == 200
        token = action.json()["token"]
        result = await client.post(
            f"/api/actions/{token}/confirm", headers=headers, json={"phrase": "CONFIRM 1"}
        )
        assert result.status_code == 200
        assert len(result.json()["tasks"]) == 1
        assert (await client.put("/api/users/3/VIEWER", headers=headers)).status_code == 200
        viewer = {"Authorization": "Bearer " + "b" * 40, "X-Telegram-User": "3"}
        assert (await client.get("/api/devices", headers=viewer)).status_code == 200
        assert (
            await client.post(
                "/api/actions", headers=viewer, json={"targets": [device], "task": {"operation": "refresh"}}
            )
        ).status_code == 403
        assert (await client.get("/api/backup", headers=viewer)).status_code == 403


async def test_pyrogram_and_tgcrypto_import_compatible():
    from pyrogram import Client, filters
    from pyrogram.handlers import MessageHandler, CallbackQueryHandler
    from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup
    import tgcrypto

    assert all(
        (
            Client,
            filters,
            MessageHandler,
            CallbackQueryHandler,
            Message,
            CallbackQuery,
            InlineKeyboardMarkup,
            tgcrypto,
        )
    )


async def test_bot_client_uses_actor_headers(setup):
    s, w, acs, db, cfg = setup
    app, unused = await client_for(setup)
    await unused.aclose()
    api = API(cfg, httpx.ASGITransport(app=app))
    try:
        assert (await api.call(1, "me"))["role"] == "OWNER"
        with pytest.raises(ValueError):
            await api.call(999, "devices")
    finally:
        await api.http.aclose()


async def test_leader_exclusion(setup):
    s, w, acs, db, cfg = setup
    assert await lease(db, "test", "first")
    assert not await lease(db, "test", "second")
    assert await lease(db, "test", "first")


async def test_report_artifacts(setup):
    s, w, acs, db, cfg = setup
    await w.ingest(acs.raw)
    rows = await db.devices.find({}).to_list(10)
    for format, signature in (("json", b"["), ("pdf", b"%PDF"), ("png", b"\x89PNG"), ("csv", b"_id,")):
        content, mime = render(rows, format)
        assert content.startswith(signature)
    content, _ = render([{"acs_id": "=1+1"}], "csv")
    assert b"'=1+1" in content


def test_hybrid_requires_https():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            deployment_mode="hybrid",
            bot_enabled=False,
            telegram_owner_id=1,
            api_token="a" * 40,
            bot_api_token="b" * 40,
            core_api_url="http://127.0.0.1:7860",
        )


async def test_hybrid_forwards_no_server_secrets(setup):
    s, w, acs, db, cfg = setup
    cfg.deployment_mode = "hybrid"
    app = create_app(cfg)
    app.state.cfg = cfg
    calls = []

    def remote(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "Invalid API credential"})

    app.state.proxy = httpx.AsyncClient(
        base_url="https://core.example.com", transport=httpx.MockTransport(remote)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://space.example.com"
    ) as client:
        response = await client.get("/api/devices")
        assert response.status_code == 403
        assert "authorization" not in calls[0].headers
    await app.state.proxy.aclose()


async def test_selections_templates_and_tree(setup):
    s, w, acs, db, cfg = setup
    device = await w.ingest(acs.raw, detailed=True)
    await s.enrollment(1, device, "APPROVED")
    app, client = await client_for(setup)
    headers = {"Authorization": "Bearer " + "a" * 40}
    async with client:
        assert (
            await client.put("/api/selections/test", headers=headers, json={"targets": [device]})
        ).status_code == 200
        response = await client.post(
            "/api/selections/test/prepare", headers=headers, json={"operation": "refresh"}
        )
        assert response.status_code == 200
        assert len(response.json()["preview"]) == 1
        assert (
            await client.put("/api/templates/basic", headers=headers, json={"operation": "refresh"})
        ).status_code == 200
        assert (
            await client.post("/api/templates/basic/prepare", headers=headers, json={"targets": [device]})
        ).status_code == 200
        tree = (await client.get(f"/api/devices/{device}/tree", headers=headers)).json()
        assert tree["items"] == [{"path": "Device", "object": True}]
        export = await client.get(f"/api/devices/{device}/export", headers=headers)
        assert b"never-store-me" not in export.content
        assert (await client.get("/api/analytics", headers=headers)).status_code == 200


async def test_measured_chart_and_empty_chart(setup):
    from datetime import timedelta
    from app.domain import now
    from conftest import sample

    s, w, acs, db, cfg = setup
    t = now() - timedelta(seconds=20)
    device = await w.ingest(sample(at=t, counter=100), detailed=True)
    await w.ingest(sample(at=t + timedelta(seconds=10), counter=200), detailed=True)
    app, client = await client_for(setup)
    headers = {"Authorization": "Bearer " + "a" * 40}
    async with client:
        response = await client.get(
            f"/api/devices/{device}/chart",
            headers=headers,
            params={"path": "Device.Ethernet.Interface.1.Stats.BytesReceived"},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
        response = await client.get(
            f"/api/devices/{device}/chart", headers=headers, params={"path": "not.reported"}
        )
        assert response.status_code == 400
