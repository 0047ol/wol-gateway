"""Short-lived, session-bound commands for the Windows HTTPS polling agent."""

import hashlib
import hmac
import re
import time
from contextlib import contextmanager

from fastapi import HTTPException, Request

from .database import atomic, db

ONLINE_WINDOW = 20
COMMAND_TTL = 30
STATUS_MESSAGES = {
    "pending": "等待电脑端获取指令",
    "dispatched": "指令已送达，等待电脑端确认",
    "accepted": "电脑端已接受，等待系统调用结果",
    "submitted": "Windows 已接受关机调用；实际关机请查看设备状态",
    "failed": "Windows 关机调用失败，请检查电脑端日志",
    "expired": "指令已过期，未执行",
    "unknown": "执行结果未确认；不会自动重试",
    "cancelled": "指令已取消",
}


@contextmanager
def transaction():
    with db() as conn, atomic(conn):
        yield conn


def require_tls(request: Request):
    # Plain HTTP is only allowed for local development on loopback.
    if request.url.scheme != "https" and not (
        request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        and request.client
        and request.client.host in {"127.0.0.1", "::1"}
    ):
        raise HTTPException(400, "关机服务需要 HTTPS")


def require_admin(request: Request):
    if not getattr(request.state, "authenticated", False):
        raise HTTPException(401, "请先登录后台")


def authenticate(conn, request, device_id):
    require_tls(request)
    supplied = request.headers.get("Authorization", "")
    if not supplied.startswith("Bearer ") or len(supplied) > 200:
        raise HTTPException(401, "Invalid agent credentials")
    digest = hashlib.sha256(supplied[7:].encode()).hexdigest()
    agent = conn.execute(
        "SELECT * FROM power_agent WHERE device_id=?", (device_id,)
    ).fetchone()
    expected = agent["token_hash"] if agent else "0" * 64
    if not hmac.compare_digest(digest, expected) or not agent:
        raise HTTPException(401, "Invalid agent credentials")
    return agent


def check_session(session):
    if not re.fullmatch(r"[0-9a-f]{32}", session):
        raise HTTPException(400, "Invalid agent session")


def expire(conn, device_id, timestamp):
    conn.execute(
        """UPDATE power_command SET status=CASE WHEN status='pending' THEN 'expired' ELSE 'unknown' END
                    WHERE device_id=? AND expires_at<=? AND status IN ('pending','dispatched','accepted')""",
        (device_id, timestamp),
    )


def device_view(conn, device_id):
    stamp = time.time()
    expire(conn, device_id, stamp)
    agent = conn.execute(
        "SELECT last_seen FROM power_agent WHERE device_id=?", (device_id,)
    ).fetchone()
    latest = conn.execute(
        "SELECT id,status,`force`,created_at FROM power_command WHERE device_id=? ORDER BY created_at DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    return {
        "installed": bool(agent),
        "online": bool(agent and stamp - agent["last_seen"] < ONLINE_WINDOW),
        "command": (
            {**dict(latest), "message": STATUS_MESSAGES[latest["status"]]}
            if latest
            else None
        ),
    }
