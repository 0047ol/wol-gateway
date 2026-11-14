"""Database access; MySQL in production, SQLite for local/offline fixtures."""

import sqlite3
import threading
import uuid
import pymysql
from contextlib import contextmanager
from datetime import datetime

from .config import settings

_lock = threading.RLock()
DatabaseError = (sqlite3.Error, pymysql.Error)
IntegrityError = (sqlite3.IntegrityError, pymysql.IntegrityError)


def _conn():
    if settings.DB_BACKEND == "mysql":
        from .mysql_database import MySQLConnection

        return MySQLConnection.from_file(settings.MYSQL_CONFIG_FILE)
    if settings.DB_BACKEND != "sqlite":
        raise ValueError("Unsupported database backend")
    conn = sqlite3.connect(
        settings.DB_PATH, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


# 全局共享连接（FastAPI 多线程 + MQTT 回调线程都会用）
_db = _conn()


@contextmanager
def db():
    with _lock:
        if settings.DB_BACKEND == "mysql":
            _db.ensure_connection()
        yield _db


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def atomic(conn):
    """Rollback the whole multi-row operation, including audit records."""
    nested = conn.in_transaction
    name = "write_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        else:
            conn.rollback()
        raise
    else:
        conn.execute(f"RELEASE {name}") if nested else conn.commit()


def _init_gateway_references(conn):
    # Website and enrollment may start together against the same database.
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(gateway)")}
        if "public_id" not in columns:
            conn.execute(
                "ALTER TABLE gateway ADD COLUMN public_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE gateway SET public_id='g_' || lower(hex(randomblob(16))) WHERE public_id=''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_public_id ON gateway(public_id) WHERE public_id!=''"
        )
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS gateway_assign_public_id
                AFTER INSERT ON gateway WHEN NEW.public_id=''
            BEGIN
                UPDATE gateway SET public_id='g_' || lower(hex(randomblob(16)))
                WHERE id=NEW.id;
            END;
        """)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db():
    with _lock:
        if settings.DB_BACKEND == "mysql":
            from .mysql_database import init_mysql

            init_mysql(_db)
            return
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateway_scan_lease (
                gateway_id INTEGER PRIMARY KEY REFERENCES gateway(id) ON DELETE CASCADE,
                cmd_id TEXT NOT NULL DEFAULT '',
                owner_session TEXT NOT NULL,
                cooldown_until REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateway_ota (
                gateway_id INTEGER PRIMARY KEY REFERENCES gateway(id) ON DELETE CASCADE,
                supported INTEGER NOT NULL DEFAULT 0,
                layout TEXT NOT NULL DEFAULT '',
                running_version TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'idle',
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                target_version TEXT NOT NULL DEFAULT '',
                image_sha256 TEXT NOT NULL DEFAULT '',
                image_size INTEGER NOT NULL DEFAULT 0,
                token_hash TEXT NOT NULL DEFAULT '',
                expected_mac TEXT NOT NULL DEFAULT '',
                binding_hash TEXT NOT NULL DEFAULT '',
                owner_user_id INTEGER,
                deadline REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                last_success_at REAL NOT NULL DEFAULT 0,
                auto_image_sha256 TEXT NOT NULL DEFAULT '',
                auto_task_id TEXT NOT NULL DEFAULT '',
                auto_attempts INTEGER NOT NULL DEFAULT 0,
                auto_retry_at REAL NOT NULL DEFAULT 0,
                cmd_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS gateway (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT UNIQUE NOT NULL,
                bind_key TEXT NOT NULL,
                name TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                netmask TEXT DEFAULT '',
                gateway_ip TEXT DEFAULT '',
                scan_cidr TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unbound',
                ping_interval INTEGER NOT NULL DEFAULT 30,
                heartbeat_timeout INTEGER NOT NULL DEFAULT 30,
                heartbeat_send_interval INTEGER NOT NULL DEFAULT 15,
                wol_broadcast_addr TEXT NOT NULL DEFAULT '255.255.255.255',
                scan_concurrency INTEGER NOT NULL DEFAULT 8,
                wifi_ssid TEXT DEFAULT '',
                wifi_password TEXT DEFAULT '',
                last_heartbeat TEXT DEFAULT '',
                bind_message TEXT DEFAULT '',
                delete_requested_at TEXT NOT NULL DEFAULT '',
                delete_cmd_id TEXT NOT NULL DEFAULT '',
                delete_last_attempt TEXT NOT NULL DEFAULT '',
                delete_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sub_device (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mac TEXT NOT NULL,
                ip TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'offline',
                last_report TEXT DEFAULT '',
                FOREIGN KEY (gateway_id) REFERENCES gateway(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scan_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                mac TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (gateway_id) REFERENCES gateway(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cmd_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                cmd_type TEXT NOT NULL,
                payload TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                ack_msg TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                ack_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS pairing_request (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                gateway_id INTEGER,
                purpose TEXT NOT NULL DEFAULT 'recovery',
                claimed_mac TEXT NOT NULL DEFAULT '',
                claimed_at INTEGER,
                FOREIGN KEY (gateway_id) REFERENCES gateway(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS power_agent (
                device_id INTEGER PRIMARY KEY REFERENCES sub_device(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                last_seen REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS power_command (
                id TEXT PRIMARY KEY,
                device_id INTEGER NOT NULL REFERENCES power_agent(device_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL,
                force INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_power_device ON power_command(device_id, created_at);
            CREATE TABLE IF NOT EXISTS hass_power (
                device_id INTEGER PRIMARY KEY REFERENCES sub_device(id) ON DELETE CASCADE,
                discovery_topic TEXT NOT NULL UNIQUE,
                last_sent REAL NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS hass_registration (
                mac TEXT PRIMARY KEY,
                discovery_topic TEXT NOT NULL,
                command_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_session (
                token_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                credential_version TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempt (
                address TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_share (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL UNIQUE REFERENCES sub_device(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                password_secret TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                can_wake INTEGER NOT NULL DEFAULT 1,
                can_shutdown INTEGER NOT NULL DEFAULT 1,
                expires_at REAL,
                credential_version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS share_session (
                token_hash TEXT PRIMARY KEY,
                share_id INTEGER NOT NULL REFERENCES device_share(id) ON DELETE CASCADE,
                credential_version INTEGER NOT NULL,
                csrf_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS share_login_attempt (
                share_id INTEGER NOT NULL REFERENCES device_share(id) ON DELETE CASCADE,
                address TEXT NOT NULL,
                failures INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (share_id, address)
            );
            CREATE INDEX IF NOT EXISTS idx_share_session_expiry ON share_session(expires_at);
            CREATE INDEX IF NOT EXISTS idx_share_login_expiry ON share_login_attempt(expires_at);
            CREATE TABLE IF NOT EXISTS device_power_operation (
                device_id INTEGER PRIMARY KEY REFERENCES sub_device(id) ON DELETE CASCADE,
                operation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                command_id TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT ''
            );
            """)

        ota_columns = {r["name"] for r in _db.execute("PRAGMA table_info(gateway_ota)")}
        for name, definition in {
            "auto_image_sha256": "TEXT NOT NULL DEFAULT ''",
            "auto_task_id": "TEXT NOT NULL DEFAULT ''",
            "auto_attempts": "INTEGER NOT NULL DEFAULT 0",
            "auto_retry_at": "REAL NOT NULL DEFAULT 0",
        }.items():
            if name not in ota_columns:
                _db.execute(f"ALTER TABLE gateway_ota ADD COLUMN {name} {definition}")
        gateway_columns = {r["name"] for r in _db.execute("PRAGMA table_info(gateway)")}
        if "ota_auto_update" not in gateway_columns:
            _db.execute(
                "ALTER TABLE gateway ADD COLUMN ota_auto_update INTEGER NOT NULL DEFAULT 0"
            )
        if "last_success_at" not in ota_columns:
            _db.execute(
                "ALTER TABLE gateway_ota ADD COLUMN last_success_at REAL NOT NULL DEFAULT 0"
            )
            _db.execute(
                "UPDATE gateway_ota SET last_success_at=updated_at WHERE state='success' AND updated_at>0"
            )
        share_columns = {
            r["name"] for r in _db.execute("PRAGMA table_info(device_share)")
        }
        if "password_secret" not in share_columns:
            _db.execute(
                "ALTER TABLE device_share ADD COLUMN password_secret TEXT NOT NULL DEFAULT ''"
            )
        pairing_columns = {
            r["name"] for r in _db.execute("PRAGMA table_info(pairing_request)")
        }
        if "purpose" not in pairing_columns:
            _db.execute(
                "ALTER TABLE pairing_request ADD COLUMN purpose TEXT NOT NULL DEFAULT 'recovery'"
            )
        columns = {r["name"] for r in _db.execute("PRAGMA table_info(cmd_log)")}
        if "cmd_id" not in columns:
            _db.execute("ALTER TABLE cmd_log ADD COLUMN cmd_id TEXT")
        _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cmd_id ON cmd_log(cmd_id)")
        _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmd_retention ON cmd_log(status,created_at)"
        )
        _init_gateway_references(_db)
        _init_user_ownership(_db)
        gateway_columns = {r["name"] for r in _db.execute("PRAGMA table_info(gateway)")}
        for field in (
            "delete_requested_at",
            "delete_cmd_id",
            "delete_last_attempt",
            "delete_error",
            "firmware_version",
        ):
            if field not in gateway_columns:
                _db.execute(
                    f"ALTER TABLE gateway ADD COLUMN {field} TEXT NOT NULL DEFAULT ''"
                )
        scan_columns = {
            r["name"] for r in _db.execute("PRAGMA table_info(scan_result)")
        }
        device_columns = {
            r["name"] for r in _db.execute("PRAGMA table_info(sub_device)")
        }
        for field, definition in (
            ("last_online", "TEXT NOT NULL DEFAULT ''"),
            ("offline_since", "TEXT NOT NULL DEFAULT ''"),
            ("offline_last_check", "TEXT NOT NULL DEFAULT ''"),
            ("offline_checks", "INTEGER NOT NULL DEFAULT 0"),
            ("pc_service_bound", "INTEGER NOT NULL DEFAULT 0"),
            ("pc_service_ignored", "INTEGER NOT NULL DEFAULT 0"),
            ("pc_last_report", "TEXT NOT NULL DEFAULT ''"),
            ("type_override", "TEXT NOT NULL DEFAULT ''"),
        ):
            if field not in device_columns:
                _db.execute(f"ALTER TABLE sub_device ADD COLUMN {field} {definition}")
        for field in ("device_name", "model", "os", "info_source", "scan_cmd_id"):
            if field not in scan_columns:
                _db.execute(
                    f"ALTER TABLE scan_result ADD COLUMN {field} TEXT NOT NULL DEFAULT ''"
                )
        _db.executescript("""
            CREATE TRIGGER IF NOT EXISTS gateway_delete_logs AFTER DELETE ON gateway
            BEGIN
                DELETE FROM cmd_log WHERE gateway_id=OLD.id;
            END;
            DELETE FROM cmd_log WHERE gateway_id NOT IN (SELECT id FROM gateway);
        """)
        _init_edit_revisions(_db)


