"""Minimal PC service protocol. Live sessions, never retained command replay."""

import ipaddress
import json
import re
import threading
import time

from .database import db, now
from .utils import norm_mac
from .client_policy import pc_allowed
from .config import settings
from .access import service_mac_conflict


class PCRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.commands = {}

    def track_command(self, mac, command_id, session):
        with self.lock:
            self.commands[norm_mac(mac)] = {
                "id": command_id,
                "session": session,
                "status": "pending",
                "sent_at": time.monotonic(),
                "exit_code": None,
            }
            if len(self.commands) > 1000:
                self.commands.pop(next(iter(self.commands)))

    def command_view(self, mac):
        with self.lock:
            command = self.commands.get(norm_mac(mac))
            if not command:
                return None
            status = command["status"]
            if time.monotonic() - command["sent_at"] > 35 and status in (
                "pending",
                "accepted",
            ):
                status = "unconfirmed"
            messages = {
                "pending": "关机指令已发送，等待电脑确认接收",
                "accepted": "电脑已接收关机指令，等待系统执行结果",
                "submitted": "电脑已提交关机请求，请等待电脑离线",
                "failed": "电脑执行关机失败，请查看电脑端诊断日志",
                "rejected": "电脑拒绝关机指令，请检查电脑系统时间或诊断日志",
                "unconfirmed": "未收到电脑执行结果，请检查服务或诊断日志；不会自动重发",
                "uncertain": "发送结果未确认，请检查电脑；不会自动重发",
            }
            return {
                "id": command["id"],
                "status": status,
                "message": messages.get(status, messages["unconfirmed"]),
                "exit_code": command["exit_code"],
            }

    def receive_ack(self, topic, payload, retained=False):
        match = re.fullmatch(r"wol/pc/([0-9a-f]{12})/ack", topic)
        if not match:
            return False
        if retained or len(payload) > 4096:
            return True
        try:
            value = json.loads(payload)
            with self.lock:
                command = self.commands.get(match[1])
                if (
                    not command
                    or value.get("id") != command["id"]
                    or value.get("session") != command["session"]
                ):
                    return True
                if time.monotonic() - command["sent_at"] > 60:
                    return True
                status = value.get("status")
                allowed = {
                    "pending": {"accepted", "rejected"},
                    "accepted": {"submitted", "failed"},
                }
                if status not in allowed.get(command["status"], set()):
                    return True
                command["status"] = status
                code = value.get("exit_code")
                command["exit_code"] = (
                    code
                    if type(code) is int and -2147483648 <= code <= 2147483647
                    else None
                )
        except (ValueError, TypeError, AttributeError):
            pass
        return True

    def reset_online(self):
        with self.lock:
            for item in self.items.values():
                item["seen"] = 0

    def forget(self, mac):
        mac = norm_mac(mac)
        with self.lock:
            self.items.pop(mac, None)
            self.commands.pop(mac, None)

    def receive(self, topic, payload, retained=False):
        match = re.fullmatch(r"wol/pc/([0-9a-f]{12})/status", topic)
        if not match:
            return False
        if len(payload) > 4096:
            return True
        try:
            value = json.loads(payload)
            if (
                not isinstance(value, dict)
                or norm_mac(value.get("mac", "")) != match[1]
            ):
                return True
            if not re.fullmatch(r"[0-9a-f]{32}", value.get("session", "")):
                return True
            if value.get("state") not in ("online", "offline"):
                return True
            ip = str(ipaddress.IPv4Address(value["ip"]))
            item = {
                key: str(value.get(key, ""))[:128]
                for key in ("name", "mac", "session", "version")
            }
            allowed = pc_allowed(value.get("version"))
            item.update(
                upgrade_required=not allowed,
                error=(
                    ""
                    if allowed
                    else "WOL关机服务版本过低，请升级到 " + settings.MIN_PC_VERSION
                ),
            )
            item.update(
                mac=match[1],
                ip=ip,
                seen=(
                    time.monotonic()
                    if allowed and not retained and value["state"] == "online"
                    else 0
                ),
            )
            with self.lock:
                self.items[match[1]] = item
                if len(self.items) > 1000:
                    self.items.pop(next(iter(self.items)))
            with db() as conn:
                if service_mac_conflict(conn, match[1]):
                    return True
                if not retained:
                    conn.execute(
                        "UPDATE sub_device SET pc_last_report=? WHERE replace(replace(lower(mac),':',''),'-','')=? AND pc_service_ignored=0 AND pc_service_bound=1",
                        (now(), match[1]),
                    )
                if item["seen"]:
                    stamp = now()
                    conn.execute(
                        """UPDATE sub_device SET ip=?,status='online',last_report=?,last_online=?,
                        offline_since='',offline_last_check='',offline_checks=0
                        WHERE replace(replace(lower(mac),':',''),'-','')=? AND pc_service_ignored=0 AND pc_service_bound=1""",
                        (ip, stamp, stamp, match[1]),
                    )
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        return True

    def get(self, mac):
        mac = norm_mac(mac)
        with self.lock:
            value = self.items.get(mac)
            if not value:
                return None
            return {
                **value,
                "online": bool(value["seen"] and time.monotonic() - value["seen"] < 65),
            }


pc_registry = PCRegistry()
