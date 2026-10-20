"""LLM providers (OpenRouter, Google AI Studio, vLLM, OpenAI, custom OpenAI-compatible, mock)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import Ctx, get_ctx, require_admin
from app.core.security import decrypt_secret, encrypt_secret
from app.llm.client import LLMClient, LLMError
from app.llm.providers import DEFAULT_BASE_URLS, client_from_provider
from app.models import Provider
from app.services.audit import audit
from app.services.serialize import provider_out

router = APIRouter(prefix="/api/providers", tags=["providers"])

KINDS = ("openrouter", "google", "vllm", "openai", "custom", "mock")


class ProviderIn(BaseModel):
    name: str
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    default_model: str
    is_default: bool = False
    supports_tools: bool = True
    extra: dict | None = None


class ProviderPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    is_default: bool | None = None
    supports_tools: bool | None = None
    extra: dict | None = None


class ModelsPreviewIn(BaseModel):
    """List models for unsaved settings (the Add/Edit dialog); `provider_id` supplies the stored key if none is typed."""
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    provider_id: str | None = None


async def _get(ctx: Ctx, pid: str) -> Provider:
    p = await ctx.db.get(Provider, pid)
    if p is None or p.org_id != ctx.org_id:
        raise HTTPException(404, "Provider not found")
    return p


async def _set_default(ctx: Ctx, p: Provider) -> None:
    for other in (await ctx.db.execute(select(Provider).where(Provider.org_id == ctx.org_id))).scalars().all():
        other.is_default = other.id == p.id
    ctx.org.settings = {**(ctx.org.settings or {}), "default_provider_id": p.id}


@router.get("")
async def list_providers(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Provider).where(Provider.org_id == ctx.org_id)
                                 .order_by(Provider.created_at))).scalars().all()
    return {"items": [provider_out(p) for p in rows], "total": len(rows)}


@router.post("")
async def create_provider(body: ProviderIn, ctx: Ctx = Depends(require_admin)):
    if body.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(KINDS)}")
    base = (body.base_url or DEFAULT_BASE_URLS.get(body.kind) or "").rstrip("/")
    if body.kind in ("vllm", "custom") and not body.base_url:
        raise HTTPException(400, "base_url is required for vLLM / custom providers (e.g. http://host:8000/v1)")
    p = Provider(org_id=ctx.org_id, name=body.name.strip()[:200], kind=body.kind, base_url=base,
                 api_key_enc=encrypt_secret(body.api_key), default_model=body.default_model.strip(),
                 supports_tools=body.supports_tools, extra=body.extra or {})
    ctx.db.add(p)
    await ctx.db.flush()
    first = len((await ctx.db.execute(select(Provider.id).where(Provider.org_id == ctx.org_id))).all()) == 1
    if body.is_default or first:
        await _set_default(ctx, p)
    await audit(ctx.org_id, "provider.created", user_id=ctx.user_id, target_type="provider", target_id=p.id,
                details={"kind": p.kind, "model": p.default_model}, db=ctx.db)
    return provider_out(p)


@router.patch("/{pid}")
async def patch_provider(pid: str, body: ProviderPatch, ctx: Ctx = Depends(require_admin)):
    p = await _get(ctx, pid)
    data = body.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] not in KINDS:
        raise HTTPException(400, "invalid kind")
    if data.get("kind") and data["kind"] != p.kind:
        # Switching type: a blank endpoint moves to the new type's default; drop OpenRouter-only extras.
        if not data.get("base_url"):
            if data["kind"] in ("vllm", "custom"):
                raise HTTPException(400, "base_url is required for vLLM / custom providers (e.g. http://host:8000/v1)")
            p.base_url = DEFAULT_BASE_URLS.get(data["kind"], "")
        if data["kind"] != "openrouter" and "extra" not in data:
            p.extra = {k: v for k, v in (p.extra or {}).items() if k not in ("fallback_models", "reasoning_effort")}
    for k in ("name", "kind", "default_model", "supports_tools", "extra"):
        if k in data and data[k] is not None:
            setattr(p, k, data[k])
    if "base_url" in data and data["base_url"]:
        p.base_url = data["base_url"].rstrip("/")
    if "api_key" in data:
        p.api_key_enc = encrypt_secret(data["api_key"]) if data["api_key"] else None
    if data.get("is_default"):
        await _set_default(ctx, p)
    await audit(ctx.org_id, "provider.updated", user_id=ctx.user_id, target_type="provider", target_id=p.id, db=ctx.db)
    return provider_out(p)


@router.delete("/{pid}")
async def delete_provider(pid: str, ctx: Ctx = Depends(require_admin)):
    p = await _get(ctx, pid)
    await ctx.db.delete(p)
    if (ctx.org.settings or {}).get("default_provider_id") == pid:
        ctx.org.settings = {**ctx.org.settings, "default_provider_id": None}
    await audit(ctx.org_id, "provider.deleted", user_id=ctx.user_id, target_type="provider", target_id=pid, db=ctx.db)
    return {"ok": True}


@router.post("/{pid}/test")
async def test_provider(pid: str, ctx: Ctx = Depends(get_ctx)):
    p = await _get(ctx, pid)
    client = client_from_provider(p)
    t0 = time.perf_counter()
    try:
        res = await client.chat([{"role": "system", "content": "You are a connectivity check."},
                                 {"role": "user", "content": "Reply with exactly: Ren is online."}],
                                temperature=0, max_tokens=20, timeout=45)
        return {"ok": True, "latency_ms": int((time.perf_counter() - t0) * 1000), "model": res.model or p.default_model,
                "sample": (res.content or "").strip()[:200], "error": None}
    except LLMError as e:
        return {"ok": False, "latency_ms": int((time.perf_counter() - t0) * 1000), "model": p.default_model,
                "sample": None, "error": str(e)[:500]}


@router.post("/models/preview")
async def preview_models(body: ModelsPreviewIn, ctx: Ctx = Depends(require_admin)):
    if body.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(KINDS)}")
    if body.kind == "mock":
        return {"items": [{"id": "mock-1", "name": "mock-1", "context_length": None}], "total": 1}
    base = (body.base_url or DEFAULT_BASE_URLS.get(body.kind) or "").rstrip("/")
    if not base:
        raise HTTPException(400, "Enter the base URL first")
    key = body.api_key or None
    if not key and body.provider_id:
        stored = await _get(ctx, body.provider_id)
        if stored.kind == body.kind:  # never send one vendor's key to another vendor
            key = decrypt_secret(stored.api_key_enc)
    client = LLMClient(kind=body.kind, base_url=base, api_key=key, model="", name=body.kind)
    try:
        models = await client.list_models()
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    models.sort(key=lambda m: m.get("id") or "")
    return {"items": models, "total": len(models)}


@router.get("/{pid}/models")
async def provider_models(pid: str, ctx: Ctx = Depends(get_ctx)):
    p = await _get(ctx, pid)
    try:
        models = await client_from_provider(p).list_models()
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    models.sort(key=lambda m: m.get("id") or "")
    return {"items": models, "total": len(models)}
