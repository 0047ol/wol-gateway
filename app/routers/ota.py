"""Authenticated upload/status APIs; firmware downloads use the separate TLS listener."""

import re

from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import JSONResponse

from .. import ota
from ..batch_selection import gateway_ids
from ..background import reconcile_heartbeat_status
from ..concurrency import mutation, check
from ..database import db
from ..device_status import gateway_snapshot
from ..gateway_refs import GatewayId, resolve_gateway_ref

router = APIRouter()


def published_package():
    try:
        return ota.latest_release()
    except (OSError, ValueError, HTTPException):
        raise HTTPException(503, "本站固件暂不可用，请稍后重试") from None


def selected_gateways(conn, ids):
    refs = ids.split(",")
    if (
        not 1 <= len(refs) <= 64
        or len(set(refs)) != len(refs)
        or any(not re.fullmatch(r"g_[a-f0-9]{32}", ref) for ref in refs)
    ):
        raise HTTPException(400, "请选择 1 至 64 台有效网关")
    return [gateway_snapshot(conn, resolve_gateway_ref(ref, conn)) for ref in refs]


def submit_batch(conn, gateways, verified, request, preview=False):
    plan = ota.batch_plan(conn, gateways, verified)
    if preview:
        return plan
    if request.headers.get("X-WOL-OTA-Plan") != plan["plan"]:
        raise HTTPException(409, "升级名单或版本已变化，请重新检查固件包")
    results = []
    for gateway, entry in zip(gateways, plan["entries"]):
        item = {**entry, "started": False}
        if entry["eligible"]:
            try:
                result = ota.start(conn, gateway, verified)
                item.update(
                    started=result["ok"],
                    ota=result["ota"],
                    reason="" if result["ok"] else result["msg"],
                )
            except HTTPException as error:
                item["reason"] = str(error.detail)
        results.append(item)
    return {
        "ok": True,
        "entries": results,
        "version": plan["version"],
        "started": sum(item["started"] for item in results),
    }


@router.post("/gateway/{gid}/ota/auto")
def automatic_update(gid: GatewayId, enabled: str = Form(...)):
    if enabled not in {"true", "false"}:
        raise HTTPException(400, "无效的自动更新设置")
    with mutation() as conn:
        check(conn, "gateway", gid, allow_ota=True)
        return ota.set_auto_update(conn, gateway_snapshot(conn, gid), enabled == "true")


async def read_package(request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "请上传 ZIP 固件包")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > ota.MAX_PACKAGE:
            raise HTTPException(413, "升级包过大，最多支持 3 MiB")
        raw.extend(part)
    return ota.verify_package(raw)


@router.get("/gateway/batch_ota/status")
def batch_status(ids: str):
    with db() as conn:
        gateways = selected_gateways(conn, ids)
        return {
            "ok": True,
            "entries": [
                {"id": gateway["public_id"], "ota": ota.view(conn, gateway)}
                for gateway in gateways
            ],
        }


@router.get("/gateway/batch_ota/check")
def batch_check(ids: str):
    with db() as conn:
        selected_gateways(conn, ids)  # Authorize before reading the release.
    verified = published_package()
    with db() as conn:
        reconcile_heartbeat_status(conn)
        return ota.batch_plan(conn, selected_gateways(conn, ids), verified)


@router.post("/gateway/batch_ota/latest")
def batch_latest(request: Request, ids: str, preview: bool = False):
    with mutation() as conn:
        gateway_ids(conn, ids, allow_ota=True)
    verified = published_package()
    with mutation() as conn:
        reconcile_heartbeat_status(conn)
        return submit_batch(
            conn, gateway_ids(conn, ids, allow_ota=True), verified, request, preview
        )


@router.post("/gateway/batch_ota")
async def batch_upload(request: Request, ids: str, preview: bool = False):
    # Reject unauthorized or stale targets before reading a potentially large body.
    with mutation() as conn:
        reconcile_heartbeat_status(conn)
        gateway_ids(conn, ids, allow_ota=True)
    verified = await read_package(request)
    with mutation() as conn:
        reconcile_heartbeat_status(conn)
        gateways = gateway_ids(conn, ids, allow_ota=True)
        return submit_batch(conn, gateways, verified, request, preview)


@router.get("/gateway/{gid}/ota/check")
def check_latest(gid: GatewayId):
    with db() as conn:
        gateway_snapshot(conn, gid)
    verified = published_package()
    with db() as conn:
        reconcile_heartbeat_status(conn)
        return ota.batch_plan(conn, [gateway_snapshot(conn, gid)], verified)


@router.post("/gateway/{gid}/ota/latest")
def install_latest(gid: GatewayId, request: Request):
    with mutation(("gateway", gid)) as conn:
        gateway_snapshot(conn, gid)
    verified = published_package()
    with mutation(("gateway", gid)) as conn:
        reconcile_heartbeat_status(conn)
        gateway = gateway_snapshot(conn, gid)
        plan = ota.batch_plan(conn, [gateway], verified)
        if request.headers.get("X-WOL-OTA-Plan") != plan["plan"]:
            raise HTTPException(409, "升级名单或版本已变化，请重新检查固件包")
        if not plan["entries"][0]["eligible"]:
            raise HTTPException(409, plan["entries"][0]["reason"])
        return ota.start(conn, gateway, verified)


@router.get("/gateway/{gid}/ota")
def status(gid: GatewayId):
    with db() as conn:
        gateway = gateway_snapshot(conn, gid)
        return {"ok": True, "ota": ota.view(conn, gateway)}


@router.post("/gateway/{gid}/ota")
async def upload(gid: GatewayId, request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "请上传 ZIP 固件包")
    with mutation(("gateway", gid)) as conn:
        gateway = gateway_snapshot(conn, gid)
        status = ota.view(conn, gateway)
        if not status["can_upgrade"]:
            raise HTTPException(409, status["disabled_reason"])
    verified = await read_package(request)
    with mutation(("gateway", gid)) as conn:
        gateway = gateway_snapshot(conn, gid)
        return JSONResponse(ota.start(conn, gateway, verified))
