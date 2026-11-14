"""Signed application packages and persistent, device-bound OTA tasks."""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time
import uuid
import zlib
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from .config import settings
from .database import atomic, db
from .presentation import gateway_state

LAYOUT = "c3-4m-ab-v1"
SLOT_SIZE = 0x1F0000
MAX_PACKAGE = 3 * 1024 * 1024
PUBLIC_KEY_PATH = Path(__file__).with_name("ota_public_key.pem")
ACTIVE = frozenset({"queued", "downloading", "verifying", "rebooting", "validating"})
TERMINAL = frozenset({"success", "failed", "rolled_back"})
log = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def storage():
    path = (
        Path(settings.OTA_STORAGE_PATH)
        if settings.OTA_STORAGE_PATH
        else Path(settings.ADMIN_AUTH_PATH).resolve().parent / ".firmware-ota"
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def image_path(image_hash):
    if not re.fullmatch(r"[0-9a-f]{64}", image_hash):
        raise ValueError("Invalid firmware digest")
    return storage() / (image_hash + ".bin")


def verify_package(raw):
    if not raw or len(raw) > MAX_PACKAGE:
        raise HTTPException(413, "升级包过大，最多支持 3 MiB")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 20:
                raise ValueError()
            for name, limit in (
                ("ota-manifest.json", 1024),
                ("ota-signature.hex", 256),
                ("wol_gateway.bin", SLOT_SIZE),
            ):
                info = archive.getinfo(name)
                if info.file_size < 1 or info.file_size > limit:
                    raise ValueError()
            manifest_raw = archive.read("ota-manifest.json")
            signature = bytes.fromhex(
                archive.read("ota-signature.hex").decode("ascii").strip()
            )
            key = serialization.load_pem_public_key(PUBLIC_KEY_PATH.read_bytes())
            key.verify(signature, manifest_raw, ec.ECDSA(hashes.SHA256()))
            manifest = json.loads(manifest_raw)
            if set(manifest) != {
                "schema",
                "chip",
                "layout",
                "version",
                "size",
                "sha256",
            }:
                raise ValueError()
            if (
                type(manifest["schema"]) is not int
                or manifest["schema"] != 1
                or manifest["chip"] != "esp32c3"
                or manifest["layout"] != LAYOUT
            ):
                raise ValueError()
            if not isinstance(manifest["version"], str) or not re.fullmatch(
                r"\d{1,3}\.\d{1,3}\.\d{1,3}", manifest["version"]
            ):
                raise ValueError()
            if (
                type(manifest["size"]) is not int
                or not 256 <= manifest["size"] <= SLOT_SIZE
            ):
                raise ValueError()
            image = archive.read("wol_gateway.bin")
            if len(image) != manifest["size"] or digest(image) != manifest["sha256"]:
                raise ValueError()
            # ESP image header: C3 chip id 5; application descriptor follows the first segment header.
            if (
                image[0] != 0xE9
                or int.from_bytes(image[12:14], "little") != 5
                or image[32:36] != bytes.fromhex("3254cdab")
            ):
                raise ValueError()
            if (
                image[48:80].split(b"\0", 1)[0].decode("ascii") != manifest["version"]
                or image[80:112].split(b"\0", 1)[0] != b"wol_gateway"
            ):
                raise ValueError()
            return manifest_raw.decode("utf-8"), signature.hex(), manifest, image
    except (
        ValueError,
        KeyError,
        BadZipFile,
        InvalidSignature,
        UnicodeError,
        RuntimeError,
        zlib.error,
        EOFError,
        TypeError,
        AttributeError,
    ):
        raise HTTPException(
            400, "升级包无效或签名不匹配，请上传本站提供的 ESP32-C3 OTA 固件包"
        ) from None


def current(conn, gid):
    return conn.execute(
        "SELECT * FROM gateway_ota WHERE gateway_id=?", (gid,)
    ).fetchone()


def busy(conn, gid):
    row = current(conn, gid)
    return bool(row and row["state"] in ACTIVE and row["deadline"] > time.time())


def assert_not_busy(conn, gid):
    if busy(conn, gid):
        raise HTTPException(409, "网关正在升级固件，请等待升级结束后再操作")


def view(conn, gateway):
    row = current(conn, gateway["id"])
    result = {
        "supported": bool(row and row["supported"] and row["layout"] == LAYOUT),
        "state": "idle",
        "progress": 0,
        "target_version": "",
        "running_version": (
            gateway.get("firmware_version", "")
            if isinstance(gateway, dict)
            else gateway["firmware_version"]
        ),
        "message": "",
        "task_id": "",
        "last_updated_at": row["last_success_at"] if row else 0,
    }
    if row:
        for key in (
            "state",
            "progress",
            "target_version",
            "running_version",
            "message",
            "task_id",
        ):
            result[key] = row[key]
    result["busy"] = busy(conn, gateway["id"])
    if gateway_state(gateway) != "online":
        reason = "仅在线且状态正常的网关支持远程 OTA 升级"
    elif not result["supported"]:
        reason = "当前固件不支持远程 OTA，请先通过 USB 刷入 OTA 基础版"
    elif result["busy"]:
        reason = "网关正在升级固件，请等待升级结束"
    else:
        reason = ""
    result.update(can_upgrade=not reason, disabled_reason=reason)
    result["auto_update"] = auto_update_view(conn, gateway, row, result["supported"])
    return result


def auto_update_view(conn, gateway, job, supported):
    enabled = bool(dict(gateway).get("ota_auto_update", 0))
    can_enable = supported and gateway_state(gateway) in {"online", "offline"}
    reason = ""
    if enabled:
        if job and job["auto_task_id"] and job["auto_task_id"] == job["task_id"]:
            if job["state"] == "rolled_back" or (
                job["state"] == "failed" and job["auto_attempts"] >= 3
            ):
                reason = "此版本自动更新已暂停；可重新开关自动更新后重试"
            elif job["state"] == "failed":
                reason = "自动更新失败，将稍后重试"
        if not reason and gateway_state(gateway) != "online":
            reason = "等待网关恢复正常在线后自动更新"
    return {"enabled": enabled, "can_enable": can_enable, "message": reason}


def set_auto_update(conn, gateway, enabled):
    state = view(conn, gateway)
    if enabled and not state["auto_update"]["can_enable"]:
        raise HTTPException(409, "仅状态正常且支持 OTA 的网关可开启自动更新")
    # Repeated submissions do not reset the retry budget.
    if bool(gateway["ota_auto_update"]) != enabled:
        conn.execute(
            "UPDATE gateway SET ota_auto_update=? WHERE id=?",
            (int(enabled), gateway["id"]),
        )
        if enabled:
            conn.execute(
                "UPDATE gateway_ota SET auto_image_sha256='',auto_attempts=0,auto_retry_at=0 WHERE gateway_id=?",
                (gateway["id"],),
            )
    latest = conn.execute(
        "SELECT * FROM gateway WHERE id=?", (gateway["id"],)
    ).fetchone()
    return {
        "ok": True,
        "msg": "已开启 OTA 自动更新" if enabled else "已关闭 OTA 自动更新",
        "revision": latest["edit_revision"],
        "ota": view(conn, latest),
    }


def identity_matches(job, gateway):
    return (
        gateway
        and job["expected_mac"] == gateway["mac"]
        and job["binding_hash"] == digest(gateway["bind_key"])
        and job["owner_user_id"] == gateway["owner_user_id"]
    )


def upgrade_reason(conn, gateway, target_version, *, batch=False):
    """Shared admission checks; batch upgrades strictly advance a known version."""
    from .mqtt_client import mqtt_client

    status = view(conn, gateway)
    if not status["can_upgrade"]:
        return status["disabled_reason"]
    running = status["running_version"]
    if not isinstance(running, str) or not re.fullmatch(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}", running
    ):
        return "无法确认当前固件版本，请先检查网关"
    current_version = tuple(map(int, running.split(".")))
    target = tuple(map(int, target_version.split(".")))
    if current_version > target:
        return "不支持降级到旧版固件"
    if batch and current_version == target:
        return "已是相同版本，无需升级"
    lease = conn.execute(
        "SELECT expires_at FROM gateway_scan_lease WHERE gateway_id=?", (gateway["id"],)
    ).fetchone()
    if lease and lease[0] > time.time():
        command = conn.execute(
            "SELECT status FROM cmd_log WHERE cmd_id=(SELECT cmd_id FROM gateway_scan_lease WHERE gateway_id=?)",
            (gateway["id"],),
        ).fetchone()
        if not command or command[0] == "sent":
            return "网关正在扫描，请等待扫描结束后再升级"
    if conn.execute(
        "SELECT id FROM pairing_request WHERE gateway_id=? AND claimed_mac='' AND expires_at>?",
        (gateway["id"], int(time.time())),
    ).fetchone():
        return "网关存在未完成的配对或转移，请完成后再升级"
    with mqtt_client._lock:
        if mqtt_client.task_locks.get(gateway["mac"]):
            return "网关仍有指令正在执行，请稍后再升级"
    return ""


