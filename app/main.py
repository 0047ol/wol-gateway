"""FastAPI 入口：装配路由、启动 MQTT、后台心跳看门狗。"""

import asyncio
import logging
import uuid

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from .background import (
    heartbeat_watchdog,
    ack_watchdog,
    log_retention_watchdog,
    subdevice_sync_watchdog,
)
from .gateway_deletion import gateway_deletion_watchdog
from .config import settings
from .database import init_db
from .mqtt_client import mqtt_client
from .mqtt_handlers import register_handlers
from .routers import api, devices, gateways, scan, monitoring, power, shares, accounts
from .auth import AdminMiddleware, router as auth_router
from .releases import router as releases_router
from .routers.ota import router as ota_router
from .ota import watchdog as ota_watchdog

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

app = FastAPI(title="WOL Gateway")


class WebsiteGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        # Installers and firmware archives are already compressed.
        if scope.get("path", "").startswith("/downloads/"):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


app.add_middleware(WebsiteGZipMiddleware, minimum_size=800, compresslevel=5)
app.add_middleware(AdminMiddleware)


@app.middleware("http")
async def redirect_missing_pages(request, call_next):
    request_id = uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-WOL-Request-ID"] = request_id
    if response.status_code >= 500:
        logging.warning(
            "HTTP failure id=%s method=%s path=%s status=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
        )
    # Only document navigation redirects; fetch/API callers retain meaningful 404s.
    document = request.headers.get(
        "sec-fetch-dest"
    ) == "document" or "text/html" in request.headers.get("accept", "")
    if (
        request.method == "GET"
        and response.status_code in {403, 404}
        and document
        and not request.url.path.startswith(("/api/", "/static/"))
        and request.url.path != "/"
    ):
        return RedirectResponse("/", status_code=303)
    return response


app.include_router(auth_router)
app.include_router(accounts.router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(gateways.router)
app.include_router(devices.router)
app.include_router(scan.router)
app.include_router(api.router)
app.include_router(monitoring.router)
app.include_router(power.router)
app.include_router(releases_router)
app.include_router(shares.router)
app.include_router(ota_router)


@app.on_event("startup")
async def _startup():
    init_db()
    register_handlers()
    mqtt_client.start()
    asyncio.create_task(heartbeat_watchdog())
    asyncio.create_task(ack_watchdog())
    asyncio.create_task(subdevice_sync_watchdog())
    asyncio.create_task(gateway_deletion_watchdog())
    asyncio.create_task(log_retention_watchdog())
    asyncio.create_task(ota_watchdog())
    logging.info("server started on :%s (debug=%s)", settings.PORT, settings.DEBUG)


@app.on_event("shutdown")
async def _shutdown():
    mqtt_client.stop()
