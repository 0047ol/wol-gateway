"""Queue gateway factory-reset commands and remove records only after ACK."""

import asyncio
import logging
from datetime import datetime

from .database import db, now
from .config import settings
from .mqtt_client import mqtt_client

log = logging.getLogger("gateway.deletion")
RETRY_SECONDS = 10


def remove_gateway(conn, gateway_id):
    row = conn.execute("SELECT mac FROM gateway WHERE id=?", (gateway_id,)).fetchone()
    if not row:
        return
    commands = [
        r[0]
        for r in conn.execute(
            "SELECT cmd_id FROM cmd_log WHERE gateway_id=?", (gateway_id,)
        )
    ]
    conn.execute("DELETE FROM cmd_log WHERE gateway_id=?", (gateway_id,))
    conn.execute("DELETE FROM gateway WHERE id=?", (gateway_id,))
    prefix = f"{settings.MQTT_TOPIC_PREFIX}/{row['mac']}/"
    with mqtt_client._lock:
        kept = [
            item for item in mqtt_client.msg_log if not item["topic"].startswith(prefix)
        ]
        mqtt_client.msg_log.clear()
        mqtt_client.msg_log.extend(kept)
        for cmd_id in commands:
            mqtt_client.cmd_log.pop(cmd_id, None)
            mqtt_client.pending_acks.pop(cmd_id, None)
        mqtt_client.task_locks.pop(row["mac"], None)


def queue_gateway_deletion(gateway_id: int, force_offline: bool = False):
    with db() as conn:
        gateway = conn.execute(
            "SELECT * FROM gateway WHERE id=?", (gateway_id,)
        ).fetchone()
        if not gateway:
            return {"ok": False, "msg": "网关不存在", "status": 404}
        if gateway["delete_requested_at"]:
            return {"ok": False, "msg": "网关正在等待删除，请先撤销删除", "status": 409}
        if gateway["status"] == "unbound":
            remove_gateway(conn, gateway_id)
            return {"ok": True, "msg": "未完成绑定的网关记录已删除"}
        if gateway["status"] != "online" and force_offline:
            remove_gateway(conn, gateway_id)
            return {
                "ok": True,
                "forced": True,
                "msg": "已强制删除后台网关记录；设备本地配置未确认清除",
            }
        conn.execute(
            """UPDATE gateway SET delete_requested_at=?, delete_cmd_id='',
                   delete_last_attempt='', delete_error='' WHERE id=?""",
            (now(), gateway_id),
        )
        dispatch = gateway["status"] == "online"
        result = {
            "ok": True,
            "queued": True,
            "msg": (
                "已发送删除请求，等待设备确认重置"
                if dispatch
                else "已排队；设备重新上线后将下发重置，确认后删除记录"
            ),
        }
    if dispatch:
        dispatch_gateway_deletion(gateway_id)
    return result


def dispatch_gateway_deletion(gateway_id: int):
    with db() as conn:
        gateway = conn.execute(
            "SELECT * FROM gateway WHERE id=?", (gateway_id,)
        ).fetchone()
        if (
            not gateway
            or not gateway["delete_requested_at"]
            or gateway["status"] != "online"
        ):
            return
        if gateway["delete_cmd_id"]:
            command = mqtt_client.get_cmd_status(gateway["delete_cmd_id"])
            if command.get("status") == "pending":
                return
        if gateway["delete_last_attempt"]:
            try:
                last_attempt = datetime.strptime(
                    gateway["delete_last_attempt"], "%Y-%m-%d %H:%M:%S"
                )
                if (datetime.now() - last_attempt).total_seconds() < RETRY_SECONDS:
                    return
            except ValueError:
                pass
        mac = gateway["mac"]
        conn.execute(
            "UPDATE gateway SET delete_last_attempt=?, delete_cmd_id='' WHERE id=?",
            (now(), gateway_id),
        )

    def set_command_id(cmd_id):
        with db() as conn:
            updated = conn.execute(
                "UPDATE gateway SET delete_cmd_id=? WHERE id=? AND delete_requested_at!=''",
                (cmd_id, gateway_id),
            )
            if not updated.rowcount:
                raise RuntimeError("删除请求已撤销，未下发重置")

    result = mqtt_client.publish_cmd(mac, "reset", {}, before_publish=set_command_id)
    if not result["ok"]:
        with db() as conn:
            conn.execute(
                "UPDATE gateway SET delete_error=? WHERE id=? AND delete_requested_at!=''",
                (result.get("msg", "重置命令下发失败")[:300], gateway_id),
            )
        return

    status = mqtt_client.get_cmd_status(result["cmd_id"]).get("status")
    with db() as conn:
        if (
            status == "ok"
            and conn.execute(
                "SELECT 1 FROM gateway WHERE id=? AND delete_requested_at!='' AND delete_cmd_id=?",
                (gateway_id, result["cmd_id"]),
            ).fetchone()
        ):
            remove_gateway(conn, gateway_id)
        elif status == "fail":
            conn.execute(
                "UPDATE gateway SET delete_error=?, delete_cmd_id='' WHERE id=? AND delete_requested_at!=''",
                ("设备拒绝了重置命令", gateway_id),
            )
        else:
            conn.execute(
                "UPDATE gateway SET delete_cmd_id=?, delete_error='' WHERE id=? AND delete_requested_at!=''",
                (result["cmd_id"], gateway_id),
            )


def cancel_gateway_deletion(gateway_id: int):
    with db() as conn:
        updated = conn.execute(
            """UPDATE gateway SET delete_requested_at='', delete_cmd_id='',
                   delete_last_attempt='', delete_error=''
               WHERE id=? AND delete_requested_at!=''
                 AND delete_cmd_id='' AND delete_last_attempt=''""",
            (gateway_id,),
        )
        if updated.rowcount:
            return {"ok": True, "msg": "已撤销删除，网关已恢复正常管理"}
        row = conn.execute(
            "SELECT delete_requested_at FROM gateway WHERE id=?", (gateway_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "msg": "网关不存在", "status": 404}
        if not row["delete_requested_at"]:
            return {"ok": True, "msg": "该网关没有等待删除的请求"}
        return {
            "ok": False,
            "msg": "重置已开始下发，无法撤销删除",
            "status": 409,
        }


def complete_gateway_deletion(cmd_id: str, ok: bool):
    if not cmd_id:
        return
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM gateway WHERE delete_cmd_id=? AND delete_requested_at!=''",
            (cmd_id,),
        ).fetchone()
        if not row:
            return
        if ok:
            remove_gateway(conn, row["id"])
        else:
            conn.execute(
                "UPDATE gateway SET delete_cmd_id='', delete_error=? WHERE id=?",
                ("设备拒绝了重置命令，将在稍后重试", row["id"]),
            )


async def gateway_deletion_watchdog():
    while True:
        try:
            with db() as conn:
                ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM gateway WHERE delete_requested_at!='' ORDER BY id"
                    ).fetchall()
                ]
            for gateway_id in ids:
                dispatch_gateway_deletion(gateway_id)
        except Exception:
            log.exception("gateway deletion watchdog failed")
        await asyncio.sleep(2)
