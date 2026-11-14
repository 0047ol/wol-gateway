"""Administrator and user login, revocable sessions and CSRF checks."""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .database import db
from .database import now
from .access import actor_context, require_pairing_access
from .concurrency import request_versions, parse_versions
from .permissions import (
    decode_permissions,
    enforce,
    route_permissions,
    required_permissions,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
SESSION_COOKIE = "wol_session"
CSRF_COOKIE = "wol_csrf"
SESSION_SECONDS = 400 * 24 * 3600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    count = 600000
    value = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), count
    ).hex()
    return f"pbkdf2_sha256${count}${salt}${value}"


def verify_password(password, encoded):
    try:
        algorithm, count, salt, expected = encoded.split("$")
        count = int(count)
        if algorithm != "pbkdf2_sha256" or not 100000 <= count <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), count
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def credentials():
    try:
        raw = Path(settings.ADMIN_AUTH_PATH).read_bytes()
        value = json.loads(raw)
        if not isinstance(value.get("username"), str) or not isinstance(
            value.get("password_hash"), str
        ):
            return None
        return {**value, "version": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, AttributeError):
        return None


def admin_account(auth):
    """Import the existing hash once. Keep the private file for legacy sessions."""
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO app_user
            (public_id,username,display_name,role,password_hash,created_at,updated_at)
            VALUES (?,?,?,'admin',?,?,?)""",
            (
                "u_" + secrets.token_hex(16),
                auth["username"],
                "管理员",
                auth["password_hash"],
                now(),
                now(),
            ),
        )
        conn.execute(
            """UPDATE app_user SET username=?,password_hash=?,updated_at=?
            WHERE role='admin' AND (username!=? OR password_hash!=?)""",
            (
                auth["username"],
                auth["password_hash"],
                now(),
                auth["username"],
                auth["password_hash"],
            ),
        )
        return dict(
            conn.execute("SELECT * FROM app_user WHERE role='admin'").fetchone()
        )


def safe_next(value):
    return (
        value
        if value.startswith("/")
        and not value.startswith("//")
        and not any(c in value for c in ("\\", "\r", "\n"))
        else "/"
    )


def same_origin(request):
    supplied = request.headers.get("origin")
    if not supplied:
        return True
    origin = urlsplit(supplied)
    return origin.scheme == request.url.scheme and origin.netloc == request.url.netloc


def session_for(request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 128:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM admin_session WHERE token_hash=? AND expires_at>?",
            (digest(token), time.time()),
        ).fetchone()
        if not row:
            return None
        version = row["credential_version"]
        if version.startswith("u:"):
            parts = version.split(":")
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                return None
            account = conn.execute(
                "SELECT * FROM app_user WHERE id=? AND role='user' AND session_version=?",
                (int(parts[1]), int(parts[2])),
            ).fetchone()
        else:
            auth = credentials()
            account = (
                admin_account(auth)
                if auth and hmac.compare_digest(version, auth["version"])
                else None
            )
    if account:
        return {
            **dict(row),
            "user_id": account["id"],
            "permissions": decode_permissions(account["permissions"]),
            **{
                key: account[key]
                for key in ("public_id", "username", "display_name", "role", "enabled")
            },
        }
    return None


class AdminMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        request.state.authenticated = False
        request.state.actor = None
        # Device-only endpoints retain their own scoped bearer authentication.
        import re

        share_public = path.startswith("/share/") or path.startswith("/api/share/")
        public = (
            path in {"/login", "/setup"}
            or path.startswith("/static/")
            or bool(re.fullmatch(r"/api/power-agent/[0-9]+/(poll|ack)", path))
            or share_public
        )
        if not public:
            session = session_for(request)
            if not session:
                if request.method in {"GET", "HEAD"} and not path.startswith("/api/"):
                    response = RedirectResponse(
                        "/login?next="
                        + quote(
                            safe_next(
                                str(request.url.path)
                                + ("?" + request.url.query if request.url.query else "")
                            ),
                            safe="",
                        ),
                        status_code=303,
                    )
                else:
                    response = JSONResponse(
                        {"ok": False, "detail": "请先登录后台"}, status_code=401
                    )
                response.headers["Cache-Control"] = "no-store"
                return response
            request.state.authenticated = True
            request.state.actor = session
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("X-CSRF-Token", "")
                if (
                    not csrf
                    or len(csrf) > 128
                    or not same_origin(request)
                    or not hmac.compare_digest(digest(csrf), session["csrf_hash"])
                ):
                    return JSONResponse(
                        {"ok": False, "detail": "登录校验已失效，请刷新页面"},
                        status_code=403,
                    )
            if not session["enabled"] and path not in {
                "/logout",
                "/api/account/status",
                "/account-disabled",
            }:
                document = request.method == "GET" and (
                    request.headers.get("sec-fetch-dest") == "document"
                    or "text/html" in request.headers.get("accept", "")
                )
                if document:
                    return disabled_page(
                        request,
                        request.url.path
                        + ("?" + request.url.query if request.url.query else ""),
                    )
                return JSONResponse(
                    {
                        "ok": False,
                        "account_disabled": True,
                        "detail": "当前用户已被禁用，请联系管理员重新启用",
                    },
                    status_code=423,
                    headers={"Cache-Control": "no-store"},
                )
        context_token = actor_context.set(request.state.actor)
        versions_token = request_versions.set(None)
        permissions_token = required_permissions.set(
            route_permissions(path, request.method) if not public else ()
        )
        try:
            if not public:
                from fastapi import HTTPException

                try:
                    enforce(session, required_permissions.get())
                except HTTPException as error:
                    return JSONResponse(
                        {
                            "ok": False,
                            "permission_denied": True,
                            "detail": error.detail,
                        },
                        status_code=403,
                    )
            if not public and request.method not in {"GET", "HEAD", "OPTIONS"}:
                from fastapi import HTTPException

                try:
                    revisions = request.headers.get("X-WOL-Revisions")
                    if revisions is None and request.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0] in {
                        "application/x-www-form-urlencoded",
                        "multipart/form-data",
                    }:
                        # Cache the bytes before parsing so downstream form handlers
                        # can still consume the original request body.
                        await request.body()
                        revisions = (await request.form()).get("_wol_revisions")
                    request_versions.set(parse_versions(revisions))
                except HTTPException as error:
                    return JSONResponse(
                        {"detail": error.detail}, status_code=error.status_code
                    )
            if not public:
                # Pairing IDs are independent of gateway path dependencies.
                match = re.fullmatch(
                    r"/pairing/([0-9]+)/(renew|delete|cancel|rename)", path
                )
                if match:
                    from fastapi import HTTPException

                    try:
                        with db() as conn:
                            require_pairing_access(conn, int(match[1]))
                    except HTTPException as error:
                        return JSONResponse(
                            {"detail": error.detail}, status_code=error.status_code
                        )
                if session["role"] != "admin" and path in {
                    "/docs",
                    "/redoc",
                    "/openapi.json",
                    "/api/hass-power/commands",
                }:
                    return JSONResponse({"detail": "仅管理员可访问"}, status_code=403)
            response = await call_next(request)
        finally:
            request_versions.reset(versions_token)
            required_permissions.reset(permissions_token)
            actor_context.reset(context_token)
        if not public and path != "/logout":
            renew = session["expires_at"] - time.time() < SESSION_SECONDS - 86400
            if renew:
                with db() as conn:
                    conn.execute(
                        "UPDATE admin_session SET expires_at=? WHERE token_hash=?",
                        (time.time() + SESSION_SECONDS, session["token_hash"]),
                    )
            # Upgrade existing cookie attributes on page visits, without adding
            # Set-Cookie headers to every status polling response.
            if renew or (
                request.method == "GET"
                and response.headers.get("content-type", "").startswith("text/html")
            ):
                set_session_cookies(
                    response,
                    request,
                    request.cookies.get(SESSION_COOKIE, ""),
                    request.cookies.get(CSRF_COOKIE, ""),
                )
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = (
            "no-referrer" if share_public else "same-origin"
        )
        return response


def login_page(request, next_path="/", error="", status_code=200):
    if error and "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": False, "msg": error}, status_code=status_code)
    session = session_for(request)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "next_path": safe_next(next_path),
            "error": error,
            "username": session["username"] if session else "",
        },
        status_code=status_code,
    )


@router.get("/login")
async def login_get(request: Request, next: str = "/"):
    if not Path(settings.ADMIN_AUTH_PATH).exists():
        return RedirectResponse("/setup", status_code=303)
    if session_for(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(request, next)


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if not same_origin(request):
        return login_page(request, next, "登录来源无效", 403)
    auth = credentials()
    if not auth:
        return login_page(request, next, "后台尚未设置登录密码，请联系管理员", 503)
    address = request.client.host if request.client else "unknown"
    stamp = time.time()
    with db() as conn:
        conn.execute("DELETE FROM login_attempt WHERE expires_at<=?", (stamp,))
        attempt = conn.execute(
            "SELECT failures FROM login_attempt WHERE address=?", (address,)
        ).fetchone()
        if attempt and attempt[0] >= 5:
            return login_page(request, next, "登录失败次数过多，请十五分钟后重试", 429)
        # Reserve the attempt before slow verification to bound concurrent guesses.
        conn.execute(
            """INSERT INTO login_attempt(address,failures,expires_at) VALUES (?,1,?)
            ON CONFLICT(address) DO UPDATE SET failures=failures+1""",
            (address, stamp + 900),
        )
    admin = admin_account(auth)
    with db() as conn:
        account = conn.execute(
            "SELECT * FROM app_user WHERE username=? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    admin_login = username.strip().casefold() == auth["username"].casefold()
    encoded = (
        auth["password_hash"]
        if admin_login
        else (
            account["password_hash"]
            if account and account["role"] == "user"
            else auth["password_hash"]
        )
    )
    valid = len(password) <= 1024 and await run_in_threadpool(
        verify_password, password, encoded
    )
    if not valid or not (admin_login or (account and account["role"] == "user")):
        return login_page(request, next, "用户名或密码错误", 401)
    # A concurrent reset/disable must not mint a valid session after verification.
    expected_version = (
        auth["version"]
        if admin_login
        else f"u:{account['id']}:{account['session_version']}"
    )
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM login_attempt WHERE address=?", (address,))
        conn.execute("DELETE FROM admin_session WHERE expires_at<=?", (stamp,))
        conn.execute(
            "INSERT INTO admin_session VALUES (?,?,?,?)",
            (digest(token), digest(csrf), expected_version, stamp + SESSION_SECONDS),
        )
    response = (
        JSONResponse({"ok": True, "redirect": safe_next(next)})
        if "application/json" in request.headers.get("accept", "")
        else RedirectResponse(safe_next(next), status_code=303)
    )
    set_session_cookies(response, request, token, csrf)
    return response


def disabled_page(request, next_path="/"):
    return templates.TemplateResponse(
        request=request,
        name="account_disabled.html",
        context={"next_path": safe_next(next_path)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/account-disabled")
async def account_disabled(request: Request, next: str = "/"):
    if request.state.actor and request.state.actor["enabled"]:
        return RedirectResponse(safe_next(next), status_code=303)
    return disabled_page(request, next)


def set_session_cookies(response, request, token, csrf):
    secure = request.url.scheme == "https"
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    # Lax permits normal top-level navigation from another site. POST requests
    # still require the server-side session, a matching CSRF token and origin.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=SESSION_SECONDS,
        expires=expires,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=SESSION_SECONDS,
        expires=expires,
    )


def setup_page(request, error="", status_code=200):
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={"request": request, "error": error},
        status_code=status_code,
    )


@router.get("/setup")
async def setup_get(request: Request):
    if Path(settings.ADMIN_AUTH_PATH).exists():
        return RedirectResponse("/login", status_code=303)
    return setup_page(request)


@router.post("/setup")
async def setup_post(
    request: Request, password: str = Form(...), confirm_password: str = Form(...)
):
    path = Path(settings.ADMIN_AUTH_PATH)
    if path.exists():
        return RedirectResponse("/login", status_code=303)
    if not same_origin(request):
        return setup_page(request, "登录来源无效", 403)
    if not 8 <= len(password) <= 128 or password != confirm_password:
        return setup_page(request, "密码需为 8 至 128 个字符，且两次输入一致", 400)
    encoded = await run_in_threadpool(password_hash, password)
    # Exclusive creation makes initialization one-time, including concurrent requests.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return RedirectResponse("/login", status_code=303)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"username": "admin", "password_hash": encoded}, stream)
    return await login_post(request, "admin", password, "/")


@router.post("/logout")
async def logout(request: Request):
    with db() as conn:
        conn.execute(
            "DELETE FROM admin_session WHERE token_hash=?",
            (digest(request.cookies.get(SESSION_COOKIE, "")),),
        )
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return response
