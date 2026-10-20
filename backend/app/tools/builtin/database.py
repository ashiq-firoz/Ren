"""Database tools: explore schemas and run SQL against the org's PostgreSQL, MySQL and SQLite connections.

Read-only by default, with two independent guards for db_query/db_explain:
  1. a dialect-aware statement check (comments and literals are stripped, then only a single SELECT / WITH /
     EXPLAIN / SHOW / DESCRIBE statement without data-modifying keywords or dangerous functions is accepted);
  2. the statement runs inside a read-only transaction (PostgreSQL ``SET TRANSACTION READ ONLY``, MySQL
     ``START TRANSACTION READ ONLY``, SQLite ``mode=ro`` + ``PRAGMA query_only=ON``) with a statement timeout.
db_execute (writes) is CRITICAL risk and only works when the connector's "Allow writes" is enabled.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.workspace import Workspace, WorkspaceError
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin._exec import clip, fence, ws_path
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

KINDS = ("postgres", "mysql", "sqlite")

db = ToolGroup(
    "database", "Databases",
    "Explore schemas and run read-only SQL (writes only where an admin allows them) against connected PostgreSQL, "
    "MySQL and SQLite databases",
    prefix="db", icon="database", category="data", connector_kinds=KINDS,
)

QUERY_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10
MAX_ROWS = 1000
TABLE_BUDGET = 12_000

_READ_START = {"select", "with", "explain", "show", "describe", "desc"}
_WRITE_WORDS = {"insert", "update", "delete", "merge", "create", "drop", "alter", "truncate", "grant", "revoke",
                "into", "outfile", "dumpfile"}
_DENY_FUNCS = (
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile", "pg_promote",
    "pg_switch_wal", "pg_create_restore_point", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_file_write", "lo_import", "lo_export", "lo_unlink", "lo_put", "lo_create", "lo_from_bytea", "dblink",
    "dblink_exec", "dblink_connect", "dblink_send_query", "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
    "load_file", "sys_exec", "sys_eval", "get_lock", "release_lock", "release_all_locks", "load_extension",
    "writefile", "readfile",
)
_DENY_FUNC_RX = re.compile(r"\b(" + "|".join(_DENY_FUNCS) + r")\s*\(", re.IGNORECASE)
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


# --------------------------------------------------------------------------- SQL analysis
def _scan(sql: str, dialect: str, backslash: bool) -> str:
    """Copy of `sql` (same length) with comments and the inside of quoted strings/identifiers blanked out.

    Dialect rules: `#` comments and `-- ` (with a space) only for MySQL, dollar quoting only for PostgreSQL, backticks
    for MySQL/SQLite, [brackets] for SQLite. `backslash` treats backslash as an escape inside quotes (MySQL default,
    legacy PostgreSQL); callers check both interpretations so a literal can never hide code from the checks.
    """
    buf = list(sql)
    n = len(sql)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if buf[k] not in "\r\n":
                buf[k] = " "

    i = 0
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        line_comment = c == "-" and nxt == "-" and (dialect != "mysql" or i + 2 >= n or sql[i + 2] in " \t\r\n\f\v")
        if line_comment or (c == "#" and dialect == "mysql"):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                raise ToolError("Unterminated /* … */ comment in the SQL.")
            blank(i, j + 2)
            i = j + 2
            continue
        if c in ("'", '"') or (c == "`" and dialect != "postgres") or (c == "[" and dialect == "sqlite"):
            close = "]" if c == "[" else c
            esc = backslash and c in ("'", '"')
            if (dialect == "postgres" and c == "'" and i > 0 and sql[i - 1] in "eE"
                    and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] in "_$"))):
                esc = True  # E'...' escape string
            j = i + 1
            while True:
                if j >= n:
                    raise ToolError("Unterminated quoted string or identifier in the SQL.")
                ch = sql[j]
                if esc and ch == "\\":
                    j += 2
                    continue
                if ch == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
            continue
        if c == "$" and dialect == "postgres" and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            m = _DOLLAR.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j == -1:
                    raise ToolError("Unterminated dollar-quoted string in the SQL.")
                blank(m.end(), j)
                i = j + len(tag)
                continue
        i += 1
    return "".join(buf)


def _code_variants(sql: str, dialect: str) -> list[str]:
    if dialect == "mysql" and "/*!" in sql:
        raise ToolError("MySQL executable comments (/*! … */) are not allowed.")
    variants = [_scan(sql, dialect, backslash=False)]
    if dialect in ("mysql", "postgres"):
        variants.append(_scan(sql, dialect, backslash=True))
    return variants


def _single_statement(sql: str, dialect: str) -> tuple[str, list[str]]:
    """(sql without trailing semicolons/comments, scanned code variants); rejects multiple statements."""
    if not sql or not sql.strip():
        raise ToolError("The SQL statement is empty.")
    if "\x00" in sql:
        raise ToolError("The SQL contains a NUL byte.")
    variants = _code_variants(sql, dialect)
    end = len(sql)
    for code in variants:
        code_end = len(code.rstrip(" \t\r\n\f\v;"))
        if ";" in code[:code_end]:
            raise ToolError("Only a single SQL statement is allowed per call (remove the extra ';').")
        end = min(end, code_end)
    if end == 0:
        raise ToolError("The SQL contains no statement (only comments?).")
    return sql[:end], [v[:end] for v in variants]


def _first_word(code: str) -> str:
    m = re.match(r"[\s(]*([A-Za-z]+)", code)
    return m.group(1).lower() if m else ""


def check_read_only(sql: str, dialect: str) -> str:
    """Validate a read-only single statement for `dialect`; returns the cleaned SQL. Raises ToolError otherwise."""
    clean, variants = _single_statement(sql, dialect)
    for code in variants:
        first = _first_word(code)
        if first not in _READ_START:
            raise ToolError(f"Only read-only queries are allowed here (SELECT, WITH, EXPLAIN, SHOW, DESCRIBE); got "
                            f"'{first.upper() or '?'}'. Use db_execute for changes (if the connection allows writes).")
        bad = sorted({w.lower() for w in _WORD.findall(code)} & _WRITE_WORDS)
        if bad:
            raise ToolError(f"'{bad[0].upper()}' isn't allowed in a read-only query.")
        m = _DENY_FUNC_RX.search(code)
        if m:
            raise ToolError(f"The function {m.group(1)}() isn't allowed in queries.")
    return clean


def _check_write(sql: str, dialect: str) -> str:
    clean, variants = _single_statement(sql, dialect)
    for code in variants:
        words = {w.lower() for w in _WORD.findall(code)}
        if dialect == "sqlite" and ("attach" in words or "detach" in words or {"vacuum", "into"} <= words):
            raise ToolError("ATTACH/DETACH and VACUUM INTO are not allowed (they touch files outside the database).")
        m = _DENY_FUNC_RX.search(code)
        if m:
            raise ToolError(f"The function {m.group(1)}() isn't allowed.")
    return clean


# --------------------------------------------------------------------------- formatting
def _cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b.hex() if len(b) <= 32 else f"<{len(b)} bytes>"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    s = json.dumps(v, default=str, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("|", "\\|")
    return s if len(s) <= 300 else s[:300] + "…"


def _table(columns: list[str], rows: list, budget: int = TABLE_BUDGET) -> tuple[str, int]:
    """Markdown pipe table that fits `budget` characters → (text, rows rendered)."""
    head = "| " + " | ".join(_cell(c) for c in columns) + " |"
    lines = [head, "|" + "|".join("---" for _ in columns) + "|"]
    used = sum(len(x) + 1 for x in lines)
    count = 0
    for r in rows:
        line = "| " + " | ".join(_cell(v) for v in r) + " |"
        if count and used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        count += 1
    return "\n".join(lines), count


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on")


# --------------------------------------------------------------------------- connections
@dataclass
class _Conn:
    name: str
    kind: str
    cfg: dict
    id: str | None = None
    path: Path | None = None  # sqlite file

    @property
    def writable(self) -> bool:
        return _truthy(self.cfg.get("allow_writes"))

    def describe(self) -> str:
        if self.kind == "sqlite":
            target = str(self.cfg.get("path") or "?")
        else:
            default_port = 5432 if self.kind == "postgres" else 3306
            target = (f"{self.cfg.get('database') or self.cfg.get('dbname') or '?'} on "
                      f"{self.cfg.get('host') or 'localhost'}:{self.cfg.get('port') or default_port}")
        return f"{self.name} ({self.kind}) — {target} — {'writes allowed' if self.writable else 'read-only'}"


async def _connections(ctx: ToolContext) -> list[_Conn]:
    conns: list[_Conn] = []
    overrides = ctx.extra.get("connectors") or {}
    for kind in KINDS:
        if kind in overrides:
            cfg = dict(overrides[kind])
            name = str(cfg.get("_connector_name") or cfg.get("name") or kind)
            conns.append(_Conn(name=name, kind=kind, cfg=cfg, id=cfg.get("_connector_id")))
    remaining = [k for k in KINDS if k not in overrides]
    if remaining and not ctx.extra.get("no_db"):
        from sqlalchemy import select

        from app.core.security import decrypt_json
        from app.models import Connector

        async with ctx.session() as session:
            rows = (await session.execute(
                select(Connector).where(Connector.org_id == ctx.org_id, Connector.kind.in_(remaining))
                .order_by(Connector.created_at)
            )).scalars().all()
        for row in rows:
            cfg = dict(row.config or {})
            cfg.update(decrypt_json(row.secrets_enc))
            conns.append(_Conn(name=row.name, kind=row.kind, cfg=cfg, id=row.id))
    return conns


def _sqlite_file(ctx: ToolContext, cfg: dict, *, must_exist: bool = True) -> Path:
    raw = str(cfg.get("path") or cfg.get("database") or "").strip()
    if not raw:
        raise ToolError("The SQLite connection has no file path configured.")
    if not ctx.workspace.normalize(raw).startswith("shared/"):
        raise ToolError("SQLite databases must be files in the shared drive (e.g. shared/data/app.db); ask an admin "
                        "to fix the connector's path.")
    p = ws_path(ctx, raw, must_exist=must_exist)
    if p.exists() and not p.is_file():
        raise ToolError(f"The SQLite path {raw} is not a file.")
    return p


async def _pick(ctx: ToolContext, connection: str | None, *, must_exist: bool = True) -> _Conn:
    conns = await _connections(ctx)
    if not conns:
        raise ToolError("No database is connected. An admin can add a PostgreSQL, MySQL or SQLite connection in "
                        "Connectors → Integrations.")
    chosen: _Conn | None = None
    if connection:
        key = connection.strip().lower()
        chosen = next((c for c in conns if c.name.lower() == key or (c.id and c.id == connection.strip())), None)
        if chosen is None:
            by_kind = [c for c in conns if c.kind == key]
            chosen = by_kind[0] if len(by_kind) == 1 else None
        if chosen is None:
            raise ToolError(f"Unknown connection '{connection}'. Available: {', '.join(c.name for c in conns)}.")
    elif len(conns) == 1:
        chosen = conns[0]
    else:
        raise ToolError(f"Several databases are connected ({', '.join(c.name for c in conns)}); pass "
                        "connection=<name>.")
    if chosen.kind == "sqlite":
        chosen.path = _sqlite_file(ctx, chosen.cfg, must_exist=must_exist)
    return chosen


# --------------------------------------------------------------------------- drivers (sync; run in threads)
def _pg_connect(cfg: dict, timeout: int = CONNECT_TIMEOUT_S):
    import psycopg

    params = {
        "host": cfg.get("host") or "localhost", "port": int(cfg.get("port") or 5432),
        "dbname": cfg.get("database") or cfg.get("dbname"), "user": cfg.get("user") or cfg.get("username"),
        "password": cfg.get("password"), "sslmode": cfg.get("sslmode") or "prefer",
        "connect_timeout": timeout, "application_name": "ren-agent",
    }
    return psycopg.connect(**{k: v for k, v in params.items() if v not in (None, "")})


def _mysql_ssl(mode: str | None):
    import ssl

    mode = (mode or "prefer").strip().lower()
    if mode in ("disable", "disabled", "off", "false"):
        return None
    context = ssl.create_default_context()
    if mode in ("verify-full", "verify-identity", "verify_identity"):
        return context
    context.check_hostname = False
    if mode in ("verify-ca", "verify_ca"):
        return context
    context.verify_mode = ssl.CERT_NONE  # prefer / require: encrypted, unverified
    return context


def _mysql_connect(cfg: dict, *, read_timeout: int, unbuffered: bool = False):
    import pymysql
    import pymysql.cursors

    kwargs: dict[str, Any] = {
        "host": cfg.get("host") or "localhost", "port": int(cfg.get("port") or 3306),
        "user": cfg.get("user") or cfg.get("username") or "", "password": cfg.get("password") or "",
        "database": cfg.get("database") or None, "connect_timeout": CONNECT_TIMEOUT_S, "charset": "utf8mb4",
        "autocommit": False, "read_timeout": read_timeout, "write_timeout": read_timeout,
    }
    ssl_ctx = _mysql_ssl(cfg.get("sslmode"))
    if ssl_ctx is not None:
        kwargs["ssl"] = ssl_ctx
    if unbuffered:
        kwargs["cursorclass"] = pymysql.cursors.SSCursor
    conn = pymysql.connect(**kwargs)
    if (cfg.get("sslmode") or "prefer").strip().lower() not in ("disable", "disabled", "off", "false", "prefer",
                                                                 "allow"):
        with conn.cursor(pymysql.cursors.Cursor) as c:
            c.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = c.fetchone()
        if not row or not row[1]:
            conn.close()
            raise ToolError("The MySQL server did not negotiate TLS but the connector requires it (sslmode).")
    return conn


def _sqlite_open(path: Path, *, write: bool, timeout_s: float) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10)
    else:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA query_only = ON")
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    return conn


def _read_sync(c: _Conn, sql: str, params: Any, max_rows: int, stream: bool) -> tuple[list[str], list, bool]:
    """Run one read-only statement inside a read-only transaction → (columns, rows, truncated)."""
    if c.kind == "postgres":
        with _pg_connect(c.cfg) as conn:
            try:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {QUERY_TIMEOUT_S * 1000}")
                server_side = stream and _first_word(_scan(sql, "postgres", False)) in ("select", "with")
                cur = conn.cursor(name=f"ren_{uuid.uuid4().hex[:10]}") if server_side else conn.cursor()
                try:
                    cur.execute(sql, params)
                    if cur.description is None:
                        return [], [], False
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
                finally:
                    try:
                        cur.close()
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                conn.rollback()
        return cols, list(rows[:max_rows]), len(rows) > max_rows
    if c.kind == "mysql":
        import pymysql.cursors

        conn = _mysql_connect(c.cfg, read_timeout=QUERY_TIMEOUT_S + 10, unbuffered=stream)
        try:
            with conn.cursor(pymysql.cursors.Cursor) as setup:
                try:
                    setup.execute(f"SET SESSION MAX_EXECUTION_TIME = {QUERY_TIMEOUT_S * 1000}")
                except Exception:  # noqa: BLE001  — MariaDB
                    try:
                        setup.execute(f"SET SESSION max_statement_time = {QUERY_TIMEOUT_S}")
                    except Exception:  # noqa: BLE001
                        pass
                setup.execute("START TRANSACTION READ ONLY")
            cur = conn.cursor()
            cur.execute(sql, params)
            if cur.description is None:
                return [], [], False
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
            return cols, list(rows[:max_rows]), len(rows) > max_rows
        finally:
            conn.close()  # rolls back; abandons any unread rows
    assert c.path is not None
    conn = _sqlite_open(c.path, write=False, timeout_s=QUERY_TIMEOUT_S)
    try:
        cur = conn.execute(sql, params or ())
        if cur.description is None:
            return [], [], False
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
        return cols, list(rows[:max_rows]), len(rows) > max_rows
    finally:
        conn.close()


def _write_sync(c: _Conn, sql: str) -> tuple[int, str, list[str], list]:
    """Run one statement and commit → (rowcount, status, columns, returned rows)."""
    if c.kind == "postgres":
        with _pg_connect(c.cfg) as conn:
            conn.execute(f"SET LOCAL statement_timeout = {QUERY_TIMEOUT_S * 1000}")
            cur = conn.execute(sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchmany(50) if cur.description else []
            status, count = cur.statusmessage or "", cur.rowcount
            conn.commit()
        return count, status, cols, rows
    if c.kind == "mysql":
        conn = _mysql_connect(c.cfg, read_timeout=QUERY_TIMEOUT_S + 10)
        try:
            with conn.cursor() as cur:
                count = cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = list(cur.fetchmany(50)) if cur.description else []
            conn.commit()
            return count, "OK", cols, rows
        finally:
            conn.close()
    assert c.path is not None
    conn = _sqlite_open(c.path, write=True, timeout_s=QUERY_TIMEOUT_S)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(50) if cur.description else []
        conn.commit()
        return cur.rowcount, "OK", cols, rows
    finally:
        conn.close()


def _db_error(c: _Conn, e: BaseException) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if isinstance(e, sqlite3.OperationalError) and "interrupted" in msg:
        return f"The query was cancelled after {QUERY_TIMEOUT_S}s (timeout). Add filters or a LIMIT."
    if "read-only" in msg.lower() or "read only" in msg.lower() or "readonly" in msg.lower():
        return f"{c.name}: the statement tried to modify data in a read-only query ({msg})."
    return f"{c.name} ({c.kind}) error: {clip(msg, 1500)}"


async def _read(c: _Conn, sql: str, params: Any = None, *, max_rows: int = MAX_ROWS,
                stream: bool = False) -> tuple[list[str], list, bool]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_read_sync, c, sql, params, max_rows, stream),
                                      timeout=QUERY_TIMEOUT_S + CONNECT_TIMEOUT_S + 15)
    except ToolError:
        raise
    except TimeoutError as e:
        raise ToolError(f"The database '{c.name}' did not respond in time.") from e
    except Exception as e:  # noqa: BLE001
        raise ToolError(_db_error(c, e)) from e


def _quote_ident(kind: str, *parts: str) -> str:
    if kind == "mysql":
        return ".".join("`" + p.replace("`", "``") + "`" for p in parts if p)
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts if p)


def _split_table(table: str, schema: str | None) -> tuple[str | None, str]:
    table = (table or "").strip()
    if not table:
        raise ToolError("table is required.")
    if schema is None and "." in table and not (table.startswith('"') or table.startswith("`")):
        schema, table = table.split(".", 1)
    return schema, table.strip('"`[]')


# --------------------------------------------------------------------------- tools
@db.tool(risk=Risk.LOW, scope="read")
async def list_connections(ctx: ToolContext) -> str:
    """List the databases connected to this workspace: name, engine, database and whether writes are allowed."""
    conns = await _connections(ctx)
    if not conns:
        return ("No databases are connected. An admin can add PostgreSQL, MySQL or SQLite connections in Connectors → "
                "Integrations.")
    return "\n".join(f"- {c.describe()}" for c in conns)


@db.tool(risk=Risk.LOW, scope="read")
async def list_tables(ctx: ToolContext, connection: str | None = None, schema_name: str | None = None) -> str:
    """List the tables and views of a database (with approximate row counts where the engine provides them).

    Args:
        connection: Connection name (see db_list_connections); optional when only one database is connected.
        schema_name: Only this schema (PostgreSQL) or database (MySQL).
    """
    c = await _pick(ctx, connection)
    schema = schema_name
    if c.kind == "postgres":
        sql = ("SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint FROM pg_catalog.pg_class c "
               "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
               "WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema') "
               "AND n.nspname NOT LIKE 'pg_toast%%' AND (%(s)s::text IS NULL OR n.nspname = %(s)s) "
               "ORDER BY 1, 2 LIMIT 2000")
        _, rows, _ = await _read(c, sql, {"s": schema})
        kinds = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}
        lines = [f"{s}.{t} ({kinds.get(k, k)}" + (f", ~{int(n):,} rows)" if n is not None and n >= 0 else ")")
                 for s, t, k, n in rows]
    elif c.kind == "mysql":
        sql = ("SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS FROM information_schema.TABLES "
               "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) ORDER BY TABLE_NAME LIMIT 2000")
        _, rows, _ = await _read(c, sql, (schema,))
        lines = [f"{t} ({'view' if 'VIEW' in str(k) else 'table'}" + (f", ~{int(n):,} rows)" if n is not None else ")")
                 for t, k, n in rows]
    else:
        _, rows, _ = await _read(c, "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT "
                                    "LIKE 'sqlite_%' ORDER BY name")
        lines = [f"{t} ({k})" for t, k in rows]
    if not lines:
        return f"No tables found in {c.name}."
    return f"{len(lines)} table(s) in {c.name}:\n" + "\n".join(lines)


@db.tool(risk=Risk.LOW, scope="read")
async def describe_table(ctx: ToolContext, table: str, connection: str | None = None,
                         schema_name: str | None = None, sample_rows: int = 0) -> str:
    """Describe a table: columns (type, nullability, default, primary key), indexes and foreign keys.

    Args:
        table: Table name, optionally schema-qualified ("public.orders").
        connection: Connection name; optional when only one database is connected.
        schema_name: Schema (PostgreSQL) or database (MySQL) of the table, if not in `table`.
        sample_rows: Also show this many example rows (0-20).
    """
    c = await _pick(ctx, connection)
    schema, name = _split_table(table, schema_name)
    out: list[str] = []
    if c.kind == "postgres":
        if schema is None:
            _, found, _ = await _read(c, "SELECT table_schema FROM information_schema.tables WHERE table_name = %s "
                                         "ORDER BY (table_schema = ANY(current_schemas(false))) DESC, table_schema "
                                         "LIMIT 1", (name,))
            if not found:
                raise ToolError(f"Table '{name}' not found in {c.name}. Use db_list_tables to see the tables.")
            schema = found[0][0]
        _, cols, _ = await _read(c, "SELECT column_name, data_type, is_nullable, column_default FROM "
                                    "information_schema.columns WHERE table_schema = %s AND table_name = %s "
                                    "ORDER BY ordinal_position", (schema, name))
        if not cols:
            raise ToolError(f"Table '{schema}.{name}' not found in {c.name}.")
        reg = _quote_ident("postgres", schema, name)
        _, pk, _ = await _read(c, "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                                  "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
                               (reg,))
        pks = {r[0] for r in pk}
        out.append(f"{schema}.{name} — columns:")
        for col, typ, nullable, default in cols:
            flags = [typ, "NULL" if nullable == "YES" else "NOT NULL"] + (["PK"] if col in pks else [])
            out.append(f"  {col}: {' '.join(flags)}" + (f" default {default}" if default is not None else ""))
        _, idx, _ = await _read(c, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = "
                                   "%s ORDER BY indexname", (schema, name))
        _, fks, _ = await _read(c, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                                   "%s::regclass AND contype IN ('f','u','c') ORDER BY conname", (reg,))
        if idx:
            out.append("Indexes:")
            out += [f"  {n}: {d}" for n, d in idx]
        if fks:
            out.append("Constraints:")
            out += [f"  {n}: {d}" for n, d in fks]
    elif c.kind == "mysql":
        _, cols, _ = await _read(c, "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, EXTRA "
                                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) "
                                    "AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (schema, name))
        if not cols:
            raise ToolError(f"Table '{name}' not found in {c.name}. Use db_list_tables to see the tables.")
        out.append(f"{name} — columns:")
        for col, typ, nullable, default, key, extra in cols:
            flags = [str(typ), "NULL" if nullable == "YES" else "NOT NULL"] + ([str(key)] if key else [])
            out.append(f"  {col}: {' '.join(flags)}" + (f" default {default}" if default is not None else "")
                       + (f" {extra}" if extra else ""))
        _, idx, _ = await _read(c, "SELECT INDEX_NAME, NON_UNIQUE, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                                   "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) "
                                   "AND TABLE_NAME = %s GROUP BY INDEX_NAME, NON_UNIQUE", (schema, name))
        _, fks, _ = await _read(c, "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME FROM "
                                   "information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) "
                                   "AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL", (schema, name))
        if idx:
            out.append("Indexes:")
            out += [f"  {n} ({'non-unique' if nu else 'unique'}): {cs}" for n, nu, cs in idx]
        if fks:
            out.append("Foreign keys:")
            out += [f"  {col} → {t}.{rc}" for col, t, rc in fks]
    else:
        _, cols, _ = await _read(c, "SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info(?)", (name,))
        if not cols:
            raise ToolError(f"Table '{name}' not found in {c.name}. Use db_list_tables to see the tables.")
        out.append(f"{name} — columns:")
        for col, typ, notnull, default, pk in cols:
            flags = [typ or "ANY", "NOT NULL" if notnull else "NULL"] + (["PK"] if pk else [])
            out.append(f"  {col}: {' '.join(flags)}" + (f" default {default}" if default is not None else ""))
        _, idx, _ = await _read(c, "SELECT name, \"unique\" FROM pragma_index_list(?)", (name,))
        _, fks, _ = await _read(c, "SELECT \"from\", \"table\", \"to\" FROM pragma_foreign_key_list(?)", (name,))
        if idx:
            out.append("Indexes:")
            for iname, unique in idx:
                _, icols, _ = await _read(c, "SELECT name FROM pragma_index_info(?)", (iname,))
                out.append(f"  {iname} ({'unique' if unique else 'non-unique'}): "
                           f"{', '.join(str(r[0]) for r in icols)}")
        if fks:
            out.append("Foreign keys:")
            out += [f"  {f} → {t}.{to}" for f, t, to in fks]
    sample_rows = max(0, min(sample_rows, 20))
    if sample_rows:
        ident = _quote_ident(c.kind, schema or "", name) if c.kind != "sqlite" else _quote_ident("sqlite", name)
        cols_, rows, _ = await _read(c, f"SELECT * FROM {ident} LIMIT {sample_rows}")
        out.append(f"Sample rows ({len(rows)}):")
        out.append(_table(cols_, rows, budget=6000)[0])
    return "\n".join(out)


@db.tool(risk=Risk.LOW, scope="read")
async def search_schema(ctx: ToolContext, term: str, connection: str | None = None) -> str:
    """Find tables and columns whose names contain a term (case-insensitive), e.g. "customer" or "email".

    Args:
        term: Text to look for in table and column names.
        connection: Connection name; optional when only one database is connected.
    """
    term = term.strip()
    if not term:
        raise ToolError("term is required.")
    c = await _pick(ctx, connection)
    hits: dict[str, list[str]] = {}
    if c.kind in ("postgres", "mysql"):
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if c.kind == "postgres":
            sql = ("SELECT table_schema || '.' || table_name, column_name, data_type FROM information_schema.columns "
                   "WHERE table_schema NOT IN ('pg_catalog','information_schema') AND (table_name ILIKE %(t)s OR "
                   "column_name ILIKE %(t)s) ORDER BY 1, ordinal_position LIMIT 500")
            _, rows, _ = await _read(c, sql, {"t": like})
        else:
            sql = ("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                   "DATABASE() AND (LOWER(TABLE_NAME) LIKE LOWER(%s) OR LOWER(COLUMN_NAME) LIKE LOWER(%s)) "
                   "ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT 500")
            _, rows, _ = await _read(c, sql, (like, like))
        low = term.lower()
        for tname, col, typ in rows:
            entry = hits.setdefault(str(tname), [])
            if low in str(col).lower():
                entry.append(f"{col} ({typ})")
    else:
        _, tables, _ = await _read(c, "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE "
                                      "'sqlite_%' ORDER BY name LIMIT 1000")
        low = term.lower()
        for (tname,) in tables:
            _, cols, _ = await _read(c, "SELECT name, type FROM pragma_table_info(?)", (tname,))
            matched = [f"{col} ({typ or 'ANY'})" for col, typ in cols if low in str(col).lower()]
            if matched or low in str(tname).lower():
                hits[str(tname)] = matched
    if not hits:
        return f"No tables or columns matching '{term}' in {c.name}."
    lines = [f"{t}: " + (", ".join(cols) if cols else "(table name matches)") for t, cols in hits.items()]
    return f"Matches for '{term}' in {c.name}:\n" + clip("\n".join(lines), TABLE_BUDGET)


@db.tool(risk=Risk.LOW, scope="read", timeout=QUERY_TIMEOUT_S + 60)
async def query(ctx: ToolContext, sql: str, connection: str | None = None, max_rows: int = 200) -> str:
    """Run a read-only SQL query (SELECT / WITH / EXPLAIN / SHOW / DESCRIBE) and return the rows as a table.

    The query runs in a read-only transaction with a 30-second timeout; statements that modify data are rejected.
    Prefer selecting only the columns you need and adding a LIMIT.

    Args:
        sql: A single read-only SQL statement in the connection's dialect.
        connection: Connection name (see db_list_connections); optional when only one database is connected.
        max_rows: Maximum rows to return (1-1000).
    """
    c = await _pick(ctx, connection)
    clean = check_read_only(sql, c.kind)
    max_rows = max(1, min(max_rows, MAX_ROWS))
    cols, rows, truncated = await _read(c, clean, max_rows=max_rows, stream=True)
    if not cols:
        return "The statement returned no result set."
    if not rows:
        return "0 rows.\n" + _table(cols, [])[0]
    text, shown = _table(cols, rows)
    head = f"{len(rows)} row(s)" + (f" (stopped at max_rows={max_rows}; more rows exist)" if truncated else "")
    if shown < len(rows):
        head += f"; showing the first {shown} to fit (select fewer columns or rows)"
    return head + "\n" + text


@db.tool(risk=Risk.LOW, scope="read", timeout=QUERY_TIMEOUT_S + 60)
async def explain(ctx: ToolContext, sql: str, connection: str | None = None, analyze: bool = False) -> str:
    """Show the execution plan of a read-only query (EXPLAIN), to diagnose slow queries or check index usage.

    Args:
        sql: The SELECT/WITH query to explain (without the EXPLAIN keyword).
        connection: Connection name; optional when only one database is connected.
        analyze: Actually run the query and report real timings (EXPLAIN ANALYZE; PostgreSQL/MySQL 8.0.18+).
    """
    c = await _pick(ctx, connection)
    clean = check_read_only(sql, c.kind)
    if _first_word(_scan(clean, c.kind, False)) not in ("select", "with"):
        raise ToolError("Pass a SELECT or WITH query, without the EXPLAIN keyword.")
    if c.kind == "sqlite":
        stmt = f"EXPLAIN QUERY PLAN {clean}"
    else:
        stmt = f"EXPLAIN {'ANALYZE ' if analyze else ''}{clean}"
    cols, rows, _ = await _read(c, stmt, max_rows=500)
    if c.kind == "postgres" or (c.kind == "mysql" and len(cols) == 1):
        return "\n".join(str(r[0]) for r in rows) or "(empty plan)"
    if c.kind == "sqlite":
        depth: dict[Any, int] = {}
        lines = []
        for node, parent, _, detail in rows:
            depth[node] = depth.get(parent, -1) + 1
            lines.append("  " * depth[node] + str(detail))
        return "\n".join(lines) or "(empty plan)"
    return _table(cols, rows)[0]


def _execute_preview(args: dict) -> str:
    target = args.get("connection") or "the default connection"
    return (f"**Modify the database** `{target}`\n\n{fence(clip(str(args.get('sql', '')), 6000), 'sql')}\n\n"
            "This changes data in an external database and may not be reversible.")


@db.tool(risk=Risk.CRITICAL, scope="write", timeout=QUERY_TIMEOUT_S + 60, preview=_execute_preview)
async def execute(ctx: ToolContext, sql: str, connection: str | None = None) -> str:
    """Run a single statement that changes data or schema (INSERT, UPDATE, DELETE, CREATE, ALTER…) and commit it.

    Only works when an admin enabled "Allow writes" on the connection. Prefer db_query for reading.

    Args:
        sql: A single SQL statement in the connection's dialect.
        connection: Connection name; optional when only one database is connected.
    """
    if not ctx.can_write:
        raise ToolError(f"Your role ({ctx.role}) is read-only, so you cannot modify databases.")
    c = await _pick(ctx, connection, must_exist=False)
    if not c.writable:
        raise ToolError(f"Writes are disabled for the '{c.name}' connection. An admin can enable \"Allow writes\" on "
                        "the connector; until then use db_query for read-only access.")
    clean = _check_write(sql, c.kind)
    try:
        count, status, cols, rows = await asyncio.wait_for(asyncio.to_thread(_write_sync, c, clean),
                                                           timeout=QUERY_TIMEOUT_S + CONNECT_TIMEOUT_S + 15)
    except ToolError:
        raise
    except TimeoutError as e:
        raise ToolError(f"The database '{c.name}' did not respond in time; the statement may still complete.") from e
    except Exception as e:  # noqa: BLE001
        raise ToolError(_db_error(c, e)) from e
    msg = f"Committed on {c.name}: {status or 'OK'}" + (f" ({count} row(s) affected)" if count is not None
                                                          and count >= 0 else "")
    if cols:
        msg += "\n" + _table(cols, rows, budget=6000)[0]
    return msg


# --------------------------------------------------------------------------- connector kinds
async def _run_test(fn, *args) -> tuple[bool, str]:  # noqa: ANN001
    try:
        return True, await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=CONNECT_TIMEOUT_S + 15)
    except ToolError as e:
        return False, str(e)
    except TimeoutError:
        return False, "Connection timed out."
    except Exception as e:  # noqa: BLE001
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return False, f"Connection failed: {clip(first, 500)}"


async def _test_postgres(cfg: dict) -> tuple[bool, str]:
    def run() -> str:
        with _pg_connect(cfg) as conn:
            version = conn.execute("SELECT version()").fetchone()[0]
            ro = conn.execute("SELECT pg_catalog.has_database_privilege(current_user, current_database(), 'CREATE')"
                              ).fetchone()[0]
            conn.rollback()
        return f"Connected: {str(version).split(',')[0]}" + ("" if not ro else " (the user can create objects; a "
                                                                              "read-only role is recommended)")

    return await _run_test(run)


async def _test_mysql(cfg: dict) -> tuple[bool, str]:
    def run() -> str:
        conn = _mysql_connect(cfg, read_timeout=CONNECT_TIMEOUT_S)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
        finally:
            conn.close()
        return f"Connected: MySQL {version}"

    return await _run_test(run)


async def _connector_org(cfg: dict) -> str | None:
    org_id = cfg.get("_org_id") or cfg.get("org_id")
    if org_id or not cfg.get("_connector_id"):
        return org_id
    try:
        from app.core.db import session_scope
        from app.models import Connector

        async with session_scope() as session:
            row = await session.get(Connector, cfg["_connector_id"])
        return row.org_id if row else None
    except Exception:  # noqa: BLE001
        return None


async def _test_sqlite(cfg: dict) -> tuple[bool, str]:
    raw = str(cfg.get("path") or "").strip()
    norm = raw.replace("\\", "/").lstrip("/")
    if not norm.startswith("shared/"):
        return False, "The path must be a file in the shared drive, e.g. shared/data/app.db."
    org_id = await _connector_org(cfg)
    if not org_id:
        return False, "Save the connector first, then test it (the workspace could not be determined)."
    try:
        p = Workspace(org_id, None).resolve(raw)
    except WorkspaceError as e:
        return False, str(e)
    if not p.is_file():
        return False, f"File not found: {norm} (upload it to the shared drive first)."

    def run() -> str:
        conn = _sqlite_open(p, write=False, timeout_s=CONNECT_TIMEOUT_S)
        try:
            n = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
        return f"Opened {norm} ({n} tables)."

    return await _run_test(run)


_WRITES_FIELD = ConnectorField(
    "allow_writes", "Allow writes", type="select", options=["false", "true"],
    help="When true, agents may run INSERT/UPDATE/DDL with db_execute (always requires approval). Using a read-only "
         "database user is still recommended.",
)
_PG_SSL = ["prefer", "require", "verify-ca", "verify-full", "disable", "allow"]
_MYSQL_SSL = ["prefer", "require", "verify-ca", "verify-full", "disable"]

register_connector_kind(ConnectorKindSpec(
    kind="postgres", name="PostgreSQL", description="Query a PostgreSQL database (read-only unless writes are allowed)",
    icon="database", category="data", tool_groups=["database"],
    docs_url="https://www.postgresql.org/docs/current/libpq-connect.html",
    fields=[
        ConnectorField("host", "Host", required=True, placeholder="db.example.com"),
        ConnectorField("port", "Port", type="number", placeholder="5432"),
        ConnectorField("database", "Database", required=True, placeholder="analytics"),
        ConnectorField("user", "User", required=True, placeholder="ren_readonly"),
        ConnectorField("password", "Password", type="password", secret=True),
        ConnectorField("sslmode", "SSL mode", type="select", options=_PG_SSL,
                       help="Use require or verify-full for databases reached over the internet."),
        _WRITES_FIELD,
    ],
    test=_test_postgres,
))
register_connector_kind(ConnectorKindSpec(
    kind="mysql", name="MySQL / MariaDB",
    description="Query a MySQL or MariaDB database (read-only unless writes are allowed)",
    icon="database", category="data", tool_groups=["database"],
    docs_url="https://dev.mysql.com/doc/refman/8.0/en/connecting.html",
    fields=[
        ConnectorField("host", "Host", required=True, placeholder="db.example.com"),
        ConnectorField("port", "Port", type="number", placeholder="3306"),
        ConnectorField("database", "Database", required=True, placeholder="shop"),
        ConnectorField("user", "User", required=True, placeholder="ren_readonly"),
        ConnectorField("password", "Password", type="password", secret=True),
        ConnectorField("sslmode", "SSL mode", type="select", options=_MYSQL_SSL,
                       help="prefer encrypts when the server supports TLS; require/verify-* refuse plaintext."),
        _WRITES_FIELD,
    ],
    test=_test_mysql,
))
register_connector_kind(ConnectorKindSpec(
    kind="sqlite", name="SQLite", description="Query a SQLite database file stored in the shared drive",
    icon="database", category="data", tool_groups=["database"],
    fields=[
        ConnectorField("path", "Database file", required=True, placeholder="shared/data/app.db",
                       help="Path of the .db/.sqlite file inside the shared drive (upload it under Files → shared)."),
        _WRITES_FIELD,
    ],
    test=_test_sqlite,
))
