"""Project-only Mosquitto password/ACL writer. No passwords in source or logs."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from contextlib import contextmanager

from .config import settings
from .utils import norm_mac

lock = threading.RLock()


@contextmanager
def account_lock():
    with lock:
        with (directory() / "accounts.lock").open("a") as stream:
            if os.name != "nt":
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name != "nt":
                    fcntl.flock(stream, fcntl.LOCK_UN)


def password_hash(password):
    salt = secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 200000)
    return (
        "$7$200000$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def atomic(path, content, mode=0o600):
    # Root maintenance jobs must not replace service-owned account files with
    # root-only files. New files inherit the account directory's owner/group.
    owner = path.stat() if path.exists() else path.parent.stat()
    temp = path.with_suffix(path.suffix + ".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        if hasattr(os, "fchown") and os.geteuid() == 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temp, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def directory():
    if not settings.MQTT_AUTH_DIR:
        raise RuntimeError("MQTT 账号管理尚未启用")
    return Path(settings.MQTT_AUTH_DIR)


def load():
    path = directory() / "accounts.json"
    return json.loads(path.read_text()) if path.exists() else {}


def render(accounts):
    passwords, acl = [], []
    for username, account in accounts.items():
        if not account.get("active", True):
            continue
        passwords.append(username + ":" + account["hash"])
        acl.append("user " + username)
        if account["role"] == "server":
            acl.extend(
                [
                    f"topic readwrite {settings.MQTT_TOPIC_PREFIX}/_health/+",
                    "topic read wol/gateway/+/pair",
                    "topic read wol/gateway/+/register",
                    "topic read wol/gateway/+/heartbeat",
                    "topic read wol/gateway/+/subdevice/report",
                    "topic read wol/gateway/+/scan/result",
                    "topic read wol/gateway/+/cmd/ack",
                    "topic write wol/gateway/+/cmd",
                    "topic write wol/gateway/+/pair/result",
                    "topic write wol/gateway/+/register/result",
                    "topic read wol/pc/+/status",
                    "topic read wol/pc/+/ack",
                    "topic write wol/pc/+/command",
                ]
            )
        elif account["role"] == "esp":
            prefix = "wol/gateway/" + account["mac"] + "/"
            acl.extend(
                "topic read " + prefix + suffix
                for suffix in ("cmd", "pair/result", "register/result")
            )
            acl.extend(
                "topic write " + prefix + suffix
                for suffix in (
                    "pair",
                    "register",
                    "heartbeat",
                    "subdevice/report",
                    "scan/result",
                    "cmd/ack",
                )
            )
        elif account["role"] == "pc":
            prefix = "wol/pc/" + account["mac"] + "/"
            acl.extend(
                [
                    "topic read " + prefix + "command",
                    "topic write " + prefix + "status",
                    "topic write " + prefix + "ack",
                ]
            )
        acl.append("")
    return "\n".join(passwords) + "\n", "\n".join(acl) + "\n"


def persist(accounts):
    root = directory()
    passwords, acl = render(accounts)
    atomic(root / "passwords", passwords, 0o640)
    atomic(root / "acl", acl, 0o640)
    atomic(root / "accounts.json", json.dumps(accounts))
    # A project-specific systemd.path unit reloads Mosquitto; no sudo in the web app.
    atomic(root / "reload.trigger", str(time.time_ns()), 0o640)


def issue(role, mac, rotate=False):
    mac = norm_mac(mac)
    if role not in ("esp", "pc") or not re.fullmatch("[0-9a-f]{12}", mac):
        raise ValueError("请选择设备类型并填写完整的 MAC")
    with account_lock():
        accounts = load()
        previous = accounts.get(mac)
        if previous and previous["role"] != role:
            raise ValueError("该 MAC 已属于另一类设备")
        master_file = directory() / "master.key"
        if not master_file.exists():
            atomic(master_file, secrets.token_hex(32))
        master = bytes.fromhex(master_file.read_text().strip())
        epoch = previous.get("epoch", 0) if previous else 0
        if rotate or (previous and not previous.get("active", True)):
            epoch += 1
        username = mac
        password = (
            base64.urlsafe_b64encode(
                hmac.new(
                    master, f"{role}:{mac}:{epoch}".encode(), hashlib.sha256
                ).digest()
            )
            .decode()
            .rstrip("=")
        )
        if not previous or rotate or not previous.get("active", True):
            accounts[username] = dict(
                role=role,
                mac=mac,
                epoch=epoch,
                hash=password_hash(password),
                active=True,
                created_at=int(time.time()),
            )
            persist(accounts)
    return dict(
        username=username,
        password=password,
        mac=mac,
        role=role,
        host=settings.MQTT_PUBLIC_HOST,
        port=settings.MQTT_PUBLIC_PORT,
    )


def inventory():
    with account_lock():
        return [
            dict(
                username=name,
                **{
                    key: value.get(key)
                    for key in ("role", "mac", "created_at", "active")
                },
            )
            for name, value in load().items()
            if value["role"] != "server"
        ]
