"""One-time pairing codes: an unclaimed ticket does not need a gateway MAC."""

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from math import ceil

from .database import atomic, db, now
from .utils import gen_bind_key, norm_mac
from .names import valid_name, NAME_ERROR
from .presentation import gateway_can_transfer, gateway_can_repair_pairing
from .background import reconcile_heartbeat_status
from .access import (
    scope_clause,
    target_owner,
    require_gateway_access,
    require_pairing_access,
)

PAIRING_TTL = 15 * 60


def normalize_code(code):
    if not isinstance(code, str):
        return ""
    compact = re.sub(r"[\s-]", "", code).upper()
    return compact if re.fullmatch(r"[0-9A-F]{16}", compact) else ""


def _hash(code):
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def create_pairing(
    name="", gateway_id=None, renew_id=None, *, transfer=False, owner=""
):
    name = name.strip()
    if not valid_name(name):
        return {"ok": False, "msg": NAME_ERROR}
    code = secrets.token_hex(8).upper()
    created = int(time.time())
    with db() as conn:
        owner_user_id = target_owner(conn, owner)
        if gateway_id is not None:
            require_gateway_access(conn, gateway_id)
            reconcile_heartbeat_status(conn)
            gateway = conn.execute(
                "SELECT * FROM gateway WHERE id=?", (gateway_id,)
            ).fetchone()
            if not gateway:
                return {"ok": False, "msg": "网关不存在"}
            owner_user_id = gateway["owner_user_id"]
            if transfer and not gateway_can_transfer(gateway):
                return {"ok": False, "msg": "仅状态正常的在线或离线网关可转移"}
            if gateway["delete_requested_at"]:
                return {"ok": False, "msg": "网关正在等待删除，请先撤销删除"}
            if not transfer and not gateway_can_repair_pairing(gateway):
                return {"ok": False, "msg": "仅状态正常的离线网关可重新生成配对码"}
            name = gateway["name"]
        if renew_id is not None:
            require_pairing_access(conn, renew_id)
            ticket = conn.execute(
                "SELECT * FROM pairing_request WHERE id=?", (renew_id,)
            ).fetchone()
            if not ticket or ticket["claimed_mac"]:
                return {
                    "ok": False,
                    "msg": "配对记录不存在或已使用；请从网关列表生成新的配对码",
                }
            if ticket["expires_at"] > created:
                return {
                    "ok": False,
                    "msg": "配对码尚未过期，请到期后重新生成",
                    "expires_at": ticket["expires_at"],
                }
            if ticket["gateway_id"] is not None:
                gateway = conn.execute(
                    "SELECT * FROM gateway WHERE id=?", (ticket["gateway_id"],)
                ).fetchone()
                if ticket["purpose"] == "transfer":
                    if not gateway or not gateway_can_transfer(gateway):
                        return {"ok": False, "msg": "仅状态正常的在线或离线网关可转移"}
                elif not gateway or not gateway_can_repair_pairing(gateway):
                    return {"ok": False, "msg": "仅状态正常的离线网关可重新生成配对码"}
            transfer = ticket["purpose"] == "transfer"
            changed = conn.execute(
                "UPDATE pairing_request SET code_hash=?, created_at=?, expires_at=? WHERE id=? AND claimed_mac='' AND expires_at<=?",
                (_hash(code), created, created + PAIRING_TTL, renew_id, created),
            )
            if not changed.rowcount:
                return {"ok": False, "msg": "配对记录已变化，请刷新后重试"}
            ticket_id = renew_id
        else:
            # Reissuing a code for an existing gateway invalidates its unused codes.
            if gateway_id is not None:
                active = conn.execute(
                    "SELECT expires_at FROM pairing_request WHERE gateway_id=? AND claimed_mac='' AND expires_at>? LIMIT 1",
                    (gateway_id, created),
                ).fetchone()
                if active:
                    return {
                        "ok": False,
                        "msg": "配对码尚未过期，请到期后重新生成",
                        "expires_at": active["expires_at"],
                    }
                conn.execute(
                    "DELETE FROM pairing_request WHERE gateway_id=? AND claimed_mac=''",
                    (gateway_id,),
                )
                conn.execute(
                    "UPDATE pairing_request SET expires_at=CASE WHEN expires_at<? THEN expires_at ELSE ? END WHERE gateway_id=? AND claimed_mac!=''",
                    (created, created, gateway_id),
                )
            ticket_id = conn.execute(
                "INSERT INTO pairing_request(code_hash,name,created_at,expires_at,gateway_id,purpose,owner_user_id) VALUES (?,?,?,?,?,?,?)",
                (
                    _hash(code),
                    name,
                    created,
                    created + PAIRING_TTL,
                    gateway_id,
                    "transfer" if transfer else "recovery",
                    owner_user_id,
                ),
            ).lastrowid
    return {
        "ok": True,
        "pairing_id": ticket_id,
        "pairing_code": "-".join(code[i : i + 4] for i in range(0, 16, 4)),
        "expires_at": created + PAIRING_TTL,
        "ttl_sec": PAIRING_TTL,
        "purpose": "transfer" if transfer else "recovery",
    }


