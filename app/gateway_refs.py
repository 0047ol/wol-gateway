"""Opaque browser references; database primary keys stay inside the server."""

import re
from typing import Annotated

from fastapi import Depends, HTTPException, Path

from .database import db
from .access import require_gateway_access


def resolve_gateway_ref(value, conn=None):
    if not isinstance(value, str) or not re.fullmatch(r"g_[0-9a-f]{32}", value):
        raise HTTPException(404, "网关不存在")
    if conn is None:
        with db() as connection:
            return resolve_gateway_ref(value, connection)
    row = conn.execute("SELECT id FROM gateway WHERE public_id=?", (value,)).fetchone()
    if row is None:
        raise HTTPException(404, "网关不存在")
    require_gateway_access(conn, row["id"])
    return row["id"]


def gateway_path_id(gid: str = Path()):
    return resolve_gateway_ref(gid)


GatewayId = Annotated[int, Depends(gateway_path_id)]


def browser_gateway(row):
    value = dict(row)
    value["id"] = value.pop("public_id")
    value.pop("owner_user_id", None)
    if "edit_revision" in value:
        value["revision"] = value.pop("edit_revision")
    return value


def browser_pairing(row):
    if row is None:
        return None
    return {key: value for key, value in dict(row).items() if key != "gateway_id"}