def batch_plan(conn, gateways, verified):
    manifest = verified[2]
    entries = []
    for gateway in gateways:
        state = view(conn, gateway)
        reason = upgrade_reason(conn, gateway, manifest["version"], batch=True)
        entries.append(
            {
                "id": gateway["public_id"],
                "name": gateway["name"],
                "mac": gateway["mac"],
                "version": state["running_version"],
                "revision": gateway["edit_revision"],
                "eligible": not reason,
                "reason": reason,
            }
        )
    signature = digest(
        json.dumps(
            {
                "image": manifest["sha256"],
                "version": manifest["version"],
                "entries": sorted(entries, key=lambda entry: entry["id"]),
            },
            sort_keys=True,
            ensure_ascii=True,
        )
    )
    return {
        "ok": True,
        "version": manifest["version"],
        "plan": signature,
        "entries": entries,
    }


def start(conn, gateway, verified, *, automatic=False):
    from .mqtt_client import mqtt_client

    raw_manifest, signature, manifest, image = verified
    reason = upgrade_reason(conn, gateway, manifest["version"])
    if reason:
        raise HTTPException(400 if reason == "不支持降级到旧版固件" else 409, reason)
    target = image_path(manifest["sha256"])
    if not target.exists() or digest(target.read_bytes()) != manifest["sha256"]:
        temporary = storage() / ("upload-" + uuid.uuid4().hex)
        try:
            temporary.write_bytes(image)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    task = uuid.uuid4().hex
    token = secrets.token_hex(32)
    stamp = time.time()
    conn.execute(
        "UPDATE gateway_ota SET task_id=?,state='queued',progress=0,message=?,target_version=?,image_sha256=?,image_size=?,token_hash=?,expected_mac=?,binding_hash=?,owner_user_id=?,deadline=?,updated_at=?,cmd_id='',auto_task_id=? WHERE gateway_id=?",
        (
            task,
            "等待网关接收升级任务",
            manifest["version"],
            manifest["sha256"],
            manifest["size"],
            digest(token),
            gateway["mac"],
            digest(gateway["bind_key"]),
            gateway["owner_user_id"],
            stamp + 900,
            stamp,
            task if automatic else "",
            gateway["id"],
        ),
    )
    result = mqtt_client.publish_cmd(
        gateway["mac"],
        "ota_start",
        {
            "task_id": task,
            "manifest": raw_manifest,
            "signature": signature,
            "token": token,
        },
        before_publish=lambda cmd: conn.execute(
            "UPDATE gateway_ota SET cmd_id=? WHERE gateway_id=? AND task_id=?",
            (cmd, gateway["id"], task),
        ),
    )
    if not result["ok"]:
        conn.execute(
            "UPDATE gateway_ota SET state='failed',message=?,token_hash='' WHERE gateway_id=?",
            (result["msg"], gateway["id"]),
        )
    return {
        "ok": result["ok"],
        "msg": "升级任务已提交，正在等待网关升级" if result["ok"] else result["msg"],
        "ota": view(conn, gateway),
    }


