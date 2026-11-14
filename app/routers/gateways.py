"""网关管理 API + 页面路由。"""

import logging
import json
import ipaddress
import re
import time

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..access import actor, is_admin, scope_clause, target_owner, require_pairing_access
from ..permissions import allowed
from ..names import valid_name, NAME_ERROR
from ..gateway_refs import GatewayId, resolve_gateway_ref
from ..database import db, now
from ..concurrency import mutation, check
from ..batch_selection import gateway_ids
from ..presentation import (
    gateway_state,
    gateway_notice,
    gateway_can_cancel_delete,
    gateway_can_transfer,
    gateway_can_repair_pairing,
    gateway_counts,
    GATEWAY_LABELS,
    gateway_can_manage,
    normalize_status_filter,
    matches_status_filter,
)
from ..device_status import gateway_snapshot, gateway_action_error
from ..device_catalog import with_device_counts
from ..background import reconcile_heartbeat_status
from ..gateway_deletion import (
    queue_gateway_deletion,
    dispatch_gateway_deletion,
    remove_gateway,
    cancel_gateway_deletion,
)
from ..mqtt_client import mqtt_client
from ..pairing import create_pairing, list_pairings
from ..utils import gen_bind_key, norm_mac, fmt_mac, paginate, device_search
from ..table_sort import sort_options, sorted_rows

log = logging.getLogger("gateway.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_mac"] = fmt_mac
templates.env.filters["gateway_state"] = gateway_state
templates.env.filters["gateway_notice"] = gateway_notice
templates.env.filters["gateway_can_cancel_delete"] = gateway_can_cancel_delete
templates.env.filters["gateway_can_transfer"] = gateway_can_transfer
templates.env.filters["gateway_can_repair_pairing"] = gateway_can_repair_pairing
templates.env.globals["gateway_labels"] = GATEWAY_LABELS
templates.env.filters["gateway_can_manage"] = gateway_can_manage


