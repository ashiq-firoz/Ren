"""Credential broker: agents can see *which* integrations and credentials exist and whether they work — never the
secret values. Integration tools read credentials internally via `ctx.connector(kind)`; nothing here returns them.
"""
from __future__ import annotations

import re
from typing import Any

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.connectors import connector_kinds

_TOKENISH = re.compile(r"[A-Za-z0-9_\-./+=]{24,}")

group = ToolGroup(
    "secrets", "Credential broker",
    "See which integrations and credentials are configured (field names only — never values) and test whether a "
    "connection works. Tools use the credentials internally; agents never see them.",
    prefix="secrets", icon="key-round", category="infrastructure",
)


def redact(text: Any, limit: int = 300) -> str | None:
    """Defensive: mask long token-like strings in free text (e.g. stored connector error messages)."""
    if not text:
        return None
    return H.clip(_TOKENISH.sub("••••", str(text)), limit)


def _filled(d: dict[str, Any]) -> set[str]:
    return {str(k) for k, v in (d or {}).items() if not str(k).startswith("_") and v not in (None, "", [], {})}


@group.tool(risk=Risk.LOW, scope="read")
async def list_metadata(ctx: ToolContext, kind: str | None = None) -> dict:
    """List the workspace's configured integrations: kind, name, status and which credential fields are set
    (names only, never values), plus integrations that could still be connected.

    Args:
        kind: Only show this integration kind, e.g. "github".
    """
    from sqlalchemy import select

    from app.core.security import decrypt_json
    from app.models import Connector, Organization

    async with ctx.session() as db:
        q = select(Connector).where(Connector.org_id == ctx.org_id)
        if kind:
            q = q.where(Connector.kind == kind)
        rows = (await db.execute(q.order_by(Connector.kind, Connector.created_at))).scalars().all()
        org = await db.get(Organization, ctx.org_id)
        org_secret_names = sorted(_filled(decrypt_json(org.secrets_enc))) if org is not None and org.secrets_enc else []

    configured: list[dict] = []
    for r in rows:
        spec = connector_kinds.get(r.kind)
        secret_names = _filled(decrypt_json(r.secrets_enc))
        set_fields = _filled(r.config or {}) | secret_names
        required = [f.key for f in (spec.fields if spec else []) if f.required]
        configured.append(H.compact({
            "id": r.id, "kind": r.kind, "name": r.name, "integration": spec.name if spec else r.kind,
            "status": r.status, "error": redact(r.error, 200), "fields_set": sorted(set_fields),
            "secret_fields_set": sorted(secret_names),
            "missing_required": [k for k in required if k not in set_fields] or None,
            "tool_groups": list(spec.tool_groups) if spec else None, "created_at": H.iso(r.created_at),
        }))
    for k, cfg in (ctx.extra.get("connectors") or {}).items():  # runtime-injected credentials (tests, delegated runs)
        if kind and k != kind:
            continue
        spec = connector_kinds.get(k)
        secret_keys = spec.secret_keys if spec else set()
        names = _filled(cfg)
        configured.append({"kind": k, "name": str(cfg.get("_connector_name") or k), "source": "runtime",
                           "integration": spec.name if spec else k, "fields_set": sorted(names),
                           "secret_fields_set": sorted(n for n in names if n in secret_keys)})
    have = {c["kind"] for c in configured}
    available = [{"kind": s.kind, "name": s.name, "category": s.category}
                 for s in sorted(connector_kinds.values(), key=lambda s: (s.category, s.name)) if s.kind not in have]
    return {"configured": configured, "org_secrets_set": org_secret_names,
            "available_integrations": [] if kind else available,
            "note": "Secret values are never shown to agents; tools use them internally."}


@group.tool(risk=Risk.LOW, scope="read")
async def check(ctx: ToolContext, kind: str, name: str | None = None) -> dict:
    """Check whether an integration is configured and its connection test passes (e.g. before relying on it).

    Args:
        kind: Integration kind, e.g. "slack", "github", "gmail".
        name: A specific connector name when several of the same kind exist.
    """
    spec = connector_kinds.get(kind)
    if spec is None:
        known = ", ".join(sorted(connector_kinds)) or "none"
        raise ToolError(f"Unknown integration '{kind}'. Known kinds: {known}.")
    try:
        cfg = await H.load_connector(ctx, kind, name)
    except ToolError:
        return {"kind": kind, "integration": spec.name, "configured": False,
                "message": f"{spec.name} is not connected — an admin can connect it in Connectors → Integrations."}
    if spec.test is None:
        return {"kind": kind, "integration": spec.name, "configured": True, "tested": False,
                "message": "Configured (no connection test available for this integration)."}
    try:
        ok, message = await spec.test(cfg)
    except Exception as e:  # noqa: BLE001 — tests should never raise, but never leak details if one does
        ok, message = False, f"Connection test failed ({type(e).__name__})."
    return {"kind": kind, "integration": spec.name, "name": cfg.get("_connector_name") or name, "configured": True,
            "ok": bool(ok), "message": redact(message, 400)}
