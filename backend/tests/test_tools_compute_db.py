"""Database tool group: read-only SQL guard and an end-to-end SQLite connection."""
import sqlite3
import uuid

import pytest

import app.tools.builtin.database as dbtools
from app.tools.base import ToolContext, ToolError, registry


async def call(ctx, tool, /, **args):
    spec = registry.get(tool)
    assert spec is not None, tool
    return await spec.handler(ctx, **spec.validate(args))


ALLOWED = [
    ("postgres", "SELECT 1"),
    ("postgres", "  select * from t where name = 'a;b' ;  "),
    ("postgres", "-- leading note\nSELECT 1; -- trailing note"),
    ("postgres", "WITH x AS (SELECT 1 AS v) SELECT v FROM x"),
    ("postgres", "SELECT $$; DROP TABLE t; $$ AS s"),
    ("postgres", "SELECT $tag$ it's; fine $tag$"),
    ("postgres", "EXPLAIN ANALYZE SELECT * FROM t"),
    ("postgres", 'SELECT "update", updated_at, deleted FROM t'),
    ("postgres", "SELECT E'it\\'s' AS s"),
    ("postgres", "(SELECT 1) UNION (SELECT 2)"),
    ("mysql", "SHOW TABLES"),
    ("mysql", "DESCRIBE customers"),
    ("mysql", "SELECT REPLACE(name, 'a', 'b') FROM t"),
    ("mysql", "SELECT 1 # trailing comment ; DROP TABLE t"),
    ("mysql", "SELECT `select` FROM `order`"),
    ("sqlite", "SELECT * FROM [order] WHERE \"group\" = 'x'"),
    ("sqlite", "SELECT * FROM pragma_table_info('t')"),
]

REJECTED = [
    ("postgres", "DELETE FROM t"),
    ("postgres", "SELECT 1; DROP TABLE t"),
    ("postgres", "SELECT 1 # ; DROP TABLE t"),  # '#' is an operator in PostgreSQL, not a comment
    ("postgres", "SHOW search_path; COMMIT; DROP TABLE t"),
    ("postgres", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d"),
    ("postgres", "SELECT * INTO new_t FROM t"),
    ("postgres", "SELECT * FROM t FOR UPDATE"),
    ("postgres", "EXPLAIN ANALYZE DELETE FROM t"),
    ("postgres", "SELECT pg_terminate_backend(42)"),
    ("postgres", "SELECT set_config('transaction_read_only', 'off', false)"),
    ("postgres", "SELECT 'abc\\'; DROP TABLE t; --'"),
    ("postgres", "/* unterminated SELECT 1"),
    ("postgres", "COPY t TO '/tmp/x'"),
    ("mysql", "/*!50000 DROP TABLE t */ SELECT 1"),
    ("mysql", "SELECT 'a\\'; DROP TABLE t; -- '"),
    ("mysql", "SELECT * FROM t INTO OUTFILE '/tmp/x'"),
    ("mysql", "SELECT 1--1; DROP TABLE t"),  # '--' without a space is not a MySQL comment
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "REPLACE INTO t VALUES (1)"),
    ("sqlite", "PRAGMA query_only = OFF"),
    ("sqlite", "ATTACH DATABASE 'x.db' AS y"),
    ("sqlite", "SELECT load_extension('evil')"),
    ("sqlite", "-- only a comment"),
    ("sqlite", ""),
]


@pytest.mark.parametrize(("dialect", "sql"), ALLOWED)
def test_sql_guard_allows_read_only(dialect, sql):
    clean = dbtools.check_read_only(sql, dialect)
    assert clean and not clean.rstrip().endswith(";")


@pytest.mark.parametrize(("dialect", "sql"), REJECTED)
def test_sql_guard_rejects_writes_and_tricks(dialect, sql):
    with pytest.raises(ToolError):
        dbtools.check_read_only(sql, dialect)


