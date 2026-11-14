"""Account settings, administrator user management and gateway assignment."""

import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from ..access import actor, require_admin, service_mac_conflict
from ..auth import credentials, password_hash, session_for, verify_password
from ..config import settings
from ..database import atomic, db, now, IntegrityError
from ..concurrency import mutation, check
from ..batch_selection import gateway_ids
from ..gateway_refs import GatewayId
from ..utils import fmt_mac
from ..permissions import CATALOG, KEYS, decode_permissions

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_mac"] = fmt_mac


def audit(conn, event, target):
    conn.execute(
        "INSERT INTO account_audit(actor_id,event,target,created_at) VALUES (?,?,?,?)",
        (actor()["user_id"], event, target, now()),
    )


def users(conn):
    return [
        {**dict(r), "permissions": decode_permissions(r["permissions"])}
        for r in conn.execute(
            """SELECT u.public_id,u.username,u.role,u.enabled,u.permissions,u.created_at,u.edit_revision AS revision,
        (SELECT COUNT(*) FROM gateway g WHERE g.owner_user_id=u.id) AS gateway_count,
        (SELECT COUNT(*) FROM sub_device d JOIN gateway g ON g.id=d.gateway_id WHERE g.owner_user_id=u.id) AS device_count,
        (SELECT COUNT(*) FROM device_share s JOIN sub_device d ON d.id=s.device_id JOIN gateway g ON g.id=d.gateway_id WHERE g.owner_user_id=u.id) AS share_count,
        EXISTS(SELECT 1 FROM pairing_request p WHERE p.owner_user_id=u.id AND p.gateway_id IS NULL
        AND p.claimed_mac='' AND p.expires_at>?) AS has_pending_pairing
        FROM app_user u WHERE role='user' ORDER BY u.id DESC""",
            (time.time(),),
        )
    ]


