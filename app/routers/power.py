"""Windows agent provisioning and device-scoped power management."""

import hashlib
import io
import json
import secrets
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response, FileResponse

from ..config import settings
from ..device_status import (
    gateway_snapshot,
    gateway_available,
    gateway_action_error,
    device_snapshot,
)
from ..device_icons import device_icon
from ..gateway_refs import GatewayId
from ..database import db
from ..access import service_mac_conflict
from ..hass_power import discovery
from ..pc_power import pc_registry
from ..releases import FIRMWARE_VERSION
from ..mqtt_client import mqtt_client
from ..power import (
    COMMAND_TTL,
    ONLINE_WINDOW,
    authenticate,
    check_session,
    device_view,
    expire,
    require_admin,
    transaction,
)

router = APIRouter()
AGENT_FILES = Path(__file__).resolve().parents[2] / "windows-agent"


def device(conn, gid, did):
    row = conn.execute(
        "SELECT * FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
    ).fetchone()
    if not row:
        raise HTTPException(404, "子设备不存在")
    return row


def power_state(conn, row, gateway):
    """One device's existing service evidence, shared by dashboard and share page."""
    transport = bool(mqtt_client.connected_flag and mqtt_client.client.is_connected())
    result = device_view(conn, row["id"])
    result["mac"] = row["mac"]
    pc = None if service_mac_conflict(conn, row["mac"]) else pc_registry.get(row["mac"])
    linked = bool(row["pc_service_bound"] and not row["pc_service_ignored"])
    if pc or linked or row["pc_service_ignored"]:
        result = {
            "installed": True,
            "linked": linked,
            "can_link": bool(
                pc and pc["online"] and not pc["upgrade_required"] and transport
            ),
            "provider": "native",
            "online": bool(pc and pc["online"] and transport),
            "mac": row["mac"],
            "name": pc["name"] if pc else "",
            "ip": pc["ip"] if pc else "",
            "upgrade_required": bool(pc and pc["upgrade_required"]),
            "error": pc["error"] if pc else "",
            "needs_configuration": False,
            "device_online": device_snapshot(row, gateway)["status"] == "online",
            "command": pc_registry.command_view(row["mac"]) if linked else None,
        }
    elif settings.ENFORCE_CLIENT_VERSIONS:
        result = {
            "installed": False,
            "linked": False,
            "can_link": False,
            "provider": "native",
            "online": False,
            "mac": row["mac"],
            "needs_configuration": True,
            "device_online": device_snapshot(row, gateway)["status"] == "online",
        }
    else:
        command = (
            None
            if service_mac_conflict(conn, row["mac"])
            else discovery.for_mac(row["mac"])
        )
        if command or not result["installed"]:
            binding = (
                conn.execute(
                    "SELECT * FROM hass_power WHERE discovery_topic=?",
                    (command["discovery_topic"],),
                ).fetchone()
                if command
                else None
            )
            result = {
                "installed": bool(command),
                "provider": "hass",
                "online": bool(
                    command and command["online"] and mqtt_client.connected_flag
                ),
                "command": (
                    {"message": binding["last_result"]}
                    if binding and binding["last_result"]
                    else None
                ),
                "mac": row["mac"],
                "needs_configuration": not bool(command),
                "device_online": device_snapshot(row, gateway)["status"] == "online",
            }
    result["power_capable"] = device_icon(
        dict(row),
        service_detected=bool(
            result.get("installed") and result.get("provider") == "native"
        ),
    )["power_capable"]
    return result


@router.get("/api/gateway/{gid}/power")
async def states(gid: GatewayId, ids: str | None = None):
    from ..batch_selection import device_ids

    selected = device_ids(ids) if ids is not None else None
    with db() as conn:
        gateway = gateway_snapshot(conn, gid)
        where = "gateway_id=?"
        params = [gid]
        if selected is not None:
            where += " AND id IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        rows = conn.execute(
            "SELECT * FROM sub_device WHERE " + where, params
        ).fetchall()
        result = {str(row["id"]): power_state(conn, row, gateway) for row in rows}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/downloads/pc-service.zip")
async def download_pc_service():
    # Already-open browser pages still use this URL. ZIP updates use the
    # separately authenticated /api/pc/package endpoint.
    return await download_pc_executable()


