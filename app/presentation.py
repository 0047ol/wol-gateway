"""Presentation states do not change gateway control/deletion semantics."""

from datetime import datetime, timedelta

GATEWAY_LABELS = {
    "online": "在线",
    "offline": "离线",
    "long_offline": "长期离线",
    "unbound": "待绑定",
    "deleting": "待删除",
    "reset_failed": "重置失败",
    "pairing_expired": "配对超时",
    "pairing_error": "配对码错误",
    "bind_failed": "绑定失败",
    "error": "异常",
}

STATUS_FILTERS = ("all", "online", "offline", "normal", "abnormal", "unknown")


def normalize_status_filter(value, *, unknown=False):
    allowed = STATUS_FILTERS if unknown else STATUS_FILTERS[:-1]
    return value if value in allowed else "all"


def matches_status_filter(state, selected):
    normal = state in ("online", "offline", "long_offline")
    if selected == "normal":
        return normal
    if selected == "abnormal":
        return not normal
    if selected == "offline":
        return state in ("offline", "long_offline")
    return selected == "all" or state == selected


def gateway_state(row, current=None):
    value = dict(row)
    message = str(value.get("bind_message") or "").strip()
    if value.get("delete_requested_at"):
        return "reset_failed" if value.get("delete_error") else "deleting"
    if "配对码" in message and any(
        word in message for word in ("错误", "无效", "不正确")
    ):
        return "pairing_error"
    if any(word in message for word in ("配对超时", "配对码已过期", "配对码过期")):
        return "pairing_expired"
    if "密钥不匹配" in message or "绑定失败" in message:
        return "bind_failed"
    if message and message not in (
        "配对完成，等待设备注册",
        "等待设备上线",
        "等待配对",
    ):
        return "error"
    if value.get("status") == "offline" and value.get("last_heartbeat"):
        try:
            last = datetime.strptime(value["last_heartbeat"], "%Y-%m-%d %H:%M:%S")
            if (current or datetime.now()) - last > timedelta(days=15):
                return "long_offline"
        except (ValueError, TypeError):
            pass
    return (
        value.get("status")
        if value.get("status") in ("online", "offline", "unbound")
        else "error"
    )


def gateway_counts(rows, pending_pairings=()):
    """Keep three dashboard totals independent of the more detailed badges."""
    counts = {"online": 0, "offline": 0, "abnormal": 0}
    for row in rows:
        state = gateway_state(row)
        category = "offline" if state == "long_offline" else state
        counts[category if category in counts else "abnormal"] += 1
    counts["abnormal"] += sum(1 for _ in pending_pairings)
    return counts


def gateway_notice(row):
    value = dict(row)
    if value.get("delete_requested_at"):
        return value.get("delete_error") or (
            "设备离线；已排队，等待上线后重置"
            if value.get("status") == "offline"
            else "等待设备确认重置"
        )
    if gateway_state(value) == "long_offline":
        return "已超过 15 天未收到有效心跳；最后心跳：" + value["last_heartbeat"]
    return value.get("bind_message") or ""


def gateway_can_manage(row):
    """Only healthy registered gateways permit device/configuration mutations."""
    return gateway_state(row) in ("online", "offline", "long_offline")


def gateway_can_cancel_delete(row):
    value = dict(row)
    return bool(value.get("delete_requested_at")) and not (
        value.get("delete_cmd_id") or value.get("delete_last_attempt")
    )


def gateway_can_transfer(row):
    """A healthy online or offline gateway may be replaced in place."""
    value = dict(row)
    return gateway_can_manage(value)


def gateway_can_repair_pairing(row):
    """Recovery pairing is only needed for a healthy offline gateway."""
    value = dict(row)
    return value.get("status") == "offline" and gateway_can_manage(value)
