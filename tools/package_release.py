"""Build a public source archive from a deliberate allowlist."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import struct
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
HOST_REPLACEMENTS = {
    "YOUR_SERVER_HOST": "YOUR_SERVER_HOST",
    "/opt/wol-gateway": "/opt/wol-gateway",
    "/srv/www/wol-gateway": "/srv/www/wol-gateway",
    "/var/backups/wol-gateway": "/var/backups/wol-gateway",
    "@project-owner": "@project-owner",
}


def source_paths():
    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "LICENSE",
        ROOT / "NOTICE",
        ROOT / "CONTRIBUTING.md",
        ROOT / ".gitignore",
        ROOT / ".env.example",
        ROOT / "run.py",
        ROOT / "requirements.txt",
        ROOT / "requirements-dev.txt",
        ROOT / "deploy" / "README.md",
        ROOT / "deploy" / "setup.html",
        ROOT / "tests" / "deployment_configurator.test.cjs",
        ROOT / "tools" / "package_release.py",
        ROOT / "tools" / "package_website_public.py",
        ROOT / "tools" / "format_web.py",
        ROOT / "screenshots" / "gateway-overview-desktop.png",
        ROOT / "screenshots" / "device-list-mobile.png",
        ROOT / "screenshots" / "gateway-overview-desktop-en.png",
        ROOT / "screenshots" / "device-list-mobile-en.png",
    ]
    for base, extensions in (
        (ROOT / "app", {".py", ".js", ".css", ".html", ".pem"}),
        (ROOT / "firmware" / "main", {".c", ".h", ".html", ".txt"}),
        (ROOT / "windows-agent" / "native", {".cs", ".ps1", ".config", ".pem"}),
    ):
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in extensions:
                if any(part.lower() in {"build", "bin", "obj", "__pycache__"} for part in path.relative_to(base).parts):
                    continue
                paths.append(path)
    paths.extend(
        ROOT / name
        for name in (
            "firmware/CMakeLists.txt",
            "firmware/partitions.csv",
            "firmware/sdkconfig.defaults",
            "firmware/.gitignore",
            "windows-agent/native/Build.ps1",
        )
    )
    return sorted(set(paths))


def public_bytes(path):
    raw = path.read_bytes()
    if path.suffix.lower() == ".png":
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"Non-UTF8 source excluded: {path.relative_to(ROOT)}") from error
    for private_value, public_value in HOST_REPLACEMENTS.items():
        text = text.replace(private_value, public_value)
    return text.encode("utf-8")


def validate(name, raw):
    normalized = name.lower()
    forbidden_names = (".db", ".sqlite", ".sqlite3", ".exe", ".zip", ".log")
    if normalized.endswith(forbidden_names) or any(
        part in normalized for part in ("/.local/", "/build/", "/dist/", "/.venv/")
    ):
        raise RuntimeError(f"Forbidden release artifact: {name}")
    if normalized.endswith(".png"):
        if len(raw) > 3_000_000 or len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError(f"Invalid or oversized screenshot: {name}")
        width, height = struct.unpack(">II", raw[16:24])
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            raise RuntimeError(f"Invalid screenshot dimensions: {name}")
        return
    data = raw.decode("utf-8", errors="replace")
    checks = (
        (r"189\.1\.243\.201", "production server address"),
        (r"/opt/wol-gateway|/srv/www/wol-gateway|/var/backups/wol-gateway", "production path"),
        (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
        (r"(?i)\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|gh[pousr]_[A-Za-z0-9_]{30,}", "credential"),
    )
    for pattern, label in checks:
        if re.search(pattern, data):
            raise RuntimeError(f"Release scan found {label} in {name}")
    if name.lower().endswith(".pem") and "PRIVATE KEY" in data:
        raise RuntimeError(f"Private key material found in {name}")


def main():
    files = {}
    for path in source_paths():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Invalid release source: {path}")
        name = path.relative_to(ROOT).as_posix()
        raw = public_bytes(path)
        validate(name, raw)
        files[name] = raw
    if not {
        "LICENSE",
        "README.md",
        "README.zh-CN.md",
        "deploy/setup.html",
        "deploy/README.md",
    } <= files.keys():
        raise RuntimeError("Public release documentation is incomplete")
    manifest = {name: sha256(data).hexdigest() for name, data in sorted(files.items())}
    version_text = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    version_match = re.search(r"v(\d+\.\d+\.\d+)", version_text)
    version = version_match.group(1) if version_match else "source"
    output = ROOT / "dist" / f"wol-gateway-source-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
        archive.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    with ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Corrupt archive member: {bad}")
        assert len(archive.namelist()) == len(files) + 1
    print(f"PASS: {output} ({len(files)} source files; private paths, production address, databases and binaries excluded)")


if __name__ == "__main__":
    main()