# ---------- 页面 ----------
@router.get("/", response_class=HTMLResponse)
async def page_gateways(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    q: str = "",
    sort: str = "id",
    direction: str = "desc",
    owner: str = "all",
    status: str = "all",
):
    q = q.strip()
    status = normalize_status_filter(status)
    with db() as conn:
        reconcile_heartbeat_status(conn)
        clause, scope_args = scope_clause(owner=owner)
        if actor() and not allowed(actor(), "gateway_view"):
            clause, scope_args = "1=0", []
        where, args = "WHERE " + clause, list(scope_args)
        if q:
            search_clause, search_args = device_search(q)
            where += " AND " + search_clause
            args.extend(search_args)
        total = conn.execute(
            f"SELECT COUNT(*) c FROM gateway {where}", args
        ).fetchone()["c"]
        states = conn.execute(
            f"SELECT status,last_heartbeat,bind_message,delete_requested_at,delete_error FROM gateway WHERE {clause}",
            scope_args,
        ).fetchall()
        online_total = gateway_counts(states)["online"]
        all_gateways = len(states)
        all_pairings = conn.execute(
            f"SELECT COUNT(*) FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NULL AND {clause}",
            scope_args,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT gateway.* FROM gateway {where} ORDER BY id DESC",
            args,
        ).fetchall()
        rows = with_device_counts(conn, rows)
        from ..ota import view as ota_view

        rows = [{**dict(row), "ota": ota_view(conn, row)} for row in rows]
        linked_pairings = {
            row["gateway_id"]: dict(row)
            for row in conn.execute(
                "SELECT id,gateway_id,expires_at,purpose FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NOT NULL ORDER BY id"
            )
        }
        account_users = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT public_id,username,enabled FROM app_user WHERE role='user' ORDER BY username COLLATE NOCASE"
                )
            ]
            if is_admin()
            else []
        )
        owner_ids = (
            {
                r["id"]: r["public_id"]
                for r in conn.execute("SELECT id,public_id FROM app_user")
            }
            if is_admin()
            else {}
        )
        owner_labels = (
            {
                r["id"]: r["username"]
                for r in conn.execute("SELECT id,username FROM app_user")
            }
            if is_admin()
            else {}
        )
    pending_pairings = (
        list_pairings(owner) if not actor() or allowed(actor(), "gateway_view") else []
    )
    if q:
        needle = q.casefold()
        pending_pairings = [
            p
            for p in pending_pairings
            if needle in str(p["name"] or "").casefold()
            or needle == f"P-{p['id']}".casefold()
        ]
    sort, direction = sort_options(
        sort, direction, {"id", "name", "ip", "mac", "last_heartbeat"}
    )
    entries = sorted_rows(
        [
            {**dict(row), "kind": "gateway", "pairing": linked_pairings.get(row["id"])}
            for row in rows
        ]
        + [{**item, "kind": "pairing"} for item in pending_pairings],
        sort,
        direction,
    )
    entries = [
        item
        for item in entries
        if matches_status_filter(
            gateway_state(item) if item["kind"] == "gateway" else "unbound", status
        )
    ]
    if is_admin():
        for item in entries:
            if item["kind"] == "gateway":
                item["owner_public_id"] = owner_ids.get(item.get("owner_user_id"), "")
                item["owner_label"] = owner_labels.get(
                    item.get("owner_user_id"), "未分配"
                )
    for index, row in enumerate(sorted_rows(entries, "id", "asc"), 1):
        row["display_order"] = index
    selected_total = len(entries)
    page, page_size, offset, total_pages = paginate(page, page_size, selected_total)
    entries = entries[offset : offset + page_size]
    rows = [row for row in entries if row["kind"] == "gateway"]
    return templates.TemplateResponse(
        request=request,
        name="gateways.html",
        context={
            "request": request,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total": selected_total,
            "gateway_total": total,
            "online_total": online_total,
            "all_total": all_gateways + all_pairings,
            "pairing_total": len(pending_pairings),
            "has_gateway_data": bool(all_gateways or all_pairings),
            "q": q,
            "pairings": [row for row in entries if row["kind"] == "pairing"],
            "entries": entries,
            "sort": sort,
            "direction": direction,
            "status_filter": status,
            "current_epoch": int(time.time()),
            "account_users": account_users,
            "owner_ids": owner_ids,
            "owner": owner,
            "scoped_owner": next(
                (user for user in account_users if user["public_id"] == owner), None
            ),
            "page_user": next(
                (user for user in account_users if user["public_id"] == owner),
                None if is_admin() else actor(),
            ),
            "show_system_status": not is_admin() or owner == "all",
            "is_admin": is_admin(),
        },
    )


# ---------- 新增网关 ----------
@router.post("/gateway/create")
async def create_gateway(
    name: str = Form(""), mac: str = Form(""), owner: str = Form("")
):
    name = name.strip()
    if not valid_name(name):
        return JSONResponse({"ok": False, "msg": NAME_ERROR}, status_code=400)
    mac = norm_mac(mac)
    if not mac:
        with mutation() as conn:
            target_owner(conn, owner, required=True)
            return JSONResponse(create_pairing(name, owner=owner))
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        # MAC 由 ESP32 上报时自动匹配；允许先不填 MAC 纯预生成密钥
        # 但为了让 ESP32 首次上报能匹配，这里要求必须填 MAC（从 ESP32 外壳/路由器查到）
        return JSONResponse(
            {"ok": False, "msg": "请填写有效的 ESP32 MAC 地址"}, status_code=400
        )
    bind_key = gen_bind_key()
    with mutation() as conn:
        owner_id = target_owner(conn, owner, required=True)
        try:
            conn.execute(
                """INSERT INTO gateway (mac, bind_key, name, status, created_at,owner_user_id)
                   VALUES (?,?,?, 'unbound', ?,?)""",
                (mac, bind_key, name, now(), owner_id),
            )
        except Exception:
            return JSONResponse(
                {"ok": False, "msg": "MAC 已存在或写入失败"}, status_code=400
            )
    return JSONResponse({"ok": True, "bind_key": bind_key, "mac": fmt_mac(mac)})


