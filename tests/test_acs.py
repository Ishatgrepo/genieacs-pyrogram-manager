import json
import httpx
from app.acs import ACS


async def test_encoded_device_id_and_202(setup):
    *_, cfg = setup
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"_id": "abc"})

    acs = ACS(cfg, httpx.MockTransport(handler))
    code, data = await acs.submit("A%2FB-Serial?&", {"name": "reboot"})
    assert code == 202
    assert b"A%252FB-Serial%3F%26" in seen[0].url.raw_path
    assert json.loads(seen[0].content)["name"] == "reboot"
    await acs.close()


async def test_projection_keyset_pagination(setup):
    *_, cfg = setup

    def handler(request):
        q = json.loads(request.url.params["query"])
        assert q["$and"][1]["_id"]["$gt"] == "last"
        assert request.url.params["projection"] == "_id,_lastInform"
        return httpx.Response(200, json=[])

    acs = ACS(cfg, httpx.MockTransport(handler))
    assert await acs.get("devices", {}, "_id,_lastInform", after="last") == []
    await acs.close()
