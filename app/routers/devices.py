"""子设备管理 API + 页面。"""

import logging
import ipaddress
import re

from fastapi import APIRouter, Form, Request
from ..names import valid_name, NAME_ERROR
from ..device_status import (
    gateway_snapshot,
    gateway_view,
    gateway_available,
    gateway_action_error,
    device_snapshot,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..gateway_refs import GatewayId
from ..database import db, now
from ..concurrency import mutation
from ..presentation import gateway_state, gateway_notice, GATEWAY_LABELS
from ..mqtt_client import mqtt_client
from ..utils import norm_mac, fmt_mac
from ..device_catalog import catalog
from ..device_icons import device_icon, ICONS
from ..batch_selection import device_ids

log = logging.getLogger("device.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_mac"] = fmt_mac
templates.env.filters["gateway_state"] = gateway_state
templates.env.filters["gateway_notice"] = gateway_notice
templates.env.globals["gateway_labels"] = GATEWAY_LABELS
templates.env.globals["device_type_icons"] = ICONS


def _push_sync(conn, gateway_id: int):
    """把最新子设备列表下发给在线网关。"""
    from ..subdevice_sync import push_subdevices

    return push_subdevices(conn, gateway_id)


# ---------- 页面：子设备列表（按网关） ----------
@router.get("/gateway/{gid}/devices", response_class=HTMLResponse)
async def page_devices(
    request: Request,
    gid: GatewayId,
    page: int = 1,
    page_size: int = 20,
    q: str = "",
    sort: str = "id",
    direction: str = "desc",
    status: str = "all",
):
    q = q.strip()
    deferred = request.headers.get("x-requested-with") == "pjax"
    with db() as conn:
        gw = gateway_snapshot(conn, gid)
        result = catalog(
            conn,
            gw,
            q,
            page,
            page_size,
            sort,
            direction,
            include_items=not deferred,
            status=status,
        )
    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "request": request,
            "gw": gw,
            **result,
            "rows": result["items"],
            "gateway_view": gateway_view(gw),
            "q": q,
            "deferred": deferred,
        },
    )


# ---------- 批量新增（每行：name,mac,ip） ----------
@router.post("/gateway/{gid}/devices/batch_add")
async def batch_add(gid: GatewayId, lines: str = Form(...)):
    if not lines.strip() or len(lines.encode("utf-8")) > 65536:
        return JSONResponse(
            {"ok": False, "msg": "请填写设备信息，内容不能超过 64 KB"}, status_code=400
        )
    added, skipped, errors = 0, [], []

    def reject(raw, reason):
        skipped.append(f"{raw}（{reason}）")
        errors.append({"line": raw, "reason": reason})

    with mutation(("gateway", gid)) as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        count = conn.execute(
            "SELECT COUNT(*) FROM sub_device WHERE gateway_id=?", (gid,)
        ).fetchone()[0]
        for raw in lines.strip().splitlines():
            if not raw.strip():
                continue
            parts = [p.strip() for p in raw.replace("，", ",").split(",")]
            if not 2 <= len(parts) <= 3:
                reject(raw, "格式应为：名称,MAC,IP")
                continue
            name, mac = parts[0], norm_mac(parts[1])
            ip = parts[2] if len(parts) > 2 else ""
            if not re.fullmatch(r"[0-9a-f]{12}", mac):
                reject(raw, "MAC 地址无效")
                continue
            name = name or f"未命名设备-{mac[-6:]}"
            try:
                if ip:
                    ipaddress.IPv4Address(ip)
            except ValueError:
                reject(raw, "IP 地址无效，请填写 IPv4 地址")
                continue
            if not valid_name(name):
                reject(raw, NAME_ERROR)
                continue
            # 同网关下 MAC 去重
            dup = conn.execute(
                "SELECT id FROM sub_device WHERE gateway_id=? AND mac=?", (gid, mac)
            ).fetchone()
            if dup:
                reject(raw, "MAC 已存在")
                continue
            if count + added >= 64:
                reject(raw, "每个网关最多 64 台设备")
                continue
            try:
                conn.execute(
                    "INSERT INTO sub_device (gateway_id, name, mac, ip, status) VALUES (?,?,?,?,'offline')",
                    (gid, name, mac, ip),
                )
                added += 1
            except Exception:
                log.exception("Unable to add device to gateway %s", gid)
                reject(raw, "添加失败")
        pushed = _push_sync(conn, gid) if added else False
    return JSONResponse(
        {
            "ok": True,
            "added": added,
            "skipped": skipped,
            "errors": errors,
            "pushed_to_gateway": pushed,
        }
    )


