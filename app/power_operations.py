"""Device-scoped command reservations shared by administrator and guest entry points."""

import json
import time
import uuid

from fastapi.responses import JSONResponse

from .database import db
from .device_status import device_snapshot, gateway_snapshot
from .pc_power import pc_registry


def operation_view(conn, row, gateway):
    operation = conn.execute(
        "SELECT * FROM device_power_operation WHERE device_id=?", (row["id"],)
    ).fetchone()
    if not operation:
        return None
    value = dict(operation)
    if value["status"] in {"sending", "waiting"}:
        status, message = value["status"], value["message"]
        evidence = device_snapshot(row, gateway)
        command = (
            pc_registry.command_view(row["mac"])
            if value["action"] == "shutdown"
            else None
        )
        log = conn.execute(
            "SELECT status FROM cmd_log WHERE cmd_id=?", (value["command_id"],)
        ).fetchone()
        if value["status"] == "waiting" and evidence["status"] == (
            "online" if value["action"] == "wake" else "offline"
        ):
            status, message = "completed", (
                "设备已上线" if value["action"] == "wake" else "设备已离线"
            )
        elif (
            command
            and command["id"] == value["command_id"]
            and command["status"] in {"failed", "rejected", "unconfirmed", "uncertain"}
        ):
            status, message = "failed", "操作结果未确认，请联系分享者检查；不会自动重发"
        elif log and log["status"] in {"fail", "failed", "timeout", "error"}:
            status, message = "failed", "指令执行失败或超时；不会自动重发"
        elif time.time() >= value["expires_at"]:
            status, message = "unconfirmed", "状态尚未确认，请稍后刷新；不会自动重发"
        if status != value["status"]:
            conn.execute(
                "UPDATE device_power_operation SET status=?,message=? WHERE device_id=? AND operation_id=?",
                (status, message, row["id"], value["operation_id"]),
            )
            value.update(status=status, message=message)
    return {
        "id": value["operation_id"],
        "action": value["action"],
        "status": value["status"],
        "message": value["message"],
        "expires_at": value["expires_at"],
    }


async def run_operation(gid, device_ids, action, execute):
    operation_id = uuid.uuid4().hex
    stamp = time.time()
    reserved = []
    with db() as conn:
        gateway = gateway_snapshot(conn, gid)
        for did in set(device_ids):
            row = conn.execute(
                "SELECT * FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
            ).fetchone()
            if not row:
                continue  # The existing executor reports invalid selections.
            current = operation_view(conn, row, gateway)
            if current and current["status"] in {"sending", "waiting"}:
                return JSONResponse(
                    {"ok": False, "msg": "已有未结束的电源请求，请勿重复操作"},
                    status_code=409,
                )
            reserved.append(did)
        for did in reserved:
            conn.execute(
                """INSERT INTO device_power_operation(device_id,operation_id,action,status,created_at,expires_at)
                VALUES (?,?,?,'sending',?,?) ON CONFLICT(device_id) DO UPDATE SET
                operation_id=excluded.operation_id,action=excluded.action,status='sending',
                created_at=excluded.created_at,expires_at=excluded.expires_at,command_id='',message=''""",
                (did, operation_id, action, stamp, stamp + 90),
            )
    try:
        response = await execute()
        payload = (
            json.loads(response.body)
            if isinstance(response, JSONResponse)
            else response
        )
        success = bool(payload.get("ok"))
        with db() as conn:
            conn.execute(
                "UPDATE device_power_operation SET status=?,command_id=?,message=? WHERE operation_id=?",
                (
                    "waiting" if success else "failed",
                    payload.get("cmd_id") or "",
                    (
                        "正在等待设备上线"
                        if success and action == "wake"
                        else (
                            "正在等待设备离线"
                            if success
                            else "指令发送失败；不会自动重发"
                        )
                    ),
                    operation_id,
                ),
            )
        return response
    except BaseException:
        with db() as conn:
            conn.execute(
                "UPDATE device_power_operation SET status='failed',message='指令发送失败；不会自动重发' WHERE operation_id=?",
                (operation_id,),
            )
        raise