@router.get("/api/pairings")
async def api_pairings():
    return list_pairings()


@router.post("/pairing/{pid}/renew")
async def renew_pairing(pid: int):
    with mutation(("pairing", pid)):
        result = create_pairing(renew_id=pid)
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@router.post("/gateway/{gid}/pairing/renew")
async def renew_gateway_pairing(gid: GatewayId):
    with mutation(("gateway", gid)):
        result = create_pairing(gateway_id=gid)
    return JSONResponse(
        result,
        status_code=(
            200 if result["ok"] else 404 if result["msg"] == "网关不存在" else 409
        ),
    )


@router.post("/gateway/{gid}/transfer")
async def transfer_gateway(gid: GatewayId):
    with mutation(("gateway", gid)):
        result = create_pairing(gateway_id=gid, transfer=True)
    return JSONResponse(
        result,
        status_code=(
            200 if result["ok"] else 404 if result["msg"] == "网关不存在" else 409
        ),
    )


@router.post("/pairing/{pid}/delete")
@router.post("/pairing/{pid}/cancel")
async def delete_pairing(pid: int):
    with mutation(("pairing", pid)) as conn:
        require_pairing_access(conn, pid)
        deleted = conn.execute(
            "DELETE FROM pairing_request WHERE id=? AND claimed_mac=''", (pid,)
        )
    return {
        "ok": bool(deleted.rowcount),
        "msg": "配对记录已删除" if deleted.rowcount else "配对记录不存在或已完成",
    }


@router.post("/pairing/{pid}/rename")
async def rename_pairing(pid: int, name: str = Form("")):
    with db() as conn:
        require_pairing_access(conn, pid)
    return JSONResponse(
        {"ok": False, "msg": "待配对网关不支持修改名称"}, status_code=409
    )


# ---------- 编辑网关配置 ----------
BULK_CONFIG_FIELDS = {
    "ping_interval",
    "heartbeat_timeout",
    "heartbeat_send_interval",
    "scan_concurrency",
}


def validate_detection_config(config):
    if not (
        5 <= config["ping_interval"] <= 3600
        and 5 <= config["heartbeat_send_interval"] <= 300
        and config["heartbeat_timeout"] >= config["heartbeat_send_interval"] * 2
        and 1 <= config["scan_concurrency"] <= 8
    ):
        raise ValueError(
            "Ping 周期 5–3600 秒，心跳周期 5–300 秒，超时至少为心跳周期两倍，并发数 1–8"
        )


@router.post("/gateway/batch_update")
async def batch_update_gateways(ids: str = Form(...), config: str = Form(...)):
    try:
        changes = json.loads(config)
        if (
            not isinstance(changes, dict)
            or not changes
            or not set(changes) <= BULK_CONFIG_FIELDS
            or any(type(value) is not int for value in changes.values())
        ):
            raise ValueError("请选择有效的批量编辑项目，不支持修改名称或网络设置")
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from None
    with mutation() as conn:
        reconcile_heartbeat_status(conn)
        rows = gateway_ids(conn, ids)
        for row in rows:
            if error := gateway_action_error(row, allow_offline=True):
                raise HTTPException(error.status_code, json.loads(error.body)["msg"])
            try:
                validate_detection_config({**dict(row), **changes})
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        columns = sorted(changes)
        for row in rows:
            conn.execute(
                "UPDATE gateway SET "
                + ",".join(key + "=?" for key in columns)
                + " WHERE id=?",
                [changes[key] for key in columns] + [row["id"]],
            )
        failed = 0
        for row in rows:
            if row["status"] == "online" and set(changes) != {"heartbeat_timeout"}:
                result = mqtt_client.publish_cmd(
                    row["mac"],
                    "update_config",
                    {
                        key: value
                        for key, value in changes.items()
                        if key != "heartbeat_timeout"
                    },
                )
                failed += not result["ok"]
    return {
        "ok": True,
        "updated": len(rows),
        "push_failed": failed,
        "msg": (
            "配置已保存；在线网关已下发，离线网关上线后同步"
            if not failed
            else "配置已保存，部分网关下发失败，将在重新连接后同步"
        ),
    }


