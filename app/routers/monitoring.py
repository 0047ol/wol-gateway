"""Management status and gateway-scoped diagnostic logs."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..background import reconcile_heartbeat_status
from ..gateway_refs import GatewayId, resolve_gateway_ref
from ..database import db, DatabaseError
from ..mqtt_client import mqtt_client
from ..utils import fmt_mac, paginate
from ..system_status import host_status, utc_now, server_load_percent
from ..presentation import gateway_counts
from ..device_status import gateway_snapshot, gateway_action_error, gateway_view
from ..presentation import gateway_can_manage, gateway_state, GATEWAY_LABELS
from ..table_sort import sort_options, sorted_rows
from ..access import actor, is_admin, scope_clause
from ..permissions import allowed

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_mac"] = fmt_mac
templates.env.filters["gateway_state"] = gateway_state
templates.env.globals["gateway_labels"] = GATEWAY_LABELS


@router.get("/api/status")
@router.get("/status", include_in_schema=False)
def system_status():
    # All signed-in dashboards use the sanitized operational status allowlist.
    diagnostics = host_status()
    counts = gateway_counts([])
    users_total = 0
    devices_total = 0
    try:
        with db() as conn:
            reconcile_heartbeat_status(conn)
            scope, args = scope_clause()
            rows = conn.execute(
                f"SELECT status,last_heartbeat,bind_message,delete_requested_at,delete_error FROM gateway WHERE {scope}",
                args,
            ).fetchall()
            pending_pairings = conn.execute(
                f"SELECT expires_at FROM pairing_request WHERE claimed_mac='' AND gateway_id IS NULL AND {scope}",
                args,
            ).fetchall()
            counts = gateway_counts(rows, pending_pairings)
            if actor() and not allowed(actor(), "gateway_view"):
                counts = gateway_counts([])
            device_scope, device_args = scope_clause("g.")
            devices_total = conn.execute(
                f"SELECT COUNT(*) FROM sub_device d JOIN gateway g ON g.id=d.gateway_id WHERE {device_scope}",
                device_args,
            ).fetchone()[0]
            if actor() and not allowed(actor(), "device_view"):
                devices_total = 0
            if is_admin():
                users_total = conn.execute(
                    "SELECT COUNT(*) FROM app_user WHERE role='user'"
                ).fetchone()[0]
    except DatabaseError as error:
        if diagnostics:
            diagnostics["database"].update(status="error", error=type(error).__name__)
        counts = gateway_counts([])
    connected = bool(mqtt_client.client.is_connected())
    mqtt = {
        "connected": connected,
        "uptime_seconds": diagnostics["services"]["mqtt_broker"].get("uptime_seconds"),
        "last_connected": mqtt_client.last_connect_ts,
        "last_disconnected": mqtt_client.last_disconnect_ts,
        "roundtrip": {"status": mqtt_client.health_probe()["status"]},
    }
    issues = []
    if not connected:
        issues.append("mqtt_disconnected")
    if mqtt["roundtrip"]["status"] in ("timeout", "error"):
        issues.append("mqtt_roundtrip_failed")
    if diagnostics["database"]["status"] != "ok":
        issues.append("database_error")
    for name, service in diagnostics["services"].items():
        if service["active"] is False and service["state"] != "not_installed":
            issues.append(name + "_inactive")
    nginx_probe = diagnostics["services"]["nginx"].get("http_probe", {})
    if nginx_probe.get("http_status", 0) >= 500:
        issues.append("nginx_http_error")
    for label, percent in (
        ("memory", (diagnostics["server"].get("memory") or {}).get("used_percent")),
        ("disk", (diagnostics["server"].get("disk") or {}).get("used_percent")),
    ):
        if percent is not None and percent >= 90:
            issues.append(label + "_usage_high")
    return {
        # Deliberate allowlist: never serialize the internal diagnostics object.
        "schema_version": 5,
        "generated_at": utc_now(),
        "health": {
            "status": "degraded" if issues else "ok",
            "issues": issues,
            "unknown_checks": [
                name
                for name, service in diagnostics["services"].items()
                if service["active"] is None
            ],
        },
        "mqtt": mqtt,
        "server": {"load_percent": server_load_percent(diagnostics["server"])},
        "backend": {
            "status": "ok" if diagnostics["database"]["status"] == "ok" else "degraded",
            "uptime_seconds": diagnostics["application"]["uptime_seconds"],
        },
        "database": {
            "engine": "MySQL" if settings.DB_BACKEND == "mysql" else "SQLite",
            "status": diagnostics["database"]["status"],
            "latency_ms": (
                diagnostics["database"].get("latency_ms")
                if diagnostics["database"]["status"] == "ok"
                else None
            ),
        },
        "http": {
            "nginx": {
                "active": diagnostics["services"]["nginx"]["active"],
                "http_probe": {
                    "status": nginx_probe.get("status", "unknown"),
                    "http_status": nginx_probe.get("http_status"),
                },
            },
        },
        "gateways": counts,
        **({"devices_total": devices_total} if not is_admin() else {}),
        **({"users_total": users_total} if is_admin() else {}),
    }


@router.get("/gateway/{gid}/logs", response_class=HTMLResponse)
async def gateway_logs(request: Request, gid: GatewayId):
    with db() as conn:
        gw = gateway_snapshot(conn, gid)
    return templates.TemplateResponse(
        request=request,
        name="gateway_logs.html",
        context={"gw": gw, "gateway_view": gateway_view(gw)},
    )


@router.get("/api/gateway/{gid}/logs")
async def logs(
    gid: GatewayId,
    messages_page: int = Query(1, ge=1),
    commands_page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
):
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        command_total = conn.execute(
            "SELECT COUNT(*) FROM cmd_log WHERE gateway_id=?", (gid,)
        ).fetchone()[0]
        commands_page, page_size, command_offset, command_pages = paginate(
            commands_page, page_size, command_total
        )
        commands = [
            {key: value for key, value in dict(row).items() if key != "gateway_id"}
            for row in conn.execute(
                "SELECT * FROM cmd_log WHERE gateway_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (gid, page_size, command_offset),
            )
        ]
        recent_commands = [
            {key: value for key, value in dict(row).items() if key != "gateway_id"}
            for row in conn.execute(
                "SELECT * FROM cmd_log WHERE gateway_id=? ORDER BY id DESC LIMIT 5",
                (gid,),
            )
        ]
    prefix = f"{settings.MQTT_TOPIC_PREFIX}/{gw['mac']}/"
    with mqtt_client._lock:
        all_messages = [
            dict(item)
            for item in mqtt_client.msg_log
            if item["topic"].startswith(prefix)
        ]
        message_total = len(all_messages)
        messages_page, page_size, message_offset, message_pages = paginate(
            messages_page, page_size, message_total
        )
        messages = all_messages[message_offset : message_offset + page_size]
        recent_messages = all_messages[:5]
    return {
        "messages": messages,
        "commands": commands,
        "recent_messages": recent_messages,
        "recent_commands": recent_commands,
        "messages_page": messages_page,
        "commands_page": commands_page,
        "messages_total": message_total,
        "commands_total": command_total,
        "messages_total_pages": message_pages,
        "commands_total_pages": command_pages,
        "page_size": page_size,
    }


@router.get("/api/gateway/{gid}/log_entries")
async def log_entries(
    gid: GatewayId,
    kind: str = "messages",
    before: int | None = Query(None, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    sort: str = "id",
    direction: str = "desc",
    offset: int = Query(0, ge=0),
):
    if kind not in {"messages", "commands"}:
        return JSONResponse({"ok": False, "msg": "日志类型无效"}, status_code=400)
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if kind == "commands":
            total = conn.execute(
                "SELECT COUNT(*) FROM cmd_log WHERE gateway_id=?", (gid,)
            ).fetchone()[0]
            where, args = "gateway_id=?", [gid]
            if before is not None:
                where += " AND id<?"
                args.append(before)
            allowed = {
                "id",
                "cmd_type",
                "status",
                "payload",
                "created_at",
                "ack_at",
                "ack_msg",
            }
            sort, direction = sort_options(sort, direction, allowed)
            sql_direction = "ASC" if direction == "asc" else "DESC"
            sort_expression = sort if sort == "id" else f"{sort} COLLATE NOCASE"
            items = [
                {key: value for key, value in dict(row).items() if key != "gateway_id"}
                for row in conn.execute(
                    f"SELECT * FROM cmd_log WHERE {where} ORDER BY {sort_expression} {sql_direction},id {sql_direction} LIMIT ? OFFSET ?",
                    [*args, page_size + 1, offset],
                )
            ]
    if kind == "messages":
        prefix = f"{settings.MQTT_TOPIC_PREFIX}/{gw['mac']}/"
        with mqtt_client._lock:
            messages = [
                dict(item)
                for item in mqtt_client.msg_log
                if item["topic"].startswith(prefix)
            ]
        total = len(messages)
        sort, direction = sort_options(
            sort, direction, {"id", "dir", "ts", "topic", "payload"}
        )
        items = sorted_rows(
            [item for item in messages if before is None or item["id"] < before],
            sort,
            direction,
        )[offset : offset + page_size + 1]
    has_more = len(items) > page_size
    items = items[:page_size]
    return {
        "items": items,
        "can_clear": gateway_can_manage(gw),
        "total": total,
        "has_more": has_more,
        "next_cursor": items[-1]["id"] if items else None,
        "next_offset": offset + len(items),
        "sort": sort,
        "direction": direction,
    }


@router.post("/api/logs/clear")
async def clear_logs(gid: str | None = None, kind: str = "all"):
    from ..concurrency import verify_actor

    if kind not in {"all", "messages", "commands"}:
        return JSONResponse({"ok": False, "msg": "日志类型无效"}, status_code=400)
    # Active commands and their locks must survive clearing historical logs.
    with db() as conn:
        verify_actor(conn)
        prefix = None
        if gid is not None:
            gid = resolve_gateway_ref(gid, conn)
            gw = gateway_snapshot(conn, gid)
            if error := gateway_action_error(gw, allow_offline=True):
                return error
            prefix = f"{settings.MQTT_TOPIC_PREFIX}/{gw['mac']}/"
        else:
            scope, scope_args = scope_clause()
            for gateway in conn.execute(
                f"SELECT * FROM gateway WHERE {scope}", scope_args
            ).fetchall():
                if error := gateway_action_error(gateway, allow_offline=True):
                    return error
        removed_commands = []
        if kind in {"all", "commands"}:
            with mqtt_client._lock:
                active = set(mqtt_client.pending_acks)
                sql = "SELECT id,cmd_id FROM cmd_log"
                args = ()
                if gid is not None:
                    sql += " WHERE gateway_id=?"
                    args = (gid,)
                else:
                    gateway_ids = [
                        row["id"]
                        for row in conn.execute(
                            f"SELECT id FROM gateway WHERE {scope}", scope_args
                        )
                    ]
                    if not gateway_ids:
                        sql += " WHERE 0"
                    else:
                        sql += (
                            " WHERE gateway_id IN ("
                            + ",".join("?" for _ in gateway_ids)
                            + ")"
                        )
                        args = tuple(gateway_ids)
                rows = conn.execute(sql, args).fetchall()
                removed_commands = [row for row in rows if row["cmd_id"] not in active]
                conn.executemany(
                    "DELETE FROM cmd_log WHERE id=?",
                    [(row["id"],) for row in removed_commands],
                )
                for row in removed_commands:
                    mqtt_client.cmd_log.pop(row["cmd_id"], None)
        removed_messages = 0
        if kind in {"all", "messages"}:
            scoped_macs = set()
            if not prefix:
                scoped_macs = {
                    row["mac"]
                    for row in conn.execute(
                        f"SELECT mac FROM gateway WHERE {scope}", scope_args
                    )
                }
            with mqtt_client._lock:
                kept = [
                    item
                    for item in mqtt_client.msg_log
                    if (prefix and not item["topic"].startswith(prefix))
                    or (
                        not prefix
                        and (
                            len(item["topic"].split("/")) < 3
                            or item["topic"].split("/")[-2] not in scoped_macs
                        )
                    )
                ]
                removed_messages = len(mqtt_client.msg_log) - len(kept)
                mqtt_client.msg_log.clear()
                mqtt_client.msg_log.extend(kept)
    count = removed_messages if kind == "messages" else len(removed_commands)
    label = "条 MQTT 报文" if kind == "messages" else "条指令日志"
    return {"ok": True, "msg": f"已清理 {count} {label}；执行中的指令保留"}
