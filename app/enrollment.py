"""ESP enrollment requires binding proof; PC enrollment uses the registered MAC policy."""

import hmac
import re
from fastapi import HTTPException
from .client_policy import gateway_allowed, pc_allowed
from .database import db
from .mqtt_accounts import issue
from .pairing import claim_pairing
from .utils import norm_mac


def enroll(role, body):
    mac = norm_mac(body.get("mac", ""))
    if not re.fullmatch("[0-9a-f]{12}", mac):
        raise HTTPException(400, "MAC 无效")
    if not (
        gateway_allowed(body.get("version"))
        if role == "esp"
        else pc_allowed(body.get("version"))
    ):
        raise HTTPException(426, "version_unsupported")
    result = {}
    if role == "esp":
        key = body.get("bind_key", "")
        if key:
            with db() as conn:
                row = conn.execute(
                    "SELECT bind_key FROM gateway WHERE mac=?", (mac,)
                ).fetchone()
            if not row or not hmac.compare_digest(str(row["bind_key"]), str(key)):
                raise HTTPException(403, "key_mismatch")
            result["bind_key"] = key
        else:
            result = claim_pairing(mac, body.get("code", ""))
            if result["code"] != "paired":
                raise HTTPException(403, result["code"])
    else:
        # Deliberately relaxed PC identity policy requested by the owner. ACLs still
        # prevent PC credentials from publishing commands or accessing ESP topics.
        with db() as conn:
            known = conn.execute(
                "SELECT 1 FROM sub_device WHERE replace(replace(lower(mac),':',''),'-','')=?",
                (mac,),
            ).fetchone()
            gateway = conn.execute(
                "SELECT 1 FROM gateway WHERE mac=?", (mac,)
            ).fetchone()
        if not known or gateway:
            raise HTTPException(
                403, "请先在后台设备列表添加本机 MAC；网关 MAC 不允许作为电脑接入"
            )
    credentials = issue(role, mac)
    return dict(
        ok=True,
        **credentials,
        **{key: value for key, value in result.items() if key == "bind_key"}
    )
