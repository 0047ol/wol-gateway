"""Build a deployable website source archive without project-specific endpoints."""

import hashlib
import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.releases import WEBSITE_FILES

HOST_REPLACEMENTS = {
    "YOUR_SERVER_HOST": "YOUR_SERVER_HOST",
    "/opt/wol-gateway": "/opt/wol-gateway",
    "/srv/www/wol-gateway": "/srv/www/wol-gateway",
    "/var/backups/wol-gateway": "/var/backups/wol-gateway",
    "@project-owner": "@project-owner",
}


def main():
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    version = re.search(r"v(\d+\.\d+\.\d+)", base).group(1)
    build = re.search(r"app\.css\?v=([\d-]+)", base).group(1)
    files = {}
    for name in WEBSITE_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Invalid website source path: {name}")
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(f"Non-UTF8 website source: {name}") from error
        for private_value, public_value in HOST_REPLACEMENTS.items():
            content = content.replace(private_value, public_value)
        raw = content.encode("utf-8")
        if re.search(rb"189\.1\.243\.201|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", raw):
            raise RuntimeError(f"Sensitive website content: {name}")
        files[name] = raw
    manifest = {
        "version": version,
        "build": build,
        "sha256": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(files.items())
        },
    }
    output = ROOT / "dist" / f"wol-website-{version}-public.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
        archive.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    with ZipFile(output) as archive:
        if archive.testzip():
            raise RuntimeError("Website source archive failed CRC validation")
        for name, data in files.items():
            if archive.read(name) != data:
                raise RuntimeError(f"Archive content mismatch: {name}")
    print(f"PASS: {output} ({len(files)} files; server endpoint replaced)")


if __name__ == "__main__":
    main()