def find_user(conn, uid):
    row = conn.execute(
        "SELECT * FROM app_user WHERE public_id=? AND role='user'", (uid,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    return row


def revoke_sessions(conn, user):
    if user["role"] == "admin":
        conn.execute(
            "DELETE FROM admin_session WHERE credential_version NOT LIKE 'u:%'"
        )
    else:
        conn.execute(
            "DELETE FROM admin_session WHERE credential_version LIKE ?",
            (f"u:{user['id']}:%",),
        )
    conn.execute(
        "UPDATE app_user SET session_version=session_version+1 WHERE id=?",
        (user["id"],),
    )


def validate_name(username, display_name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", username):
        raise HTTPException(400, "账号需为3至32位字母、数字、点、下划线或短横线")
    if len(display_name) > 32:
        raise HTTPException(400, "显示名称最多32个字符")


@router.get("/account")
async def account_page(request: Request):
    return RedirectResponse("/?password=1", status_code=303)


@router.get("/api/account")
async def account_details():
    value = actor()
    with db() as conn:
        revision = conn.execute(
            "SELECT edit_revision FROM app_user WHERE id=?", (value["user_id"],)
        ).fetchone()[0]
    return {
        "ok": True,
        "account": {
            **{k: value[k] for k in ("public_id", "username", "display_name", "role")},
            "revision": revision,
        },
    }


@router.post("/api/account")
async def update_account(
    request: Request,
    username: str | None = Form(None),
    display_name: str | None = Form(None),
    current_password: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    value = actor()
    username = value["username"] if username is None else username.strip()
    display_name = (
        value["display_name"] if display_name is None else display_name.strip()
    )
    validate_name(username, display_name)
    if value["role"] == "admin" and username != value["username"]:
        raise HTTPException(400, "管理员账号不可修改")
    sensitive = bool(password or username != value["username"])
    with db() as conn:
        user = conn.execute(
            "SELECT * FROM app_user WHERE id=?", (value["user_id"],)
        ).fetchone()
        expected = (
            credentials()["password_hash"]
            if user["role"] == "admin"
            else user["password_hash"]
        )
    if sensitive:
        valid = len(current_password) <= 1024 and await run_in_threadpool(
            verify_password, current_password, expected
        )
        if not valid:
            raise HTTPException(400, "当前密码错误")
    if password and (not 8 <= len(password) <= 128 or password != confirm_password):
        raise HTTPException(400, "密码需为8至128个字符，且两次输入一致")
    encoded = await run_in_threadpool(password_hash, password) if password else expected
    with mutation() as conn:
        # Revalidate after the expensive hash work and before any write.
        check(conn, "user", value["user_id"])
        current = session_for(request)
        if not current or current["user_id"] != value["user_id"]:
            raise HTTPException(401, "登录已失效，请重新登录")
        path, previous = None, None
        try:
            with atomic(conn):
                conn.execute(
                    "UPDATE app_user SET username=?,display_name=?,password_hash=?,updated_at=? WHERE id=?",
                    (username, display_name, encoded, now(), user["id"]),
                )
                if password and user["role"] == "admin":
                    path = Path(settings.ADMIN_AUTH_PATH)
                    previous = path.read_bytes()
                    staging = path.with_name(path.name + "." + secrets.token_hex(8))
                    fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            json.dump(
                                {"username": username, "password_hash": encoded}, stream
                            )
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(staging, path)
                    finally:
                        staging.unlink(missing_ok=True)
                if sensitive:
                    revoke_sessions(conn, user)
                audit(conn, "account_updated", user["public_id"])
        except BaseException as error:
            if path is not None and previous is not None:
                path.write_bytes(previous)
            if isinstance(error, IntegrityError):
                raise HTTPException(409, "账号已存在") from error
            raise
    return {
        "ok": True,
        "msg": "账号已更新，请重新登录" if sensitive else "账号已更新",
        "reauthenticate": sensitive,
    }


@router.get("/users")
async def users_page(request: Request, sort: str = "username", direction: str = "asc"):
    from ..table_sort import sort_options

    require_admin()
    sort, direction = sort_options(
        sort,
        direction,
        {"username", "gateway_count", "device_count", "share_count", "enabled"},
        default="username",
    )
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={"sort": sort, "direction": direction, "permission_catalog": CATALOG},
    )


@router.get("/api/users")
async def user_list():
    require_admin()
    with db() as conn:
        rows = users(conn)
        return {"ok": True, "users": rows, "total": len(rows)}


@router.get("/api/account/status")
async def account_status():
    with db() as conn:
        user = conn.execute(
            "SELECT enabled,role,permissions,edit_revision FROM app_user WHERE id=?",
            (actor()["user_id"],),
        ).fetchone()
    return {
        "ok": True,
        "enabled": bool(user and user["enabled"]),
        "role": user["role"],
        "permissions": decode_permissions(user["permissions"]),
        "revision": user["edit_revision"],
    }


def validate_password(password):
    if not 8 <= len(password) <= 128:
        raise HTTPException(400, "密码需为8至128个字符")


@router.post("/api/users")
async def create_user(username: str = Form(...), password: str = Form(...)):
    require_admin()
    username = username.strip()
    validate_name(username, "")
    validate_password(password)
    encoded = await run_in_threadpool(password_hash, password)
    uid = "u_" + secrets.token_hex(16)
    with mutation() as conn:
        try:
            with atomic(conn):
                conn.execute(
                    """INSERT INTO app_user(public_id,username,role,password_hash,created_at,updated_at)
                VALUES (?,?,'user',?,?,?)""",
                    (uid, username, encoded, now(), now()),
                )
                audit(conn, "user_created", uid)
        except IntegrityError as error:
            raise HTTPException(409, "账号已存在") from error
    return {
        "ok": True,
        "msg": "用户已创建",
        "username": username,
        "public_id": uid,
    }


@router.post("/api/users/{uid}/reset")
async def reset_user_password(
    uid: str, password: str = Form(...), confirm_password: str = Form(...)
):
    require_admin()
    validate_password(password)
    if password != confirm_password:
        raise HTTPException(400, "两次输入的密码不一致")
    encoded = await run_in_threadpool(password_hash, password)
    with mutation(("user", uid)) as conn:
        user = find_user(conn, uid)
        conn.execute(
            "UPDATE app_user SET password_hash=?,updated_at=? WHERE id=?",
            (encoded, now(), user["id"]),
        )
        revoke_sessions(conn, user)
        audit(conn, "password_reset", uid)
    return {
        "ok": True,
        "msg": "密码已重置，旧登录已失效",
        "username": user["username"],
    }


@router.post("/api/users/{uid}/update")
async def edit_user(uid: str, username: str = Form(...), password: str = Form("")):
    require_admin()
    username = username.strip()
    validate_name(username, "")
    if password:
        validate_password(password)
    encoded = await run_in_threadpool(password_hash, password) if password else None
    with mutation(("user", uid)) as conn:
        user = find_user(conn, uid)
        try:
            conn.execute(
                "UPDATE app_user SET username=?,password_hash=?,updated_at=? WHERE id=?",
                (username, encoded or user["password_hash"], now(), user["id"]),
            )
        except IntegrityError as error:
            raise HTTPException(409, "账号已存在") from error
        if password or username != user["username"]:
            revoke_sessions(conn, user)
        audit(conn, "user_updated", uid)
    return {
        "ok": True,
        "msg": (
            "用户已更新，原登录已失效"
            if password or username != user["username"]
            else "用户已更新"
        ),
    }


def parse_user_permissions(permissions):
    try:
        values = json.loads(permissions)
        if not isinstance(values, list) or any(
            not isinstance(k, str) or k not in KEYS for k in values
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "无效的权限设置") from None
    return json.dumps(sorted(set(values)))


def apply_user_permissions(conn, user, permissions):
    conn.execute(
        "UPDATE app_user SET permissions=?,updated_at=? WHERE id=?",
        (permissions, now(), user["id"]),
    )
    audit(conn, "permissions_updated", user["public_id"])


@router.post("/api/users/{uid}/permissions")
async def update_user_permissions(uid: str, permissions: str = Form(...)):
    require_admin()
    encoded = parse_user_permissions(permissions)
    with mutation(("user", uid)) as conn:
        user = find_user(conn, uid)
        apply_user_permissions(conn, user, encoded)
    return {"ok": True, "msg": "权限已更新"}


@router.post("/api/users/{uid}/toggle")
async def toggle_user(uid: str):
    require_admin()
    with mutation(("user", uid)) as conn:
        user = find_user(conn, uid)
        conn.execute(
            "UPDATE app_user SET enabled=?,updated_at=? WHERE id=?",
            (not user["enabled"], now(), user["id"]),
        )
        audit(conn, "user_disabled" if user["enabled"] else "user_enabled", uid)
    return {"ok": True, "msg": "用户已禁用" if user["enabled"] else "用户已启用"}


def require_empty_user(conn, user):
    if (
        conn.execute(
            "SELECT 1 FROM gateway WHERE owner_user_id=? LIMIT 1", (user["id"],)
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM pairing_request WHERE owner_user_id=? AND gateway_id IS NULL AND claimed_mac='' AND expires_at>? LIMIT 1",
            (user["id"], time.time()),
        ).fetchone()
    ):
        raise HTTPException(409, "请先移交该用户的网关并取消有效配对码")


def remove_user(conn, user):
    revoke_sessions(conn, user)
    conn.execute(
        "DELETE FROM pairing_request WHERE owner_user_id=? AND gateway_id IS NULL",
        (user["id"],),
    )
    conn.execute(
        "UPDATE pairing_request SET owner_user_id=NULL WHERE owner_user_id=?",
        (user["id"],),
    )
    conn.execute("DELETE FROM app_user WHERE id=?", (user["id"],))
    audit(conn, "user_deleted", user["public_id"])


@router.post("/api/users/bulk")
async def bulk_users(
    ids: str = Form(...),
    action: str = Form(...),
    password: str = Form(""),
    permissions: str = Form(""),
):
    require_admin()
    selected = list(dict.fromkeys(ids.split(",")))
    if (
        action not in {"enable", "disable", "delete", "password", "permissions"}
        or not 1 <= len(selected) <= 500
        or any(not re.fullmatch(r"u_[0-9a-f]{32}", uid) for uid in selected)
    ):
        raise HTTPException(400, "请选择用户及有效操作")
    if action == "password":
        validate_password(password)
    encoded_permissions = (
        parse_user_permissions(permissions) if action == "permissions" else None
    )
    # Each user gets a distinct salt, even when the chosen password is shared.
    hashes = (
        await run_in_threadpool(lambda: [password_hash(password) for _ in selected])
        if action == "password"
        else []
    )
    with mutation(*[("user", uid) for uid in selected]) as conn:
        targets = [find_user(conn, uid) for uid in selected]
        # Preflight the entire selection before revoking sessions or changing rows.
        if action == "delete":
            for user in targets:
                require_empty_user(conn, user)
        for index, user in enumerate(targets):
            if action == "delete":
                remove_user(conn, user)
            elif action == "permissions":
                apply_user_permissions(conn, user, encoded_permissions)
            elif action == "password":
                conn.execute(
                    "UPDATE app_user SET password_hash=?,updated_at=? WHERE id=?",
                    (hashes[index], now(), user["id"]),
                )
                revoke_sessions(conn, user)
                audit(conn, "password_reset", user["public_id"])
            else:
                enabled = action == "enable"
                if bool(user["enabled"]) != enabled:
                    conn.execute(
                        "UPDATE app_user SET enabled=?,updated_at=? WHERE id=?",
                        (enabled, now(), user["id"]),
                    )
                    audit(
                        conn,
                        "user_enabled" if enabled else "user_disabled",
                        user["public_id"],
                    )
    return {
        "ok": True,
        "msg": {
            "permissions": "权限已更新",
            "password": "密码已更新，原登录已失效",
            "delete": "用户已删除",
            "enable": "用户已启用",
            "disable": "用户已禁用",
        }[action],
        "count": len(targets),
    }


@router.post("/api/users/{uid}/delete")
async def delete_user(uid: str):
    require_admin()
    with mutation(("user", uid)) as conn:
        user = find_user(conn, uid)
        require_empty_user(conn, user)
        remove_user(conn, user)
    return {"ok": True, "msg": "用户已删除"}


def apply_gateway_assignments(conn, gateways, target, owner):
    changed = []
    for gateway in gateways:
        if gateway["delete_requested_at"]:
            raise HTTPException(409, "请先撤销删除再分配网关")
        if gateway["owner_user_id"] == target:
            continue
        gid = gateway["id"]
        conn.execute("UPDATE gateway SET owner_user_id=? WHERE id=?", (target, gid))
        changed.append(gateway)
    # Check the final owners of the whole batch, not an intermediate state.
    for gateway in changed:
        gid = gateway["id"]
        for device in conn.execute(
            "SELECT * FROM sub_device WHERE gateway_id=?", (gid,)
        ):
            linked = (
                device["pc_service_bound"]
                or conn.execute(
                    "SELECT 1 FROM hass_power WHERE device_id=?", (device["id"],)
                ).fetchone()
            )
            if linked and service_mac_conflict(conn, device["mac"]):
                raise HTTPException(
                    409, "存在跨用户同MAC的关机服务，请先解除关联再分配"
                )
        # Existing guests must not retain control after an ownership change.
        conn.execute(
            "UPDATE device_share SET enabled=0,credential_version=credential_version+1 WHERE device_id IN (SELECT id FROM sub_device WHERE gateway_id=?)",
            (gid,),
        )
        conn.execute(
            "DELETE FROM share_session WHERE share_id IN (SELECT s.id FROM device_share s JOIN sub_device d ON d.id=s.device_id WHERE d.gateway_id=?)",
            (gid,),
        )
        conn.execute(
            "UPDATE pairing_request SET owner_user_id=?,expires_at=CASE WHEN expires_at<? THEN expires_at ELSE ? END WHERE gateway_id=? AND claimed_mac=''",
            (target, time.time(), time.time(), gid),
        )
        conn.execute(
            "UPDATE power_command SET status='cancelled' WHERE device_id IN (SELECT id FROM sub_device WHERE gateway_id=?) AND status IN ('pending','dispatched','accepted')",
            (gid,),
        )
        audit(conn, "gateway_assigned:" + owner, gateway["public_id"])
    return len(changed)


@router.post("/gateway/{gid}/assign")
async def assign_gateway(gid: GatewayId, owner: str = Form("")):
    from ..access import target_owner

    require_admin()
    with mutation(("gateway", gid)) as conn:
        target = target_owner(conn, owner, required=True)
        gateway = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        changed = apply_gateway_assignments(conn, [gateway], target, owner)
    if not changed:
        return {"ok": True, "msg": "归属未变化"}
    return {"ok": True, "msg": "网关归属已更新，原分享已停用"}


@router.post("/gateway/batch_assign")
async def batch_assign_gateways(ids: str = Form(...), owner: str = Form("")):
    from ..access import target_owner

    require_admin()
    with mutation() as conn:
        target = target_owner(conn, owner, required=True)
        rows = gateway_ids(conn, ids)
        changed = apply_gateway_assignments(conn, rows, target, owner)
    return {
        "ok": True,
        "updated": changed,
        "msg": "网关归属已更新，原分享已停用" if changed else "归属未变化",
    }
