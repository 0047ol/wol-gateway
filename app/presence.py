"""Accept positive evidence immediately; require spaced failures before going offline."""

from datetime import datetime

from .database import now


def _age(stamp, previous):
    if not previous:
        return None
    return (
        datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        - datetime.strptime(previous, "%Y-%m-%d %H:%M:%S")
    ).total_seconds()


def record_probe(conn, gateway, mac, status, stamp=None):
    if status not in ("online", "offline"):
        return
    stamp = stamp or now()
    device = conn.execute(
        "SELECT * FROM sub_device WHERE gateway_id=? AND mac=?", (gateway["id"], mac)
    ).fetchone()
    if not device:
        return
    if device["pc_service_bound"]:
        return
    if status == "online":
        conn.execute(
            """UPDATE sub_device SET status='online',last_report=?,last_online=?,
                        offline_since='',offline_last_check='',offline_checks=0 WHERE id=?""",
            (stamp, stamp, device["id"]),
        )
        return
    # Count distinct probe rounds, not duplicate entries or dashboard polling.
    elapsed = _age(stamp, device["offline_last_check"])
    distinct = elapsed is None or elapsed >= max(5, gateway["ping_interval"] / 2)
    checks = device["offline_checks"] + int(distinct)
    since = device["offline_since"] or stamp
    age = _age(stamp, device["last_online"] or since)
    offline = checks >= 3 and age >= max(90, gateway["ping_interval"] * 3)
    conn.execute(
        """UPDATE sub_device SET status=?,last_report=?,offline_since=?,
                    offline_last_check=?,offline_checks=? WHERE id=?""",
        (
            "offline" if offline else device["status"],
            stamp,
            since,
            stamp if distinct else device["offline_last_check"],
            checks,
            device["id"],
        ),
    )
