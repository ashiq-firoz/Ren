import asyncio, os, tempfile
from typing import Literal

def test_schema_and_db(tmp_path, monkeypatch):
    from app.tools.base import ToolGroup, Risk, ToolContext, registry
    g = ToolGroup("demo_smoke", "Demo", "demo group", prefix="demo")

    @g.tool(risk=Risk.MEDIUM, scope="write", content_fields=("body",))
    async def send(ctx: ToolContext, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                   mode: Literal["html", "text"] = "text", priority: int = 3) -> dict:
        """Send a demo message.

        Args:
            to: Recipient addresses.
            subject: Subject line.
            body: Message body
                spanning lines.
            cc: Optional CC list.
            mode: Body format.
            priority: 1-5.
        """
        return {"ok": True}

    spec = registry.get("demo_send")
    assert spec and spec.display_name == "demo.send"
    p = spec.parameters
    assert p["required"] == ["to", "subject", "body"], p
    assert p["properties"]["to"]["type"] == "array"
    assert p["properties"]["mode"]["enum"] == ["html", "text"]
    assert "spanning lines" in p["properties"]["body"]["description"]
    assert p["properties"]["cc"]["type"] == "array"
    assert spec.description.startswith("Send a demo message")
    v = spec.validate({"to": ["a@b.c"], "subject": "s", "body": "b", "priority": "4"})
    assert v["priority"] == 4

def test_db_init():
    from app.core.db import init_db, engine
    asyncio.run(init_db())


def test_every_required_param_is_visible_to_the_model():
    """Regression: params named like JSON-schema keywords (e.g. `title`) must stay in `properties`."""
    from app.tools.base import registry
    from app.tools.builtin import load_builtin_tools

    load_builtin_tools()
    assert "title" in registry.get("agent_delegate").parameters["properties"]
    hidden = []
    for name, spec in registry.tools.items():
        props = spec.parameters.get("properties", {})
        for req in spec.parameters.get("required", []):
            if req not in props:
                hidden.append(f"{name}.{req}")
    assert not hidden, hidden


def test_additive_schema_migration(tmp_path):
    """A table created by an older version gains newly added model columns on init."""
    import sqlite3

    import sqlalchemy as sa

    from app.core.db import Base, _add_missing_columns
    from app import models  # noqa: F401

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE artifacts (id VARCHAR(36) PRIMARY KEY, org_id VARCHAR(36), name VARCHAR(300), "
                "path VARCHAR(1000), mime VARCHAR(200), size INTEGER)")
    con.execute("INSERT INTO artifacts VALUES ('a1', 'o1', 'f.txt', 'workspace/f.txt', 'text/plain', 3)")
    con.commit()
    con.close()
    eng = sa.create_engine(f"sqlite:///{path.as_posix()}")
    with eng.begin() as conn:
        added = _add_missing_columns(conn)
    assert "artifacts.document_id" in added and "artifacts.task_id" in added
    with eng.connect() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(artifacts)"))}
    assert {"document_id", "conversation_id", "created_at"} <= cols
    assert Base.metadata.tables["artifacts"] is not None