@router.post("/gateway/{gid}/devices/{did}/update")
async def update_device(
    request: Request,
    gid: GatewayId,
    did: int,
    name: str = Form(...),
    device_type: str | None = Form(None),
):
    if device_type is None and "device_type" in await request.form():
        device_type = ""
    name = name.strip()
    if not name or not valid_name(name):
        return JSONResponse(
            {"ok": False, "msg": "名称不能为空且最多 16 个字符"}, status_code=400
        )
    if device_type is not None and device_type not in {
        "",
        *[key for key in ICONS if key != "windows"],
    }:
        return JSONResponse({"ok": False, "msg": "无效的设备类型"}, status_code=400)
    with mutation(("gateway", gid), ("device", did)) as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        row = conn.execute(
            "SELECT * FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "msg": "子设备不存在"}, status_code=404)
        if row["pc_service_bound"] and not row["pc_service_ignored"]:
            if device_type is not None and device_type != row["type_override"]:
                return JSONResponse(
                    {"ok": False, "msg": "请先解绑WOL关机服务，再修改设备类型"},
                    status_code=409,
                )
            # Renaming an automatically identified PC must not change its type
            # while its shutdown service remains linked.
            previous = device_icon(dict(row), service_detected=True)["device_type"]
            renamed = device_icon({**dict(row), "name": name}, service_detected=True)
            if renamed["device_type"] != previous:
                device_type = previous
        result = conn.execute(
            "UPDATE sub_device SET name=?,type_override=COALESCE(?,type_override) WHERE gateway_id=? AND id=?",
            (name, device_type, gid, did),
        )
        if not result.rowcount:
            return JSONResponse({"ok": False, "msg": "子设备不存在"}, status_code=404)
        pushed = _push_sync(conn, gid)
    return JSONResponse(
        {
            "ok": True,
            "msg": "设备信息已保存"
            + ("并已同步到网关" if pushed else "；网关上线后同步"),
        }
    )


@router.post("/gateway/{gid}/devices/{did}/windows-service")
async def update_windows_service(gid: GatewayId, did: int, action: str = Form(...)):
    if action not in {"bind", "unbind"}:
        return JSONResponse({"ok": False, "msg": "无效的服务关联操作"}, status_code=400)
    from ..pc_power import pc_registry
    from ..access import service_mac_conflict

    with mutation(("gateway", gid), ("device", did)) as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        row = conn.execute(
            "SELECT * FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
        ).fetchone()
        if not row:
            return JSONResponse(
                {"ok": False, "msg": "局域网设备不存在"}, status_code=404
            )
        bound = bool(row["pc_service_bound"] and not row["pc_service_ignored"])
        if bound == (action == "bind"):
            return JSONResponse(
                {"ok": False, "msg": "服务关联状态已变化，请刷新后重试"},
                status_code=409,
            )
        if action == "bind":
            if service_mac_conflict(conn, row["mac"]):
                return JSONResponse(
                    {
                        "ok": False,
                        "msg": "该MAC同时属于不同用户，无法安全关联WOL关机服务",
                    },
                    status_code=409,
                )
            pc = pc_registry.get(row["mac"])
            if not device_icon(dict(row), service_detected=bool(pc))["power_capable"]:
                return JSONResponse(
                    {"ok": False, "msg": "仅电脑和笔记本支持WOL关机服务关联"},
                    status_code=409,
                )
            if pc and pc["upgrade_required"]:
                return JSONResponse({"ok": False, "msg": pc["error"]}, status_code=426)
            if (
                not pc
                or not pc["online"]
                or not mqtt_client.connected_flag
                or not mqtt_client.client.is_connected()
            ):
                return JSONResponse(
                    {"ok": False, "msg": "WOL关机服务未在线，请启动服务后重试关联"},
                    status_code=409,
                )
        ignored = int(action == "unbind")
        conn.execute(
            "UPDATE sub_device SET pc_service_ignored=?,pc_service_bound=?,status='unknown',last_report='',pc_last_report='',offline_since='',offline_last_check='',offline_checks=0 WHERE gateway_id=? AND id=?",
            (ignored, int(action == "bind"), gid, did),
        )
        if action == "bind":
            stamp = now()
            conn.execute(
                "UPDATE sub_device SET ip=?,status='online',last_report=?,last_online=?,pc_last_report=? WHERE gateway_id=? AND id=?",
                (pc["ip"], stamp, stamp, stamp, gid, did),
            )
        pushed = _push_sync(conn, gid)
    return JSONResponse(
        {
            "ok": True,
            "msg": (
                "WOL关机服务已解绑，已恢复网关探测" if ignored else "WOL关机服务已关联"
            )
            + ("，并已同步网关" if pushed else "；网关上线后同步"),
        }
    )


@router.post("/gateway/{gid}/devices/batch_unbind_service")
async def batch_unbind_service(gid: GatewayId, ids: str = Form(...)):
    selected = device_ids(ids)
    with mutation(("gateway", gid), *[("device", did) for did in selected]) as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        placeholders = ",".join("?" for _ in selected)
        rows = conn.execute(
            f"SELECT * FROM sub_device WHERE gateway_id=? AND id IN ({placeholders})",
            [gid, *selected],
        ).fetchall()
        if len(rows) != len(selected) or any(
            not row["pc_service_bound"] or row["pc_service_ignored"] for row in rows
        ):
            return JSONResponse(
                {"ok": False, "msg": "服务关联状态或设备选择已变化，请重新确认"},
                status_code=409,
            )
        conn.execute(
            f"UPDATE sub_device SET pc_service_ignored=1,pc_service_bound=0,status='unknown',last_report='',pc_last_report='',offline_since='',offline_last_check='',offline_checks=0 WHERE gateway_id=? AND id IN ({placeholders})",
            [gid, *selected],
        )
        pushed = _push_sync(conn, gid)
    return {
        "ok": True,
        "unbound": len(selected),
        "msg": "WOL关机服务已解绑，已恢复网关探测"
        + ("，并已同步网关" if pushed else "；网关上线后同步"),
    }


@router.post("/gateway/{gid}/devices/{did}/delete")
async def delete_device(gid: GatewayId, did: int):
    with mutation(("gateway", gid), ("device", did)) as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        result = conn.execute(
            "DELETE FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
        )
        if not result.rowcount:
            return JSONResponse({"ok": False, "msg": "子设备不存在"}, status_code=404)
        pushed = _push_sync(conn, gid)
    return JSONResponse(
        {
            "ok": True,
            "msg": "子设备已删除"
            + ("并已同步到网关" if pushed else "；网关上线后同步"),
        }
    )


# ---------- 批量删除 ----------
@router.post("/gateway/{gid}/devices/batch_delete")
async def batch_delete(gid: GatewayId, ids: str = Form(...)):
    """ids: 逗号分隔的子设备 id"""
    keys = [x.strip() for x in ids.split(",") if x.strip()]
    if (
        not keys
        or len(keys) > 80
        or any(not re.fullmatch(r"[1-9]\d{0,9}", key) for key in keys)
    ):
        return JSONResponse({"ok": False, "msg": "未选择设备"})
    ids = list({int(key) for key in keys})
    with mutation(("gateway", gid), *[("device", did) for did in ids]) as conn:
        if error := gateway_action_error(
            gateway_snapshot(conn, gid), allow_offline=True
        ):
            return error
        ph = ",".join("?" * len(ids))
        found = conn.execute(
            f"SELECT COUNT(*) FROM sub_device WHERE gateway_id=? AND id IN ({ph})",
            [gid, *ids],
        ).fetchone()[0]
        if found != len(ids):
            return JSONResponse(
                {"ok": False, "msg": "设备选择已变化，请刷新后重试"}, status_code=409
            )
        conn.execute(
            f"DELETE FROM sub_device WHERE gateway_id=? AND id IN ({ph})", [gid, *ids]
        )
        pushed = _push_sync(conn, gid)
    return JSONResponse(
        {
            "ok": True,
            "deleted": len(ids),
            "pushed_to_gateway": pushed,
            "msg": "子设备已删除"
            + ("并已同步到网关" if pushed else "；网关上线后同步"),
        }
    )


# ---------- 批量唤醒 ----------
async def _execute_wake(gid: int, ids: str):
    from ..concurrency import verify_actor

    if any(not part.strip().isdigit() for part in ids.split(",") if part.strip()):
        return JSONResponse(
            {"ok": False, "msg": "设备编号无效"},
            status_code=400,
        )
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return JSONResponse({"ok": False, "msg": "未选择设备"})
    with db() as conn:
        verify_actor(conn)
        gw = gateway_snapshot(conn, gid)
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if gw["delete_requested_at"]:
            return gateway_action_error(gw)
        if error := gateway_action_error(gw, allow_offline=True):
            return error
        if not gateway_available(gw):
            return JSONResponse(
                {"ok": False, "msg": "网关离线，无法下发唤醒指令"}, status_code=400
            )
        ph = ",".join("?" * len(id_list))
        cur = conn.execute(
            f"SELECT * FROM sub_device WHERE gateway_id=? AND id IN ({ph})",
            [gid, *id_list],
        )
        rows = [device_snapshot(r, gw) for r in cur.fetchall()]
        if not rows or len(rows) > 64:
            return JSONResponse(
                {"ok": False, "msg": "请选择 1 至 64 台有效设备"}, status_code=400
            )
        if len(rows) != len(set(id_list)):
            return JSONResponse(
                {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                status_code=409,
            )
        from ..pc_power import pc_registry

        if any(
            not device_icon(row, service_detected=bool(pc_registry.get(row["mac"])))[
                "power_capable"
            ]
            for row in rows
        ):
            return JSONResponse(
                {"ok": False, "msg": "仅电脑和笔记本支持开关机"}, status_code=409
            )
        if all(r["status"] == "online" for r in rows):
            return JSONResponse({"ok": False, "msg": "选中的设备均在线，无需唤醒"})
        if any(r["status"] != "offline" for r in rows):
            return JSONResponse(
                {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                status_code=409,
            )
        macs = [norm_mac(r["mac"]) for r in rows]
        ret = mqtt_client.publish_cmd(gw["mac"], "wake", {"macs": macs})
    return JSONResponse(ret)


async def execute_wake(gid: int, ids: str):
    from ..power_operations import run_operation

    selected = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    return await run_operation(gid, selected, "wake", lambda: _execute_wake(gid, ids))


@router.post("/gateway/{gid}/devices/wake")
async def wake(gid: GatewayId, ids: str = Form(...)):
    return await execute_wake(gid, ids)
