"""Workspace creation and seeding (default swarm, policies, skills, collections, env provider)."""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.defaults import BUSINESS_AGENTS, BUSINESS_POLICIES, ORCHESTRATOR, PERSONAL_AGENTS, PERSONAL_POLICIES
from app.core.security import encrypt_secret
from app.llm.providers import DEFAULT_BASE_URLS, env_fallback_models
from app.models import Agent, Membership, Organization, Policy, Provider, Skill
from app.skills.builtin import SKILLS

log = logging.getLogger("ren.seed")

PERSONAL_SLUG_MAP = {
    "research-analyst": "researcher", "communications-writer": "writer", "executive-assistant": "personal-assistant",
    "data-analyst": "analyst", "software-engineer": "coder", "finance-analyst": "analyst",
    "knowledge-librarian": "researcher", "legal-reviewer": "researcher", "operations-manager": "personal-assistant",
    "sales-specialist": "writer",
}


async def unique_slug(db: AsyncSession, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "workspace"
    slug, i = base, 1
    while (await db.execute(select(Organization.id).where(Organization.slug == slug))).first():
        i += 1
        slug = f"{base}-{i}"
    return slug


async def create_org(db: AsyncSession, *, name: str, mode: str, owner_id: str) -> Organization:
    org = Organization(name=name.strip()[:200] or "My Workspace", slug=await unique_slug(db, name),
                       mode=mode if mode in ("business", "personal") else "business", settings={})
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=owner_id, role="owner"))
    await db.flush()
    await seed_org(db, org)
    return org


def _env_provider(org_id: str) -> Provider | None:
    from app.core.config import settings

    if settings.vllm_base_url:
        return Provider(org_id=org_id, name="vLLM", kind="vllm", base_url=settings.vllm_base_url.rstrip("/"),
                        api_key_enc=encrypt_secret(settings.vllm_api_key), default_model=settings.vllm_model or "default",
                        is_default=True, supports_tools=True, extra={})
    if settings.google_ai_studio_key:
        return Provider(org_id=org_id, name="Google AI Studio", kind="google", base_url=DEFAULT_BASE_URLS["google"],
                        api_key_enc=encrypt_secret(settings.google_ai_studio_key),
                        default_model=settings.google_ai_studio_model, is_default=True, supports_tools=True, extra={})
    if settings.openrouter_api_key:
        return Provider(org_id=org_id, name="OpenRouter", kind="openrouter", base_url=DEFAULT_BASE_URLS["openrouter"],
                        api_key_enc=encrypt_secret(settings.openrouter_api_key), default_model=settings.openrouter_model,
                        is_default=True, supports_tools=True,
                        extra={"fallback_models": env_fallback_models()} if env_fallback_models() else {})
    if settings.openai_api_key:
        return Provider(org_id=org_id, name="OpenAI", kind="openai", base_url=DEFAULT_BASE_URLS["openai"],
                        api_key_enc=encrypt_secret(settings.openai_api_key), default_model=settings.openai_model,
                        is_default=True, supports_tools=True, extra={})
    return None


async def seed_org(db: AsyncSession, org: Organization) -> None:
    personal = org.mode == "personal"
    db.add(Agent(org_id=org.id, **ORCHESTRATOR, autonomy="balanced"))
    slug_to_id: dict[str, str] = {}
    for spec in (PERSONAL_AGENTS if personal else BUSINESS_AGENTS):
        a = Agent(org_id=org.id, autonomy="balanced", temperature=0.3, max_steps=24, **spec)
        db.add(a)
        await db.flush()
        slug_to_id[a.slug] = a.id
    for p in (PERSONAL_POLICIES if personal else BUSINESS_POLICIES):
        db.add(Policy(org_id=org.id, scope="org", enabled=True, **p))
    for s in SKILLS:
        agent_slug = s["agent"]
        if personal:
            agent_slug = PERSONAL_SLUG_MAP.get(agent_slug, agent_slug)
        db.add(Skill(org_id=org.id, name=s["name"], slug=s["slug"], description=s["description"],
                     category=s["category"], icon=s["icon"], instructions=s["instructions"],
                     agent_id=slug_to_id.get(agent_slug), tool_groups=s["tool_groups"], inputs=s["inputs"],
                     builtin=True))
    prov = _env_provider(org.id)
    if prov is not None:
        db.add(prov)
        await db.flush()
        org.settings = {**(org.settings or {}), "default_provider_id": prov.id}
    await db.flush()
    try:
        from app.rag.service import ensure_default_collections

        await ensure_default_collections(db, org.id)
    except Exception:  # noqa: BLE001
        log.warning("default collections not created (RAG module unavailable)", exc_info=True)
