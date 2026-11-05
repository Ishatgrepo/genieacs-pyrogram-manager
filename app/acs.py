import json
from urllib.parse import quote

import httpx


class ACS:
    def __init__(self, settings, transport=None):
        token = settings.genieacs_nbi_auth_token.get_secret_value()
        self.http = httpx.AsyncClient(
            base_url=settings.genieacs_nbi_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=25,
            limits=httpx.Limits(max_connections=20),
            transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    async def get(self, collection, query=None, projection=None, limit=100, after=None):
        if collection not in {"devices", "tasks", "faults", "files"}:
            raise ValueError("Unsupported collection")
        query = dict(query or {})
        if after is not None:
            query = {"$and": [query, {"_id": {"$gt": after}}]}
        params = {"query": json.dumps(query), "limit": limit, "sort": '{"_id":1}'}
        if projection:
            params["projection"] = projection
        r = await self.http.get(collection + "/", params=params)
        r.raise_for_status()
        return r.json()

    async def device(self, device_id):
        rows = await self.get("devices", {"_id": device_id}, limit=1)
        if not rows:
            raise ValueError("Device no longer exists in ACS")
        return rows[0]

    async def submit(self, device_id, payload):
        # Never automatically retry POST: a timed-out request may have been accepted.
        r = await self.http.post(
            f"devices/{quote(device_id, safe='')}/tasks",
            params={"connection_request": "", "timeout": "15000"},
            json=payload,
        )
        r.raise_for_status()
        if r.status_code not in (200, 202):
            raise ValueError("Unexpected ACS task response")
        return r.status_code, r.json()

    async def retry(self, task_id):
        r = await self.http.post(f"tasks/{quote(task_id, safe='')}/retry")
        r.raise_for_status()

    async def cancel(self, task_id):
        r = await self.http.delete(f"tasks/{quote(task_id, safe='')}")
        r.raise_for_status()

    async def tag(self, device_id, tag, remove=False):
        r = await self.http.request(
            "DELETE" if remove else "POST", f"devices/{quote(device_id, safe='')}/tags/{quote(tag, safe='')}"
        )
        r.raise_for_status()
