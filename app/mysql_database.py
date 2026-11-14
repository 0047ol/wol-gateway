"""Small DB-API adapter and versioned InnoDB schema (MySQL 5.5+).

The application keeps qmark parameters and row access compatible with its local
SQLite fixtures. Production connections never fall back to SQLite on failure.
All application writers take the same InnoDB mutex before checking permissions
or revisions, including writers in the enrollment process.
"""

import json
import re
import uuid
from pathlib import Path

import pymysql
from pymysql.constants import CLIENT


class Row:
    def __init__(self, names, values):
        self._mapping = dict(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def keys(self):
        return self._mapping.keys()

    def items(self):
        return self._mapping.items()

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class Result:
    def __init__(self, cursor):
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        names = [c[0] for c in cursor.description] if cursor.description else []
        self._rows = [Row(names, r) for r in cursor.fetchall()] if names else []
        self._position = 0

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def _qmarks(sql):
    """Convert only unquoted placeholders; leave literal question marks intact."""
    result, quote, index = [], None, 0
    while index < len(sql):
        char = sql[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    result.append(sql[index])
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
            result.append(char)
        else:
            result.append("%s" if char == "?" else char)
        index += 1
    return "".join(result)


def _translate(sql):
    # These are the three dialect differences used by application DML. DDL is
    # maintained separately; do not attempt to translate arbitrary SQLite SQL.
    sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", sql, flags=re.I)
    sql = re.sub(
        r"\bON\s+CONFLICT\s*\([\w,\s]+\)\s+DO\s+UPDATE\s+SET\b",
        "ON DUPLICATE KEY UPDATE",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", sql, flags=re.I)
    sql = re.sub(r"\bCOLLATE\s+NOCASE\b", "COLLATE utf8mb4_general_ci", sql, flags=re.I)
    sql = re.sub(r"^RELEASE\s+(?!SAVEPOINT\b)", "RELEASE SAVEPOINT ", sql, flags=re.I)
    return sql


def _insert_defaults(sql, params):
    """Use cryptographic random public IDs/revisions on application inserts."""
    from .mysql_schema import REVISION_FIELDS

    match = re.match(
        r"\s*INSERT(?:\s+OR\s+IGNORE)?\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(",
        sql,
        re.I,
    )
    if not match:
        return sql, params
    columns = [c.strip() for c in match[2].split(",")]
    extras = {"edit_revision": uuid.uuid4().hex} if match[1] in REVISION_FIELDS else {}
    if match[1] == "cmd_log":
        extras.update(payload="", ack_msg="")
    if match[1] == "gateway":
        extras["public_id"] = "g_" + uuid.uuid4().hex
    added = {k: v for k, v in extras.items() if k not in columns}
    prefix_values = ["?"] * len(added)
    prefix_params = list(added.values())
    if match[1] == "app_user" and "admin_slot" not in columns:
        values_end = _values_end(sql, match.end())
        values = _assignments(sql[match.end() : values_end])
        role_index = columns.index("role")
        before = sum(_qmarks(v).count("%s") for v in values[:role_index])
        amount = _qmarks(values[role_index]).count("%s")
        added["admin_slot"] = None
        prefix_values.append(f"IF(({values[role_index]})='admin',1,NULL)")
        prefix_params.extend(params[before : before + amount])
    if not added:
        return sql, params
    # Prefix the new columns/values so existing placeholders, literals and
    # ON DUPLICATE KEY assignments retain their original order.
    sql = (
        sql[: match.start(2)]
        + ",".join(added)
        + ","
        + sql[match.start(2) : match.end()]
        + ",".join(prefix_values)
        + ","
        + sql[match.end() :]
    )
    return sql, tuple(prefix_params) + tuple(params)


def _values_end(sql, start):
    depth, quote = 0, None
    for i in range(start, len(sql)):
        c = sql[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return i
            depth -= 1
    raise ValueError("Invalid INSERT values")


def _assignments(sql):
    parts, start, depth, quote = [], 0, 0, None
    for i, c in enumerate(sql):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(sql[start:i].strip())
            start = i + 1
    return parts + [sql[start:].strip()]


def _where_start(sql, start):
    """Find the UPDATE's WHERE, ignoring subqueries and quoted SQL literals."""
    depth, quote = 0, None
    for i in range(start, len(sql)):
        char = sql[i]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\bWHERE\b", sql[i:], re.I):
            return i
    return len(sql)


def _update_revisions(sql, params):
    from .mysql_schema import REVISION_FIELDS

    match = re.match(r"\s*UPDATE\s+(\w+)\s+SET\s+", sql, re.I)
    if not match or match[1] not in REVISION_FIELDS:
        return sql, params
    end_at = _where_start(sql, match.end())
    assignments = _assignments(sql[match.end() : end_at])
    changed, compare_params, offset = [], [], 0
    admin_expr, admin_params = None, []
    for assignment in assignments:
        column, expression = assignment.split("=", 1)
        column, expression = column.strip().strip("`"), expression.strip()
        amount = _qmarks(expression).count("%s")
        arguments = list(params[offset : offset + amount])
        offset += amount
        if column == "edit_revision":
            raise ValueError(
                "Configuration revision is managed by the database adapter"
            )
        if column in REVISION_FIELDS[match[1]]:
            changed.append(f"NOT (BINARY `{column}` <=> BINARY ({expression}))")
            compare_params.extend(arguments)
        if match[1] == "app_user" and column == "role":
            admin_expr, admin_params = expression, arguments
    additions, prefix_params = [], []
    if changed:
        additions.append(f"edit_revision=IF({' OR '.join(changed)},?,edit_revision)")
        prefix_params.extend(compare_params + [uuid.uuid4().hex])
    if admin_expr is not None:
        additions.append(f"admin_slot=IF(({admin_expr})='admin',1,NULL)")
        prefix_params.extend(admin_params)
    if additions:
        sql = sql[: match.end()] + ",".join(additions) + "," + sql[match.end() :]
    return sql, tuple(prefix_params) + tuple(params)


class MySQLConnection:
    def __init__(self, options):
        self.options = dict(options)
        self.in_transaction = False
        self._raw = None
        self._connect()

    @classmethod
    def from_file(cls, filename):
        if not filename:
            raise ValueError("MYSQL_CONFIG_FILE is required for MySQL")
        options = json.loads(Path(filename).read_text(encoding="utf-8"))
        allowed = {"host", "port", "user", "password", "database", "unix_socket"}
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError("Invalid MySQL configuration keys")
        if not options.get("database") or not options.get("user"):
            raise ValueError("MySQL database and user are required")
        return cls(options)

    def _connect(self):
        self._raw = pymysql.connect(
            **self.options,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            client_flag=CLIENT.FOUND_ROWS,
        )
        self._direct("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        self._direct("SET NAMES utf8mb4 COLLATE utf8mb4_bin")
        self._direct(
            "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES'"
        )
        self._direct("SET SESSION innodb_lock_wait_timeout=15")

    def ensure_connection(self):
        if not self.in_transaction:
            try:
                self._raw.ping(reconnect=False)
            except pymysql.Error:
                if self._raw.open:
                    self._raw.close()
                self._connect()

    def _direct(self, sql, params=None):
        with self._raw.cursor() as cursor:
            cursor.execute(sql, params)
            return Result(cursor)

    def _begin(self):
        self._raw.begin()
        self.in_transaction = True
        try:
            return self._direct("SELECT id FROM wol_write_lock WHERE id=1 FOR UPDATE")
        except BaseException:
            self.rollback()
            raise

    def execute(self, sql, params=()):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            return self._begin()
        is_write = bool(re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I))
        standalone = is_write and not self.in_transaction
        if standalone:
            self._begin()
        try:
            sql, params = _insert_defaults(sql, params)
            sql, params = _update_revisions(sql, params)
            sql = _translate(sql)
            # PyMySQL applies percent interpolation to the entire query.
            sql = _qmarks(sql.replace("%", "%%")) if params else _qmarks(sql)
            result = self._direct(sql, tuple(params) if params else None)
            if standalone:
                self.commit()
            return result
        except BaseException:
            if standalone:
                self.rollback()
            raise

    def executemany(self, sql, rows):
        standalone = not self.in_transaction
        if standalone:
            self._begin()
        try:
            for values in rows:
                self.execute(sql, values)
            if standalone:
                self.commit()
        except BaseException:
            if standalone:
                self.rollback()
            raise

    def commit(self):
        self._raw.commit()
        self.in_transaction = False

    def rollback(self):
        try:
            self._raw.rollback()
        finally:
            self.in_transaction = False

    def close(self):
        if self._raw.open:
            self._raw.close()


def init_mysql(conn):
    from .mysql_schema import TABLES

    lock = "wol-schema-" + conn.options["database"]
    if conn._direct("SELECT GET_LOCK(%s,30)", (lock,)).fetchone()[0] != 1:
        raise RuntimeError("Database schema is busy")
    try:
        for sql in TABLES:
            conn._direct(sql)
        for table, columns in {
            "gateway": {"ota_auto_update": "BIGINT NOT NULL DEFAULT 0"},
            "gateway_ota": {
                "auto_image_sha256": "VARCHAR(64) NOT NULL DEFAULT ''",
                "auto_task_id": "VARCHAR(64) NOT NULL DEFAULT ''",
                "auto_attempts": "BIGINT NOT NULL DEFAULT 0",
                "auto_retry_at": "DOUBLE NOT NULL DEFAULT 0",
            },
        }.items():
            for name, definition in columns.items():
                if not conn._direct(
                    f"SHOW COLUMNS FROM {table} LIKE '{name}'"
                ).fetchone():
                    conn._direct(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        if not conn._direct(
            "SHOW COLUMNS FROM gateway_ota LIKE 'last_success_at'"
        ).fetchone():
            conn._direct(
                "ALTER TABLE gateway_ota ADD COLUMN last_success_at DOUBLE NOT NULL DEFAULT 0"
            )
            conn._direct(
                "UPDATE gateway_ota SET last_success_at=updated_at WHERE state='success' AND updated_at>0"
            )
        if not conn._direct("SHOW COLUMNS FROM app_user LIKE 'permissions'").fetchone():
            conn._direct(
                "ALTER TABLE app_user ADD COLUMN permissions VARCHAR(2048) NOT NULL DEFAULT '*'"
            )
        version = conn._direct(
            "SELECT version FROM wol_schema_version WHERE id=1"
        ).fetchone()
        if version and version[0] != 1:
            raise RuntimeError("Unsupported MySQL schema version")
        # The production account need not have SUPER/TRIGGER privileges.
        # Revision and admin-slot expressions are inserted into the same DML
        # statement above, under the cross-process InnoDB write mutex.
        conn._direct("INSERT IGNORE INTO wol_write_lock(id) VALUES(1)")
        conn._direct("INSERT IGNORE INTO wol_schema_version(id,version) VALUES(1,1)")
    finally:
        conn._direct("SELECT RELEASE_LOCK(%s)", (lock,))