@router.post("/gateway/{gid}/update")
async def update_gateway(
    gid: GatewayId,
    name: str = Form(""),
    ping_interval: int = Form(30),
    heartbeat_timeout: int = Form(30),
    heartbeat_send_interval: int = Form(15),
    wol_broadcast_addr: str = Form("255.255.255.255"),
    scan_cidr: str = Form(""),
    scan_concurrency: int = Form(8),
    wifi_ssid: str = Form(""),
    wifi_password: str = Form(""),
):
    try:
        name = name.strip()
        if not valid_name(name):
            raise ValueError(NAME_ERROR)
        ipaddress.IPv4Address(wol_broadcast_addr)
        if scan_cidr:
            subnet = ipaddress.IPv4Network(scan_cidr, strict=False)
            if not 24 <= subnet.prefixlen <= 30:
                raise ValueError("扫描网段仅支持 /24 至 /30")
        validate_detection_config(
            {
                "ping_interval": ping_interval,
                "heartbeat_timeout": heartbeat_timeout,
                "heartbeat_send_interval": heartbeat_send_interval,
                "scan_concurrency": scan_concurrency,
            }
        )
        if wifi_ssid and (
            len(wifi_ssid.encode()) > 32
            or (
                wifi_password
                and not 8 <= len(wifi_password.encode()) <= 63
                and not re.fullmatch(r"[0-9a-fA-F]{64}", wifi_password)
            )
        ):
            raise ValueError(
                "Wi-Fi 名称最多 32 字节，密码需 8–63 字节或 64 位十六进制 PSK"
            )
        if wifi_password and not wifi_ssid:
            raise ValueError("修改 Wi-Fi 时请同时填写名称")
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    with mutation(("gateway", gid)) as conn:
        row = gateway_snapshot(conn, gid)
        if error := gateway_action_error(row, allow_offline=True):
            return error
        conn.execute(
            """UPDATE gateway SET name=?, ping_interval=?, heartbeat_timeout=?,
                   heartbeat_send_interval=?, wol_broadcast_addr=?, scan_cidr=?,
                   scan_concurrency=? WHERE id=?""",
            (
                name,
                ping_interval,
                heartbeat_timeout,
                heartbeat_send_interval,
                wol_broadcast_addr,
                scan_cidr,
                scan_concurrency,
                gid,
            ),
        )

        pushed = []
        # 先更新业务配置，再更新会导致重启的 Wi-Fi。
        if row["status"] == "online":
            ret = mqtt_client.publish_cmd(
                row["mac"],
                "update_config",
                {
                    "name": name,
                    "ping_interval": ping_interval,
                    "heartbeat_send_interval": heartbeat_send_interval,
                    "wol_broadcast_addr": wol_broadcast_addr,
                    "scan_cidr": scan_cidr,
                    "scan_concurrency": scan_concurrency,
                },
            )
            if not ret["ok"]:
                return JSONResponse(
                    {**ret, "msg": "配置已保存，但下发失败：" + ret["msg"]},
                    status_code=503,
                )
            pushed.append("config")

        next_wifi_password = wifi_password or row["wifi_password"]
        wifi_changed = bool(wifi_ssid) and (
            wifi_ssid != row["wifi_ssid"]
            or (bool(wifi_password) and wifi_password != row["wifi_password"])
        )
        if wifi_changed:
            if row["status"] != "online":
                return JSONResponse(
                    {"ok": False, "msg": "业务配置已保存；Wi-Fi 修改需要网关在线"},
                    status_code=400,
                )
            ret = mqtt_client.publish_cmd(
                row["mac"],
                "update_wifi",
                {"ssid": wifi_ssid, "password": next_wifi_password},
            )
            if not ret["ok"]:
                return JSONResponse(ret, status_code=503)
            conn.execute(
                "UPDATE gateway SET wifi_ssid=?, wifi_password=? WHERE id=?",
                (wifi_ssid, next_wifi_password, gid),
            )
            pushed.append("wifi")
    return JSONResponse(
        {
            "ok": True,
            "pushed": pushed,
            "msg": (
                "配置已保存；已下发的配置等待设备执行"
                if pushed
                else "配置已保存，网关上线后同步"
            ),
        }
    )


