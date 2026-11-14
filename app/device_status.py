"""Display current evidence without changing stored gateway probe results."""

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .background import reconcile_heartbeat_status
from .pc_power import pc_registry
from .presentation import gateway_state, gateway_notice, gateway_can_manage
from .access import require_gateway_access


def gateway_snapshot(conn, gid):
    from .concurrency import verify_actor

    verify_actor(conn)
    require_gateway_access(conn, gid)
    reconcile_heartbeat_status(conn)
    row = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
    if not row:
        raise HTTPException(404, "网关不存在")
    return row


def gateway_available(gateway):
    return gateway_state(gateway) == "online"


def gateway_view(gateway):
    return {
        "revision": gateway["edit_revision"],
        "online": gateway_available(gateway),
        "delete_pending": bool(gateway["delete_requested_at"]),
        "status": gateway_state(gateway),
        "message": gateway_notice(gateway),
        "can_manage": gateway_can_manage(gateway),
        "action_error": gateway_action_message(gateway),
    }


def gateway_action_message(gateway, *, allow_offline=True, scan=False):
    if gateway["delete_requested_at"]:
        return "网关正在等待删除，请先撤销删除"
    if gateway["status"] == "unbound":
        return "网关尚未完成绑定，无法操作设备"
    if not gateway_can_manage(gateway):
        return "网关状态异常，仅可查看设备和日志"
    if not allow_offline and not gateway_available(gateway):
        return "网关离线，无法扫描" if scan else "网关离线，无法操作设备"
    return ""


def gateway_action_error(gateway, *, allow_offline=False, scan=False):
    """Apply current management policy again immediately before a mutation."""
    message = gateway_action_message(gateway, allow_offline=allow_offline, scan=scan)
    if not message:
        return None
    return JSONResponse({"ok": False, "msg": message}, status_code=409)


def device_snapshot(row, gateway):
    from .mqtt_client import mqtt_client

    value = dict(row)
    available = gateway_available(gateway)
    value["observed_status"] = value["status"]
    value["gateway_online"] = available
    value["status_reason"] = ""
    pc = pc_registry.get(row["mac"])
    if value.get("pc_service_bound") and not value.get("pc_service_ignored"):
        value["status_source"] = "pc"
        value["last_report"] = value.get("pc_last_report") or ""
        if not mqtt_client.connected_flag or not pc or pc["upgrade_required"]:
            value["status"] = "unknown"
            value["status_reason"] = "WOL关机服务状态未知，等待有效上报"
        else:
            value["status"] = "online" if pc["online"] else "offline"
            value["status_reason"] = "状态由WOL关机服务上报"
            if not available and pc["online"]:
                value["status_reason"] = "网关离线；WOL关机服务仍在线"
    else:
        value["status_source"] = "gateway"
        if not available:
            value["status"] = "unknown"
            value["status_reason"] = (
                "网关等待删除，设备状态未知；保留最后上报时间"
                if gateway["delete_requested_at"]
                else "网关离线，设备状态未知；保留最后上报时间"
            )
    return value
