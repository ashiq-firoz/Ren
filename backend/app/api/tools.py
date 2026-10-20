"""Tool catalog, integrations (connectors) and external MCP servers."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import Ctx, get_ctx, require_admin
from app.core.security import decrypt_json, encrypt_json
from app.models import Connector, McpServer, ToolGroupSetting
from app.services.audit import audit
from app.services.serialize import connector_out, mcp_server_out
from app.tools.base import registry
from app.tools.connectors import connector_kinds
from app.tools.executor import connected_kinds, disabled_groups
from app.tools.mcp_client import PRESETS, mcp_manager

router = APIRouter(prefix="/api", tags=["tools"])


class GroupToggle(BaseModel):
    enabled: bool


class ConnectorIn(BaseModel):
    kind: str
    name: str
    config: dict = {}


class ConnectorPatch(BaseModel):
    name: str | None = None
    config: dict | None = None


class McpIn(BaseModel):
    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None


class McpPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None


def _group_out(g, disabled: set[str], kinds: set[str]) -> dict:
    ok, reason = g.availability()
    connected = (not g.connector_kinds) or bool(set(g.connector_kinds) & kinds)
    return {"id": g.id, "name": g.name, "description": g.description, "icon": g.icon, "category": g.category,
            "connector_kind": g.connector_kind, "connected": connected,
            "enabled": g.id not in disabled, "available": ok, "unavailable_reason": reason,
            "tools": [{"name": t.name, "display_name": t.display_name, "description": t.description.split("\n")[0][:300],
                       "risk": t.risk.value, "scope": t.scope} for t in g.tools]}


@router.get("/tools/catalog")
async def catalog(ctx: Ctx = Depends(get_ctx)):
    disabled = await disabled_groups(ctx.db, ctx.org_id)
    kinds = await connected_kinds(ctx.db, ctx.org_id)
    items = [_group_out(g, disabled, kinds) for g in sorted(registry.groups.values(), key=lambda g: (g.category, g.name))
             if g.tools]
    servers = (await ctx.db.execute(select(McpServer).where(McpServer.org_id == ctx.org_id))).scalars().all()
    for s in servers:
        specs = await mcp_manager.tool_specs(ctx.org_id, s.id)
        status, err = mcp_manager.status(s.id)
        items.append({"id": f"mcp:{s.id}", "name": s.name, "description": f"External MCP server ({s.transport})",
                      "icon": "plug-zap", "category": "mcp", "connector_kind": None,
                      "connected": status == "connected" or bool(s.tools_cache), "enabled": s.enabled,
                      "available": s.status != "error", "unavailable_reason": s.error if s.status == "error" else None,
                      "tools": [{"name": t.name, "display_name": t.display_name, "description": t.description[:300],
                                 "risk": t.risk.value, "scope": t.scope} for t in specs]})
    return {"items": items, "total": len(items)}


@router.patch("/tools/groups/{group_id}")
async def toggle_group(group_id: str, body: GroupToggle, ctx: Ctx = Depends(require_admin)):
    if group_id.startswith("mcp:"):
        s = await ctx.db.get(McpServer, group_id.split(":", 1)[1])
        if s is None or s.org_id != ctx.org_id:
            raise HTTPException(404, "Unknown tool group")
        s.enabled = body.enabled
        await ctx.db.commit()
        if body.enabled:
            await mcp_manager.connect(s.id, wait=False)
        else:
            await mcp_manager.disconnect(s.id)
        return {"id": group_id, "enabled": body.enabled}
    g = registry.group(group_id)
    if g is None:
        raise HTTPException(404, "Unknown tool group")
    row = (await ctx.db.execute(select(ToolGroupSetting).where(ToolGroupSetting.org_id == ctx.org_id,
                                                               ToolGroupSetting.group_id == group_id))).scalars().first()
    if row is None:
        row = ToolGroupSetting(org_id=ctx.org_id, group_id=group_id, enabled=body.enabled)
        ctx.db.add(row)
    else:
        row.enabled = body.enabled
    await ctx.db.flush()
    await audit(ctx.org_id, "tools.group_toggled", user_id=ctx.user_id, target_type="tool_group", target_id=group_id,
                details={"enabled": body.enabled}, db=ctx.db)
    disabled = await disabled_groups(ctx.db, ctx.org_id)
    return _group_out(g, disabled, await connected_kinds(ctx.db, ctx.org_id))


# ------------------------------------------------------------------ connectors
@router.get("/connectors/kinds")
async def kinds(ctx: Ctx = Depends(get_ctx)):
    items = [k.public() for k in sorted(connector_kinds.values(), key=lambda k: (k.category, k.name))]
    return {"items": items, "total": len(items)}


def _split(kind: str, config: dict) -> tuple[dict, dict]:
    spec = connector_kinds.get(kind)
    secret_keys = spec.secret_keys if spec else set()
    plain = {k: v for k, v in config.items() if k not in secret_keys}
    secret = {k: v for k, v in config.items() if k in secret_keys and v not in (None, "", "••••")}
    return plain, secret


def _out(c: Connector) -> dict:
    return connector_out(c, set_secret_keys=set(decrypt_json(c.secrets_enc).keys()))


async def _connector(ctx: Ctx, cid: str) -> Connector:
    c = await ctx.db.get(Connector, cid)
    if c is None or c.org_id != ctx.org_id:
        raise HTTPException(404, "Connector not found")
    return c


@router.get("/connectors")
async def list_connectors(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Connector).where(Connector.org_id == ctx.org_id)
                                 .order_by(Connector.created_at))).scalars().all()
    return {"items": [_out(c) for c in rows], "total": len(rows)}


@router.post("/connectors")
async def create_connector(body: ConnectorIn, ctx: Ctx = Depends(require_admin)):
    spec = connector_kinds.get(body.kind)
    if spec is None:
        raise HTTPException(400, f"Unknown connector kind '{body.kind}'")
    missing = [f.label for f in spec.fields if f.required and not body.config.get(f.key)]
    if missing:
        raise HTTPException(400, "Missing required fields: " + ", ".join(missing))
    plain, secret = _split(body.kind, body.config)
    c = Connector(org_id=ctx.org_id, kind=body.kind, name=body.name.strip()[:200] or spec.name, config=plain,
                  secrets_enc=encrypt_json(secret), created_by=ctx.user_id)
    ctx.db.add(c)
    await ctx.db.flush()
    await audit(ctx.org_id, "connector.created", user_id=ctx.user_id, target_type="connector", target_id=c.id,
                details={"kind": c.kind, "name": c.name}, db=ctx.db)
    return _out(c)


@router.patch("/connectors/{cid}")
async def patch_connector(cid: str, body: ConnectorPatch, ctx: Ctx = Depends(require_admin)):
    c = await _connector(ctx, cid)
    if body.name:
        c.name = body.name.strip()[:200]
    if body.config is not None:
        plain, secret = _split(c.kind, body.config)
        c.config = {**(c.config or {}), **plain}
        merged = decrypt_json(c.secrets_enc)
        merged.update(secret)
        c.secrets_enc = encrypt_json(merged)
        c.status, c.error = "unknown", None
    await audit(ctx.org_id, "connector.updated", user_id=ctx.user_id, target_type="connector", target_id=c.id, db=ctx.db)
    return _out(c)


@router.delete("/connectors/{cid}")
async def delete_connector(cid: str, ctx: Ctx = Depends(require_admin)):
    c = await _connector(ctx, cid)
    await ctx.db.delete(c)
    await audit(ctx.org_id, "connector.deleted", user_id=ctx.user_id, target_type="connector", target_id=cid,
                details={"kind": c.kind}, db=ctx.db)
    return {"ok": True}


@router.post("/connectors/{cid}/test")
async def test_connector(cid: str, ctx: Ctx = Depends(require_admin)):
    c = await _connector(ctx, cid)
    spec = connector_kinds.get(c.kind)
    cfg = {**(c.config or {}), **decrypt_json(c.secrets_enc), "_connector_id": c.id}
    if spec is None or spec.test is None:
        ok, message = True, "Saved. This integration has no automatic connection test."
    else:
        try:
            ok, message = await spec.test(cfg)
        except Exception as e:  # noqa: BLE001
            ok, message = False, f"{e.__class__.__name__}: {e}"[:500]
    c.status = "connected" if ok else "error"
    c.error = None if ok else message
    return {"ok": ok, "message": message}


# ------------------------------------------------------------------ MCP servers
async def _server(ctx: Ctx, sid: str) -> McpServer:
    s = await ctx.db.get(McpServer, sid)
    if s is None or s.org_id != ctx.org_id:
        raise HTTPException(404, "MCP server not found")
    return s


def _server_out(s: McpServer) -> dict:
    out = mcp_server_out(s)
    status, err = mcp_manager.status(s.id)
    if status == "connected":
        out["status"], out["error"] = "connected", None
    elif err:
        out["status"], out["error"] = "error", err
    return out


@router.get("/mcp/servers")
async def list_servers(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(McpServer).where(McpServer.org_id == ctx.org_id)
                                 .order_by(McpServer.created_at))).scalars().all()
    return {"items": [_server_out(s) for s in rows], "total": len(rows)}


@router.get("/mcp/presets")
async def presets(ctx: Ctx = Depends(get_ctx)):
    return {"items": PRESETS, "total": len(PRESETS)}


@router.post("/mcp/servers")
async def create_server(body: McpIn, ctx: Ctx = Depends(require_admin)):
    if body.transport not in ("stdio", "http", "sse"):
        raise HTTPException(400, "transport must be stdio, http or sse")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(400, "command is required for stdio servers")
    if body.transport in ("http", "sse") and not (body.url or "").startswith(("http://", "https://")):
        raise HTTPException(400, "A valid http(s) url is required")
    s = McpServer(org_id=ctx.org_id, name=body.name.strip()[:200], transport=body.transport, command=body.command,
                  args=body.args or [], url=body.url, env_enc=encrypt_json(body.env or {}),
                  headers_enc=encrypt_json(body.headers or {}), env_keys=sorted((body.env or {}).keys()),
                  header_keys=sorted((body.headers or {}).keys()), enabled=True, status="disconnected")
    ctx.db.add(s)
    await ctx.db.flush()
    await audit(ctx.org_id, "mcp.server_added", user_id=ctx.user_id, target_type="mcp_server", target_id=s.id,
                details={"name": s.name, "transport": s.transport}, db=ctx.db)
    await ctx.db.commit()
    await mcp_manager.connect(s.id, wait=True)
    await ctx.db.refresh(s)
    return _server_out(s)


@router.patch("/mcp/servers/{sid}")
async def patch_server(sid: str, body: McpPatch, ctx: Ctx = Depends(require_admin)):
    s = await _server(ctx, sid)
    data = body.model_dump(exclude_unset=True)
    for k in ("name", "enabled", "command", "args", "url"):
        if k in data and data[k] is not None:
            setattr(s, k, data[k])
    if data.get("env") is not None:
        merged = decrypt_json(s.env_enc)
        merged.update({k: v for k, v in data["env"].items() if v not in ("", "••••")})
        s.env_enc, s.env_keys = encrypt_json(merged), sorted(merged)
    if data.get("headers") is not None:
        merged = decrypt_json(s.headers_enc)
        merged.update({k: v for k, v in data["headers"].items() if v not in ("", "••••")})
        s.headers_enc, s.header_keys = encrypt_json(merged), sorted(merged)
    await ctx.db.commit()
    if s.enabled:
        await mcp_manager.connect(s.id, wait=True)
    else:
        await mcp_manager.disconnect(s.id)
        s.status = "disconnected"
        await ctx.db.commit()
    await ctx.db.refresh(s)
    return _server_out(s)


@router.delete("/mcp/servers/{sid}")
async def delete_server(sid: str, ctx: Ctx = Depends(require_admin)):
    s = await _server(ctx, sid)
    await mcp_manager.disconnect(s.id)
    await ctx.db.delete(s)
    await audit(ctx.org_id, "mcp.server_removed", user_id=ctx.user_id, target_type="mcp_server", target_id=sid, db=ctx.db)
    return {"ok": True}


@router.post("/mcp/servers/{sid}/refresh")
async def refresh_server(sid: str, ctx: Ctx = Depends(require_admin)):
    s = await _server(ctx, sid)
    await ctx.db.commit()
    await mcp_manager.connect(s.id, wait=True)
    await ctx.db.refresh(s)
    return _server_out(s)
