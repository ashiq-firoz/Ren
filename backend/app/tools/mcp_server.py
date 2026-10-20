"""Ren's own tools exposed as an MCP server (Streamable HTTP, stateless) at /mcp.

External MCP clients (Claude Desktop, IDEs, other agent runtimes) can use the workspace's governed toolbox:
the same permission checks, policy guard, approvals and audit trail apply. Auth: `Authorization: Bearer <Ren token>`
and `X-Org-Id` (optional when the user belongs to exactly one workspace).
"""
from __future__ import annotations

import logging
from typing import Any

import mcp_types as types
from sqlalchemy import select

from app.core.config import VERSION
from app.core.db import session_scope
from app.core.deps import _bearer, user_from_token
from app.models import Membership, Organization
from app.tools.base import ToolContext, registry
from app.tools.executor import disabled_groups, execute, scope_allowed

log = logging.getLogger("ren.mcp_server")

EXCLUDED_GROUPS = {"computer"}


async def _auth(request: Any) -> tuple[str, str, str, str] | None:
    """Returns (user_id, org_id, role, org_mode) or None."""
    if request is None:
        return None
    headers = request.headers
    async with session_scope() as db:
        user = await user_from_token(db, _bearer(headers.get("authorization")))
        if user is None:
            return None
        org_id = headers.get("x-org-id")
        q = select(Membership, Organization).join(Organization, Organization.id == Membership.org_id).where(
            Membership.user_id == user.id)
        if org_id:
            q = q.where(Membership.org_id == org_id)
        rows = (await db.execute(q)).all()
        if len(rows) != 1:
            return None
        m, org = rows[0]
        return user.id, org.id, m.role, org.mode


async def _available(org_id: str, role: str) -> dict:
    async with session_scope() as db:
        disabled = await disabled_groups(db, org_id)
    out = {}
    for g in registry.groups.values():
        if g.id in disabled or g.id in EXCLUDED_GROUPS or not g.availability()[0]:
            continue
        for spec in g.tools:
            if scope_allowed(spec, role):
                out[spec.name] = spec
    return out


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    auth = await _auth(getattr(ctx, "request", None))
    if auth is None:
        return types.ListToolsResult(tools=[])
    _uid, org_id, role, _mode = auth
    tools = []
    for spec in (await _available(org_id, role)).values():
        tools.append(types.Tool(
            name=spec.name, description=spec.description, input_schema=spec.parameters,
            annotations=types.ToolAnnotations(title=spec.display_name, read_only_hint=spec.scope == "read",
                                              destructive_hint=spec.risk.value in ("high", "critical"))))
    return types.ListToolsResult(tools=tools)


async def on_call_tool(ctx, params) -> types.CallToolResult:
    auth = await _auth(getattr(ctx, "request", None))
    if auth is None:
        return types.CallToolResult(content=[types.TextContent(type="text", text="Unauthorized")], is_error=True)
    uid, org_id, role, mode = auth
    tools = await _available(org_id, role)
    spec = tools.get(params.name)
    tctx = ToolContext(org_id=org_id, user_id=uid, role=role, agent_name="MCP client", org_mode=mode)
    res = await execute(tctx, spec, params.name, dict(params.arguments or {}), autonomy="balanced")
    return types.CallToolResult(content=[types.TextContent(type="text", text=res.text)], is_error=not res.ok)


def build_mcp_app():
    """Returns (session_manager, asgi_app). Run `session_manager.run()` in the app lifespan."""
    from mcp.server.lowlevel.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = Server("ren", version=VERSION, title="Ren by f69 Labs",
                    instructions="Governed toolbox of a Ren workspace: files, documents, data, web, knowledge base "
                                 "(STAIR retrieval) and connected business apps. Risky actions require human approval.",
                    on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    async def asgi(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    return manager, asgi
