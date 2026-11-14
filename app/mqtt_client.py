"""MQTT transport, signed commands and exact ACK correlation."""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque

import paho.mqtt.client as mqtt

from .config import settings
from .database import db, get_gateway_by_mac, now
from .utils import norm_mac

log = logging.getLogger(__name__)


def redact_log_payload(value):
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if key
                in {
                    "bind_key",
                    "pairing_code",
                    "password",
                    "mqtt_password",
                    "signed_body",
                    "sign",
                    "token",
                }
                else redact_log_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_log_payload(item) for item in value]
    return value


def log_severity(value):
    """Use explicit protocol severity/result fields, never device presence states."""
    if isinstance(value, dict):
        if value.get("error") or value.get("errors"):
            return "error"
        levels = [
            str(value.get(key, "")).lower() for key in ("level", "severity", "status")
        ]
        if value.get("ok") is False or any(
            level
            in {"error", "critical", "fatal", "fail", "failed", "timeout", "rejected"}
            for level in levels
        ):
            return "error"
        nested = [log_severity(item) for item in value.values()]
        if "error" in nested:
            return "error"
        if (
            value.get("warning")
            or value.get("warnings")
            or any(level in {"warn", "warning"} for level in levels)
            or "warning" in nested
        ):
            return "warning"
    elif isinstance(value, list):
        nested = [log_severity(item) for item in value]
        for level in ("error", "warning"):
            if level in nested:
                return level
    return "info"