@router.post("/gateway/{gid}/rename")
async def rename_gateway(gid: GatewayId, name: str = Form("")):
    name = name.strip()
    if not valid_name(name):
        return JSONResponse({"ok": False, "msg": NAME_ERROR}, status_code=400)
    with mutation(("gateway", gid)) as conn:
        row = gateway_snapshot(conn, gid)
        if error := gateway_action_error(row, allow_offline=True):
            return error
        conn.execute("UPDATE gateway SET name=? WHERE id=?", (name, gid))
    if row["status"] != "online":
        return {"ok": True, "msg": "名称已保存，待网关上线后同步"}
    ret = mqtt_client.publish_cmd(row["mac"], "update_config", {"name": name})
    return {
        "ok": True,
        "msg": (
            "名称已保存，已下发等待设备执行"
            if ret["ok"]
            else "名称已保存，下发失败；网关重新连接后同步"
        ),
    }


# ---------- 删除网关 ----------
@router.post("/gateway/{gid}/delete/cancel")
async def cancel_delete_gateway(gid: GatewayId):
    with mutation(("gateway", gid)):
        result = cancel_gateway_deletion(gid)
    return JSONResponse(result, status_code=result.pop("status", 200))


@router.post("/gateway/{gid}/delete")
@router.post("/gateway/{gid}/unbind")
async def delete_gateway(gid: GatewayId, force_offline: bool = Form(False)):
    with mutation(("gateway", gid)):
        result = queue_gateway_deletion(gid, force_offline)
    status_code = result.pop("status", 200)
    return JSONResponse(result, status_code=status_code)


