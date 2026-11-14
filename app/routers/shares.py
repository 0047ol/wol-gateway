"""Single-device sharing with isolated, revocable visitor sessions."""

import hmac
import json
import secrets
import time
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from ..auth import digest, password_hash, session_for, same_origin, verify_password
from ..gateway_refs import GatewayId
from ..database import db, now, atomic
from ..concurrency import mutation
from ..access import can_access_gateway
from ..permissions import allowed, enforce
from ..device_icons import device_icon
from ..device_status import (
    device_snapshot,
    gateway_action_message,
    gateway_available,
    gateway_snapshot,
)
from ..mqtt_client import mqtt_client
from ..pc_power import pc_registry
from ..power_operations import operation_view
from ..share_secrets import reveal_password, seal_password
from ..batch_selection import device_ids
from .devices import execute_wake
from .power import execute_shutdown, power_state

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
SHARE_SESSION_SECONDS = 7 * 24 * 3600
LOGIN_WINDOW, LOGIN_LIMIT = 900, 5


def cookie_names(token):
    suffix = digest(token)[:16]
    return "wol_share_session_" + suffix, "wol_share_csrf_" + suffix


def identity_cookie(token):
    return "wol_share_identity_" + digest(token)[:16]


def _share(conn, token):
    if not isinstance(token, str) or not 24 <= len(token) <= 128:
        return None
    row = conn.execute(
        """SELECT s.*, d.gateway_id FROM device_share s
        JOIN sub_device d ON d.id=s.device_id JOIN gateway g ON g.id=d.gateway_id
        WHERE s.token=?""",
        (token,),
    ).fetchone()
    if (
        not row
        or not row["enabled"]
        or (row["expires_at"] and row["expires_at"] <= time.time())
    ):
        return None
    owner = conn.execute(
        "SELECT u.* FROM app_user u JOIN gateway g ON g.owner_user_id=u.id WHERE g.id=?",
        (row["gateway_id"],),
    ).fetchone()
    if owner and owner["enabled"] and not allowed(dict(owner), "device_share"):
        return None
    return row


def _csrf(request, cookie, expected):
    supplied = request.headers.get("X-CSRF-Token", "")
    if (
        not supplied
        or len(supplied) > 128
        or not same_origin(request)
        or not hmac.compare_digest(supplied, request.cookies.get(cookie, ""))
        or not hmac.compare_digest(digest(supplied), expected)
    ):
        raise HTTPException(403, "登录校验已失效，请刷新页面")


def _admin(request, write=False, *, conn=None, share=None):
    session = session_for(request)
    if session and (
        not session["enabled"]
        or not allowed(session, "device_share")
        or (
            share is not None
            and not can_access_gateway(conn, share["gateway_id"], session)
        )
    ):
        return None
    if session and write:
        _csrf(request, "wol_csrf", session["csrf_hash"])
    return session


def _require_admin(request, write=False):
    if not _admin(request, write):
        raise HTTPException(401, "请先登录后台")


def _guest(conn, request, share, write=False):
    cookie, csrf_cookie = cookie_names(share["token"])
    token = request.cookies.get(cookie, "")
    if not token or len(token) > 128:
        return None
    session = conn.execute(
        """SELECT * FROM share_session WHERE token_hash=? AND share_id=?
        AND credential_version=? AND expires_at>?""",
        (digest(token), share["id"], share["credential_version"], time.time()),
    ).fetchone()
    if session and write:
        _csrf(request, csrf_cookie, session["csrf_hash"])
    return session


def _identity(conn, request, share, write=False):
    if _admin(request, write, conn=conn, share=share):
        return "admin"
    if _guest(conn, request, share, write):
        return "guest"
    raise HTTPException(401, "请登录分享账号")


