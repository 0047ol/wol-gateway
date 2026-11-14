"""Bounded, cached host diagnostics for the authenticated management API."""

import copy
import http.client
import importlib.metadata
import os
import platform
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .database import db, DatabaseError

_started = time.monotonic()
_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
_cache = {}
_lock = threading.Lock()
_cpu_previous = None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cached(name, ttl, collect):
    with _lock:
        previous = _cache.get(name)
        if previous and time.monotonic() - previous[0] < ttl:
            return copy.deepcopy(previous[1])
        result = collect()
        _cache[name] = (time.monotonic(), result)
        return copy.deepcopy(result)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _command(arguments):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=2, check=False
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return None, "probe_timeout"
    except OSError:
        return None, "probe_unavailable"


def _versions():
    packages = {}
    for name in ("fastapi", "uvicorn", "paho-mqtt", "jinja2"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    try:
        distribution = platform.freedesktop_os_release()
    except OSError:
        distribution = {}
    # The server package includes the versioned template as its release identity.
    import re

    template = _read(Path(__file__).parent / "templates/base.html") or ""
    version = re.search(r"v(\d+\.\d+\.\d+)", template)
    build = re.search(r"app\.css\?v=([\d-]+)", template)
    return {
        "application": {
            "name": "WOL Gateway",
            "version": version.group(1) if version else None,
            "build": build.group(1) if build else None,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "packages": packages,
            "pid": os.getpid(),
        },
        "os": {
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "distribution": distribution.get("NAME"),
            "distribution_id": distribution.get("ID"),
            "distribution_version": distribution.get("VERSION_ID"),
            "pretty_name": distribution.get("PRETTY_NAME", platform.system()),
        },
    }


def _host_sample():
    global _cpu_previous
    result = {
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
        "cpu_percent": None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "uptime_seconds": None,
        "memory": None,
        "process_memory_bytes": None,
        "sampled_at": utc_now(),
        "sample_interval_seconds": 5,
    }
    uptime = _read("/proc/uptime")
    if uptime:
        result["uptime_seconds"] = int(float(uptime.split()[0]))
    stat = _read("/proc/stat")
    if stat:
        values = [int(value) for value in stat.splitlines()[0].split()[1:9]]
        total, idle = sum(values), values[3] + values[4]
        if _cpu_previous:
            delta = total - _cpu_previous[0]
            if delta > 0:
                result["cpu_percent"] = round(
                    max(0, min(100, 100 * (1 - (idle - _cpu_previous[1]) / delta))), 1
                )
        _cpu_previous = total, idle
    meminfo = _read("/proc/meminfo")
    if meminfo:
        memory = {
            line.split(":")[0]: int(line.split()[1]) * 1024
            for line in meminfo.splitlines()
        }
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable")
        result["memory"] = {
            "total_bytes": total,
            "available_bytes": available,
            "used_percent": (
                round((total - available) / total * 100, 1)
                if total and available is not None
                else None
            ),
            "swap_total_bytes": memory.get("SwapTotal"),
            "swap_free_bytes": memory.get("SwapFree"),
        }
    process = _read("/proc/self/status")
    if process:
        for line in process.splitlines():
            if line.startswith("VmRSS:"):
                result["process_memory_bytes"] = int(line.split()[1]) * 1024
    try:
        folder = (
            Path(settings.DB_PATH).resolve().parent
            if settings.DB_BACKEND == "sqlite" and settings.DB_PATH != ":memory:"
            else Path.cwd()
        )
        usage = shutil.disk_usage(folder)
        result["disk"] = {
            "scope": "database_filesystem",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": (
                round(usage.used / usage.total * 100, 1) if usage.total else None
            ),
        }
    except OSError:
        result["disk"] = None
    return result


def server_load_percent(server):
    """Normalize the one-minute runnable/I/O load by logical CPU count."""
    import math

    averages, cores = server.get("load_average"), server.get("cpu_count")
    if not isinstance(averages, (list, tuple)) or not averages:
        return None
    load = averages[0]
    if (
        type(load) not in (int, float)
        or not math.isfinite(load)
        or load < 0
        or type(cores) is not int
        or cores <= 0
    ):
        return None
    percent = load / cores * 100
    return round(percent, 1) if math.isfinite(percent) else None