@lru_cache(maxsize=1)
def _verified_release(path, stamp, size, public_key_stamp):
    verified = verify_package(Path(path).read_bytes())
    from .releases import FIRMWARE_VERSION

    if verified[2]["version"] != FIRMWARE_VERSION:
        raise ValueError(
            "Published firmware version does not match its signed manifest"
        )
    return verified


def latest_release():
    from .releases import FIRMWARE_VERSION, ROOT

    path = ROOT / "deploy" / f"esp32c3-{FIRMWARE_VERSION}.zip"
    stat = path.stat()
    if not 0 < stat.st_size <= MAX_PACKAGE:
        raise ValueError("Invalid published firmware size")
    return _verified_release(
        str(path), stat.st_mtime_ns, stat.st_size, PUBLIC_KEY_PATH.stat().st_mtime_ns
    )


def automatic_tick():
    """Reuse signed OTA tasks; admission and claiming share the DB write mutex."""
    from .background import reconcile_heartbeat_status
    from .mqtt_client import mqtt_client

    # Startup can precede the broker connection; do not spend a device retry yet.
    if not mqtt_client.client.is_connected():
        return

    with db() as conn:
        if not conn.execute(
            "SELECT id FROM gateway WHERE ota_auto_update=1 LIMIT 1"
        ).fetchone():
            return
    verified = latest_release()  # Verify disk contents before taking the write lock.
    manifest = verified[2]
    with db() as conn, atomic(conn):
        reconcile_heartbeat_status(conn)
        stamp = time.time()
        # Roll out one automatic upgrade at a time, across processes and accounts.
        if conn.execute(
            "SELECT gateway_id FROM gateway_ota WHERE auto_task_id=task_id AND auto_task_id!='' "
            "AND state IN ('queued','downloading','verifying','rebooting','validating') AND deadline>? LIMIT 1",
            (stamp,),
        ).fetchone():
            return
        gateways = conn.execute(
            "SELECT * FROM gateway WHERE ota_auto_update=1 ORDER BY id"
        ).fetchall()
        for gateway in gateways:
            if upgrade_reason(conn, gateway, manifest["version"], batch=True):
                continue
            job = current(conn, gateway["id"])
            same_image = job["auto_image_sha256"] == manifest["sha256"]
            if same_image and (
                job["auto_attempts"] >= 3
                or job["auto_retry_at"] > stamp
                or (
                    job["auto_task_id"] == job["task_id"]
                    and job["state"] == "rolled_back"
                )
            ):
                continue
            attempts = job["auto_attempts"] + 1 if same_image else 1
            start(conn, gateway, verified, automatic=True)
            # A failed/expired task remains delayed across service restarts.
            conn.execute(
                "UPDATE gateway_ota SET auto_image_sha256=?,auto_attempts=?,auto_retry_at=? WHERE gateway_id=?",
                (
                    manifest["sha256"],
                    attempts,
                    stamp + (300 if attempts == 1 else 1800),
                    gateway["id"],
                ),
            )
            log.info(
                "Automatic OTA task created for gateway %s, version %s, attempt %s",
                gateway["public_id"],
                manifest["version"],
                attempts,
            )
            return


