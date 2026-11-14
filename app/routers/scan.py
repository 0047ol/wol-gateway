"""局域网扫描：下发 scan 指令、展示扫描结果、一键添加。"""

import json
import logging
import asyncio
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..gateway_refs import GatewayId
from ..database import db
from ..concurrency import mutation
from ..access import actor
from ..mqtt_client import mqtt_client
from ..utils import norm_mac, fmt_mac
from ..names import valid_name, NAME_ERROR
from ..device_status import gateway_snapshot, gateway_action_error
from ..subdevice_sync import push_subdevices
from ..presentation import gateway_state, GATEWAY_LABELS

log = logging.getLogger("scan.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_mac"] = fmt_mac
templates.env.filters["gateway_state"] = gateway_state
templates.env.globals["gateway_labels"] = GATEWAY_LABELS


@router.get("/gateway/{gid}/scan", response_class=HTMLResponse)
async def scan_dialog(request: Request, gid: GatewayId):
    with db() as conn:
        gw = gateway_snapshot(conn, gid)
        if not gw:
            return HTMLResponse("网关不存在", status_code=404)
        if error := gateway_action_error(gw, scan=True):
            return error
    return templates.TemplateResponse(
        request=request,
        name="scan.html",
        context={
            "request": request,
            "gw": gw,
        },
    )


@router.post("/gateway/{gid}/scan/trigger")
async def trigger_scan(gid: GatewayId):
    with mutation(("gateway", gid)) as conn:
        gw = gateway_snapshot(conn, gid)
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if error := gateway_action_error(gw, scan=True):
            return error
        stamp = time.time()
        lease = conn.execute(
            "SELECT * FROM gateway_scan_lease WHERE gateway_id=?", (gid,)
        ).fetchone()
        if lease and lease["expires_at"] > stamp:
            command = conn.execute(
                "SELECT status FROM cmd_log WHERE cmd_id=?", (lease["cmd_id"],)
            ).fetchone()
            running = not command or command["status"] == "sent"
            remaining = max(
                1,
                int(
                    (lease["expires_at"] if running else lease["cooldown_until"])
                    - stamp
                    + 0.999
                ),
            )
            if running or lease["cooldown_until"] > stamp:
                return JSONResponse(
                    {
                        "ok": False,
                        "msg": (
                            "此网关已有扫描任务，请等待扫描结束后重试"
                            if running
                            else f"扫描冷却中，请 {remaining} 秒后重试"
                        ),
                        "retry_after": remaining,
                    },
                    status_code=409 if running else 429,
                )
        ret = mqtt_client.publish_cmd(gw["mac"], "scan", {})
        if not ret["ok"]:
            return JSONResponse(ret)
        conn.execute(
            """INSERT INTO gateway_scan_lease(gateway_id,cmd_id,owner_session,cooldown_until,expires_at)
            VALUES (?,?,?,?,?) ON CONFLICT(gateway_id) DO UPDATE SET cmd_id=excluded.cmd_id,
            owner_session=excluded.owner_session,cooldown_until=excluded.cooldown_until,expires_at=excluded.expires_at""",
            (
                gid,
                ret["cmd_id"],
                (actor() or {}).get("token_hash", ""),
                stamp + 10,
                stamp + 200,
            ),
        )
    # 返回包含cmd_id，前端JS读取
    return JSONResponse(ret)


@router.post("/gateway/{gid}/scan/cancel")
async def cancel_scan(gid: GatewayId, cmd_id: str = Form(...)):
    with mutation() as conn:
        gw = gateway_snapshot(conn, gid)
        if error := gateway_action_error(gw, scan=True):
            return error
        lease = conn.execute(
            "SELECT * FROM gateway_scan_lease WHERE gateway_id=? AND cmd_id=?",
            (gid, cmd_id),
        ).fetchone()
        if lease and actor() and lease["owner_session"] != actor()["token_hash"]:
            return JSONResponse(
                {"ok": False, "msg": "此扫描由其他会话发起，不能停止"}, status_code=409
            )
        with mqtt_client._lock:
            pending = mqtt_client.pending_acks.get(cmd_id)
            if pending and (
                pending["gateway_mac"] != norm_mac(gw["mac"])
                or pending["cmd_type"] != "scan"
            ):
                return JSONResponse(
                    {"ok": False, "msg": "扫描任务不属于此网关"}, status_code=400
                )
        if not pending:
            return JSONResponse({"ok": True, "msg": "扫描已结束", "stopped": True})
        result = mqtt_client.publish_cmd(
            gw["mac"], "scan_cancel", {"scan_cmd_id": cmd_id}
        )
    if not result["ok"]:
        return JSONResponse(result)
    # The cancellation ACK only means the flag was set. The original scan ACK
    # proves its worker finished; keep the task lock until that happens.
    for _ in range(80):
        if mqtt_client.get_cmd_status(cmd_id)["status"] != "pending":
            return JSONResponse({"ok": True, "msg": "扫描已停止", "stopped": True})
        await asyncio.sleep(0.1)
    return JSONResponse(
        {
            "ok": False,
            "msg": "正在等待上次扫描停止，请稍后重新打开扫描工具",
            "stopped": False,
        }
    )


@router.post("/gateway/{gid}/scan/add")
async def add_from_scan(gid: GatewayId, ids: str = Form(...), names: str = Form("{}")):
    try:
        device_names = json.loads(names)
        if not isinstance(device_names, dict):
            raise ValueError("names must be an object")
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "msg": "设备名称格式错误"}, status_code=400)
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return JSONResponse({"ok": False, "msg": "未选择"})
    added, skipped = 0, []
    with mutation(("gateway", gid)) as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if error := gateway_action_error(gateway_snapshot(conn, gid), scan=True):
            return error
        ph = ",".join("?" * len(id_list))
        count = conn.execute(
            "SELECT COUNT(*) FROM sub_device WHERE gateway_id=?", (gid,)
        ).fetchone()[0]
        cur = conn.execute(
            f"SELECT id, ip, mac, device_name FROM scan_result WHERE gateway_id=? AND id IN ({ph})",
            [gid, *id_list],
        )
        selected = cur.fetchall()
        for r in selected:
            mac = norm_mac(r["mac"])
            # 同网关下 MAC 去重
            dup = conn.execute(
                "SELECT id FROM sub_device WHERE gateway_id=? AND mac=?", (gid, mac)
            ).fetchone()
            if dup:
                skipped.append(f"{r['ip']}({mac}) 已存在")
                continue
            if count + added >= 64:
                skipped.append(f"{r['ip']}（每个网关最多 64 台设备）")
                continue
            name = str(device_names.get(str(r["id"])) or r["device_name"] or "").strip()
            if not name:
                name = f"未命名设备-{mac[-6:]}"
            if not valid_name(name):
                skipped.append(f"{r['ip']}：{NAME_ERROR}")
                continue
            conn.execute(
                "INSERT INTO sub_device (gateway_id, name, mac, ip, status) VALUES (?,?,?,?,'offline')",
                (gid, name, mac, r["ip"]),
            )
            added += 1
        pushed = False
        if added:
            pushed = push_subdevices(conn, gid)
    return JSONResponse(
        {"ok": True, "added": added, "skipped": skipped, "pushed_to_gateway": pushed}
    )