def _make_db(ctx: ToolContext) -> str:
    rel = f"shared/{uuid.uuid4().hex[:8]}/app.db"
    path = ctx.workspace.resolve(rel, for_write=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT);
        CREATE INDEX ix_customers_email ON customers(email);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total REAL);
        INSERT INTO customers (name, email) VALUES ('Ada', 'ada@example.com'), ('Linus', 'linus@example.com');
        INSERT INTO orders (customer_id, total) VALUES (1, 19.5), (2, 5);
    """)
    con.commit()
    con.close()
    return rel


async def test_database_sqlite_end_to_end(tool_ctx):
    rel = _make_db(tool_ctx)
    tool_ctx.extra["connectors"] = {"sqlite": {"path": rel, "allow_writes": "false", "name": "appdb"}}

    conns = await call(tool_ctx, "db_list_connections")
    assert "appdb (sqlite)" in conns and "read-only" in conns
    tables = await call(tool_ctx, "db_list_tables")
    assert "customers (table)" in tables and "orders (table)" in tables
    desc = await call(tool_ctx, "db_describe_table", table="customers", sample_rows=1)
    assert "id: INTEGER" in desc and "PK" in desc and "name: TEXT NOT NULL" in desc
    assert "ix_customers_email" in desc and "Sample rows (1)" in desc and "Ada" in desc
    assert "customer_id → customers.id" in await call(tool_ctx, "db_describe_table", table="orders")
    with pytest.raises(ToolError, match="not found"):
        await call(tool_ctx, "db_describe_table", table="nope")
    assert "customers: email (TEXT)" in await call(tool_ctx, "db_search_schema", term="MAIL")

    rows = await call(tool_ctx, "db_query", sql="SELECT name, email FROM customers ORDER BY id;")
    assert rows.startswith("2 row(s)") and "| Ada | ada@example.com |" in rows
    assert "more rows exist" in await call(tool_ctx, "db_query", sql="SELECT * FROM customers", max_rows=1)
    plan = await call(tool_ctx, "db_explain", sql="SELECT * FROM customers WHERE email = 'ada@example.com'")
    assert "ix_customers_email" in plan

    for bad in ("DELETE FROM customers", "SELECT 1; DROP TABLE customers", "PRAGMA query_only = OFF",
                "WITH x AS (SELECT 1) DELETE FROM customers", "ATTACH DATABASE 'x.db' AS x"):
        with pytest.raises(ToolError):
            await call(tool_ctx, "db_query", sql=bad)
    with pytest.raises(ToolError, match="Writes are disabled"):
        await call(tool_ctx, "db_execute", sql="DELETE FROM customers")

    # engine-level guard: even if the statement check were bypassed, the connection itself is read-only
    conn = await dbtools._pick(tool_ctx, None)
    with pytest.raises(sqlite3.OperationalError):
        dbtools._read_sync(conn, "INSERT INTO customers (name) VALUES ('Mallory')", None, 10, False)
    assert "| 2 |" in await call(tool_ctx, "db_query", sql="SELECT count(*) AS n FROM customers")

    tool_ctx.extra["connectors"]["sqlite"]["allow_writes"] = "true"
    msg = await call(tool_ctx, "db_execute", sql="INSERT INTO customers (name, email) VALUES ('Grace', 'g@x.org')")
    assert "1 row(s) affected" in msg
    assert "| 3 |" in await call(tool_ctx, "db_query", sql="SELECT count(*) AS n FROM customers")
    for bad in ("ATTACH DATABASE 'evil.db' AS e", "VACUUM INTO 'copy.db'",
                "INSERT INTO customers (name) VALUES ('a'); DELETE FROM customers"):
        with pytest.raises(ToolError):
            await call(tool_ctx, "db_execute", sql=bad)
    viewer = ToolContext(org_id=tool_ctx.org_id, user_id=tool_ctx.user_id, role="viewer",
                         extra={"connectors": tool_ctx.extra["connectors"]})
    with pytest.raises(ToolError, match="read-only"):
        await call(viewer, "db_execute", sql="DELETE FROM customers")


async def test_database_connection_selection_and_paths(tool_ctx):
    tool_ctx.extra["connectors"] = {"sqlite": {"path": "workspace/private.db"}}
    with pytest.raises(ToolError, match="shared drive"):
        await call(tool_ctx, "db_list_tables")
    rel = _make_db(tool_ctx)
    tool_ctx.extra["connectors"] = {
        "sqlite": {"path": rel, "name": "local"},
        "postgres": {"host": "db.invalid", "database": "x", "user": "u", "password": "pw-must-not-show",
                     "name": "warehouse"},
    }
    with pytest.raises(ToolError, match="Several databases"):
        await call(tool_ctx, "db_list_tables")
    with pytest.raises(ToolError, match="Unknown connection"):
        await call(tool_ctx, "db_list_tables", connection="nope")
    assert "customers" in await call(tool_ctx, "db_list_tables", connection="local")
    assert "customers" in await call(tool_ctx, "db_list_tables", connection="sqlite")
    listing = await call(tool_ctx, "db_list_connections")
    assert "warehouse (postgres)" in listing and "x on db.invalid:5432" in listing
    assert "pw-must-not-show" not in listing


class _Desc(tuple):  # DB-API description entry: pymysql indexes it, psycopg uses .name
    @property
    def name(self):
        return self[0]


class _FakeCursor:
    statusmessage = "UPDATE 1"
    rowcount = 1

    def __init__(self, log, name=None, rows=((1,), (2,), (3,))):
        self.log, self.name, self.rows, self.description = log, name, list(rows), None

    def execute(self, sql, params=None):
        self.log.append(("named" if self.name else "cursor", sql, params))
        self.description = [_Desc(("n", None, None, None, None, None, None))]
        return 1

    def fetchmany(self, n):
        return self.rows[:n]

    def close(self):
        self.log.append(("close",))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _FakePg:
    def __init__(self):
        self.log = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append(("exit",))

    def execute(self, sql, params=None):
        self.log.append(("conn", sql))
        return _FakeCursor(self.log)

    def cursor(self, name=None):
        return _FakeCursor(self.log, name)

    def rollback(self):
        self.log.append(("rollback",))

    def commit(self):
        self.log.append(("commit",))


async def test_postgres_query_runs_in_read_only_transaction(tool_ctx, monkeypatch):
    fake = _FakePg()
    monkeypatch.setattr(dbtools, "_pg_connect", lambda cfg, timeout=10: fake)
    tool_ctx.extra["connectors"] = {"postgres": {"host": "db", "database": "app", "user": "ro", "password": "x"}}
    out = await call(tool_ctx, "db_query", sql="-- top customers\nSELECT n FROM t;", max_rows=2)
    assert out.startswith("2 row(s) (stopped at max_rows=2") and "| n |" in out
    kinds = [entry[0] for entry in fake.log]
    assert fake.log[0] == ("conn", "SET TRANSACTION READ ONLY")  # first statement of the transaction
    assert fake.log[1] == ("conn", "SET LOCAL statement_timeout = 30000")
    assert fake.log[2][0] == "named" and fake.log[2][1] == "-- top customers\nSELECT n FROM t"
    assert kinds.index("rollback") > kinds.index("named")
    fake.log.clear()
    await call(tool_ctx, "db_explain", sql="SELECT n FROM t")
    assert fake.log[0] == ("conn", "SET TRANSACTION READ ONLY")
    assert any(e[0] == "cursor" and e[1].startswith("EXPLAIN SELECT") for e in fake.log)
    with pytest.raises(ToolError):
        await call(tool_ctx, "db_query", sql="SELECT 1; COMMIT; DROP TABLE t")
    tool_ctx.extra["connectors"]["postgres"]["allow_writes"] = "true"
    fake.log.clear()
    assert "Committed on postgres: UPDATE 1 (1 row(s) affected)" in await call(tool_ctx, "db_execute",
                                                                                sql="UPDATE t SET n = 1")
    assert ("conn", "UPDATE t SET n = 1") in fake.log and fake.log[-2] == ("commit",)
    assert ("conn", "SET TRANSACTION READ ONLY") not in fake.log


async def test_mysql_query_runs_in_read_only_transaction(tool_ctx, monkeypatch):
    log = []

    class FakeMy:
        def cursor(self, cls=None):
            return _FakeCursor(log)

        def close(self):
            log.append(("conn-close",))

    monkeypatch.setattr(dbtools, "_mysql_connect", lambda cfg, read_timeout, unbuffered=False: FakeMy())
    tool_ctx.extra["connectors"] = {"mysql": {"host": "db", "database": "shop", "user": "ro"}}
    out = await call(tool_ctx, "db_query", sql="SELECT n FROM t")
    assert out.startswith("3 row(s)")
    statements = [e[1] for e in log if e[0] == "cursor"]
    assert statements[:3] == ["SET SESSION MAX_EXECUTION_TIME = 30000", "START TRANSACTION READ ONLY", "SELECT n FROM t"]
    assert log[-1] == ("conn-close",)
    with pytest.raises(ToolError):
        await call(tool_ctx, "db_query", sql="SELECT * FROM t INTO OUTFILE '/tmp/x'")


async def test_sqlite_connector_test_function(tool_ctx):
    from app.tools.connectors import connector_kinds

    rel = _make_db(tool_ctx)
    ok, msg = await connector_kinds["sqlite"].test({"path": rel, "_org_id": tool_ctx.org_id})
    assert ok and "(2 tables)" in msg
    ok, msg = await connector_kinds["sqlite"].test({"path": "workspace/x.db", "_org_id": tool_ctx.org_id})
    assert not ok and "shared" in msg
    ok, _ = await connector_kinds["sqlite"].test({"path": "shared/missing.db", "_org_id": tool_ctx.org_id})
    assert not ok
