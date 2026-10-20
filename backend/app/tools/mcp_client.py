"""External MCP servers (stdio / Streamable HTTP / SSE) as tool groups.

Each connected server runs in its own long-lived asyncio task that owns the MCP client context (anyio scopes
must be entered and exited in the same task). Calls are queued to that task and served concurrently.
Tools are namespaced `mcp_<server>_<tool>` and exposed to agents as group `mcp:<server_id>`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select

from app.core.config import VERSION
from app.core.db import session_scope
from app.core.security import decrypt_json
from app.tools.base import Risk, ToolContext, ToolError, ToolSpec

log = logging.getLogger("ren.mcp")

PRESETS: list[dict] = [
    {"id": "playwright", "name": "Playwright browser", "description": "Microsoft's Playwright MCP: full browser automation via accessibility snapshots.",
     "transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"], "env_fields": [],
     "docs_url": "https://github.com/microsoft/playwright-mcp"},
    {"id": "filesystem", "name": "Filesystem (reference)", "description": "Reference filesystem server scoped to a directory.",
     "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/shared"],
     "env_fields": [], "docs_url": "https://github.com/modelcontextprotocol/servers"},
    {"id": "github", "name": "GitHub (official)", "description": "GitHub's official MCP server (remote).",
     "transport": "http", "url": "https://api.githubcopilot.com/mcp/",
     "env_fields": [{"key": "Authorization", "label": "Authorization header", "type": "password", "required": True,
                     "secret": True, "placeholder": "Bearer ghp_…", "help": "Sent as an HTTP header"}],
     "docs_url": "https://github.com/github/github-mcp-server"},
    {"id": "notion", "name": "Notion (official)", "description": "Notion's hosted MCP server.",
     "transport": "http", "url": "https://mcp.notion.com/mcp", "env_fields": [], "docs_url": "https://developers.notion.com/docs/mcp"},
    {"id": "linear", "name": "Linear (official)", "description": "Linear's hosted MCP server.",
     "transport": "http", "url": "https://mcp.linear.app/mcp", "env_fields": [], "docs_url": "https://linear.app/docs/mcp"},
    {"id": "atlassian", "name": "Atlassian (Jira & Confluence)", "description": "Atlassian Remote MCP server.",
     "transport": "http", "url": "https://mcp.atlassian.com/v1/sse", "env_fields": [],
     "docs_url": "https://www.atlassian.com/platform/remote-mcp-server"},
    {"id": "sentry", "name": "Sentry", "description": "Errors and performance issues from Sentry.",
     "transport": "http", "url": "https://mcp.sentry.dev/mcp", "env_fields": [], "docs_url": "https://docs.sentry.io/product/sentry-mcp/"},
    {"id": "stripe", "name": "Stripe", "description": "Stripe's MCP server (payments data; keep it read-only).",
     "transport": "http", "url": "https://mcp.stripe.com",
     "env_fields": [{"key": "Authorization", "label": "Authorization header", "type": "password", "required": True,
                     "secret": True, "placeholder": "Bearer rk_live_…"}], "docs_url": "https://docs.stripe.com/mcp"},
    {"id": "aws", "name": "AWS API (awslabs)", "description": "AWS Labs MCP server for AWS APIs.",
     "transport": "stdio", "command": "uvx", "args": ["awslabs.aws-api-mcp-server@latest"],
     "env_fields": [{"key": "AWS_ACCESS_KEY_ID", "label": "AWS access key id", "type": "text", "required": True, "secret": False},
                    {"key": "AWS_SECRET_ACCESS_KEY", "label": "AWS secret access key", "type": "password", "required": True, "secret": True},
                    {"key": "AWS_REGION", "label": "Region", "type": "text", "required": True, "secret": False, "placeholder": "us-east-1"}],
     "docs_url": "https://github.com/awslabs/mcp"},
    {"id": "azure", "name": "Azure (official)", "description": "Microsoft's Azure MCP server.",
     "transport": "stdio", "command": "npx", "args": ["-y", "@azure/mcp@latest", "server", "start"],
     "env_fields": [{"key": "AZURE_TENANT_ID", "label": "Tenant id", "type": "text", "required": True, "secret": False},
                    {"key": "AZURE_CLIENT_ID", "label": "Client id", "type": "text", "required": True, "secret": False},
                    {"key": "AZURE_CLIENT_SECRET", "label": "Client secret", "type": "password", "required": True, "secret": True}],
     "docs_url": "https://github.com/Azure/azure-mcp"},
    {"id": "laya", "name": "Laya decisions", "description": "Laya's MCP server — fast calibrated decisions (routing, triage, guardrails).",
     "transport": "stdio", "command": "laya-mcp-server", "args": [], "env_fields": [
         {"key": "LAYA_BASE_URL", "label": "Laya server URL", "type": "url", "required": False, "secret": False,
          "placeholder": "http://laya:8000"}], "docs_url": "https://github.com/NandhaKishorM/laya"},
]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:20] or "server"


def _tool_name(server_name: str, tool: str) -> str:
    name = f"mcp_{_slug(server_name)}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', tool)}"
    return name[:64]


def _result_text(res: Any) -> tuple[str, bool]:
    parts: list[str] = []
    for c in getattr(res, "content", None) or []:
        ctype = getattr(c, "type", None)
        if ctype == "text":
            parts.append(getattr(c, "text", ""))
        elif ctype == "image":
            parts.append("[image content returned]")
        elif ctype == "resource":
            r = getattr(c, "resource", None)
            parts.append(getattr(r, "text", None) or f"[resource {getattr(r, 'uri', '')}]")
        else:
            try:
                parts.append(json.dumps(c.model_dump(mode="json"))[:2000])
            except Exception:  # noqa: BLE001
                parts.append(str(c)[:2000])
    sc = getattr(res, "structured_content", None)
    if sc and not parts:
        parts.append(json.dumps(sc, ensure_ascii=False, default=str))
    return "\n".join(parts) or "(no content)", bool(getattr(res, "is_error", False))


class McpConnection:
    def __init__(self, server_id: str, name: str, transport: str, command: str | None, args: list[str],
                 url: str | None, env: dict[str, str], headers: dict[str, str]):
        self.server_id = server_id
        self.name = name
        self.transport = transport
        self.command = command
        self.args = args
        self.url = url
        self.env = env
        self.headers = headers
        self.ready = asyncio.Event()
        self.error: str | None = None
        self.tools: list[dict] = []
        self._requests: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run(), name=f"mcp:{server_id}")

    def _target(self):
        from mcp import StdioServerParameters

        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio server requires a command")
            return StdioServerParameters(command=self.command, args=list(self.args or []), env=self.env or None)
        if self.transport == "sse":
            from mcp.client.sse import sse_client

            return sse_client(self.url, headers=self.headers or None)
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        if self.headers:
            return streamable_http_client(self.url, http_client=create_mcp_http_client(headers=self.headers))
        return self.url

    async def _run(self) -> None:
        from mcp import Client
        from mcp_types import Implementation

        try:
            async with Client(self._target(), read_timeout_seconds=120,
                              client_info=Implementation(name="ren", version=VERSION)) as client:
                listed = await client.list_tools()
                tools = list(listed.tools)
                cursor = getattr(listed, "next_cursor", None)
                while cursor:
                    more = await client.list_tools(cursor=cursor)
                    tools.extend(more.tools)
                    cursor = getattr(more, "next_cursor", None)
                self.tools = [self._tool_dict(t) for t in tools]
                self.ready.set()
                while True:
                    op, payload, fut = await self._requests.get()
                    if op == "close":
                        if fut and not fut.done():
                            fut.set_result(True)
                        break
                    asyncio.create_task(self._serve(client, op, payload, fut))
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001  (anyio ExceptionGroups)
            self.error = _flatten_error(e)
            log.warning("MCP server %s failed: %s", self.name, self.error)
        finally:
            self.ready.set()
            while not self._requests.empty():
                _op, _p, fut = self._requests.get_nowait()
                if fut and not fut.done():
                    fut.set_exception(ToolError(f"MCP server '{self.name}' is disconnected"))

    @staticmethod
    def _tool_dict(t: Any) -> dict:
        ann = getattr(t, "annotations", None)
        schema = getattr(t, "input_schema", None) or {"type": "object", "properties": {}}
        return {"name": t.name, "description": (t.description or getattr(t, "title", None) or t.name)[:1024],
                "input_schema": schema if isinstance(schema, dict) else dict(schema),
                "read_only": bool(getattr(ann, "read_only_hint", False)) if ann else False,
                "destructive": bool(getattr(ann, "destructive_hint", False)) if ann else False}

    async def _serve(self, client, op: str, payload: dict, fut: asyncio.Future) -> None:
        try:
            if op == "call":
                res = await client.call_tool(payload["name"], payload.get("arguments") or {})
                fut.set_result(res)
        except BaseException as e:  # noqa: BLE001
            if not fut.done():
                fut.set_exception(ToolError(f"MCP call failed: {_flatten_error(e)}"))

    @property
    def alive(self) -> bool:
        return not self._task.done() and self.error is None

    async def call(self, name: str, arguments: dict, timeout: float = 180) -> Any:
        await asyncio.wait_for(self.ready.wait(), timeout=60)
        if not self.alive:
            raise ToolError(f"MCP server '{self.name}' is not connected: {self.error}")
        fut = asyncio.get_running_loop().create_future()
        await self._requests.put(("call", {"name": name, "arguments": arguments}, fut))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def close(self) -> None:
        if self._task.done():
            return
        fut = asyncio.get_running_loop().create_future()
        await self._requests.put(("close", None, fut))
        try:
            await asyncio.wait_for(fut, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._task.cancel()


def _flatten_error(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(_flatten_error(s) for s in subs)[:800]
    return f"{e.__class__.__name__}: {e}"[:800]


class McpManager:
    def __init__(self) -> None:
        self._conns: dict[str, McpConnection] = {}
        self._lock = asyncio.Lock()

    async def connect(self, server_id: str, wait: bool = True) -> McpConnection | None:
        from app.models import McpServer

        async with session_scope() as db:
            s = await db.get(McpServer, server_id)
            if s is None or not s.enabled:
                return None
            params = dict(name=s.name, transport=s.transport, command=s.command, args=list(s.args or []), url=s.url,
                          env=decrypt_json(s.env_enc), headers=decrypt_json(s.headers_enc))
        async with self._lock:
            old = self._conns.pop(server_id, None)
            if old is not None:
                await old.close()
            conn = McpConnection(server_id, **params)
            self._conns[server_id] = conn
        if wait:
            try:
                await asyncio.wait_for(conn.ready.wait(), timeout=90)
            except asyncio.TimeoutError:
                conn.error = conn.error or "Timed out connecting to the MCP server"
            await self._persist_status(server_id, conn)
        else:
            asyncio.create_task(self._persist_when_ready(server_id, conn))
        return conn

    async def _persist_when_ready(self, server_id: str, conn: McpConnection) -> None:
        try:
            await asyncio.wait_for(conn.ready.wait(), timeout=120)
        except asyncio.TimeoutError:
            conn.error = conn.error or "Timed out connecting to the MCP server"
        await self._persist_status(server_id, conn)

    async def _persist_status(self, server_id: str, conn: McpConnection) -> None:
        from app.models import McpServer

        async with session_scope() as db:
            s = await db.get(McpServer, server_id)
            if s is None:
                return
            if conn.alive and conn.ready.is_set():
                s.status, s.error, s.tools_cache = "connected", None, conn.tools
            else:
                s.status, s.error = "error", conn.error or "Not connected"

    async def disconnect(self, server_id: str) -> None:
        async with self._lock:
            conn = self._conns.pop(server_id, None)
        if conn is not None:
            await conn.close()

    async def tool_specs(self, org_id: str, server_id: str) -> list[ToolSpec]:
        from app.models import McpServer

        async with session_scope() as db:
            s = await db.get(McpServer, server_id)
            if s is None or s.org_id != org_id or not s.enabled:
                return []
            name, cached = s.name, list(s.tools_cache or [])
        conn = self._conns.get(server_id)
        tools = conn.tools if conn is not None and conn.tools else cached
        specs = []
        for t in tools:
            specs.append(self._spec(server_id, name, t))
        return specs

    def _spec(self, server_id: str, server_name: str, t: dict) -> ToolSpec:
        original = t["name"]
        risk = Risk.LOW if t.get("read_only") else (Risk.HIGH if t.get("destructive") else Risk.MEDIUM)

        async def handler(ctx: ToolContext, **kwargs: Any) -> str:
            conn = self._conns.get(server_id)
            if conn is None or not conn.alive:
                conn = await self.connect(server_id)
            if conn is None:
                raise ToolError(f"MCP server '{server_name}' is disabled.")
            res = await conn.call(original, kwargs)
            text, is_error = _result_text(res)
            if is_error:
                raise ToolError(text[:4000])
            return text

        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        return ToolSpec(name=_tool_name(server_name, original), group=f"mcp:{server_id}",
                        description=f"[{server_name}] {t.get('description') or original}"[:1024], parameters=schema,
                        handler=handler, risk=risk, scope="read" if t.get("read_only") else "write",
                        display_name=f"{_slug(server_name)}.{original}", args_model=None, external=True)

    def status(self, server_id: str) -> tuple[str, str | None]:
        conn = self._conns.get(server_id)
        if conn is None:
            return "disconnected", None
        if conn.alive and conn.ready.is_set():
            return "connected", None
        return ("error", conn.error) if conn.error else ("disconnected", None)

    async def startup(self) -> None:
        from app.models import McpServer

        async with session_scope() as db:
            ids = (await db.execute(select(McpServer.id).where(McpServer.enabled.is_(True)))).scalars().all()
        for sid in ids:
            try:
                await self.connect(sid, wait=False)
            except Exception:  # noqa: BLE001
                log.exception("MCP startup connect failed for %s", sid)

    async def shutdown(self) -> None:
        for sid in list(self._conns):
            await self.disconnect(sid)


mcp_manager = McpManager()
