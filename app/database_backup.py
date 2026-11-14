"""Consistent administrative backups without putting credentials in argv."""

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def backup_database(directory, environment=None):
    env = dict(os.environ if environment is None else environment)
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if env.get("DB_BACKEND", "sqlite") != "mysql":
        source = Path(env.get("DB_PATH", "wol.db")).resolve(strict=True)
        target = folder / "database.sqlite3"
        try:
            with sqlite3.connect(
                source.as_uri() + "?mode=ro", uri=True
            ) as original, sqlite3.connect(target) as saved:
                original.backup(saved)
                assert saved.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            if target.exists():
                target.chmod(0o600)
        return target
    options = json.loads(Path(env["MYSQL_CONFIG_FILE"]).read_text(encoding="utf-8"))

    def quoted(value):
        return (
            '"'
            + str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            + '"'
        )

    target = folder / "database.sql"
    fd, filename = tempfile.mkstemp(prefix=".mysql-", dir=folder)
    try:
        os.chmod(filename, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("[client]\n")
            for key in ("host", "port", "user", "password"):
                if key in options:
                    f.write(key + "=" + quoted(options[key]) + "\n")
            if options.get("unix_socket"):
                f.write("socket=" + quoted(options["unix_socket"]) + "\n")
        with target.open("xb") as output:
            target.chmod(0o600)
            result = subprocess.run(
                [
                    "mysqldump",
                    "--defaults-extra-file=" + filename,
                    "--single-transaction",
                    "--quick",
                    "--skip-lock-tables",
                    "--default-character-set=utf8mb4",
                    "--databases",
                    options["database"],
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=300,
            )
        if result.returncode:
            raise RuntimeError("MySQL backup failed; exit " + str(result.returncode))
        if target.stat().st_size == 0:
            raise RuntimeError("MySQL backup is empty")
        return target
    finally:
        Path(filename).unlink(missing_ok=True)