def _services():
    names = {
        "application": "wol-dashboard",
        "enrollment": "wol-enrollment",
        "mqtt_broker": "mosquitto",
        "nginx": "nginx",
    }
    result = {
        key: {
            "unit": name,
            "state": "unknown",
            "active": None,
            "started_monotonic_us": None,
        }
        for key, name in names.items()
    }
    executable = shutil.which("systemctl")
    if executable and platform.system() == "Linux":
        code, output = _command(
            [
                executable,
                "show",
                *[name + ".service" for name in names.values()],
                "--property=Id,LoadState,ActiveState,SubState,ExecMainStatus,ExecMainStartTimestampMonotonic",
                "--no-pager",
            ]
        )
        if code == 0:
            by_unit = {name + ".service": key for key, name in names.items()}
            for section in output.split("\n\n"):
                values = dict(
                    line.split("=", 1) for line in section.splitlines() if "=" in line
                )
                key = by_unit.get(values.get("Id"))
                if key:
                    state = values.get("ActiveState", "unknown")
                    try:
                        started = int(
                            values.get("ExecMainStartTimestampMonotonic", "0")
                        )
                    except ValueError:
                        started = 0
                    result[key].update(
                        {
                            "state": (
                                "not_installed"
                                if values.get("LoadState") == "not-found"
                                else state
                            ),
                            "active": state == "active",
                            "load_state": values.get("LoadState"),
                            "sub_state": values.get("SubState"),
                            "exit_code": values.get("ExecMainStatus"),
                            "started_monotonic_us": started if started > 0 else None,
                        }
                    )
        else:
            for service in result.values():
                service["reason"] = (
                    output if code is None else "service_manager_unavailable"
                )
    else:
        for service in result.values():
            service["reason"] = "systemd_unavailable"
    nginx = shutil.which("nginx")
    if not nginx and Path("/www/server/nginx/sbin/nginx").is_file():
        nginx = "/www/server/nginx/sbin/nginx"
    result["nginx"]["version"] = None
    if nginx:
        code, output = _command([nginx, "-v"])
        if code == 0:
            result["nginx"]["version"] = output.removeprefix("nginx version: ")[:120]
    # A local HTTP response verifies the listener separately from systemd state.
    probe = {"scope": "loopback_default_http", "port": 80, "checked_at": utc_now()}
    connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=1)
    try:
        started = time.monotonic()
        connection.request("HEAD", "/")
        response = connection.getresponse()
        probe.update(
            status="responding",
            http_status=response.status,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except (OSError, http.client.HTTPException) as error:
        probe.update(status="unavailable", error=type(error).__name__)
    finally:
        connection.close()
    result["nginx"]["http_probe"] = probe
    return {"items": result, "checked_at": utc_now(), "cache_seconds": 30}


def _integrity():
    try:
        with db() as conn:
            if settings.DB_BACKEND == "mysql":
                ok = conn.execute("SELECT 1").fetchone()[0] == 1
                return {
                    "status": "ok" if ok else "error",
                    "checked_at": utc_now(),
                    "cache_seconds": 60,
                    "check": "connectivity",
                }
            rows = conn.execute("PRAGMA quick_check(1)").fetchall()
        return {
            "status": "ok" if len(rows) == 1 and rows[0][0] == "ok" else "error",
            "checked_at": utc_now(),
            "cache_seconds": 60,
        }
    except DatabaseError as error:
        return {
            "status": "error",
            "error": type(error).__name__,
            "checked_at": utc_now(),
            "cache_seconds": 60,
        }


def database_status():
    started = time.monotonic()
    if settings.DB_BACKEND == "mysql":
        result = {"engine": "MySQL", "storage": "server", "status": "error"}
        try:
            with db() as conn:
                result["version"] = conn.execute("SELECT VERSION()").fetchone()[0]
                result["devices"] = conn.execute(
                    "SELECT COUNT(*) FROM sub_device"
                ).fetchone()[0]
                result["pending_commands"] = conn.execute(
                    "SELECT COUNT(*) FROM cmd_log WHERE status='sent'"
                ).fetchone()[0]
            result.update(
                status="ok", latency_ms=round((time.monotonic() - started) * 1000, 2)
            )
        except DatabaseError as error:
            result["error"] = type(error).__name__
        return result
    result = {
        "engine": "SQLite",
        "version": sqlite3.sqlite_version,
        "status": "error",
        "storage": "memory" if settings.DB_PATH == ":memory:" else "file",
    }
    try:
        with db() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            for name in ("journal_mode", "page_count", "page_size", "freelist_count"):
                result[name] = conn.execute("PRAGMA " + name).fetchone()[0]
            result["devices"] = conn.execute(
                "SELECT COUNT(*) FROM sub_device"
            ).fetchone()[0]
            result["pending_commands"] = conn.execute(
                "SELECT COUNT(*) FROM cmd_log WHERE status='sent'"
            ).fetchone()[0]
        result["status"] = "ok"
        result["logical_size_bytes"] = result["page_count"] * result["page_size"]
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    except DatabaseError as error:
        result["error"] = type(error).__name__
    result["integrity"] = _cached("integrity", 60, _integrity)
    if result["integrity"]["status"] != "ok":
        result["status"] = "error"
    if settings.DB_PATH != ":memory:":
        for suffix, key in (
            ("", "file_size_bytes"),
            ("-wal", "wal_size_bytes"),
            ("-shm", "shared_memory_bytes"),
        ):
            try:
                result[key] = Path(str(settings.DB_PATH) + suffix).stat().st_size
            except FileNotFoundError:
                result[key] = 0
            except OSError:
                result[key] = None
    return result


def host_status():
    result = _cached("versions", 3600, _versions)
    result["application"].update(
        {
            "started_at": _started_at,
            "uptime_seconds": int(time.monotonic() - _started),
            "http_port": settings.PORT,
            "debug": settings.DEBUG,
        }
    )
    result["os"]["timezone"] = datetime.now().astimezone().tzname()
    result["os"]["utc_offset"] = datetime.now().astimezone().strftime("%z")
    result["server"] = _cached("host", 5, _host_sample)
    service_data = _cached("services", 30, _services)
    result["services"] = service_data["items"]
    clock = time.monotonic()
    for service in result["services"].values():
        started = service.pop("started_monotonic_us", None)
        service["uptime_seconds"] = (
            int(clock - started / 1_000_000)
            if service["active"] is True
            and type(started) is int
            and 0 < started <= clock * 1_000_000
            else None
        )
    result["services_checked_at"] = service_data["checked_at"]
    result["services_cache_seconds"] = service_data["cache_seconds"]
    result["database"] = database_status()
    return result
