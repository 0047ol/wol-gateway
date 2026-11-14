"""Optimistic configuration writes, checked again inside a database transaction."""

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

from .access import actor, require_gateway_access, require_pairing_access
from .database import atomic, db
from .permissions import enforce, required_permissions

request_versions = ContextVar("wol_request_versions", default=None)
TABLES = {
    "gateway": "gateway",
    "device": "sub_device",
    "user": "app_user",
    "share": "device_share",
    "pairing": "pairing_request",
}
CONFLICT = "数据已被其他操作更新，请重新打开后再试；本次修改未保存"


def parse_versions(value):
    if not value:
        return None
    try:
        values = json.loads(value)
        if not isinstance(values, dict) or len(values) > 1100:
            raise ValueError()
        if any(
            not isinstance(k, str)
            or len(k) > 80
            or not isinstance(v, str)
            or not re.fullmatch(r"[0-9a-f]{32}", v)
            for k, v in values.items()
        ):
            raise ValueError()
        return values
    except (ValueError, TypeError):
        raise HTTPException(400, "无效的数据版本，请刷新页面") from None


def verify_actor(conn):
    value = actor()
    if value is None:  # Internal MQTT/enrollment work has its own authentication.
        return
    session = conn.execute(
        "SELECT credential_version FROM admin_session WHERE token_hash=? AND expires_at>?",
        (value["token_hash"], time.time()),
    ).fetchone()
    user = conn.execute(
        "SELECT * FROM app_user WHERE id=?", (value["user_id"],)
    ).fetchone()
    if (
        not session
        or not user
        or user["role"] != value["role"]
        or session[0] != value["credential_version"]
        or (
            user["role"] == "user"
            and session[0] != f"u:{user['id']}:{user['session_version']}"
        )
    ):
        raise HTTPException(401, "登录已失效，请重新登录")
    if not user["enabled"]:
        raise HTTPException(423, "当前用户已被禁用，请联系管理员重新启用")
    enforce(dict(user), required_permissions.get())


def check(conn, kind, identifier, *, allow_ota=False):
    table = TABLES[kind]
    column = (
        "device_id"
        if kind == "share"
        else "public_id" if kind == "user" and isinstance(identifier, str) else "id"
    )
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {column}=?", (identifier,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在")
    if kind == "gateway":
        require_gateway_access(conn, identifier)
        from .ota import assert_not_busy

        if not allow_ota:
            assert_not_busy(conn, identifier)
    elif kind == "device":
        require_gateway_access(conn, row["gateway_id"])
        from .ota import assert_not_busy

        assert_not_busy(conn, row["gateway_id"])
    elif kind == "share":
        device = conn.execute(
            "SELECT gateway_id FROM sub_device WHERE id=?", (identifier,)
        ).fetchone()
        if not device:
            raise HTTPException(409, CONFLICT)
        require_gateway_access(conn, device[0])
    elif kind == "pairing":
        require_pairing_access(conn, identifier)
    elif actor() and actor()["role"] != "admin" and row["id"] != actor()["user_id"]:
        raise HTTPException(403, "仅管理员可执行此操作")
    if actor() is None:
        return row
    reference = row["public_id"] if kind in ("gateway", "user") else identifier
    expected = (request_versions.get() or {}).get(f"{kind}:{reference}")
    if not expected:
        raise HTTPException(428, "缺少数据版本，请刷新页面后重试")
    if expected != row["edit_revision"]:
        raise HTTPException(409, CONFLICT)
    return row


@contextmanager
def mutation(*resources):
    """Lock before checking versions; never hold this context across an await."""
    with db() as conn, atomic(conn):
        verify_actor(conn)
        for kind, identifier in resources:
            check(conn, kind, identifier)
        yield conn