@router.get("/downloads/pc-service.exe")
async def download_pc_executable():
    executable = AGENT_FILES / "native" / "WolPCService.exe"
    if not executable.is_file():
        raise HTTPException(503, "电脑端安装包暂不可用")
    return FileResponse(
        executable,
        filename="WolPCService.exe",
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get(f"/downloads/esp32c3-{FIRMWARE_VERSION}.zip")
async def download_firmware():
    archive = AGENT_FILES.parents[0] / "deploy" / f"esp32c3-{FIRMWARE_VERSION}.zip"
    if not archive.is_file():
        raise HTTPException(503, "固件下载暂不可用")
    return FileResponse(archive, filename=archive.name, media_type="application/zip")


@router.post("/gateway/{gid}/devices/{did}/power/install")
async def install(request: Request, gid: GatewayId, did: int):
    from ..concurrency import verify_actor

    require_admin(request)
    with db() as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        device(conn, gid, did)
    if settings.ENFORCE_CLIENT_VERSIONS:
        return await download_pc_executable()
    base = (settings.POWER_AGENT_PUBLIC_URL or str(request.base_url)).rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise HTTPException(
            400, "请将 POWER_AGENT_PUBLIC_URL 设置为有效的 HTTPS 后台地址"
        )
    token = secrets.token_urlsafe(32)
    # Build everything before rotating the credentials; a missing deployment file
    # must never revoke a working installation.
    with transaction() as conn:
        verify_actor(conn)
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        row = device(conn, gid, did)
        config = {
            "server_url": base,
            "device_id": did,
            "token": token,
            "mac": row["mac"],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("Agent.ps1", "Install.ps1", "Uninstall.ps1", "README.md"):
                archive.write(AGENT_FILES / name, name)
            archive.writestr(
                "config.json", json.dumps(config, ensure_ascii=False, indent=2)
            )
        conn.execute(
            """INSERT INTO power_agent(device_id,token_hash) VALUES (?,?)
                        ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash,session_id='',last_seen=0""",
            (did, hashlib.sha256(token.encode()).hexdigest()),
        )
        conn.execute(
            "UPDATE power_command SET status='cancelled' WHERE device_id=? AND status IN ('pending','dispatched','accepted')",
            (did,),
        )
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="wol-windows-agent-{did}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/gateway/{gid}/devices/{did}/power/revoke")
async def revoke(request: Request, gid: GatewayId, did: int):
    from ..concurrency import verify_actor

    require_admin(request)
    with transaction() as conn:
        verify_actor(conn)
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        device(conn, gid, did)
        conn.execute("DELETE FROM power_agent WHERE device_id=?", (did,))
    return {"ok": True, "msg": "电脑端授权已撤销"}


async def _execute_shutdown(request: Request, gid: int, did: int, force: bool = False):
    from ..concurrency import verify_actor

    stamp = time.time()
    with transaction() as conn:
        verify_actor(conn)
        # Direct native services may operate through a healthy offline gateway;
        # pending deletion, incomplete binding and abnormal gateways remain read-only.
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        row = device(conn, gid, did)
        pc = (
            None
            if service_mac_conflict(conn, row["mac"])
            else pc_registry.get(row["mac"])
        )
        if not device_icon(dict(row), service_detected=bool(pc))["power_capable"]:
            raise HTTPException(409, "仅电脑和笔记本支持开关机")
        if row["pc_service_ignored"]:
            pc = None
        if pc:
            if pc["upgrade_required"]:
                raise HTTPException(426, pc["error"])
            if not row["pc_service_bound"]:
                raise HTTPException(409, "请先确认关联WOL关机服务")
            if force:
                raise HTTPException(400, "电脑端不支持强制关机")
            if (
                not pc["online"]
                or not mqtt_client.connected_flag
                or not mqtt_client.client.is_connected()
            ):
                raise HTTPException(409, "WOL关机服务离线，未下发关机指令")
            topic = "wol/pc/" + pc["mac"] + "/command"
            binding = conn.execute(
                "SELECT * FROM hass_power WHERE discovery_topic=?", (topic,)
            ).fetchone()
            if binding and stamp - binding["last_sent"] < 15:
                raise HTTPException(409, "关机指令刚刚发送，请勿重复操作")
            if not binding:
                conn.execute("DELETE FROM hass_power WHERE device_id=?", (did,))
                conn.execute(
                    "INSERT INTO hass_power(device_id,discovery_topic) VALUES (?,?)",
                    (did, topic),
                )
            conn.execute(
                "UPDATE hass_power SET last_sent=?,last_result=? WHERE discovery_topic=?",
                (stamp, "发送结果未确认，请检查电脑；不会自动重发", topic),
            )
            command_id = uuid.uuid4().hex
            pc_registry.track_command(pc["mac"], command_id, pc["session"])
            payload = json.dumps(
                {
                    "id": command_id,
                    "type": "shutdown",
                    "session": pc["session"],
                    "expires_at": int(stamp) + 30,
                }
            )
            try:
                info = mqtt_client.client.publish(topic, payload, qos=0, retain=False)
                if info.rc != 0:
                    raise RuntimeError("MQTT publish rejected")
            except Exception:
                with pc_registry.lock:
                    tracked = pc_registry.commands.get(pc["mac"])
                    if (
                        tracked
                        and tracked["id"] == command_id
                        and tracked["status"] == "pending"
                    ):
                        tracked["status"] = "uncertain"
                return JSONResponse(
                    {"ok": False, "msg": "发送结果未确认，请检查电脑；不会自动重发"},
                    status_code=502,
                )
            message = "关机指令已发送，等待电脑离线"
            conn.execute(
                "UPDATE hass_power SET last_result=? WHERE discovery_topic=?",
                (message, topic),
            )
            return {"ok": True, "msg": message, "cmd_id": command_id}
        if row["pc_service_bound"]:
            raise HTTPException(409, "WOL关机服务离线，未下发关机指令")
        if row["pc_service_ignored"]:
            raise HTTPException(409, "请先确认关联WOL关机服务")
        gateway = gateway_snapshot(conn, gid)
        if not gateway_available(gateway):
            raise HTTPException(409, "网关离线，设备状态未知，未发送关机指令")
        if settings.ENFORCE_CLIENT_VERSIONS:
            raise HTTPException(426, "请升级WOL关机服务到 " + settings.MIN_PC_VERSION)
        command = discovery.for_mac(row["mac"])
        if command:
            if row["status"] != "online":
                raise HTTPException(409, "设备离线，未发送关机指令")
            if force:
                raise HTTPException(400, "HASS.Agent 的关机行为由电脑端命令决定")
            if (
                not command
                or not command["online"]
                or not mqtt_client.connected_flag
                or not mqtt_client.client.is_connected()
            ):
                raise HTTPException(
                    409, "HASS.Agent 未确认在线，未发送关机指令；请等待实时状态"
                )
            binding = conn.execute(
                "SELECT * FROM hass_power WHERE discovery_topic=?",
                (command["discovery_topic"],),
            ).fetchone()
            if binding and stamp - binding["last_sent"] < 15:
                raise HTTPException(409, "关机指令刚刚发送，请勿重复操作")
            if not binding:
                conn.execute("DELETE FROM hass_power WHERE device_id=?", (did,))
                conn.execute(
                    "INSERT INTO hass_power(device_id,discovery_topic) VALUES (?,?)",
                    (did, command["discovery_topic"]),
                )
            # Reserve before publishing, and never automatically retry an uncertain send.
            conn.execute(
                "UPDATE hass_power SET last_sent=?,last_result=? WHERE discovery_topic=?",
                (
                    stamp,
                    "发送结果未确认，请检查电脑；不会自动重发",
                    command["discovery_topic"],
                ),
            )
            try:
                info = mqtt_client.client.publish(
                    command["command_topic"], command["payload"], qos=0, retain=False
                )
                if info.rc != 0:
                    raise RuntimeError("MQTT publish rejected")
            except Exception:
                # Commit the reservation so an exception cannot bypass the cooldown.
                return JSONResponse(
                    {"ok": False, "msg": "发送结果未确认，请检查电脑；不会自动重发"},
                    status_code=502,
                )
            message = "关机指令已发送，等待电脑离线"
            conn.execute(
                "UPDATE hass_power SET last_result=? WHERE discovery_topic=?",
                (message, command["discovery_topic"]),
            )
            return {"ok": True, "msg": message}
        agent = conn.execute(
            "SELECT * FROM power_agent WHERE device_id=?", (did,)
        ).fetchone()
        if not agent:
            raise HTTPException(409, "电脑尚未配置关机服务，请按 MAC 配置WOL关机服务")
        if (
            not agent
            or not agent["session_id"]
            or stamp - agent["last_seen"] >= ONLINE_WINDOW
        ):
            raise HTTPException(409, "WOL关机服务离线，未下发关机指令")
        expire(conn, did, stamp)
        active = conn.execute(
            "SELECT id FROM power_command WHERE device_id=? AND expires_at>? AND status IN ('pending','dispatched','accepted','submitted')",
            (did, stamp),
        ).fetchone()
        if active:
            raise HTTPException(409, "已有未结束的关机请求，请等待结果")
        cmd_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO power_command(id,device_id,session_id,`force`,created_at,expires_at) VALUES (?,?,?,?,?,?)",
            (cmd_id, did, agent["session_id"], int(force), stamp, stamp + COMMAND_TTL),
        )
    return {"ok": True, "cmd_id": cmd_id, "msg": "关机指令已下发，等待电脑端执行"}


