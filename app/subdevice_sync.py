"""Keep gateway monitoring limited to devices without a WOL shutdown service."""

from .utils import norm_mac
from .mqtt_client import mqtt_client


def queue_subdevice_sync(conn, gateway_id):
    """The credential listener has no MQTT connection; persist delivery for the website."""
    import uuid

    conn.execute(
        "INSERT INTO runtime_state(name,value) VALUES (?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        (f"pc_unbind_sync:{gateway_id}", uuid.uuid4().hex),
    )


def drain_subdevice_sync():
    from fastapi import HTTPException
    from .database import atomic, db
    from .presentation import gateway_state
    from .ota import assert_not_busy

    delivered = 0
    with db() as conn:
        pending = conn.execute(
            "SELECT name,value FROM runtime_state WHERE name LIKE 'pc_unbind_sync:%'"
        ).fetchall()
    for item in pending:
        with db() as conn, atomic(conn):
            current = conn.execute(
                "SELECT value FROM runtime_state WHERE name=?", (item["name"],)
            ).fetchone()
            if not current or current[0] != item["value"]:
                continue
            gid = int(item["name"].split(":", 1)[1])
            gateway = conn.execute(
                "SELECT * FROM gateway WHERE id=?", (gid,)
            ).fetchone()
            if not gateway:
                conn.execute("DELETE FROM runtime_state WHERE name=?", (item["name"],))
                continue
            if gateway_state(gateway) != "online" or gateway["delete_requested_at"]:
                continue
            try:
                assert_not_busy(conn, gid)
            except HTTPException:
                continue
            if push_subdevices(conn, gid):
                conn.execute(
                    "DELETE FROM runtime_state WHERE name=? AND value=?",
                    (item["name"], item["value"]),
                )
                delivered += 1
    return delivered


def configured_devices(conn, gateway_id):
    return [
        {"name": row["name"], "mac": norm_mac(row["mac"]), "ip": row["ip"]}
        for row in conn.execute(
            "SELECT name,mac,ip FROM sub_device WHERE gateway_id=? AND pc_service_bound=0",
            (gateway_id,),
        )
    ]


def push_subdevices(conn, gateway_id):
    gateway = conn.execute(
        "SELECT mac,status FROM gateway WHERE id=?", (gateway_id,)
    ).fetchone()
    if not gateway or gateway["status"] != "online":
        return False
    result = mqtt_client.publish_cmd(
        gateway["mac"],
        "sync_sub_devices",
        {"sub_devices": configured_devices(conn, gateway_id)},
    )
    return result["ok"]
