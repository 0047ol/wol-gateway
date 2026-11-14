"""TLS-only companion listener. No admin routes or MQTT client lifecycle here."""

import time
import threading
import hashlib
from collections import OrderedDict
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .database import init_db
from .enrollment import enroll

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
attempts = OrderedDict()
enrollment_lock = threading.Lock()


@app.on_event("startup")
def startup():
    init_db()


@app.middleware("http")
async def limited(request, call_next):
    if request.url.path.startswith("/api/firmware/"):
        # OTA uses hundreds of authenticated Range requests, not enrollment attempts.
        return await call_next(request)
    key = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else "unknown"
    )
    if request.url.path.startswith("/api/pc/"):
        # Different PCs may share a NAT address. Credentials are independently
        # authenticated in each handler, and never retained in the rate-limit key.
        key += (
            ":pc:"
            + hashlib.sha256(
                request.headers.get("authorization", "").encode()
            ).hexdigest()
        )
    current = time.monotonic()
    count, start = attempts.get(key, (0, current))
    if current - start > 60:
        count, start = 0, current
    attempts[key] = (count + 1, start)
    if len(attempts) > 10000:
        attempts.popitem(last=False)
    if count >= 30:
        return JSONResponse({"detail": "请求过于频繁，请稍后重试"}, status_code=429)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/enroll/{role}")
async def enrollment(role: str, request: Request):
    if role not in ("esp", "pc"):
        raise HTTPException(404)
    if int(request.headers.get("content-length", "0")) > 2048:
        raise HTTPException(413)
    raw = await request.body()
    if len(raw) > 2048:
        raise HTTPException(413)
    try:
        import json

        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "无效请求")
    try:
        with enrollment_lock:
            return enroll(role, body)
    except (RuntimeError, OSError):
        raise HTTPException(503, "接入服务暂不可用")


@app.api_route("/api/firmware/{task_id}", methods=["GET", "HEAD"])
def firmware_download(task_id: str, request: Request):
    from .ota import download

    return download(
        task_id,
        request.headers.get("authorization", ""),
        request.headers.get("range", ""),
        head=request.method == "HEAD",
    )


@app.get("/api/pc/runtime")
def pc_runtime(request: Request):
    from .pc_maintenance import runtime

    return runtime(request.headers.get("authorization", ""))


@app.get("/api/pc/package/{version}")
def pc_package(version: str, request: Request):
    from .pc_maintenance import download

    return download(request.headers.get("authorization", ""), version)


@app.get("/api/pc/executable/{version}")
async def pc_executable(request: Request, version: str):
    from .pc_maintenance import download_executable

    return download_executable(request.headers.get("authorization", ""), version)


@app.post("/api/pc/unbind")
async def pc_unbind(request: Request):
    from .pc_maintenance import authenticate, unbind

    authorization = request.headers.get("authorization", "")
    authenticate(authorization)
    try:
        size = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "无效的请求长度")
    if size < 0:
        raise HTTPException(400, "无效的请求长度")
    if size > 512:
        raise HTTPException(413)
    raw = await request.body()
    if len(raw) > 512:
        raise HTTPException(413)
    try:
        import json

        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"binding_token"}:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "无效的解除关联请求")
    return unbind(authorization, body["binding_token"])
