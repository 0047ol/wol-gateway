"""LAN devices and their authoritative WOL shutdown service presence."""

from .device_status import device_snapshot
from .device_icons import device_icon
from .pc_power import pc_registry
from .utils import device_search, paginate
from .table_sort import sort_options, sorted_rows
from .presentation import normalize_status_filter, matches_status_filter


def with_device_counts(conn, gateways):
    """Count current evidence in one query for already authorized gateways."""
    rows = [dict(gateway) for gateway in gateways]
    by_id = {gateway["id"]: gateway for gateway in rows}
    for gateway in rows:
        gateway.update(
            device_count=0,
            device_online_count=0,
            device_offline_count=0,
            device_unknown_count=0,
        )
    if by_id:
        placeholders = ",".join("?" for _ in by_id)
        for device in conn.execute(
            f"SELECT * FROM sub_device WHERE gateway_id IN ({placeholders})",
            list(by_id),
        ):
            gateway = by_id[device["gateway_id"]]
            state = device_snapshot(device, gateway)["status"]
            key = (
                f"device_{state}_count"
                if state in {"online", "offline"}
                else "device_unknown_count"
            )
            gateway["device_count"] += 1
            gateway[key] += 1
    return rows


def catalog(
    conn,
    gateway,
    q="",
    page=1,
    page_size=20,
    sort="id",
    direction="desc",
    *,
    include_items=True,
    ids=None,
    status="all",
):
    gid = gateway["id"]
    devices = []
    for row in conn.execute(
        """SELECT d.*, EXISTS(SELECT 1 FROM device_share s WHERE s.device_id=d.id) AS share_exists,
           (SELECT edit_revision FROM device_share s WHERE s.device_id=d.id) AS share_revision
           FROM sub_device d WHERE gateway_id=? ORDER BY id DESC""",
        (gid,),
    ):
        devices.append(
            {
                **device_snapshot(row, gateway),
                "kind": "lan",
                "display_id": str(row["id"]),
            }
        )
    all_rows = devices
    status = normalize_status_filter(status, unknown=True)
    selected = [row for row in all_rows if matches_status_filter(row["status"], status)]
    q = q.strip()
    if q:
        search_clause, search_args = device_search(q)
        allowed = {
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM sub_device WHERE gateway_id=? AND {search_clause}",
                [gid, *search_args],
            )
        }
        selected = [row for row in selected if str(row["id"]) in allowed]
    sort, direction = sort_options(
        sort, direction, {"id", "name", "ip", "mac", "last_report"}
    )
    selected = sorted_rows(selected, sort, direction)
    page, page_size, offset, pages = paginate(page, page_size, len(selected))
    items = []
    if include_items:
        visible = (
            [row for row in selected if row["id"] in ids]
            if ids is not None
            else selected[offset : offset + page_size]
        )
        for row in visible:
            service_detected = bool(pc_registry.get(row["mac"]))
            items.append(
                {
                    **row,
                    **device_icon(row, service_detected=service_detected),
                    "service_detected": service_detected,
                }
            )
            items[-1].pop("gateway_id", None)
            items[-1]["revision"] = items[-1].pop("edit_revision")
    return {
        "items": items,
        "total": len(selected),
        "all_total": len(all_rows),
        "online_total": sum(row["status"] == "online" for row in all_rows),
        "unknown_total": sum(row["status"] == "unknown" for row in all_rows),
        "page": page,
        "page_size": page_size,
        "total_pages": pages,
        "sort": sort,
        "direction": direction,
        "status_filter": status,
    }
