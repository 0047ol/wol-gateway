"""HASS.Agent discovery and fresh availability; never execute discovery content."""

import json
import re
import threading
import time

from .database import db
from .access import service_mac_conflict

CONFIG = re.compile(r"homeassistant/button/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/config")


class Discovery:
    def __init__(self):
        self.lock = threading.RLock()
        self.commands = {}
        self.availability = {}

    def reset_online(self):
        with self.lock:
            self.availability.clear()

    def receive(self, topic, payload, retained=False):
        result = self._receive(topic, payload, retained)
        # Do not acquire the DB lock while holding the discovery lock: HTTP
        # handlers use the opposite order. Register on receipt, never on click.
        if isinstance(result, dict):
            mac = result["device_name"]
            if (
                re.fullmatch(r"[0-9a-f]{12}", mac)
                and topic == f"homeassistant/button/{mac}/office_pc_shutdown/config"
            ):
                with db() as conn:
                    if service_mac_conflict(conn, mac):
                        return bool(result)
                    conn.execute(
                        """INSERT INTO hass_registration(mac,discovery_topic,command_json,updated_at)
                        VALUES (?,?,?,?) ON CONFLICT(mac) DO UPDATE SET
                        discovery_topic=excluded.discovery_topic,command_json=excluded.command_json,updated_at=excluded.updated_at""",
                        (mac, topic, json.dumps(result), time.time()),
                    )
        elif CONFIG.fullmatch(topic) and not payload:
            with db() as conn:
                conn.execute(
                    "DELETE FROM hass_registration WHERE discovery_topic=?", (topic,)
                )
        return bool(result)

    def _receive(self, topic, payload, retained=False):
        if not topic.startswith("homeassistant/"):
            return False
        with self.lock:
            match = CONFIG.fullmatch(topic)
            if match:
                if not payload:
                    self.commands.pop(topic, None)
                    return True
                if len(payload) > 16384:
                    return True
                try:
                    value = json.loads(payload)
                    dev = value.get("device", value.get("dev", {}))
                    if not isinstance(dev, dict) or "HASS.Agent" not in str(
                        dev.get("manufacturer", "")
                    ):
                        return True
                    command_topic = value.get("command_topic", value.get("cmd_t"))
                    av = value.get("availability_topic", value.get("avty_t"))
                    # Fixed discovery namespace and scalar button payload only.
                    if command_topic != topic.removesuffix("config") + "set":
                        return True
                    if av != f"homeassistant/sensor/{match[1]}/availability":
                        return True
                    press = value.get("payload_press", value.get("pl_prs", "PRESS"))
                    if match[2] != "office_pc_shutdown":
                        return True
                    if not isinstance(press, str) or not 1 <= len(press) <= 128:
                        return True
                    self.commands[topic] = {
                        "discovery_topic": topic,
                        "command_topic": command_topic,
                        "availability_topic": av,
                        "payload": press,
                        "device_name": str(dev.get("name", match[1]))[:128],
                        "name": str(value.get("name", match[2]))[:128],
                        "unique_id": str(value.get("unique_id", ""))[:128],
                    }
                    if len(self.commands) > 1000:
                        self.commands.pop(next(iter(self.commands)))
                    return dict(self.commands[topic])
                except (ValueError, AttributeError, TypeError):
                    pass
            elif re.fullmatch(
                r"homeassistant/sensor/[A-Za-z0-9_-]+/availability", topic
            ):
                if payload in (b"online", b"offline"):
                    # Retained 'online' is not proof the process is alive now.
                    self.availability[topic] = (
                        payload == b"online",
                        time.monotonic() if not retained else 0,
                    )
                    if len(self.availability) > 1000:
                        self.availability.pop(next(iter(self.availability)))
        return True

    def get(self, topic):
        with self.lock:
            command = self.commands.get(topic)
            if not command:
                return None
            online, seen = self.availability.get(
                command["availability_topic"], (False, 0)
            )
            return {
                **command,
                "online": bool(online and seen and time.monotonic() - seen < 120),
            }

    def list(self):
        with self.lock:
            return [self.get(topic) for topic in sorted(self.commands)]

    def for_mac(self, mac):
        canonical = str(mac).replace(":", "").replace("-", "").lower()
        if not re.fullmatch(r"[0-9a-f]{12}", canonical):
            return None
        with db() as conn:
            if service_mac_conflict(conn, canonical):
                return None
            row = conn.execute(
                "SELECT command_json FROM hass_registration WHERE mac=?", (canonical,)
            ).fetchone()
        if not row:
            return None
        registered = json.loads(row["command_json"])
        current = self.get(registered["discovery_topic"])
        # Registration survives restart, while liveness must be freshly proved.
        if current and all(
            current.get(key) == registered.get(key) for key in registered
        ):
            return current
        return {**registered, "online": False}


discovery = Discovery()
