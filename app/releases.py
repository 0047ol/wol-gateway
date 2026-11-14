"""Authenticated release versions and client download addresses."""

import re
import io
import json
import hashlib
from functools import lru_cache
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()
ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_VERSION = "0.8.7"
PC_VERSION = "1.3.2"

# Explicit runtime source list: stale or privately added server files are excluded.
WEBSITE_FILES = (
    "app/__init__.py",
    "app/access.py",
    "app/auth.py",
    "app/background.py",
    "app/batch_selection.py",
    "app/client_policy.py",
    "app/concurrency.py",
    "app/config.py",
    "app/database.py",
    "app/database_backup.py",
    "app/device_catalog.py",
    "app/device_icons.py",
    "app/device_logos.py",
    "app/device_status.py",
    "app/enrollment.py",
    "app/enrollment_app.py",
    "app/gateway_deletion.py",
    "app/gateway_refs.py",
    "app/hass_power.py",
    "app/main.py",
    "app/mqtt_accounts.py",
    "app/mqtt_client.py",
    "app/mqtt_handlers.py",
    "app/mysql_database.py",
    "app/mysql_schema.py",
    "app/names.py",
    "app/ota.py",
    "app/ota_public_key.pem",
    "app/pairing.py",
    "app/pc_maintenance.py",
    "app/pc_power.py",
    "app/permissions.py",
    "app/power.py",
    "app/power_operations.py",
    "app/presence.py",
    "app/presentation.py",
    "app/releases.py",
    "app/routers/__init__.py",
    "app/routers/accounts.py",
    "app/routers/api.py",
    "app/routers/devices.py",
    "app/routers/gateways.py",
    "app/routers/monitoring.py",
    "app/routers/ota.py",
    "app/routers/power.py",
    "app/routers/scan.py",
    "app/routers/shares.py",
    "app/share_secrets.py",
    "app/static/app.css",
    "app/static/app.js",
    "app/static/gateways.js",
    "app/static/i18n.js",
    "app/static/icons.js",
    "app/static/navigation.js",
    "app/static/permissions.js",
    "app/static/power.js",
    "app/static/sync.js",
    "app/static/tables.js",
    "app/static/tooltips.js",
    "app/static/users.js",
    "app/subdevice_sync.py",
    "app/system_status.py",
    "app/table_sort.py",
    "app/templates/_controls.html",
    "app/templates/_gateway_info.html",
    "app/templates/account_disabled.html",
    "app/templates/base.html",
    "app/templates/devices.html",
    "app/templates/gateway_logs.html",
    "app/templates/gateways.html",
    "app/templates/login.html",
    "app/templates/scan.html",
    "app/templates/setup.html",
    "app/templates/share.html",
    "app/templates/share_card.html",
    "app/templates/users.html",
    "app/utils.py",
    "requirements.txt",
    "run.py",
)


@lru_cache(maxsize=1)
def website_version():
    template = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    return re.search(r"v(\d+\.\d+\.\d+)", template).group(1)


@router.get("/api/releases")
async def release_details():
    version = website_version()
    return {
        "ok": True,
        "releases": [
            {"name": "网站", "version": version, "url": "/downloads/website.zip"},
            {
                "name": "固件",
                "version": FIRMWARE_VERSION,
                "url": f"/downloads/esp32c3-{FIRMWARE_VERSION}.zip",
            },
            {
                "name": "WOL关机服务",
                "version": PC_VERSION,
                "url": "/downloads/pc-service.exe",
            },
        ],
    }


@lru_cache(maxsize=1)
def website_archive(version):
    # Match the deployment's runtime source allowlist; never archive the project root.
    paths = [ROOT / name for name in WEBSITE_FILES]
    if any(
        path.is_symlink() or not path.resolve().is_relative_to(ROOT) for path in paths
    ):
        raise RuntimeError("Invalid website source path")
    data = {
        path.relative_to(ROOT).as_posix(): path.read_bytes() for path in sorted(paths)
    }
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, contents in data.items():
            archive.writestr(name, contents)
        archive.writestr(
            "release-manifest.json",
            json.dumps(
                {
                    "version": version,
                    "sha256": {
                        name: hashlib.sha256(contents).hexdigest()
                        for name, contents in data.items()
                    },
                },
                indent=2,
            ),
        )
    return output.getvalue()


@router.get("/downloads/website.zip")
def download_website():
    version = website_version()
    return Response(
        website_archive(version),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="wol-website-{version}.zip"',
            "Cache-Control": "private, no-store",
        },
    )
