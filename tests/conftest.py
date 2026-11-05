import copy
import os
import uuid
from datetime import timedelta
import pytest
from mongomock_motor import AsyncMongoMockClient
from app.config import Settings
from app.database import initialize
from app.domain import now
from app.service import Service
from app.workers import Workers


def sample(serial="1", at=None, counter=100):
    at = at or now()

    def p(value, kind="xsd:string", writable=False):
        return {"_value": value, "_type": kind, "_writable": writable, "_timestamp": at.isoformat()}

    return {
        "_id": "AABBCC-Test-" + serial,
        "_lastInform": at.isoformat(),
        "_registered": (at - timedelta(hours=1)).isoformat(),
        "_deviceId": {"_SerialNumber": serial},
        "Device": {
            "DeviceInfo": {
                "ModelName": p("Test"),
                "SoftwareVersion": p("1.0"),
                "UpTime": p(100, "xsd:unsignedInt"),
            },
            "WiFi": {
                "SSID": {
                    "1": {"SSID": p("test", writable=True), "Password": p("never-store-me", writable=True)}
                }
            },
            "Ethernet": {"Interface": {"1": {"Stats": {"BytesReceived": p(counter, "xsd:unsignedLong")}}}},
        },
    }


class FakeACS:
    def __init__(self):
        self.raw = sample()
        self.code = 202
        self.posted = []
        self.pending = True
        self.fault = False
        self.error = False
        self.rows = []
        self.retries = []

    async def get(self, collection, query=None, projection=None, limit=100, after=None):
        if collection == "tasks":
            return [{"_id": "task1"}] if self.pending else []
        if collection == "faults":
            return [{"code": "9002"}] if self.fault else []
        rows = self.rows or [self.raw]
        return [copy.deepcopy(r) for r in rows if after is None or r["_id"] > after][:limit]

    async def device(self, device_id):
        return copy.deepcopy(self.raw)

    async def submit(self, device, payload):
        self.posted.append((device, payload))
        if self.error:
            raise TimeoutError("could have been accepted")
        return self.code, {"_id": "task1"}

    async def retry(self, task):
        self.retries.append(task)

    async def close(self):
        pass


@pytest.fixture
async def setup():
    cfg = Settings(
        _env_file=None,
        bot_enabled=False,
        api_token="a" * 40,
        bot_api_token="b" * 40,
        telegram_owner_id=1,
        telegram_admin_ids="2",
        parameter_allowlist=["Device.WiFi.SSID.1.SSID"],
        model_capabilities={"Test": ["reboot", "factory_reset"]},
    )
    real_uri = os.environ.get("TEST_MONGODB_URI")
    if real_uri:
        from pymongo import AsyncMongoClient

        client = AsyncMongoClient(real_uri, tz_aware=True)
        db = client["test_manager_" + uuid.uuid4().hex]
    else:
        client = None
        db = AsyncMongoMockClient(tz_aware=True).test
    await initialize(db, cfg)
    acs = FakeACS()
    s = Service(db, cfg, acs)
    w = Workers(s)
    yield s, w, acs, db, cfg
    if client is not None:
        await client.drop_database(db.name)
        await client.close()