def command_finished(conn, cmd_id, status, message):
    job = conn.execute("SELECT * FROM gateway_ota WHERE cmd_id=?", (cmd_id,)).fetchone()
    if job and job["state"] == "queued" and status != "ok":
        conn.execute(
            "UPDATE gateway_ota SET state='failed',message=?,token_hash='',updated_at=? WHERE gateway_id=?",
            (message[:255], time.time(), job["gateway_id"]),
        )


def reported(conn, gateway, data, *, registration=False):
    value = data.get("ota")
    if not isinstance(value, dict):
        if registration:
            conn.execute(
                "UPDATE gateway_ota SET supported=0,layout='' WHERE gateway_id=?",
                (gateway["id"],),
            )
        return
    supported = value.get("supported") is True and value.get("layout") == LAYOUT
    version = data.get("firmware_version", value.get("running_version", ""))
    if not isinstance(version, str) or not re.fullmatch(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}", version
    ):
        return
    conn.execute(
        "INSERT INTO gateway_ota(gateway_id,supported,layout,running_version) VALUES(?,?,?,?) ON CONFLICT(gateway_id) DO UPDATE SET supported=excluded.supported,layout=excluded.layout,running_version=excluded.running_version",
        (gateway["id"], int(supported), LAYOUT if supported else "", version),
    )
    job = current(conn, gateway["id"])
    if (
        not job["task_id"]
        or value.get("task_id") != job["task_id"]
        or job["state"] not in ACTIVE
        or not identity_matches(job, gateway)
    ):
        return
    state, progress = value.get("state"), value.get("progress")
    if (
        state not in ACTIVE | TERMINAL
        or type(progress) is not int
        or not 0 <= progress <= 100
    ):
        return
    order = {
        "queued": 0,
        "downloading": 1,
        "verifying": 2,
        "rebooting": 3,
        "validating": 4,
    }
    if state in ACTIVE and order[state] < order[job["state"]]:
        return
    if state == "success" and (
        version != job["target_version"] or value.get("sha256") != job["image_sha256"]
    ):
        return
    messages = {
        "queued": "等待网关接收升级任务",
        "downloading": "正在下载固件",
        "verifying": "正在校验固件",
        "rebooting": "正在重启网关",
        "validating": "正在验证新版固件",
        "success": "固件升级成功",
        "failed": "固件升级失败",
        "rolled_back": "新版启动失败，已回滚旧版固件",
    }
    detail = value.get("error")
    if (
        state == "failed"
        and isinstance(detail, str)
        and re.fullmatch(r"[a-z0-9_]{1,48}", detail)
    ):
        messages[state] += "（" + detail + "）"
    stamp = time.time()
    conn.execute(
        "UPDATE gateway_ota SET state=?,progress=?,message=?,updated_at=?,last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,token_hash=CASE WHEN ? THEN '' ELSE token_hash END WHERE gateway_id=?",
        (
            state,
            100 if state == "success" else max(progress, job["progress"]),
            messages[state],
            stamp,
            int(state == "success"),
            stamp,
            int(state in TERMINAL),
            gateway["id"],
        ),
    )