async def execute_shutdown(request: Request, gid: int, did: int, force: bool = False):
    from ..power_operations import run_operation

    return await run_operation(
        gid, [did], "shutdown", lambda: _execute_shutdown(request, gid, did, force)
    )


@router.post("/gateway/{gid}/devices/{did}/shutdown")
async def shutdown(
    request: Request, gid: GatewayId, did: int, force: bool = Form(False)
):
    require_admin(request)
    return await execute_shutdown(request, gid, did, force)


@router.get("/api/hass-power/commands")
async def hass_commands():
    return JSONResponse(
        {"commands": discovery.list()}, headers={"Cache-Control": "no-store"}
    )


@router.post("/api/power-agent/{did}/poll")
async def poll(request: Request, did: int, session_id: str = Form(...)):
    if settings.ENFORCE_CLIENT_VERSIONS:
        raise HTTPException(
            426, "旧版WOL关机服务已禁用，请安装 " + settings.MIN_PC_VERSION
        )
    check_session(session_id)
    stamp = time.time()
    with transaction() as conn:
        agent = authenticate(conn, request, did)
        expire(conn, did, stamp)
        if session_id != agent["session_id"]:
            conn.execute(
                "UPDATE power_command SET status='unknown' WHERE device_id=? AND status IN ('pending','dispatched','accepted')",
                (did,),
            )
        conn.execute(
            "UPDATE power_agent SET session_id=?,last_seen=? WHERE device_id=?",
            (session_id, stamp, did),
        )
        command = conn.execute(
            "SELECT * FROM power_command WHERE device_id=? AND session_id=? AND status='pending' AND expires_at>? ORDER BY created_at LIMIT 1",
            (did, session_id, stamp),
        ).fetchone()
        result = None
        if command:
            conn.execute(
                "UPDATE power_command SET status='dispatched' WHERE id=?",
                (command["id"],),
            )
            result = {
                "id": command["id"],
                "type": "shutdown",
                "force": bool(command["force"]),
                "ttl": max(0, command["expires_at"] - time.time()),
                "session_id": session_id,
            }
    return JSONResponse({"command": result}, headers={"Cache-Control": "no-store"})