class MQTTClient:
    def __init__(self, broker_host, broker_port, server_username, server_password):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client = mqtt.Client(client_id="wol_server")
        if server_username:
            self.client.username_pw_set(server_username, server_password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        if settings.MQTT_CA_FILE:
            self.client.tls_set(ca_certs=settings.MQTT_CA_FILE)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe
        self._event_handlers = {}
        self._lock = threading.RLock()
        self.msg_log = deque(maxlen=100)
        self.message_sequence = int(time.time() * 1_000_000)
        self.pending_acks = {}
        self.cmd_log = {}
        self.task_locks = {}
        self.connected_flag = False
        self.connected_since_monotonic = None
        self.last_connect_ts = None
        self.last_disconnect_ts = None
        self.history_started_at = None
        self.connect_count = 0
        self.disconnect_count = 0
        self.last_disconnect_code = None
        self.last_connection_error = None
        self.last_connection_attempt = None
        self.history_persisted = False
        self._probe_topic = f"{settings.MQTT_TOPIC_PREFIX}/_health/{uuid.uuid4().hex}"
        self._probe_ready = False
        self._probe_subscription = None
        self._probe = None
        self._probe_result = {"status": "unknown", "latency_ms": None}
        self._probe_attempted = None

    def health_probe(self):
        """Non-blocking, cached self-echo; never publish a device command."""
        clock = time.monotonic()
        metadata = {
            "scope": "application_broker_loopback",
            "cache_seconds": 15,
            "timeout_seconds": 3,
        }
        with self._lock:
            if not self.client.is_connected() or not self.connected_flag:
                return {
                    **metadata,
                    "status": "disconnected",
                    "latency_ms": None,
                    "checked_at": None,
                }
            if not self._probe_ready:
                return {
                    **metadata,
                    "status": (
                        "error"
                        if self._probe_result.get("status") == "error"
                        else "unknown"
                    ),
                    "latency_ms": None,
                    "checked_at": None,
                }
            if self._probe and clock - self._probe[1] >= 3:
                self._probe = None
                self._probe_result = {"status": "timeout", "latency_ms": None}
            if self._probe_attempted is None or clock - self._probe_attempted >= 15:
                nonce = uuid.uuid4().hex
                self._probe = (nonce, clock)
                self._probe_attempted = clock
                if self._probe_result.get("status") != "responding":
                    self._probe_result = {"status": "probing", "latency_ms": None}
                try:
                    sent = self.client.publish(
                        self._probe_topic, nonce, qos=0, retain=False
                    )
                    if sent.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError("publish_failed")
                except Exception:
                    self._probe = None
                    self._probe_result = {"status": "error", "latency_ms": None}
            return {
                **self._probe_result,
                **metadata,
                "checked_at": self._probe_result.get("checked_at"),
            }

    def _receive_health_probe(self, msg):
        if msg.topic != self._probe_topic:
            return False
        with self._lock:
            if (
                self._probe
                and not msg.retain
                and msg.payload == self._probe[0].encode()
            ):
                elapsed = time.monotonic() - self._probe[1]
                self._probe_result = {
                    "status": "responding" if elapsed < 3 else "timeout",
                    "latency_ms": round(elapsed * 1000, 2) if elapsed < 3 else None,
                    "checked_at": now(),
                }
                self._probe = None
        return True

    def _on_subscribe(self, client, userdata, mid, granted_qos):
        if mid == self._probe_subscription:
            self._probe_ready = bool(granted_qos) and all(
                qos < 128 for qos in granted_qos
            )
            if not self._probe_ready:
                self._probe_result = {"status": "error", "latency_ms": None}

    @property
    def _history_key(self):
        return f"mqtt:{self.broker_host}:{self.broker_port}"

    def _load_history(self):
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT value FROM runtime_state WHERE name=?", (self._history_key,)
                ).fetchone()
            state = json.loads(row[0]) if row else {}
            if not isinstance(state, dict):
                state = {}
            for name in (
                "last_connect_ts",
                "last_disconnect_ts",
                "history_started_at",
                "connect_count",
                "disconnect_count",
                "last_disconnect_code",
                "last_connection_error",
                "last_connection_attempt",
            ):
                if name in state:
                    setattr(self, name, state[name])
            self.history_persisted = True
        except Exception:
            log.exception("Unable to restore MQTT connection history")
        self.history_started_at = self.history_started_at or now()

    def _save_history(self):
        self.history_started_at = self.history_started_at or now()
        state = {
            name: getattr(self, name)
            for name in (
                "last_connect_ts",
                "last_disconnect_ts",
                "history_started_at",
                "connect_count",
                "disconnect_count",
                "last_disconnect_code",
                "last_connection_error",
                "last_connection_attempt",
            )
        }
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO runtime_state(name,value) VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (self._history_key, json.dumps(state)),
                )
            self.history_persisted = True
        except Exception:
            self.history_persisted = False
            log.exception("Unable to persist MQTT connection history")

    def _on_connect_fail(self, client, userdata):
        self._probe_ready = False
        self._probe = None
        self.connected_flag = False
        self.connected_since_monotonic = None
        self.last_connection_attempt = now()
        self.last_connection_error = "Broker connection failed; retrying"
        self._save_history()
        log.warning("MQTT connection attempt failed")

    def on(self, event_name, handler):
        self._event_handlers[event_name] = handler

    def start(self):
        self._load_history()
        self.last_connection_attempt = now()
        self._save_history()
        self.client.connect_async(self.broker_host, self.broker_port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc):
        self._probe_ready = False
        self._probe = None
        self._probe_attempted = None
        self._probe_result = {"status": "unknown", "latency_ms": None}
        self.connected_flag = rc == 0
        self.connected_since_monotonic = time.monotonic() if rc == 0 else None
        if rc != 0:
            self.last_connection_attempt = now()
            self.last_connection_error = mqtt.connack_string(rc)
            self._save_history()
            log.error("MQTT connection rejected: %s", rc)
            return
        self.last_connect_ts = now()
        self.last_connection_attempt = self.last_connect_ts
        self.last_connection_error = None
        self.connect_count += 1
        self._save_history()
        from .hass_power import discovery

        discovery.reset_online()
        from .pc_power import pc_registry

        pc_registry.reset_online()
        result = client.subscribe(self._probe_topic, qos=0)
        self._probe_subscription = (
            result[1]
            if isinstance(result, tuple) and result[0] == mqtt.MQTT_ERR_SUCCESS
            else None
        )
        if self._probe_subscription is None:
            self._probe_result = {"status": "error", "latency_ms": None}
        client.subscribe("wol/pc/+/status", qos=0)
        client.subscribe("wol/pc/+/ack", qos=0)
        client.subscribe("homeassistant/button/+/+/config", qos=0)
        client.subscribe("homeassistant/sensor/+/availability", qos=0)
        for event, qos in (
            ("pair", 1),
            ("register", 1),
            ("heartbeat", 0),
            ("cmd/ack", 1),
            ("subdevice/report", 0),
            ("scan/result", 1),
        ):
            client.subscribe(f"{settings.MQTT_TOPIC_PREFIX}/+/{event}", qos=qos)

    def _on_disconnect(self, client, userdata, rc):
        self._probe_ready = False
        self._probe = None
        self.connected_flag = False
        self.connected_since_monotonic = None
        self.last_disconnect_ts = now()
        self.last_disconnect_code = int(rc)
        self.disconnect_count += 1
        self.last_connection_error = mqtt.error_string(rc) if rc else None
        self._save_history()
        from .hass_power import discovery

        discovery.reset_online()
        from .pc_power import pc_registry

        pc_registry.reset_online()
        log.warning("MQTT disconnected rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            if self._receive_health_probe(msg):
                return
            from .pc_power import pc_registry

            if pc_registry.receive_ack(msg.topic, msg.payload, bool(msg.retain)):
                return
            if pc_registry.receive(msg.topic, msg.payload, bool(msg.retain)):
                return
            from .hass_power import discovery

            if settings.ENFORCE_CLIENT_VERSIONS and msg.topic.startswith(
                "homeassistant/"
            ):
                return
            if discovery.receive(msg.topic, msg.payload, bool(msg.retain)):
                return
            raw = msg.payload.decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict) or msg.retain:
                return
            from .client_policy import gateway_allowed

            if msg.topic.startswith(
                settings.MQTT_TOPIC_PREFIX + "/"
            ) and not gateway_allowed(data.get("firmware_version")):
                parts = msg.topic.split("/")
                if len(parts) >= 4 and parts[-1] in ("register", "pair"):
                    mac = norm_mac(parts[2])
                    with db() as conn:
                        conn.execute(
                            "UPDATE gateway SET status='offline',bind_message=? WHERE mac=?",
                            (
                                "固件版本过低，请升级到 "
                                + settings.MIN_FIRMWARE_VERSION,
                                mac,
                            ),
                        )
                    if parts[-1] == "register":
                        self.publish_registration_result(
                            mac, data.get("request_id", ""), "version_unsupported"
                        )
                    else:
                        self.publish_pairing_result(
                            mac,
                            data.get("request_id", ""),
                            {"code": "version_unsupported"},
                            "",
                        )
                return
            safe = redact_log_payload(data)
            with self._lock:
                self.message_sequence += 1
                self.msg_log.appendleft(
                    {
                        "id": self.message_sequence,
                        "dir": "IN",
                        "ts": now(),
                        "topic": msg.topic,
                        "payload": json.dumps(safe, ensure_ascii=False),
                        "level": log_severity(data),
                    }
                )
            parts = msg.topic.split("/")
            if len(parts) < 4 or parts[:2] != settings.MQTT_TOPIC_PREFIX.split("/"):
                return
            handler = self._event_handlers.get("/".join(parts[3:]))
            if handler:
                handler(msg.topic, data)
        except Exception:
            log.exception("MQTT message failed: %s", msg.topic)

    def get_gateway_bindkey(self, mac):
        gateway = get_gateway_by_mac(mac)
        return gateway["bind_key"] if gateway else None

    def _publish_logged(self, topic, payload, qos=1, retain=False):
        data = json.loads(payload)
        safe = json.dumps(redact_log_payload(data), ensure_ascii=False)
        result = self.client.publish(topic, payload, qos=qos, retain=retain)
        with self._lock:
            self.message_sequence += 1
            self.msg_log.appendleft(
                {
                    "id": self.message_sequence,
                    "dir": "OUT",
                    "ts": now(),
                    "topic": topic,
                    "payload": safe,
                    "level": log_severity(data),
                }
            )
        return result

    def publish_registration_result(self, mac, request_id, code):
        """Correlate setup feedback to a fresh registration; never return secrets."""
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            return
        body = {"request_id": request_id, "ok": code == "accepted", "code": code}
        self._publish_logged(
            f"{settings.MQTT_TOPIC_PREFIX}/{mac}/register/result",
            json.dumps(body, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    def publish_pairing_result(self, mac, request_id, result, pairing_code):
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            return
        if result["code"] == "paired":
            ts = str(int(time.time()))
            body = {
                "cmd_type": "pairing_complete",
                "payload": {
                    "bind_key": result["bind_key"],
                    "pair_request_id": request_id,
                },
                "timestamp": ts,
                "cmd_id": uuid.uuid4().hex,
            }
            raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            wire = {
                **body,
                "signed_body": raw,
                "sign": hashlib.sha256(f"{pairing_code}{ts}{raw}".encode()).hexdigest(),
            }
        else:
            wire = {"request_id": request_id, "ok": False, "code": result["code"]}
        self._publish_logged(
            f"{settings.MQTT_TOPIC_PREFIX}/{mac}/pair/result",
            json.dumps(wire, separators=(",", ":"), ensure_ascii=False),
            qos=1,
            retain=False,
        )

    def publish_cmd(
        self, gateway_mac, cmd_type, payload, timeout_sec=None, before_publish=None
    ):
        gateway_mac = norm_mac(gateway_mac)
        gateway = get_gateway_by_mac(gateway_mac)
        if not gateway:
            return {"ok": False, "msg": "网关不存在", "cmd_id": None}
        if cmd_type not in {"ota_start", "sync_all"}:
            from .ota import busy

            with db() as conn:
                if busy(conn, gateway["id"]):
                    return {
                        "ok": False,
                        "msg": "网关正在升级固件，请等待升级结束后再操作",
                        "cmd_id": None,
                    }
        from .client_policy import gateway_allowed

        if not gateway_allowed(gateway["firmware_version"]):
            return {
                "ok": False,
                "msg": "固件版本过低，请升级到 " + settings.MIN_FIRMWARE_VERSION,
                "cmd_id": None,
            }
        if not self.client.is_connected():
            return {"ok": False, "msg": "MQTT 未连接，指令未下发", "cmd_id": None}
        ts = str(int(time.time()))
        cmd_id = uuid.uuid4().hex
        timeout = (
            timeout_sec
            if timeout_sec is not None
            else (200 if cmd_type == "scan" else settings.CMD_ACK_TIMEOUT)
        )
        body = {
            "cmd_type": cmd_type,
            "payload": payload or {},
            "timestamp": ts,
            "cmd_id": cmd_id,
        }
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        signature = hashlib.sha256(
            f"{gateway['bind_key']}{ts}{raw}".encode("utf-8")
        ).hexdigest()
        envelope = {**body, "signed_body": raw, "sign": signature}
        wire = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        if len(wire.encode("utf-8")) > 24576:
            return {"ok": False, "msg": "指令超过固件的消息大小限制", "cmd_id": None}
        with self._lock:
            locks = self.task_locks.setdefault(gateway_mac, set())
            if cmd_type in locks:
                pending = next(
                    (
                        item
                        for item in self.pending_acks.values()
                        if item["gateway_mac"] == gateway_mac
                        and item["cmd_type"] == cmd_type
                    ),
                    None,
                )
                remaining = (
                    max(1, int(pending["timeout_ts"] - time.time() + 0.999))
                    if pending
                    else 1
                )
                task_name = "扫描" if cmd_type == "scan" else "当前指令"
                return {
                    "ok": False,
                    "msg": f"{task_name}任务仍在执行或等待网关回复，最多还需 {remaining} 秒；完成或超时后可再次操作。",
                    "cmd_id": None,
                    "remaining_sec": remaining,
                }
            locks.add(cmd_type)
            self.pending_acks[cmd_id] = {
                "gateway_mac": gateway_mac,
                "cmd_type": cmd_type,
                "send_ts": int(ts),
                "send_monotonic": time.monotonic(),
                "timeout_ts": time.time() + timeout,
            }
            self.cmd_log[cmd_id] = {"status": "pending", "msg": "等待设备执行结果"}
            if len(self.cmd_log) > 1000:
                for old in list(self.cmd_log):
                    if old not in self.pending_acks:
                        del self.cmd_log[old]
                        break
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at, cmd_id) VALUES (?,?,?,?,?)",
                    (
                        gateway["id"],
                        cmd_type,
                        (
                            "[redacted]"
                            if cmd_type in {"update_wifi", "ota_start"}
                            else json.dumps(payload, ensure_ascii=False)
                        ),
                        now(),
                        cmd_id,
                    ),
                )
            if before_publish:
                before_publish(cmd_id)
            info = self._publish_logged(
                f"{settings.MQTT_TOPIC_PREFIX}/{gateway_mac}/cmd",
                wire,
                qos=1,
                retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish rc={info.rc}")
        except Exception:
            log.exception("Command publish failed")
            self._finish(cmd_id, "fail", "指令下发失败")
            return {"ok": False, "msg": "指令下发失败", "cmd_id": cmd_id}
        return {
            "ok": True,
            "msg": "指令已下发，等待设备执行",
            "cmd_id": cmd_id,
            "timeout_sec": timeout,
        }

    def _finish(self, cmd_id, status, message):
        with self._lock:
            item = self.pending_acks.pop(cmd_id, None)
            if not item:
                return False
            self.task_locks.get(item["gateway_mac"], set()).discard(item["cmd_type"])
            self.cmd_log[cmd_id] = {"status": status, "msg": message}
        with db() as conn:
            conn.execute(
                "UPDATE cmd_log SET status=?, ack_msg=?, ack_at=? WHERE cmd_id=?",
                (status, message, now(), cmd_id),
            )
            if item["cmd_type"] == "ota_start":
                from .ota import command_finished

                command_finished(conn, cmd_id, status, message)
        if item["cmd_type"] == "reset":
            try:
                from .gateway_deletion import complete_gateway_deletion

                complete_gateway_deletion(cmd_id, status == "ok")
            except Exception:
                log.exception("Gateway deletion acknowledgement processing failed")
        return True

    def check_ack_timeout(self):
        with self._lock:
            expired = [
                cid
                for cid, item in self.pending_acks.items()
                if time.time() > item["timeout_ts"]
            ]
        for cmd_id in expired:
            self._finish(cmd_id, "timeout", "未收到设备执行结果，指令超时")

    def get_cmd_status(self, cmd_id):
        with self._lock:
            result = self.cmd_log.get(cmd_id)
            if result:
                return dict(result)
        with db() as conn:
            row = conn.execute(
                "SELECT status,ack_msg,created_at FROM cmd_log WHERE cmd_id=?",
                (cmd_id,),
            ).fetchone()
        if row:
            return {
                "status": row["status"],
                "msg": row["ack_msg"] or "等待设备执行结果",
            }
        return {"status": "unknown", "msg": "未找到此任务，请重新扫描"}

    def handle_gateway_uptime(self, gateway_mac, uptime_ms, reported_at):
        """A scan cannot survive a reboot after its command was sent."""
        if type(uptime_ms) not in (int, float) or not 0 <= uptime_ms < 1e13:
            return
        if type(reported_at) not in (int, float):
            return
        age = time.time() - reported_at
        if not -5 <= age <= 30:
            return
        clock = time.monotonic()
        with self._lock:
            interrupted = [
                cmd_id
                for cmd_id, item in self.pending_acks.items()
                if item["gateway_mac"] == gateway_mac
                and item["cmd_type"] == "scan"
                and clock - item.get("send_monotonic", clock)
                > uptime_ms / 1000 + max(0, age) + 5
            ]
        for cmd_id in interrupted:
            self._finish(cmd_id, "fail", "网关已重启，本次扫描已中断，请重新扫描")

    def handle_scan_complete(self, topic, data):
        """The explicit completion marker also proves the scan worker finished."""
        if (
            data.get("partial") is not True
            or data.get("done") is not True
            or data.get("reset")
        ):
            return False
        return self.handle_cmd_ack_callback(
            topic,
            {
                "cmd_id": data.get("cmd_id"),
                "cmd_type": "scan",
                "ok": True,
                "msg": "扫描完成",
            },
        )

    def handle_cmd_ack_callback(self, topic, data):
        mac = norm_mac(topic.split("/")[2])
        cmd_id = data.get("cmd_id")
        with self._lock:
            item = self.pending_acks.get(cmd_id)
            if (
                not item
                or item["gateway_mac"] != mac
                or item["cmd_type"] != data.get("cmd_type")
            ):
                return False
        return self._finish(
            cmd_id,
            "ok" if data.get("ok") is True else "fail",
            str(data.get("msg", ""))[:500],
        )


mqtt_client = MQTTClient(
    settings.MQTT_HOST,
    settings.MQTT_PORT,
    settings.MQTT_USERNAME,
    settings.MQTT_PASSWORD,
)
