"""Approval gate, policy pre-check on outbound content, external MCP servers, Ren's MCP endpoint."""
import asyncio
import sys
from pathlib import Path

import pytest

from app.tools.base import Risk, ToolContext, ToolGroup, registry

_gov = ToolGroup("test_gov", "Test governance", "test-only tools", prefix="testgov")


@_gov.tool(risk=Risk.HIGH, scope="write", content_fields=("body",))
async def send(ctx: ToolContext, to: str, body: str) -> str:
    """Pretend to send a message.

    Args:
        to: Recipient.
        body: Message body.
    """
    return f"sent to {to}"


def test_approval_gate_approve_and_reject(org_ctx):
    from app.agents.approvals import broker
    from app.core.db import session_scope
    from app.models import Approval
    from app.tools.executor import execute
    from sqlalchemy import select

    async def scenario(decision: bool):
        broker.reset()
        ctx = ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="owner", agent_name="Tester")
        spec = registry.get("testgov_send")
        run = asyncio.create_task(execute(ctx, spec, spec.name, {"to": "cfo@acme.com", "body": "Q3 numbers attached"},
                                          autonomy="balanced", call_id=f"call-{decision}"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with session_scope() as db:
                appr = (await db.execute(select(Approval).where(Approval.call_id == f"call-{decision}"))).scalars().first()
            if appr is not None:
                break
        assert appr is not None and appr.status == "pending" and appr.risk == "high"
        assert "cfo@acme.com" in appr.preview
        await broker.decide(appr.id, approve=decision, user_id=org_ctx["user_id"], comment="ok" if decision else "no")
        return await asyncio.wait_for(run, 10)

    res = asyncio.run(scenario(True))
    assert res.ok and res.text == "sent to cfo@acme.com" and res.approval == "approved"
    res = asyncio.run(scenario(False))
    assert not res.ok and res.approval == "rejected"


def test_autonomous_skips_approval_and_policy_blocks_outbound(org_ctx):
    from app.core.db import session_scope
    from app.models import Policy
    from app.tools.executor import execute

    async def scenario():
        async with session_scope() as db:
            db.add(Policy(org_id=org_ctx["org_id"], name="No secrets out", rules=["Never send API keys."],
                          patterns=["@secret:api_key"], severity="block", scope="org", applies_to="outbound"))
        ctx = ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="owner")
        spec = registry.get("testgov_send")
        ok = await execute(ctx, spec, spec.name, {"to": "a@b.c", "body": "hello"}, autonomy="autonomous")
        blocked = await execute(ctx, spec, spec.name, {"to": "a@b.c", "body": "key sk-abcdefghijklmnopqrstuvwxyz123456"},
                                autonomy="autonomous")
        return ok, blocked

    ok, blocked = asyncio.run(scenario())
    assert ok.ok and ok.approval is None
    assert not blocked.ok and "Blocked by workspace policy" in blocked.text


def test_viewer_cannot_use_write_tools(org_ctx):
    from app.tools.executor import execute

    ctx = ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="viewer")
    spec = registry.get("testgov_send")
    res = asyncio.run(execute(ctx, spec, spec.name, {"to": "x", "body": "y"}, autonomy="autonomous"))
    assert not res.ok and "role" in res.text


def test_external_stdio_mcp_server(org_ctx):
    from app.core.db import session_scope
    from app.models import McpServer
    from app.tools.base import ToolContext
    from app.tools.executor import execute, resolve_tools
    from app.tools.mcp_client import mcp_manager

    script = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")

    async def scenario():
        async with session_scope() as db:
            s = McpServer(org_id=org_ctx["org_id"], name="Echo Test", transport="stdio", command=sys.executable,
                          args=[script], enabled=True)
            db.add(s)
            await db.flush()
            sid = s.id
        conn = await mcp_manager.connect(sid, wait=True)
        assert conn is not None and conn.alive, conn.error if conn else "no conn"
        async with session_scope() as db:
            tools = await resolve_tools(db, org_ctx["org_id"], [f"mcp:{sid}"], "owner")
        names = sorted(tools)
        echo = next(t for n, t in tools.items() if n.endswith("_echo"))
        assert echo.risk == Risk.LOW and echo.scope == "read"
        ctx = ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="owner")
        res = await execute(ctx, echo, echo.name, {"text": "hello swarm"}, autonomy="balanced")
        add = next(t for n, t in tools.items() if n.endswith("_add"))
        res2 = await execute(ctx, add, add.name, {"a": 2, "b": 40}, autonomy="autonomous")
        await mcp_manager.shutdown()
        return names, res, res2

    names, res, res2 = asyncio.run(scenario())
    assert any(n.startswith("mcp_echo_test_") for n in names)
    assert res.ok and "HELLO SWARM" in res.text
    assert res2.ok and "42" in res2.text


def test_ren_as_mcp_server_live(db_ready):
    """External MCP clients can list and call Ren's governed tools over Streamable HTTP with a bearer token."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from app.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/auth/register",
                       json={"email": "mcp@ren.dev", "password": "supersecret1", "name": "Mo", "mode": "personal"})
        d = r.json()
        headers = {"Authorization": f"Bearer {d['access_token']}", "X-Org-Id": d["orgs"][0]["id"]}

        async def client_session():
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client

            transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp",
                                               http_client=create_mcp_http_client(headers=headers))
            async with Client(transport) as c:
                tools = (await c.list_tools()).tools
                names = {t.name for t in tools}
                res = await c.call_tool("identity_whoami", {})
                text = "".join(getattr(p, "text", "") for p in res.content)
                return names, text

        names, text = asyncio.run(client_session())
        assert "identity_whoami" in names and "memory_search" in names
        assert "Mo" in text and "owner" in text

        async def unauth():
            from mcp import Client

            async with Client(f"http://127.0.0.1:{port}/mcp") as c:
                return (await c.list_tools()).tools

        assert asyncio.run(unauth()) == []
    finally:
        server.should_exit = True
        th.join(timeout=20)