def list_pairings(owner=None):
    clause, args = scope_clause(owner=owner)
    with db() as conn:
        rows = conn.execute(
            f"SELECT id,name,expires_at,gateway_id,claimed_mac,edit_revision AS revision FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NULL AND {clause} ORDER BY id DESC",
            args,
        ).fetchall()
    current = time.time()
    return [
        {
            **dict(row),
            "expires_at_display": datetime.fromtimestamp(
                row["expires_at"], timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": max(0, ceil(row["expires_at"] - current)),
            "state": (
                "claimed"
                if row["claimed_mac"]
                else "expired" if row["expires_at"] <= current else "pending"
            ),
        }
        for row in rows
    ]


def claim_pairing(mac, submitted_code):
    """Atomically consume a ticket; retries by the same MAC recover a lost reply."""
    mac = norm_mac(mac)
    code = normalize_code(submitted_code)
    if not re.fullmatch(r"[0-9a-f]{12}", mac) or not code:
        return {"code": "pairing_invalid"}
    with db() as conn, atomic(conn):
        return _claim(conn, mac, code)


def _claim(conn, mac, code):
    reconcile_heartbeat_status(conn)
    ticket = conn.execute(
        "SELECT * FROM pairing_request WHERE code_hash=?", (_hash(code),)
    ).fetchone()
    if not ticket:
        return {"code": "pairing_invalid"}
    if ticket["expires_at"] <= time.time():
        return {"code": "pairing_expired"}
    if (
        ticket["owner_user_id"] is not None
        and not conn.execute(
            "SELECT 1 FROM app_user WHERE id=? AND enabled=1 AND role='user'",
            (ticket["owner_user_id"],),
        ).fetchone()
    ):
        return {"code": "pairing_invalid"}
    if ticket["claimed_mac"]:
        if ticket["claimed_mac"] != mac:
            return {"code": "pairing_used"}
        gateway = conn.execute(
            "SELECT * FROM gateway WHERE id=? AND mac=?", (ticket["gateway_id"], mac)
        ).fetchone()
        if not gateway:
            return {"code": "pairing_invalid"}
        return {
            "code": "paired",
            "bind_key": gateway["bind_key"],
            "gateway_id": gateway["id"],
        }
    existing = conn.execute("SELECT * FROM gateway WHERE mac=?", (mac,)).fetchone()
    if existing and existing["owner_user_id"] != ticket["owner_user_id"]:
        return {"code": "device_already_bound"}
    if ticket["gateway_id"] is not None and ticket["purpose"] == "transfer":
        gateway = conn.execute(
            "SELECT * FROM gateway WHERE id=?", (ticket["gateway_id"],)
        ).fetchone()
        if not gateway or not gateway_can_transfer(gateway):
            return {"code": "device_already_bound"}
        if gateway["mac"] == mac:
            return {"code": "wrong_device"}
        if existing and existing["id"] != gateway["id"]:
            return {"code": "device_already_bound"}
        key = gen_bind_key()
        conn.execute(
            "UPDATE pairing_request SET expires_at=CASE WHEN expires_at<? THEN expires_at ELSE ? END WHERE gateway_id=? AND id!=?",
            (int(time.time()), int(time.time()), gateway["id"], ticket["id"]),
        )
        conn.execute(
            """UPDATE gateway SET mac=?,bind_key=?,status='unbound',
               ip='',netmask='',gateway_ip='',firmware_version='',
               last_heartbeat='',bind_message=? WHERE id=?""",
            (mac, key, "配对完成，等待设备注册", gateway["id"]),
        )
        conn.execute(
            "UPDATE pairing_request SET claimed_mac=?,claimed_at=? WHERE id=?",
            (mac, int(time.time()), ticket["id"]),
        )
        return {"code": "paired", "bind_key": key, "gateway_id": gateway["id"]}
    if ticket["gateway_id"] is not None:
        if not existing or existing["id"] != ticket["gateway_id"]:
            return {"code": "wrong_device"}
        if existing["delete_requested_at"]:
            return {"code": "device_already_bound"}
        if not gateway_can_repair_pairing(existing):
            return {"code": "device_already_bound"}
    elif (
        existing
        and not existing["delete_requested_at"]
        and (existing["status"] != "unbound" or existing["last_heartbeat"])
    ):
        return {"code": "device_already_bound"}
    if existing and existing["delete_requested_at"]:
        conn.execute("DELETE FROM cmd_log WHERE gateway_id=?", (existing["id"],))
        conn.execute("DELETE FROM gateway WHERE id=?", (existing["id"],))
        existing = None
    key = gen_bind_key()
    if existing:
        gateway_id = existing["id"]
        conn.execute(
            "UPDATE pairing_request SET expires_at=CASE WHEN expires_at<? THEN expires_at ELSE ? END WHERE gateway_id=? AND id!=?",
            (int(time.time()), int(time.time()), gateway_id, ticket["id"]),
        )
        conn.execute(
            "UPDATE gateway SET name=?,bind_key=?,status='unbound',last_heartbeat='',bind_message=? WHERE id=?",
            (
                ticket["name"] or existing["name"],
                key,
                "配对完成，等待设备注册",
                gateway_id,
            ),
        )
    else:
        gateway_id = conn.execute(
            "INSERT INTO gateway(mac,bind_key,name,status,bind_message,created_at,owner_user_id) VALUES (?,?,?,'unbound',?,?,?)",
            (
                mac,
                key,
                ticket["name"],
                "配对完成，等待设备注册",
                now(),
                ticket["owner_user_id"],
            ),
        ).lastrowid
    conn.execute(
        "UPDATE pairing_request SET claimed_mac=?,claimed_at=?,gateway_id=? WHERE id=?",
        (mac, int(time.time()), gateway_id, ticket["id"]),
    )
    return {"code": "paired", "bind_key": key, "gateway_id": gateway_id}
