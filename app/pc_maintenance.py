"""PC-only maintenance over the pinned TLS listener; no browser session required."""

import base64
import hashlib
import hmac
import json
import re
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import mqtt_accounts
from .database import atomic, db
from .releases import PC_VERSION, ROOT

PACKAGE = ROOT / "windows-agent/native/WolPCService.zip"
EXECUTABLE = ROOT / "windows-agent/native/WolPCService.exe"


def authenticate(authorization):
    try:
        scheme, encoded = authorization.split(" ", 1)
        if scheme.lower() != "basic" or len(encoded) > 256:
            raise ValueError()
        mac, password = base64.b64decode(encoded, validate=True).decode().split(":", 1)
        if not re.fullmatch("[0-9a-f]{12}", mac):
            raise ValueError()
        with mqtt_accounts.account_lock():
            account = mqtt_accounts.load().get(mac, {})
            if account.get("role") != "pc" or not account.get("active", True):
                raise ValueError()
            master = bytes.fromhex(
                (mqtt_accounts.directory() / "master.key").read_text().strip()
            )
            expected = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        master,
                        f"pc:{mac}:{account.get('epoch', 0)}".encode(),
                        hashlib.sha256,
                    ).digest()
                )
                .decode()
                .rstrip("=")
            )
        if not hmac.compare_digest(password, expected):
            raise ValueError()
        return mac
    except (ValueError, UnicodeError, KeyError):
        raise HTTPException(401, "Invalid PC credentials")
    except (OSError, RuntimeError):
        raise HTTPException(503, "PC maintenance unavailable")


@lru_cache(maxsize=2)
def package_digest(path, size, modified):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def release(executable=False):
    package = EXECUTABLE if executable else PACKAGE
    if not package.is_file():
        raise HTTPException(503, "PC package unavailable")
    stat = package.stat()
    return dict(
        version=PC_VERSION,
        size=stat.st_size,
        sha256=package_digest(str(package), stat.st_size, stat.st_mtime_ns),
    )


def binding_rows(conn, mac):
    return conn.execute(
        "SELECT d.id,d.gateway_id,d.edit_revision,d.pc_service_bound,"
        "d.pc_service_ignored,g.edit_revision AS gateway_revision "
        "FROM sub_device d JOIN gateway g ON g.id=d.gateway_id "
        "WHERE replace(replace(replace(lower(d.mac),':',''),'-',''),'.','')=? "
        "ORDER BY d.id",
        (mac,),
    ).fetchall()


def binding_token(rows):
    # Opaque concurrency snapshot, not a credential or an exposed device ID.
    return hashlib.sha256(
        json.dumps([dict(row) for row in rows], sort_keys=True).encode()
    ).hexdigest()


def runtime(authorization):
    mac = authenticate(authorization)
    with db() as conn:
        rows = binding_rows(conn, mac)
    # Reading this endpoint must never register or rebind a device.
    return dict(
        registered=bool(rows),
        bound=any(
            row["pc_service_bound"] and not row["pc_service_ignored"] for row in rows
        ),
        binding_token=binding_token(rows),
        release=release(),
        executable_release=release(executable=True),
    )


def unbind(authorization, token):
    mac = authenticate(authorization)
    if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{64}", token):
        raise HTTPException(400, "无效的关联状态，请重新确认")
    from .subdevice_sync import queue_subdevice_sync

    with db() as conn, atomic(conn):
        rows = binding_rows(conn, mac)
        if not hmac.compare_digest(token, binding_token(rows)):
            raise HTTPException(409, "关联状态已变化，请重新确认")
        selected = [
            row
            for row in rows
            if row["pc_service_bound"] and not row["pc_service_ignored"]
        ]
        if not selected:
            raise HTTPException(409, "当前电脑未关联WOL关机服务")
        for row in selected:
            conn.execute(
                "UPDATE sub_device SET pc_service_ignored=1,pc_service_bound=0,"
                "status='unknown',last_report='',pc_last_report='',offline_since='',"
                "offline_last_check='',offline_checks=0 WHERE id=?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE device_power_operation SET status='failed',message='WOL关机服务已解除关联' "
                "WHERE device_id=? AND action='shutdown' AND status IN ('sending','waiting')",
                (row["id"],),
            )
        for gid in {row["gateway_id"] for row in selected}:
            queue_subdevice_sync(conn, gid)
    return {"ok": True, "bound": False, "unbound_count": len(selected)}


def download(authorization, version):
    authenticate(authorization)
    current = release()
    if version != current["version"]:
        raise HTTPException(409, "Release changed; check again")
    return FileResponse(
        PACKAGE,
        media_type="application/zip",
        filename="WolPCService.zip",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def download_executable(authorization, version):
    authenticate(authorization)
    current = release(executable=True)
    if version != current["version"]:
        raise HTTPException(409, "Release changed; check again")
    return FileResponse(
        EXECUTABLE,
        media_type="application/octet-stream",
        filename="WolPCService.exe",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
