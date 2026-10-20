"""Resolve which LLM endpoint/model serves an org, an agent or a subsystem."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_secret
from app.llm.client import LLMClient, NoProviderError
from app.llm.mock import MockLLM

DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "vllm": "http://localhost:8000/v1",
    "mock": "mock://local",
}


def client_from_provider(p: Any, model: str | None = None) -> LLMClient:
    """Build a client from a Provider row."""
    if p.kind == "mock":
        return MockLLM(model=model or p.default_model or "mock-1", provider_id=p.id)
    return LLMClient(
        kind=p.kind,
        base_url=p.base_url or DEFAULT_BASE_URLS.get(p.kind, ""),
        api_key=decrypt_secret(p.api_key_enc),
        model=model or p.default_model,
        supports_tools=p.supports_tools,
        extra=p.extra or {},
        name=p.name,
        provider_id=p.id,
    )


def env_fallback_models() -> list[str]:
    return [m.strip() for m in (settings.openrouter_fallback_models or "").split(",") if m.strip()]


def env_client(model: str | None = None) -> LLMClient | None:
    """Provider bootstrapped from environment variables (used before an org configures one)."""
    if settings.vllm_base_url:
        return LLMClient(kind="vllm", base_url=settings.vllm_base_url, api_key=settings.vllm_api_key,
                         model=model or settings.vllm_model or "default", name="vLLM (env)")
    if settings.google_ai_studio_key:
        return LLMClient(kind="google", base_url=DEFAULT_BASE_URLS["google"], api_key=settings.google_ai_studio_key,
                         model=model or settings.google_ai_studio_model, name="Google AI Studio (env)")
    if settings.openrouter_api_key:
        return LLMClient(kind="openrouter", base_url=DEFAULT_BASE_URLS["openrouter"],
                         api_key=settings.openrouter_api_key, model=model or settings.openrouter_model,
                         name="OpenRouter (env)", extra={"fallback_models": env_fallback_models()})
    if settings.openai_api_key:
        return LLMClient(kind="openai", base_url=DEFAULT_BASE_URLS["openai"], api_key=settings.openai_api_key,
                         model=model or settings.openai_model, name="OpenAI (env)")
    return None


async def get_llm(
    db: AsyncSession,
    org_id: str,
    *,
    agent: Any = None,
    provider_id: str | None = None,
    model: str | None = None,
    purpose: str = "agent",
) -> LLMClient:
    """Pick the endpoint for a request.

    Order: explicit provider → agent's provider → org default → any org provider → environment.
    Model: explicit → agent.model → (orchestrator: org orchestrator_model) → provider default.
    """
    from app.models import Organization, Provider

    org = await db.get(Organization, org_id)
    org_settings = (org.settings or {}) if org else {}
    pid = provider_id or (agent.provider_id if agent is not None else None) or org_settings.get("default_provider_id")
    prov = None
    if pid:
        prov = await db.get(Provider, pid)
        if prov is not None and prov.org_id != org_id:
            prov = None
    if prov is None:
        rows = (await db.execute(
            select(Provider).where(Provider.org_id == org_id).order_by(Provider.is_default.desc(), Provider.created_at)
        )).scalars().all()
        prov = rows[0] if rows else None

    chosen_model = model or (agent.model if agent is not None and agent.model else None)
    if not chosen_model and purpose == "orchestrator":
        chosen_model = org_settings.get("orchestrator_model") or None
    if not chosen_model and purpose in ("rag", "alignment", "utility"):
        chosen_model = org_settings.get("utility_model") or None

    if prov is not None:
        return client_from_provider(prov, chosen_model)
    env = env_client(chosen_model)
    if env is not None:
        return env
    raise NoProviderError(
        "No model provider is configured for this workspace. Add OpenRouter, vLLM or another "
        "OpenAI-compatible endpoint in Settings → Models."
    )