def _device(conn, gid, did, manageable=False):
    gateway = gateway_snapshot(conn, gid)
    row = conn.execute(
        "SELECT * FROM sub_device WHERE gateway_id=? AND id=?", (gid, did)
    ).fetchone()
    if not row:
        raise HTTPException(404, "子设备不存在")
    if manageable:
        if not device_icon(
            dict(row), service_detected=bool(pc_registry.get(row["mac"]))
        )["power_capable"]:
            raise HTTPException(409, "仅电脑和笔记本支持设备分享")
        if message := gateway_action_message(gateway, allow_offline=False):
            raise HTTPException(409, message)
    return gateway, row


def _manage_share(conn, gid, did):
    _device(conn, gid, did)
    row = conn.execute(
        "SELECT * FROM device_share WHERE device_id=?", (did,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "分享尚未创建")
    return row


def _audit(conn, gid, did, event, actor="admin"):
    # No tokens, credentials, sessions or native command details in audit payloads.
    conn.execute(
        "INSERT INTO cmd_log(gateway_id,cmd_type,payload,status,created_at) VALUES (?,?,?,'ok',?)",
        (gid, "share_" + event, json.dumps({"device_id": did, "source": actor}), now()),
    )


def _details(request, row, conn=None):
    can_reset = True
    if conn is not None:
        device = conn.execute(
            "SELECT * FROM sub_device WHERE id=?", (row["device_id"],)
        ).fetchone()
        gateway = gateway_snapshot(conn, device["gateway_id"])
        can_reset = (
            not gateway_action_message(gateway, allow_offline=False)
            and device_icon(
                dict(device), service_detected=bool(pc_registry.get(device["mac"]))
            )["power_capable"]
        )
    return {
        "ok": True,
        "exists": True,
        "revision": row["edit_revision"],
        "enabled": bool(row["enabled"]),
        "username": row["username"],
        "password": reveal_password(row["password_secret"], row["token"]),
        "link": str(request.base_url).rstrip("/") + "/share/" + row["token"],
        "can_wake": bool(row["can_wake"]),
        "can_shutdown": bool(row["can_shutdown"]),
        "expires_at": row["expires_at"],
        "can_reset": bool(can_reset),
    }


def _state(conn, share, identity, session=None):
    gateway, row = _device(conn, share["gateway_id"], share["device_id"])
    evidence, service = device_snapshot(row, gateway), power_state(conn, row, gateway)
    status = (
        evidence["status"] if evidence["status"] in {"online", "offline"} else "unknown"
    )
    reason = gateway_action_message(gateway, allow_offline=False)
    owner = conn.execute(
        "SELECT * FROM app_user WHERE id=?", (gateway["owner_user_id"],)
    ).fetchone()
    if identity == "guest" and owner and not allowed(dict(owner), "device_power"):
        reason = "分享者当前无电源控制权限，请联系分享者处理"
    if identity == "admin" and session and not allowed(session, "device_power"):
        reason = "没有使用此功能的权限，请联系管理员"
    transport = bool(mqtt_client.connected_flag and mqtt_client.client.is_connected())
    wake = shutdown = False
    if not reason and not service["power_capable"]:
        reason = "仅电脑和笔记本支持设备分享"
    if not reason and not transport:
        reason = "控制通道暂不可用，请稍后刷新"
    if not reason:
        if status == "unknown":
            reason = "设备状态未知，暂时无法操作"
        elif status == "offline":
            if not gateway_available(gateway):
                reason = "网关离线，无法下发唤醒指令"
            elif not share["can_wake"]:
                reason = "分享者未授权开机"
            else:
                wake = True
        else:
            if not share["can_shutdown"]:
                reason = "分享者未授权关机"
            elif service.get("provider") == "native" and not service.get("linked"):
                reason = "WOL关机服务未关联，请联系分享者处理"
            elif (
                not service.get("installed")
                or service.get("upgrade_required")
                or not service.get("online")
            ):
                reason = "WOL关机服务不可用，请联系分享者处理"
            else:
                binding = conn.execute(
                    "SELECT last_sent FROM hass_power WHERE device_id=?", (row["id"],)
                ).fetchone()
                if binding and time.time() - binding["last_sent"] < 15:
                    reason = "关机指令刚刚发送，请勿重复操作"
                else:
                    shutdown = True
    operation = operation_view(conn, row, gateway)
    if operation and operation["status"] in {"sending", "waiting"}:
        wake = shutdown = False
        reason = operation["message"]
    labels = {"online": "在线", "offline": "离线", "unknown": "未知"}
    return {
        "name": row["name"],
        "status": status,
        "status_label": labels[status],
        "reason": reason,
        "can_wake": wake,
        "can_shutdown": shutdown,
        "operation": operation,
        "identity": identity,
    }


def _headers():
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
    }