@router.post("/gateway/batch_unbind")
async def batch_unbind(
    ids: str = Form(...),
    force_offline: bool = Form(False),
    selection_state: str = Form(""),
    selection_states: str = Form(""),
):
    entries = list(
        dict.fromkeys(value.strip() for value in ids.split(",") if value.strip())
    )
    if not entries:
        return JSONResponse({"ok": False, "msg": "请先选择网关"}, status_code=400)
    expected_states = None
    if selection_states:
        try:
            expected_states = json.loads(selection_states)
            if not isinstance(expected_states, dict) or set(expected_states) != set(
                entries
            ):
                raise ValueError("Invalid selection snapshot")
        except (ValueError, TypeError):
            return JSONResponse(
                {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                status_code=409,
            )
    removed, queued, forced, skipped = [], [], [], []
    with mutation() as conn:
        resolved = []
        references = {}
        for entry in entries:
            kind, separator, value = entry.partition(":")
            if not separator:
                kind, value = "gateway", entry
            if kind in {"p", "pairing"}:
                if not value.isdigit():
                    return JSONResponse(
                        {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                        status_code=409,
                    )
                item_id = int(value)
                require_pairing_access(conn, item_id)
                check(conn, "pairing", item_id)
                record = conn.execute(
                    "SELECT expires_at FROM pairing_request WHERE id=? AND claimed_mac=''",
                    (item_id,),
                ).fetchone()
                state = (
                    (
                        "pairing",
                        "expired" if record["expires_at"] <= time.time() else "pending",
                    )
                    if record
                    else None
                )
            elif kind in {"g", "gateway"}:
                item_id = resolve_gateway_ref(value, conn)
                check(conn, "gateway", item_id)
                references[item_id] = value
                record = conn.execute(
                    "SELECT * FROM gateway WHERE id=?", (item_id,)
                ).fetchone()
                if record and record["delete_requested_at"]:
                    return JSONResponse(
                        {"ok": False, "msg": "网关正在等待删除，请先撤销删除"},
                        status_code=409,
                    )
                state = (
                    ("gateway", gateway_state(record), record["status"])
                    if record
                    else None
                )
            else:
                state = None
            if state is None:
                return JSONResponse(
                    {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                    status_code=409,
                )
            current_state = ":".join(state)
            if (
                expected_states is not None and expected_states[entry] != current_state
            ) or (selection_state and selection_state != current_state):
                return JSONResponse(
                    {"ok": False, "msg": "设备选择或状态已变化，请重新确认"},
                    status_code=409,
                )
            resolved.append((kind, item_id, entry))
        for kind, item_id, entry in resolved:
            if kind in {"p", "pairing"}:
                result = conn.execute(
                    "DELETE FROM pairing_request WHERE id=? AND claimed_mac=''",
                    (item_id,),
                )
                if result.rowcount:
                    removed.append(entry)
                else:
                    skipped.append({"id": entry, "reason": "配对记录不存在或已完成"})
                continue
            if kind not in {"g", "gateway"}:
                skipped.append({"id": entry, "reason": "条目类型无效"})
                continue
            row = conn.execute(
                "SELECT id,status,delete_requested_at FROM gateway WHERE id=?",
                (item_id,),
            ).fetchone()
            if not row:
                skipped.append({"id": entry, "reason": "网关不存在"})
            elif row["status"] == "unbound" or (
                row["status"] != "online" and force_offline
            ):
                remove_gateway(conn, item_id)
                (removed if row["status"] == "unbound" else forced).append(item_id)
            else:
                if not row["delete_requested_at"]:
                    conn.execute(
                        "UPDATE gateway SET delete_requested_at=?,delete_cmd_id='',delete_last_attempt='',delete_error='' WHERE id=?",
                        (now(), item_id),
                    )
                queued.append(item_id)
    for gateway_id in queued:
        dispatch_gateway_deletion(gateway_id)
    return JSONResponse(
        {
            "ok": True,
            "removed": [references.get(item, item) for item in removed],
            "queued": [references[item] for item in queued],
            "forced": [references[item] for item in forced],
            "skipped": skipped,
            "msg": f"配对记录删除 {len(removed)} 条，网关等待重置确认 {len(queued)} 台，强制删除 {len(forced)} 台"
            + (f"；{len(skipped)} 个未处理" if skipped else ""),
        }
    )


# ---------- 重新获取 bind_key（不修改） ----------
@router.get("/gateway/{gid}/key")
async def get_key(gid: GatewayId):
    with db() as conn:
        row = conn.execute(
            "SELECT mac, bind_key FROM gateway WHERE id=?", (gid,)
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False}, status_code=404)
        return JSONResponse(
            {"ok": True, "mac": fmt_mac(row["mac"]), "bind_key": row["bind_key"]}
        )


# app/routers/gateways.py
@router.get("/gateway/cmd_status/{cmd_id}")
async def get_cmd_status(cmd_id: str):
    from fastapi import HTTPException
    from ..access import require_gateway_access

    with db() as conn:
        row = conn.execute(
            "SELECT gateway_id FROM cmd_log WHERE cmd_id=?", (cmd_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "指令不存在")
        require_gateway_access(conn, row["gateway_id"])
    s = mqtt_client.get_cmd_status(cmd_id)
    return s
