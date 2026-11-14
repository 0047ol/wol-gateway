"""Strict, bounded selection for operations that must succeed as a whole."""

import re
from fastapi import HTTPException


def device_ids(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise HTTPException(400, "请选择 1 至 64 台有效设备")
    parts = value.split(",")
    if not 1 <= len(parts) <= 64 or any(
        not re.fullmatch(r"[1-9]\d{0,9}", part.strip()) for part in parts
    ):
        raise HTTPException(400, "请选择 1 至 64 台有效设备")
    selected = [int(part.strip()) for part in parts]
    if len(set(selected)) != len(selected):
        raise HTTPException(400, "设备选择包含重复项，请重新选择")
    return selected


def gateway_ids(conn, value, *, allow_ota=False):
    from .gateway_refs import resolve_gateway_ref
    from .concurrency import check

    parts = value.split(",") if isinstance(value, str) else []
    if not 1 <= len(parts) <= 64 or any(
        not re.fullmatch(r"(?:g:)?g_[a-f0-9]{32}", part) for part in parts
    ):
        raise HTTPException(400, "请选择 1 至 64 台有效网关")
    refs = [part.removeprefix("g:") for part in parts]
    if len(set(refs)) != len(refs):
        raise HTTPException(400, "网关选择包含重复项，请重新选择")
    return [
        check(conn, "gateway", resolve_gateway_ref(ref, conn), allow_ota=allow_ota)
        for ref in refs
    ]
