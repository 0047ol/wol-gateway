import asyncio
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import db

log = logging.getLogger("background")


def _parse(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def reconcile_heartbeat_status(conn, now_dt=None):
    """Mark stale gateways offline before serving dashboard snapshots."""
    current = now_dt or datetime.now()
    changed = 0
    rows = conn.execute(
        "SELECT id, status, last_heartbeat, heartbeat_timeout, heartbeat_send_interval FROM gateway WHERE status='online'"
    ).fetchall()
    for row in rows:
        last = _parse(row["last_heartbeat"])
        grace = max(90, row["heartbeat_timeout"], row["heartbeat_send_interval"] * 3)
        if (
            last
            and current - last > timedelta(seconds=grace)
            and row["status"] != "offline"
        ):
            conn.execute("UPDATE gateway SET status='offline' WHERE id=?", (row["id"],))
            changed += 1
    return changed


async def heartbeat_watchdog():
    """网关心跳超时巡检，超时标记离线"""
    while True:
        try:
            with db() as conn:
                changed = reconcile_heartbeat_status(conn)
                if changed:
                    log.info(
                        "marked %s gateway(s) offline after heartbeat timeout", changed
                    )
        except Exception:
            log.exception("heartbeat_watchdog error")
        try:
            from .gateway_deletion import dispatch_gateway_deletion

            with db() as conn:
                pending = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM gateway WHERE delete_requested_at!='' AND status='online'"
                    ).fetchall()
                ]
            for gateway_id in pending:
                dispatch_gateway_deletion(gateway_id)
        except Exception:
            log.exception("gateway deletion retry error")
        await asyncio.sleep(settings.HB_SCAN_INTERVAL)


async def ack_watchdog():
    """定时清理超时 ACK，释放任务锁"""
    from .mqtt_client import mqtt_client

    while True:
        try:
            mqtt_client.check_ack_timeout()
        except Exception:
            log.exception("ack_watchdog error")
        await asyncio.sleep(0.5)


async def subdevice_sync_watchdog():
    from .subdevice_sync import drain_subdevice_sync

    while True:
        try:
            await asyncio.to_thread(drain_subdevice_sync)
        except Exception:
            log.exception("PC unlink gateway synchronization failed")
        await asyncio.sleep(2)


def prune_old_logs(now_dt=None):
    """Expire ordinary logs after three days; keep diagnostics and unfinished work."""
    import json
    from .mqtt_client import mqtt_client, log_severity

    cutoff = (now_dt or datetime.now()) - timedelta(days=3)
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    def diagnostic(payload):
        if payload in ("", "[redacted]"):
            return False
        try:
            return log_severity(json.loads(payload)) in {"warning", "error"}
        except (ValueError, TypeError):
            return True

    with db() as conn:
        rows = conn.execute(
            "SELECT id,cmd_id,payload FROM cmd_log WHERE status='ok' AND created_at<? "
            "AND COALESCE(NULLIF(ack_at,''),created_at)<?",
            (cutoff_text, cutoff_text),
        ).fetchall()
        with mqtt_client._lock:
            removed = [
                row
                for row in rows
                if row["cmd_id"] not in mqtt_client.pending_acks
                and not diagnostic(row["payload"] or "{}")
            ]
            conn.executemany(
                "DELETE FROM cmd_log WHERE id=?", [(row["id"],) for row in removed]
            )
            for row in removed:
                mqtt_client.cmd_log.pop(row["cmd_id"], None)

            def keep_message(item):
                stamp = _parse(item.get("ts"))
                if not stamp or stamp >= cutoff:
                    return True
                if str(item.get("level", "info")).lower() in {
                    "warn",
                    "warning",
                    "error",
                    "critical",
                    "fatal",
                }:
                    return True
                if diagnostic(item.get("payload")):
                    return True
                try:
                    payload = json.loads(item.get("payload", "{}"))
                    command_id = (
                        payload.get("cmd_id") if isinstance(payload, dict) else None
                    )
                    return command_id in mqtt_client.pending_acks
                except (TypeError, ValueError):
                    return True

            kept = [item for item in mqtt_client.msg_log if keep_message(item)]
            removed_messages = len(mqtt_client.msg_log) - len(kept)
            mqtt_client.msg_log.clear()
            mqtt_client.msg_log.extend(kept)
    return {"messages": removed_messages, "commands": len(removed)}


async def log_retention_watchdog():
    while True:
        try:
            removed = await asyncio.to_thread(prune_old_logs)
            if any(removed.values()):
                log.info("expired ordinary logs older than three days: %s", removed)
        except Exception:
            log.exception("log retention cleanup failed")
        await asyncio.sleep(3600)
