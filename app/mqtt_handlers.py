"""MQTT 上行消息处理器：register / heartbeat / subdevice/report / scan/result / cmd/ack"""

import logging
import re

from .database import db, now
from .client_policy import gateway_allowed
from .mqtt_client import mqtt_client
from .pairing import claim_pairing, normalize_code
from .utils import norm_mac, fmt_mac
from .presence import record_probe

log = logging.getLogger("mqtt.handler")


def _get_gateway_by_mac(cur, mac: str):
    return cur.execute("SELECT * FROM gateway WHERE mac = ?", (mac,)).fetchone()


def _reported_ssid(conn, data, gateway_id):
    """Old firmware omits this field. Never erase its stored SSID on omission."""
    value = data.get("wifi_ssid")
    if not isinstance(value, str) or not value or "\x00" in value:
        return
    try:
        valid = len(value.encode("utf-8")) <= 32
    except UnicodeEncodeError:
        return
    if valid:
        conn.execute("UPDATE gateway SET wifi_ssid=? WHERE id=?", (value, gateway_id))


def handle_pair(topic: str, data: dict):
    mac = norm_mac(topic.split("/")[2])
    request_id = data.get("request_id", "")
    if (
        not re.fullmatch(r"[0-9a-f]{12}", mac)
        or not isinstance(request_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", request_id)
    ):
        return
    if norm_mac(data.get("gateway_mac") or mac) != mac:
        return
    if not gateway_allowed(data.get("firmware_version")):
        mqtt_client.publish_pairing_result(
            mac, request_id, {"code": "version_unsupported"}, ""
        )
        return
    code = normalize_code(data.get("pairing_code", ""))
    result = claim_pairing(mac, code)
    mqtt_client.publish_pairing_result(mac, request_id, result, code)


def handle_register(topic: str, data: dict):
    """ESP32 注册上报：校验 MAC+密钥，绑定/上线；下行全套业务配置。"""
    mac = norm_mac(data.get("gateway_mac") or topic.split("/")[2])
    topic_mac = norm_mac(topic.split("/")[2])
    if mac != topic_mac:
        return
    if not gateway_allowed(data.get("firmware_version")):
        mqtt_client.publish_registration_result(
            mac, data.get("request_id", ""), "version_unsupported"
        )
        return
    request_id = data.get("request_id", "")
    bind_key = data.get("bind_key", "")
    local_ip = data.get("local_ip", "")
    netmask = data.get("netmask", "")
    gateway_ip = data.get("gateway_ip", "")
    cidr = data.get("cidr", "")

    with db() as conn:
        row = _get_gateway_by_mac(conn, mac)
        if not row:
            log.warning("register rejected: mac %s not found", mac)
            mqtt_client.publish_registration_result(
                mac, request_id, "gateway_not_found"
            )
            return
        if row["bind_key"] != bind_key:
            conn.execute(
                "UPDATE gateway SET bind_message = ? WHERE id = ?",
                ("绑定失败：密钥不匹配", row["id"]),
            )
            log.warning("register rejected: bind_key mismatch for %s", mac)
            mqtt_client.publish_registration_result(mac, request_id, "key_mismatch")
            return

        conn.execute(
            "UPDATE gateway SET firmware_version=? WHERE id=?",
            (data.get("firmware_version", ""), row["id"]),
        )
        _reported_ssid(conn, data, row["id"])
        from .ota import reported

        reported(conn, row, data, registration=True)

        # 绑定成功
        conn.execute(
            """UPDATE gateway
               SET ip=?, netmask=?, gateway_ip=?, status='online',
                   last_heartbeat=?, bind_message=''
               WHERE id=?""",
            (local_ip, netmask, gateway_ip, now(), row["id"]),
        )
        # 留空表示固件自动选择本地 /24，不把较大的 DHCP 子网保存成扫描范围。

        # 拉取该网关全量子设备
        from .subdevice_sync import configured_devices

        devices = configured_devices(conn, row["id"])

        config_payload = {
            "name": row["name"],
            "registration_request_id": request_id,
            "ping_interval": row["ping_interval"],
            "heartbeat_send_interval": row["heartbeat_send_interval"],
            "wol_broadcast_addr": row["wol_broadcast_addr"],
            "scan_cidr": row["scan_cidr"] or "",
            "scan_concurrency": row["scan_concurrency"],
            "sub_devices": devices,
        }

    mqtt_client.handle_gateway_uptime(
        mac, data.get("uptime_ms"), data.get("uptime_reported_at")
    )
    # 下发 sync_sub_devices + update_config（合并成一个 sync_all 指令）
    ret = mqtt_client.publish_cmd(mac, "sync_all", config_payload)
    mqtt_client.publish_registration_result(
        mac, request_id, "accepted" if ret["ok"] else "sync_unavailable"
    )
    log.info("gateway %s bound, config pushed", fmt_mac(mac))


def handle_heartbeat(topic: str, data: dict):
    mac = norm_mac(topic.split("/")[2])
    ip = data.get("local_ip", "")
    with db() as conn:
        conn.execute(
            "UPDATE gateway SET ip=?, status='online', last_heartbeat=? WHERE mac=? AND status != 'unbound'",
            (ip, now(), mac),
        )
        row = _get_gateway_by_mac(conn, mac)
        if row and row["status"] != "unbound":
            _reported_ssid(conn, data, row["id"])
            from .ota import reported

            reported(conn, row, data)
    mqtt_client.handle_gateway_uptime(
        mac, data.get("uptime_ms"), data.get("uptime_reported_at")
    )


def handle_subdevice_report(topic: str, data: dict):
    """批量上报子设备状态：[{mac, status}]"""
    mac = norm_mac(topic.split("/")[2])
    devices = data.get("devices", [])
    with db() as conn:
        row = _get_gateway_by_mac(conn, mac)
        if not row:
            return
        seen = set()
        for d in devices:
            dmac = norm_mac(d.get("mac", ""))
            if not dmac or dmac in seen:
                continue
            seen.add(dmac)
            record_probe(conn, row, dmac, d.get("status"))


def handle_scan_result(topic: str, data: dict):
    mac = norm_mac(topic.split("/")[2])
    hosts = data.get("hosts", [])
    partial = bool(data.get("partial"))
    scan_cmd_id = str(data.get("cmd_id") or "")[:96]
    with db() as conn:
        row = _get_gateway_by_mac(conn, mac)
        if not row:
            return
        if partial:
            if data.get("reset"):
                conn.execute("DELETE FROM scan_result WHERE gateway_id=?", (row["id"],))
            elif (
                hosts
                and not conn.execute(
                    "SELECT 1 FROM scan_result WHERE gateway_id=? AND scan_cmd_id=? LIMIT 1",
                    (row["id"], scan_cmd_id),
                ).fetchone()
            ):
                # Recover if the reset marker was lost: the first host of a new
                # incremental scan starts a fresh result set.
                conn.execute("DELETE FROM scan_result WHERE gateway_id=?", (row["id"],))
        else:
            conn.execute("DELETE FROM scan_result WHERE gateway_id=?", (row["id"],))
        for h in hosts:
            ip = h.get("ip", "")
            hmac = norm_mac(h.get("mac", ""))
            if not ip or not hmac:
                continue
            if (
                partial
                and conn.execute(
                    "SELECT 1 FROM scan_result WHERE gateway_id=? AND scan_cmd_id=? AND mac=?",
                    (row["id"], scan_cmd_id, hmac),
                ).fetchone()
            ):
                continue
            conn.execute(
                """INSERT INTO scan_result
                   (gateway_id, ip, mac, created_at, device_name, model, os, info_source, scan_cmd_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    ip,
                    hmac,
                    now(),
                    str(h.get("device_name") or "")[:128],
                    str(h.get("model") or "")[:128],
                    str(h.get("os") or "")[:128],
                    str(h.get("info_source") or "")[:128],
                    scan_cmd_id,
                ),
            )
    mqtt_client.handle_scan_complete(topic, data)


def handle_cmd_ack(topic: str, data: dict):
    """ACK 必须匹配 cmd_id、网关 MAC 和指令类型。"""
    return mqtt_client.handle_cmd_ack_callback(topic, data)


def register_handlers():
    mqtt_client.on("pair", handle_pair)
    mqtt_client.on("register", handle_register)
    mqtt_client.on("heartbeat", handle_heartbeat)
    mqtt_client.on("subdevice/report", handle_subdevice_report)
    mqtt_client.on("scan/result", handle_scan_result)
    mqtt_client.on("cmd/ack", handle_cmd_ack)