def expire_and_clean():
    with db() as conn, atomic(conn):
        jobs = conn.execute("SELECT * FROM gateway_ota WHERE task_id!=''").fetchall()
        for job in jobs:
            gateway = conn.execute(
                "SELECT * FROM gateway WHERE id=?", (job["gateway_id"],)
            ).fetchone()
            if job["state"] in ACTIVE and (
                job["deadline"] < time.time() or not identity_matches(job, gateway)
            ):
                conn.execute(
                    "UPDATE gateway_ota SET state='failed',message=?,token_hash='',updated_at=? WHERE gateway_id=?",
                    (
                        (
                            "升级任务超时，请检查网关实际版本后重试"
                            if identity_matches(job, gateway)
                            else "网关绑定已改变，升级任务已失效"
                        ),
                        time.time(),
                        job["gateway_id"],
                    ),
                )
        used = {
            r[0]
            for r in conn.execute(
                "SELECT image_sha256 FROM gateway_ota WHERE state IN ('queued','downloading','verifying','rebooting','validating')"
            )
        }
        for path in storage().glob("*.bin"):
            if re.fullmatch(r"[0-9a-f]{64}\.bin", path.name) and path.stem not in used:
                path.unlink(missing_ok=True)


async def watchdog():
    last_auto_check = 0
    while True:
        try:
            await asyncio.to_thread(expire_and_clean)
        except Exception:
            log.exception("OTA task maintenance failed")
        if time.monotonic() - last_auto_check >= 60:
            last_auto_check = time.monotonic()
            try:
                await asyncio.to_thread(automatic_tick)
            except Exception:
                log.exception("Automatic OTA check failed")
        await asyncio.sleep(15)


def download(task_id, authorization, range_header, *, head=False):
    """Only the TLS enrollment listener exposes this endpoint; tokens never enter URLs."""
    if not re.fullmatch(r"[0-9a-f]{32}", task_id) or not re.fullmatch(
        r"Bearer [0-9a-f]{64}", authorization or ""
    ):
        raise HTTPException(404)
    with db() as conn:
        job = conn.execute(
            "SELECT * FROM gateway_ota WHERE task_id=?", (task_id,)
        ).fetchone()
        gateway = (
            conn.execute(
                "SELECT * FROM gateway WHERE id=?", (job["gateway_id"],)
            ).fetchone()
            if job
            else None
        )
        if (
            not job
            or job["state"] not in ACTIVE
            or job["deadline"] < time.time()
            or not identity_matches(job, gateway)
            or not hmac.compare_digest(job["token_hash"], digest(authorization[7:]))
        ):
            raise HTTPException(404)
        path, size = image_path(job["image_sha256"]), job["image_size"]
        if not path.is_file() or path.stat().st_size != size:
            raise HTTPException(404)
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Content-Type": "application/octet-stream",
        }
        if head:
            from fastapi.responses import Response

            return Response(headers={**headers, "Content-Length": str(size)})
        if not range_header:
            from fastapi.responses import FileResponse

            return FileResponse(
                path, media_type="application/octet-stream", headers=headers
            )
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
        if not match:
            raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
        start_at, end_at = map(int, match.groups())
        end_at = min(end_at, size - 1)
        if start_at > end_at or end_at - start_at + 1 > 65536:
            raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
        with path.open("rb") as stream:
            stream.seek(start_at)
            content = stream.read(end_at - start_at + 1)
        from fastapi.responses import Response

        return Response(
            content,
            status_code=206,
            headers={**headers, "Content-Range": f"bytes {start_at}-{end_at}/{size}"},
        )
