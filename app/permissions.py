"""Browser capabilities; ownership and resource state remain separate checks."""

import json
import re
from contextvars import ContextVar

from fastapi import HTTPException

CATALOG = (
    ("account_edit", "修改账户信息", "账户"),
    ("gateway_view", "查看网关列表", "网关"),
    ("gateway_add", "添加网关", "网关"),
    ("gateway_edit", "编辑网关", "网关"),
    ("gateway_ota", "远程OTA更新", "网关"),
    ("gateway_ota_auto", "开/关 OTA 自动更新", "网关"),
    ("gateway_delete", "删除网关", "网关"),
    ("gateway_logs_view", "查看网关日志", "网关"),
    ("gateway_logs_clear", "删除网关日志", "网关"),
    ("pairing_renew", "重新生成配对码", "网关"),
    ("gateway_transfer", "生成网关转移码", "网关"),
    ("device_view", "查看设备列表", "设备"),
    ("device_add", "添加设备", "设备"),
    ("device_scan", "扫描设备", "设备"),
    ("device_edit", "编辑设备", "设备"),
    ("device_power", "电源控制", "设备"),
    ("device_share", "分享设备", "设备"),
    ("device_service", "绑定/解绑WOL关机服务", "设备"),
    ("device_delete", "删除设备", "设备"),
)
KEYS = frozenset(key for key, _, _ in CATALOG)
required_permissions = ContextVar("wol_required_permissions", default=())


def decode_permissions(raw):
    if raw == "*":  # Existing and newly created users retain current capabilities.
        return sorted(KEYS)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, list) or any(
            not isinstance(k, str) or k not in KEYS for k in value
        ):
            return []
        return sorted(set(value))
    except (TypeError, ValueError):
        return []


def allowed(value, key):
    return bool(
        value
        and value["enabled"]
        and (
            value["role"] == "admin"
            or key in decode_permissions(value.get("permissions", "*"))
        )
    )


def enforce(value, capabilities):
    if (
        value
        and value["role"] != "admin"
        and any(
            not any(allowed(value, key) for key in group.split("|"))
            for group in capabilities
        )
    ):
        raise HTTPException(403, "没有使用此功能的权限，请联系管理员")


def route_permissions(path, method):
    read = method in {"GET", "HEAD", "OPTIONS"}
    if path in {"/api/account", "/account"}:
        return ("account_edit",) if not read or path == "/account" else ()
    if path in {"/api/gateways", "/api/gateways/search", "/api/pairings"}:
        return ("gateway_view",)
    if path == "/gateway/create":
        return ("gateway_add",)
    if path == "/gateway/batch_unbind":
        return ("gateway_delete",)
    if path == "/gateway/batch_update":
        return ("gateway_edit",)
    if path in {
        "/gateway/batch_ota",
        "/gateway/batch_ota/status",
        "/gateway/batch_ota/check",
        "/gateway/batch_ota/latest",
    }:
        return ("gateway_ota",)
    if path == "/api/logs/clear":
        return ("gateway_logs_clear",)
    if path.startswith("/gateway/cmd_status/"):
        return ("device_scan|device_power|gateway_edit|gateway_delete",)
    if re.fullmatch(r"/pairing/[^/]+/renew", path):
        return ("pairing_renew",)
    if re.fullmatch(r"/pairing/[^/]+/(delete|cancel)", path):
        return ("gateway_delete",)
    if re.fullmatch(r"/pairing/[^/]+/rename", path):
        return ("gateway_edit",)
    match = re.fullmatch(r"/(?:api/)?gateway/[^/]+/(.+)", path)
    if not match:
        return ()
    tail = match[1]
    if tail == "availability":
        return ("gateway_view|device_view|device_scan|gateway_logs_view",)
    if tail in {"logs", "log_entries"}:
        return ("gateway_logs_view",)
    if tail == "key":
        return ("gateway_add",)
    if tail == "pairing/renew":
        return ("pairing_renew",)
    if tail == "transfer":
        return ("gateway_transfer",)
    if tail == "ota/auto":
        return ("gateway_ota_auto",)
    if tail in {"ota/check", "ota/latest"}:
        return ("gateway_ota",)
    if tail == "ota":
        return (
            ("gateway_edit|gateway_ota|gateway_ota_auto",) if read else ("gateway_ota",)
        )
    if tail in {"update", "rename"}:
        return ("gateway_edit",)
    if tail in {"delete", "unbind", "delete/cancel"}:
        return ("gateway_delete",)
    if tail in {"scan", "scan/trigger", "scan/cancel", "scan_results"}:
        return ("device_scan",)
    if tail == "scan/add":
        return ("device_scan", "device_add")
    if tail == "devices/batch_add":
        return ("device_add",)
    if tail == "devices/batch_unbind_service" or re.fullmatch(
        r"devices/[^/]+/(windows-service|power/install|power/revoke)", tail
    ):
        return ("device_service",)
    if tail == "devices/batch_delete" or re.fullmatch(r"devices/[^/]+/delete", tail):
        return ("device_delete",)
    if tail == "devices/batch_share" or re.fullmatch(
        r"devices/[^/]+/share(?:/[^/]+)?", tail
    ):
        return ("device_share",)
    if re.fullmatch(r"devices/[^/]+/update", tail):
        return ("device_edit",)
    if tail == "devices/wake" or re.fullmatch(r"devices/[^/]+/shutdown", tail):
        return ("device_power",)
    if tail in {"devices", "device_results", "device_status", "power"}:
        return ("device_view",)
    return ()
