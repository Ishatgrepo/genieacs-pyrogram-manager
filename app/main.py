import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.acs import ACS
from app.api import router
from app.config import Settings
from app.database import connect, initialize
from app.service import Service
from app.workers import Workers


class JSONLog(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "level": record.levelname,
                "event": record.getMessage(),
                **{k: getattr(record, k) for k in ("worker", "error_type", "seconds") if hasattr(record, k)},
            }
        )


def create_app(cfg=None, db=None, acs=None):
    @asynccontextmanager
    async def lifespan(app):
        settings = cfg or Settings()
        app.state.cfg = settings
        handler = logging.StreamHandler()
        handler.setFormatter(JSONLog())
        logging.basicConfig(handlers=[handler], level=settings.log_level, force=True)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        tasks = []
        mongo = None
        backend = acs
        proxy = None
        bot = None
        if settings.deployment_mode != "hybrid":
            mongo = connect(settings) if db is None else None
            database = mongo[settings.app_database_name] if mongo is not None else db
            await initialize(database, settings)
            backend = backend or ACS(settings)
            app.state.service = Service(database, settings, backend)
            tasks.append(asyncio.create_task(Workers(app.state.service).run()))
        else:
            proxy = httpx.AsyncClient(base_url=settings.core_api_url.rstrip("/"), timeout=30)
            app.state.proxy = proxy
        if settings.bot_enabled and settings.deployment_mode != "core":
            from app.bot import Bot

            bot = Bot(settings)
            tasks.append(asyncio.create_task(bot.run()))
        app.state.jobs = tasks
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if bot:
                await bot.api.http.aclose()
            if proxy:
                await proxy.aclose()
            if backend:
                await backend.close()
            if mongo is not None:
                await mongo.close()

    app = FastAPI(
        title="GenieACS Control Center", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def trace(request, call_next):
        request_id = uuid.uuid4().hex
        # Limit request bodies before parsing or forwarding to the core.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        if (
            getattr(app.state, "cfg", None)
            and app.state.cfg.deployment_mode == "hybrid"
            and request.url.path.startswith("/api/")
        ):
            # Forward credentials supplied by the caller; never attach privileged server credentials.
            try:
                r = await app.state.proxy.request(
                    request.method,
                    request.url.path,
                    params=request.query_params,
                    content=request._body,
                    headers={
                        k: request.headers[k]
                        for k in ("authorization", "x-telegram-user", "content-type")
                        if k in request.headers
                    },
                )
                response = Response(
                    r.content,
                    r.status_code,
                    headers={
                        k: v
                        for k, v in r.headers.items()
                        if k.lower() in {"content-type", "content-disposition"}
                    },
                )
            except httpx.HTTPError:
                response = JSONResponse({"detail": "Core backend unavailable"}, status_code=503)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)[:200]}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logging.error("request_failed", extra={"error_type": type(exc).__name__})
        return JSONResponse({"detail": "Service unavailable; see server logs"}, status_code=503)

    @app.get("/")
    async def root():
        return {
            "service": "GenieACS Control Center",
            "management": "Use the authorized Telegram bot or authenticated /api endpoints",
        }

    @app.get("/healthz")
    async def health(request: Request):
        jobs = getattr(app.state, "jobs", [])
        healthy = all(not t.done() for t in jobs)
        if hasattr(app.state, "service"):
            try:
                await app.state.service.db.command("ping")
            except Exception:
                healthy = False
        return JSONResponse({"status": "ok" if healthy else "degraded"}, status_code=200 if healthy else 503)

    app.include_router(router)
    return app


app = create_app()
