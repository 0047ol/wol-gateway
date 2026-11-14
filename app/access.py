"""Authenticated browser scope; internal MQTT work keeps its explicit device identity."""

from contextvars import ContextVar

from fastapi import HTTPException

from .database import db

actor_context = ContextVar("wol_actor", default=None)


def actor():
    return actor_context.get()


def is_admin(value=None):
    value = actor() if value is None else value
    return bool(value and value["role"] == "admin")


def require_admin():
    if not is_admin():
        raise HTTPException(403, "仅管理员可执行此操作")


def scope_clause(prefix="", *, owner=None):
    """Only code-defined column prefixes are accepted; values are always bound."""
    value = actor()
    column = prefix + "owner_user_id"
    if value and value["role"] != "admin":
        return column + "=?", [value["user_id"]]
    if owner == "unassigned":
        return column + " IS NULL", []
    if owner and owner != "all":
        with db() as conn:
            user = conn.execute(
                "SELECT id FROM app_user WHERE public_id=? AND role='user'", (owner,)
            ).fetchone()
        if not user:
            raise HTTPException(404, "用户不存在")
        return column + "=?", [user["id"]]
    return "1=1", []


def can_access_gateway(conn, gid, value):
    row = conn.execute(
        "SELECT owner_user_id FROM gateway WHERE id=?", (gid,)
    ).fetchone()
    return bool(row and (value["role"] == "admin" or row[0] == value["user_id"]))


def require_gateway_access(conn, gid, value=None):
    value = actor() if value is None else value
    if value is not None and not can_access_gateway(conn, gid, value):
        raise HTTPException(404, "网关不存在")


def require_pairing_access(conn, pid):
    value = actor()
    if value is None or value["role"] == "admin":
        return
    row = conn.execute("SELECT * FROM pairing_request WHERE id=?", (pid,)).fetchone()
    if (
        not row
        or (
            row["gateway_id"] is not None
            and not can_access_gateway(conn, row["gateway_id"], value)
        )
        or (row["gateway_id"] is None and row["owner_user_id"] != value["user_id"])
    ):
        raise HTTPException(404, "配对记录不存在")


def target_owner(conn, public_id="", *, required=False):
    value = actor()
    if value and value["role"] != "admin":
        if public_id and public_id != value["public_id"]:
            raise HTTPException(403, "不能为其他用户创建网关")
        return value["user_id"]
    if not public_id or public_id == "unassigned":
        if required:
            raise HTTPException(400, "请选择归属用户")
        return None
    row = conn.execute(
        "SELECT id FROM app_user WHERE public_id=? AND role='user' AND enabled=1",
        (public_id,),
    ).fetchone()
    if not row:
        raise HTTPException(400, "请选择有效且已启用的用户")
    return row["id"]


def service_mac_conflict(conn, mac):
    """A MAC-only service cannot distinguish identical MACs across account scopes."""
    rows = conn.execute(
        """SELECT DISTINCT COALESCE(g.owner_user_id,0) FROM sub_device d
        JOIN gateway g ON g.id=d.gateway_id
        WHERE replace(replace(replace(lower(d.mac),':',''),'-',''),'.','')=?""",
        (str(mac).replace(":", "").replace("-", "").replace(".", "").lower(),),
    ).fetchall()
    return len(rows) > 1
