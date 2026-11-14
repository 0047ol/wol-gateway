"""局部刷新用的 JSON 接口。"""

import hashlib

from fastapi import APIRouter, Query

from ..background import reconcile_heartbeat_status
from ..gateway_refs import GatewayId, browser_gateway, browser_pairing
from ..database import db
from ..utils import paginate, device_search
from ..pairing import list_pairings
from ..presentation import (
    gateway_state,
    gateway_notice,
    gateway_can_cancel_delete,
    gateway_can_transfer,
    gateway_can_repair_pairing,
    gateway_counts,
    gateway_can_manage,
    normalize_status_filter,
    matches_status_filter,
)
from ..device_status import gateway_snapshot, gateway_view
from ..device_catalog import catalog, with_device_counts
from ..table_sort import sort_options, sorted_rows
from ..access import scope_clause, is_admin

router = APIRouter()


@router.get("/api/gateways")
async def api_gateways(owner: str = "all"):
    scope, scope_args = scope_clause(owner=owner)
    with db() as conn:
        reconcile_heartbeat_status(conn)
        rows = conn.execute(
            f"SELECT id, public_id, owner_user_id, name, edit_revision, mac, ip, status, last_heartbeat, bind_message, delete_requested_at, delete_cmd_id, delete_last_attempt, delete_error, wifi_ssid FROM gateway WHERE {scope} ORDER BY id DESC",
            scope_args,
        ).fetchall()
        rows = with_device_counts(conn, rows)
        pairing_total = conn.execute(
            f"SELECT COUNT(*) FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NULL AND {scope}",
            scope_args,
        ).fetchone()[0]
        tickets = {
            row["gateway_id"]: dict(row)
            for row in conn.execute(
                f"SELECT id,gateway_id,expires_at,purpose FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NOT NULL AND gateway_id IN (SELECT id FROM gateway WHERE {scope}) ORDER BY id",
                scope_args,
            )
        }
        owners_revision = (
            hashlib.sha256(
                str(
                    [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT public_id,username,enabled FROM app_user WHERE role='user' ORDER BY id"
                        )
                    ]
                ).encode()
            ).hexdigest()[:16]
            if is_admin()
            else ""
        )
    return {
        "gateways": [
            {
                **browser_gateway(r),
                "pairing": browser_pairing(tickets.get(r["id"])),
                "display_status": gateway_state(r),
                "notice": gateway_notice(r),
                "can_cancel_delete": gateway_can_cancel_delete(r),
                "can_transfer": gateway_can_transfer(r),
                "can_repair_pairing": gateway_can_repair_pairing(r),
                "can_manage": gateway_can_manage(r),
            }
            for r in rows
        ],
        "pairings": list_pairings(owner),
        "online_total": gateway_counts(rows)["online"],
        "all_total": len(rows) + pairing_total,
        "owners_revision": owners_revision,
    }


@router.get("/api/gateways/search")
async def api_search_gateways(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = "id",
    direction: str = "desc",
    owner: str = "all",
    status: str = "all",
):
    where, args = scope_clause(owner=owner)
    q = q.strip()
    if q:
        clause, search_args = device_search(q)
        where += " AND " + clause
        args.extend(search_args)
    columns = "id, public_id, owner_user_id, edit_revision, mac, name, ip, status, last_heartbeat, bind_message, delete_requested_at, delete_error, ping_interval, heartbeat_timeout, heartbeat_send_interval, wol_broadcast_addr, scan_cidr, scan_concurrency, wifi_ssid"
    with db() as conn:
        reconcile_heartbeat_status(conn)
        sort, direction = sort_options(
            sort, direction, {"id", "name", "ip", "mac", "last_heartbeat"}
        )
        rows = sorted_rows(
            [
                dict(row)
                for row in conn.execute(
                    f"SELECT {columns} FROM gateway WHERE {where}",
                    args,
                )
                if matches_status_filter(
                    gateway_state(row), normalize_status_filter(status)
                )
            ],
            sort,
            direction,
        )
        total = len(rows)
        page, page_size, offset, total_pages = paginate(page, page_size, total)
        rows = rows[offset : offset + page_size]
        rows = with_device_counts(conn, rows)
    return {
        "items": [browser_gateway(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/api/gateway/{gid}/availability")
async def api_gateway_availability(gid: GatewayId):
    with db() as conn:
        return gateway_view(gateway_snapshot(conn, gid))


@router.get("/api/gateway/{gid}/devices")
async def api_devices(gid: GatewayId):
    with db() as conn:
        gw = gateway_snapshot(conn, gid)
        return catalog(conn, gw, page_size=100)["items"]


@router.get("/api/gateway/{gid}/device_results")
async def api_device_results(
    gid: GatewayId,
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = "id",
    direction: str = "desc",
    status: str = "all",
):
    with db() as conn:
        gw = gateway_snapshot(conn, gid)
        return {
            **catalog(conn, gw, q, page, page_size, sort, direction, status=status),
            "gateway": gateway_view(gw),
        }


@router.get("/api/gateway/{gid}/device_status")
async def api_device_status(
    gid: GatewayId, ids: str = "", q: str = "", status: str = "all"
):
    from ..batch_selection import device_ids

    with db() as conn:
        gw = gateway_snapshot(conn, gid)
        selected = set(device_ids(ids)) if ids else set()
        return {
            **catalog(conn, gw, q, ids=selected, status=status),
            "gateway": gateway_view(gw),
        }


@router.get("/api/gateway/{gid}/scan_results")
async def api_scan_results(
    gid: GatewayId,
    q: str = "",
    cmd_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
):
    discovered_where = "sr.gateway_id=?"
    base_args = [gid]
    if cmd_id is not None:
        discovered_where += " AND sr.scan_cmd_id=?"
        base_args.append(cmd_id)
    base_where = (
        discovered_where
        + " AND NOT EXISTS (SELECT 1 FROM sub_device sd WHERE sd.gateway_id=sr.gateway_id AND replace(replace(replace(upper(sd.mac), ':', ''), '-', ''), '.', '')=replace(replace(replace(upper(sr.mac), ':', ''), '-', ''), '.', ''))"
    )
    where, args = base_where, list(base_args)
    q = q.strip()
    if q:
        clause, search_args = device_search(q, "sr.device_name", "sr.mac", "sr.ip")
        where += " AND " + clause
        args.extend(search_args)
    with db() as conn:
        availability = gateway_view(gateway_snapshot(conn, gid))
        discovered_total = conn.execute(
            f"SELECT COUNT(*) FROM scan_result sr WHERE {discovered_where}", base_args
        ).fetchone()[0]
        unfiltered_total = conn.execute(
            f"SELECT COUNT(*) FROM scan_result sr WHERE {base_where}", base_args
        ).fetchone()[0]
        total = conn.execute(
            f"SELECT COUNT(*) FROM scan_result sr WHERE {where}", args
        ).fetchone()[0]
        page = max(1, page)
        page_size = max(1, min(page_size, 500))
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT sr.id, sr.ip, sr.mac, sr.created_at, sr.device_name, sr.model, sr.os, sr.info_source, sr.scan_cmd_id
               FROM scan_result sr WHERE {where} ORDER BY sr.id DESC LIMIT ? OFFSET ?""",
            [*args, page_size, offset],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "unfiltered_total": unfiltered_total,
        "total_pages": total_pages,
        "discovered_total": discovered_total,
        "already_added_total": discovered_total - unfiltered_total,
        "gateway": availability,
    }