@router.post("/api/power-agent/{did}/ack")
async def ack(
    request: Request,
    did: int,
    session_id: str = Form(...),
    command_id: str = Form(...),
    status: str = Form(...),
):
    if settings.ENFORCE_CLIENT_VERSIONS:
        raise HTTPException(
            426, "旧版WOL关机服务已禁用，请安装 " + settings.MIN_PC_VERSION
        )
    check_session(session_id)
    if status not in {"accepted", "submitted", "failed"}:
        raise HTTPException(400, "Invalid command status")
    with transaction() as conn:
        agent = authenticate(conn, request, did)
        expire(conn, did, time.time())
        command = conn.execute(
            "SELECT * FROM power_command WHERE id=? AND device_id=? AND session_id=?",
            (command_id, did, session_id),
        ).fetchone()
        if not command or session_id != agent["session_id"]:
            raise HTTPException(409, "Command does not belong to this agent session")
        allowed = {
            "accepted": {"dispatched"},
            "submitted": {"accepted"},
            "failed": {"accepted"},
        }
        if command["status"] != status:
            if command["status"] not in allowed[status]:
                raise HTTPException(409, "Command expired or already completed")
            conn.execute(
                "UPDATE power_command SET status=? WHERE id=?", (status, command_id)
            )
    return {"ok": True}
