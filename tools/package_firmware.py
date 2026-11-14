"""Package a built ESP32-C3 image with a script that preserves NVS."""

import hashlib
import json
import re
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--signing-key", type=Path, default=root / ".local/ota-signing-key.pem"
)
parser.add_argument(
    "--build-directory", type=Path, default=root / "firmware/build-idf61"
)
args = parser.parse_args()
version = re.search(
    r"project\(wol_gateway VERSION ([\d.]+)\)",
    (root / "firmware/CMakeLists.txt").read_text(encoding="utf-8"),
).group(1)
build = args.build_directory.resolve()
description = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
if description["project_version"] != version or description["target"] != "esp32c3":
    raise RuntimeError("Build version/target does not match the firmware source")
idf_version = description["git_revision"]
files = {
    name: (build / path).read_bytes()
    for name, path in {
        "bootloader.bin": "bootloader/bootloader.bin",
        "partition-table.bin": "partition_table/partition-table.bin",
        "wol_gateway.bin": "wol_gateway.bin",
        "ota_data_initial.bin": "ota_data_initial.bin",
    }.items()
}
assert len(files["wol_gateway.bin"]) <= 0x1F0000
flasher = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
assert (
    flasher["app"]["offset"] == "0x20000" and flasher["otadata"]["offset"] == "0x10000"
)
assert files["ota_data_initial.bin"] == b"\xff" * 8192
private_key = serialization.load_pem_private_key(
    args.signing_key.read_bytes(), password=None
)
public_key = private_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
assert (
    public_key
    == (root / "app/ota_public_key.pem").read_bytes()
    == (root / "firmware/main/ota_public_key.pem").read_bytes()
)
files["ota-manifest.json"] = json.dumps(
    {
        "schema": 1,
        "chip": "esp32c3",
        "layout": "c3-4m-ab-v1",
        "version": version,
        "size": len(files["wol_gateway.bin"]),
        "sha256": hashlib.sha256(files["wol_gateway.bin"]).hexdigest(),
    },
    separators=(",", ":"),
    sort_keys=True,
).encode()
files["ota-signature.hex"] = (
    private_key.sign(files["ota-manifest.json"], ec.ECDSA(hashes.SHA256()))
    .hex()
    .encode()
)
script_name = f"Flash-{version.replace('.', '')}.ps1"
files[script_name] = """param([Parameter(Mandatory=$true)][string]$Port)
$ErrorActionPreference = 'Stop'
# Run in an ESP-IDF terminal. Existing NVS is preserved.
Push-Location $PSScriptRoot
try {
    python -m esptool --chip esp32c3 --port $Port --baud 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 4MB --flash_freq 80m 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 ota_data_initial.bin 0x20000 wol_gateway.bin
    if ($LASTEXITCODE -ne 0) { throw 'Firmware flash failed' }
} finally { Pop-Location }
""".encode()
files["README.md"] = f"""# ESP32-C3 {version}

Built with ESP-IDF {idf_version} for 4 MB flash. Back up your board before flashing.
Run ./{script_name} -Port COMx in an ESP-IDF terminal after identifying its MAC.
The script preserves Wi-Fi, binding and LAN device configuration in NVS; it does not erase flash.
This release migrates the old factory layout to two OTA slots. Keep power connected during this first USB upgrade.
Upload this complete ZIP in the gateway editor for subsequent remote OTA upgrades. Only the signed application is downloaded; bootloader, partition table and NVS are not remotely overwritten.
OTA authenticity is checked using an embedded ECDSA public key; hardware Secure Boot and eFuse anti-rollback are not enabled.

The gateway uses its ESP32-C3 radio for Wi-Fi and LAN discovery. Bluetooth monitoring has been removed.
Devices with a linked Windows service use that service for online status; the gateway excludes them from periodic LAN probes and retains Wake-on-LAN support.
""".encode()
files["sha256.json"] = json.dumps(
    {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, indent=2
).encode()
target = root / f"deploy/esp32c3-{version}.zip"
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name, data in files.items():
        archive.writestr(name, data)
with ZipFile(target) as archive:
    assert archive.testzip() is None
    for name, data in files.items():
        assert archive.read(name) == data
print(
    f"PASS {target}: {len(files)} files, app={len(files['wol_gateway.bin'])} B, SHA256={hashlib.sha256(target.read_bytes()).hexdigest()}"
)