def _view_context(conn, request, token):
    mode, identity, state = "invalid", "", None
    share = _share(conn, token)
    if share:
        identity = (
            "admin"
            if _admin(request, conn=conn, share=share)
            else "guest" if _guest(conn, request, share) else ""
        )
        mode = "control" if identity else "login"
        if identity:
            state = _state(conn, share, identity, session_for(request))
    return {
        "mode": mode,
        "token": token if 24 <= len(token) <= 128 else "",
        "state": state,
        "identity": identity,
        "csrf_cookie": cookie_names(token)[1],
    }


@router.get("/share/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str):
    with db() as conn:
        context = _view_context(conn, request, token)
    return templates.TemplateResponse(
        request=request,
        name="share.html",
        context=context,
        headers=_headers(),
    )


@router.get("/api/share/{token}/view")
async def share_view(request: Request, token: str, current_view: str = ""):
    with db() as conn:
        context = _view_context(conn, request, token)
    result = {
        "ok": True,
        "mode": context["mode"],
        "identity": context["identity"],
        "state": context["state"],
    }
    # Only send markup when switching between control, login and unavailable.
    # The supplied mode saves bytes; the server still determines all access.
    if current_view != context["mode"] + ":" + context["identity"]:
        result["html"] = templates.env.get_template("share_card.html").render(context)
    return JSONResponse(result, headers=_headers())


@router.post("/api/share/{token}/login")
async def share_login(
    request: Request, token: str, username: str = Form(...), password: str = Form(...)
):
    if not same_origin(request):
        raise HTTPException(403, "登录来源无效")
    address = request.client.host if request.client else "unknown"
    stamp = time.time()
    with db() as conn:
        share = _share(conn, token)
        if not share:
            raise HTTPException(404, "分享不可用")
        conn.execute("DELETE FROM share_login_attempt WHERE expires_at<=?", (stamp,))
        attempt = conn.execute(
            "SELECT failures FROM share_login_attempt WHERE share_id=? AND address=?",
            (share["id"], address),
        ).fetchone()
        if attempt and attempt[0] >= LOGIN_LIMIT:
            raise HTTPException(429, "登录失败次数过多，请十五分钟后重试")
        conn.execute(
            """INSERT INTO share_login_attempt VALUES (?,?,1,?)
            ON CONFLICT(share_id,address) DO UPDATE SET failures=failures+1""",
            (share["id"], address, stamp + LOGIN_WINDOW),
        )
    valid = len(password) <= 1024 and await run_in_threadpool(
        verify_password, password, share["password_hash"]
    )
    if not valid or not hmac.compare_digest(
        username.encode(), share["username"].encode()
    ):
        raise HTTPException(401, "账号或密码错误")
    session_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db() as conn:
        current = _share(conn, token)
        if not current or current["credential_version"] != share["credential_version"]:
            raise HTTPException(401, "分享已变更，请重新登录")
        ttl = (
            min(SHARE_SESSION_SECONDS, max(0, int(current["expires_at"] - time.time())))
            if current["expires_at"]
            else SHARE_SESSION_SECONDS
        )
        session_cookie, csrf_cookie = cookie_names(token)
        previous = request.cookies.get(session_cookie, "")
        conn.execute(
            "DELETE FROM share_session WHERE expires_at<=? OR token_hash=?",
            (stamp, digest(previous)),
        )
        conn.execute(
            "INSERT INTO share_session VALUES (?,?,?,?,?,?)",
            (
                digest(session_token),
                share["id"],
                share["credential_version"],
                digest(csrf),
                stamp + ttl,
                stamp,
            ),
        )
        conn.execute(
            "DELETE FROM share_login_attempt WHERE share_id=? AND address=?",
            (share["id"], address),
        )
    response = JSONResponse(
        {"ok": True, "redirect": "/share/" + token}, headers=_headers()
    )
    for name, value, http_only in [
        (session_cookie, session_token, True),
        (csrf_cookie, csrf, False),
    ]:
        response.set_cookie(
            name,
            value,
            httponly=http_only,
            secure=request.url.scheme == "https",
            samesite="lax",
            max_age=ttl,
        )
    return response


@router.post("/api/share/{token}/logout")
async def share_logout(request: Request, token: str):
    with db() as conn:
        share = _share(conn, token)
        if not share:
            raise HTTPException(404, "分享不可用")
        identity = _identity(conn, request, share, True)
        if identity == "admin":
            enforce(session_for(request), ("device_power",))
        session_cookie, _ = cookie_names(token)
        conn.execute(
            "DELETE FROM share_session WHERE token_hash=? AND share_id=?",
            (digest(request.cookies.get(session_cookie, "")), share["id"]),
        )
    response = JSONResponse(
        {"ok": True, "redirect": "/" if identity == "admin" else "/share/" + token},
        headers=_headers(),
    )
    for cookie in cookie_names(token):
        response.delete_cookie(cookie)
    # Clear the legacy per-share preference without changing the admin session.
    response.delete_cookie(identity_cookie(token))
    return response


@router.get("/api/share/{token}/state")
async def share_state(request: Request, token: str):
    with db() as conn:
        share = _share(conn, token)
        if not share:
            raise HTTPException(404, "分享不可用")
        identity = _identity(conn, request, share)
        return JSONResponse(
            {"ok": True, "state": _state(conn, share, identity, session_for(request))},
            headers=_headers(),
        )


@router.post("/api/share/{token}/power")
async def share_power(request: Request, token: str, action: str = Form(...)):
    with db() as conn:
        share = _share(conn, token)
        if not share:
            raise HTTPException(404, "分享不可用")
        identity = _identity(conn, request, share, True)
        if action not in {"wake", "shutdown"}:
            raise HTTPException(400, "无效的电源操作")
        state = _state(conn, share, identity, session_for(request))
        if not state["can_" + action]:
            raise HTTPException(409, state["reason"] or "当前状态不支持此操作")
        gid, did = share["gateway_id"], share["device_id"]
        _audit(conn, gid, did, action, identity)
    if action == "wake":
        return await execute_wake(gid, str(did))
    return await execute_shutdown(request, gid, did, False)


@router.get("/api/gateway/{gid}/devices/{did}/share")
async def share_details(request: Request, gid: GatewayId, did: int):
    _require_admin(request)
    with db() as conn:
        gateway, device = _device(conn, gid, did)
        row = conn.execute(
            "SELECT * FROM device_share WHERE device_id=?", (did,)
        ).fetchone()
        result = _details(request, row, conn) if row else {"ok": True, "exists": False}
        result["device"] = {
            "name": device["name"],
            "mac": device["mac"],
            "ip": device["ip"] or "",
        }
        result["device_revision"] = device["edit_revision"]
        result["gateway_revision"] = gateway["edit_revision"]
        result["can_configure"] = (
            not gateway_action_message(gateway, allow_offline=False)
            and device_icon(
                dict(device), service_detected=bool(pc_registry.get(device["mac"]))
            )["power_capable"]
        )
        return result


@router.post("/api/gateway/{gid}/devices/{did}/share")
async def create_share(
    request: Request,
    gid: GatewayId,
    did: int,
    can_wake: bool = Form(True),
    can_shutdown: bool = Form(True),
):
    _require_admin(request, True)
    if not (can_wake or can_shutdown):
        raise HTTPException(400, "请至少选择一项电源权限")
    with mutation(("gateway", gid), ("device", did)) as conn:
        _device(conn, gid, did, True)
        existing = conn.execute(
            "SELECT * FROM device_share WHERE device_id=?", (did,)
        ).fetchone()
        if existing:
            return {**_details(request, existing), "created": False}
    password = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(24)
    secret = seal_password(password, token)
    encoded = await run_in_threadpool(password_hash, password)
    with mutation(("gateway", gid), ("device", did)) as conn:
        _device(conn, gid, did, True)
        existing = conn.execute(
            "SELECT * FROM device_share WHERE device_id=?", (did,)
        ).fetchone()
        if existing:
            return {**_details(request, existing), "created": False}
        stamp = time.time()
        conn.execute(
            """INSERT INTO device_share(device_id,token,username,password_hash,password_secret,can_wake,can_shutdown,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                did,
                token,
                "share-" + secrets.token_hex(4),
                encoded,
                secret,
                int(can_wake),
                int(can_shutdown),
                stamp,
                stamp,
            ),
        )
        _audit(conn, gid, did, "created")
        result = _details(request, _manage_share(conn, gid, did))
    return {**result, "created": True, "password": password, "can_configure": True}


def _new_batch_devices(conn, gid, selected):
    rows = []
    for did in selected:
        _, device = _device(conn, gid, did, True)
        if conn.execute(
            "SELECT 1 FROM device_share WHERE device_id=?", (did,)
        ).fetchone():
            raise HTTPException(409, "所选设备已有分享，请取消选择已有分享的设备")
        rows.append(device)
    return rows


@router.post("/api/gateway/{gid}/devices/batch_share")
async def create_batch_share(
    request: Request,
    gid: GatewayId,
    ids: str = Form(...),
    can_wake: bool | None = Form(None),
    can_shutdown: bool | None = Form(None),
):
    _require_admin(request, True)
    form = await request.form()
    can_wake = True if "can_wake" not in form else bool(can_wake)
    can_shutdown = True if "can_shutdown" not in form else bool(can_shutdown)
    if not (can_wake or can_shutdown):
        raise HTTPException(400, "请至少选择一项电源权限")
    selected = device_ids(ids)
    with mutation(("gateway", gid), *[("device", did) for did in selected]) as conn:
        _new_batch_devices(conn, gid, selected)
    password, username = secrets.token_urlsafe(16), "share-" + secrets.token_hex(4)
    encoded = await run_in_threadpool(password_hash, password)
    tokens = {did: secrets.token_urlsafe(24) for did in selected}
    sealed = {did: seal_password(password, token) for did, token in tokens.items()}
    with mutation(("gateway", gid), *[("device", did) for did in selected]) as conn:
        rows = _new_batch_devices(conn, gid, selected)
        stamp, items = time.time(), []
        with atomic(conn):
            for device in rows:
                did = device["id"]
                conn.execute(
                    """INSERT INTO device_share(device_id,token,username,password_hash,password_secret,can_wake,can_shutdown,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        did,
                        tokens[did],
                        username,
                        encoded,
                        sealed[did],
                        int(can_wake),
                        int(can_shutdown),
                        stamp,
                        stamp,
                    ),
                )
                _audit(conn, gid, did, "created")
                items.append(
                    {
                        "device_id": did,
                        "name": device["name"],
                        "link": str(request.base_url).rstrip("/")
                        + "/share/"
                        + tokens[did],
                    }
                )
    return {
        "ok": True,
        "username": username,
        "password": password,
        "items": items,
        "created": len(items),
        "can_wake": can_wake,
        "can_shutdown": can_shutdown,
        "msg": "分享已生成",
    }


def _resettable_share(conn, gid, did):
    row = _manage_share(conn, gid, did)
    _device(conn, gid, did, True)
    return row


@router.post("/api/gateway/{gid}/devices/{did}/share/reset")
async def reset_share(
    request: Request, gid: GatewayId, did: int, password: str | None = Form(None)
):
    _require_admin(request, True)
    if "password" in await request.form():
        raise HTTPException(400, "分享密码仅支持随机重置，不能自定义")
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        previous = _resettable_share(conn, gid, did)
    password = secrets.token_urlsafe(16)
    encoded = await run_in_threadpool(password_hash, password)
    sealed = seal_password(password, previous["token"])
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        row = _resettable_share(conn, gid, did)
        if (row["id"], row["credential_version"]) != (
            previous["id"],
            previous["credential_version"],
        ):
            raise HTTPException(409, "分享已变更，请重新打开设置")
        with atomic(conn):
            conn.execute(
                "UPDATE device_share SET password_hash=?,password_secret=?,credential_version=credential_version+1,updated_at=? WHERE id=?",
                (encoded, sealed, time.time(), row["id"]),
            )
            conn.execute("DELETE FROM share_session WHERE share_id=?", (row["id"],))
            _audit(conn, gid, did, "reset")
        result = _details(request, _manage_share(conn, gid, did), conn)
    return {**result, "msg": "分享密码已重置，旧会话已失效"}


@router.post("/api/gateway/{gid}/devices/{did}/share/toggle")
async def toggle_share(
    request: Request, gid: GatewayId, did: int, enabled: bool = Form(...)
):
    _require_admin(request, True)
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        row = _manage_share(conn, gid, did)
        if enabled:
            _device(conn, gid, did, True)
            if not (row["can_wake"] or row["can_shutdown"]):
                raise HTTPException(409, "请先选择电源权限后保存")
        conn.execute(
            "UPDATE device_share SET enabled=?,credential_version=credential_version+1,updated_at=? WHERE id=?",
            (int(enabled), time.time(), row["id"]),
        )
        conn.execute("DELETE FROM share_session WHERE share_id=?", (row["id"],))
        _audit(conn, gid, did, "enabled" if enabled else "disabled")
    return {
        "ok": True,
        "enabled": enabled,
        "msg": "分享已启用" if enabled else "分享已停用",
    }


@router.post("/api/gateway/{gid}/devices/{did}/share/settings")
async def share_settings(
    request: Request,
    gid: GatewayId,
    did: int,
    can_wake: bool = Form(...),
    can_shutdown: bool = Form(...),
    password: str | None = Form(None),
):
    _require_admin(request, True)
    enabled = bool(can_wake or can_shutdown)
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        previous = _manage_share(conn, gid, did)
        if enabled:
            _device(conn, gid, did, True)
    if "password" in await request.form():
        raise HTTPException(400, "分享密码仅支持随机重置，不能自定义")
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        row = _manage_share(conn, gid, did)
        if (
            row["id"] != previous["id"]
            or row["credential_version"] != previous["credential_version"]
        ):
            raise HTTPException(409, "分享已变更，请重新打开设置")
        if enabled:
            _device(conn, gid, did, True)
        revoke = bool(row["enabled"]) != enabled
        conn.execute(
            """UPDATE device_share SET enabled=?,can_wake=?,can_shutdown=?,
            credential_version=credential_version+?,updated_at=? WHERE id=?""",
            (
                int(enabled),
                int(can_wake),
                int(can_shutdown),
                int(revoke),
                time.time(),
                row["id"],
            ),
        )
        if revoke:
            conn.execute("DELETE FROM share_session WHERE share_id=?", (row["id"],))
        _audit(conn, gid, did, "permissions")
        if bool(row["enabled"]) != enabled:
            _audit(conn, gid, did, "enabled" if enabled else "disabled")
        result = _details(request, _manage_share(conn, gid, did), conn)
    return {**result, "msg": "分享设置已保存" if enabled else "分享已停用"}


@router.post("/api/gateway/{gid}/devices/{did}/share/delete")
async def delete_share(request: Request, gid: GatewayId, did: int):
    _require_admin(request, True)
    with mutation(("gateway", gid), ("device", did), ("share", did)) as conn:
        row = _manage_share(conn, gid, did)
        conn.execute("DELETE FROM device_share WHERE id=?", (row["id"],))
        _audit(conn, gid, did, "deleted")
    return {"ok": True, "msg": "分享已删除，原链接和账号已失效"}