def _init_edit_revisions(conn):
    from .mysql_schema import REVISION_FIELDS

    fields = REVISION_FIELDS
    with atomic(conn):
        for table, columns in fields.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "edit_revision" not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN edit_revision TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                f"UPDATE {table} SET edit_revision=lower(hex(randomblob(16))) WHERE edit_revision=''"
            )
            conn.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_revision_insert
                AFTER INSERT ON {table} WHEN NEW.edit_revision=''
                BEGIN UPDATE {table} SET edit_revision=lower(hex(randomblob(16))) WHERE id=NEW.id; END"""
            )
            changed = " OR ".join(
                f"OLD.{column} IS NOT NEW.{column}" for column in columns
            )
            # Recreate when the capability fields tracked by the trigger change.
            conn.execute(f"DROP TRIGGER IF EXISTS {table}_revision_update")
            conn.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_revision_update
                AFTER UPDATE OF {','.join(columns)} ON {table} WHEN {changed}
                BEGIN UPDATE {table} SET edit_revision=lower(hex(randomblob(16))) WHERE id=NEW.id; END"""
            )


def _init_user_ownership(conn):
    # Both the web service and enrollment service run this migration.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS app_user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK(role IN ('admin','user')),
            password_hash TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            session_version INTEGER NOT NULL DEFAULT 1,
            permissions TEXT NOT NULL DEFAULT '*',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON app_user(role) WHERE role='admin'"
        )
        user_columns = {r["name"] for r in conn.execute("PRAGMA table_info(app_user)")}
        if "permissions" not in user_columns:
            conn.execute(
                "ALTER TABLE app_user ADD COLUMN permissions TEXT NOT NULL DEFAULT '*'"
            )
        for table in ("gateway", "pairing_request"):
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "owner_user_id" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_user_id INTEGER REFERENCES app_user(id) ON DELETE RESTRICT"
                )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner_user_id)"
            )
        conn.execute("""CREATE TABLE IF NOT EXISTS account_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            event TEXT NOT NULL,
            target TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_gateway_by_mac(mac: str):
    """根据 MAC 查询网关记录"""
    with db() as conn:
        return conn.execute("SELECT * FROM gateway WHERE mac = ?", (mac,)).fetchone()
