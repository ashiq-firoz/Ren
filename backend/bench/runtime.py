"""The system under test: an isolated Ren instance (own SQLite DB + data dir) wired to the model being benchmarked.

`bootstrap()` must run before any `app.*` import — it points Ren's settings at the bench work dir and keeps
real keys from backend/.env out of the run.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.common import log


DEFAULT_BASE_URLS = {"vllm": "http://127.0.0.1:8000/v1", "ollama": "http://127.0.0.1:11434/v1",
                     "openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1"}


@dataclass
class ModelSpec:
    kind: str = "vllm"  # vllm | ollama | openrouter | openai | mock
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    thinking: bool = False  # off: vLLM chat_template_kwargs.enable_thinking=false / Ollama reasoning_effort=none
    extra_body: dict = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.kind}:{self.model}" + (" (thinking)" if self.thinking else "")

    @property
    def ren_kind(self) -> str:
        """Ren provider kind: Ollama is served through its OpenAI-compatible API ("custom")."""
        return "custom" if self.kind == "ollama" else self.kind

    def provider_extra(self) -> dict:
        body = dict(self.extra_body)
        if self.kind == "vllm":
            body.setdefault("chat_template_kwargs", {"enable_thinking": self.thinking})
        elif self.kind == "ollama" and not self.thinking:
            body.setdefault("reasoning_effort", "none")  # Ollama's OpenAI API: "none" turns thinking off
        extra: dict = {"body": body} if body else {}
        if self.kind == "ollama":
            extra["guided_style"] = "json_schema"  # grammar-constrained response_format; no vLLM guided params
        return extra


def bootstrap(workdir: Path, *, laya_url: str | None = None, force: bool = True) -> None:
    """Configure Ren's settings for an isolated bench instance (call before importing app modules)."""
    workdir.mkdir(parents=True, exist_ok=True)
    env = {
        "REN_NO_DOTENV": "1",
        "DATA_DIR": str(workdir / "data"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(workdir / 'ren-bench.db').as_posix()}",
        "SECRET_KEY": os.environ.get("SECRET_KEY") or "ren-bench-local-secret-key-0123456789abcdef",
        "EMBEDDING_PROVIDER": os.environ.get("BENCH_EMBEDDING_PROVIDER") or "fastembed",
        "RERANKER_PROVIDER": os.environ.get("BENCH_RERANKER_PROVIDER") or "fastembed",
    }
    for k, v in env.items():
        if force or k not in os.environ:
            os.environ[k] = v
    for k in ("OPENROUTER_API_KEY", "VLLM_BASE_URL", "OPENAI_API_KEY", "REDIS_URL", "SANDBOX_URL", "LAYA_LOCAL"):
        os.environ.pop(k, None)
    if laya_url:
        os.environ["LAYA_URL"] = laya_url
    else:
        os.environ.pop("LAYA_URL", None)


@dataclass
class BenchOrg:
    org_id: str
    user_id: str
    provider_id: str


async def init() -> None:
    from app.core.db import init_db
    from app.tools.builtin import load_builtin_tools

    await init_db()
    load_builtin_tools()


_setup_lock: asyncio.Lock | None = None


async def ensure_org(slug: str, spec: ModelSpec | None, *, settings: dict | None = None) -> BenchOrg:
    """Create (or update) the bench workspace `slug` with the model under test as its only provider (`spec=None`:
    no provider at all, so Ren takes its model-free paths). Suites start concurrently and share one bench user, so
    setup is serialized."""
    global _setup_lock
    if _setup_lock is None:
        _setup_lock = asyncio.Lock()
    async with _setup_lock:
        return await _ensure_org(slug, spec, settings)


async def _ensure_org(slug: str, spec: ModelSpec | None, settings: dict | None) -> BenchOrg:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.core.security import encrypt_secret, hash_password
    from app.models import Membership, Organization, Provider, User
    from app.rag.service import ensure_default_collections

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == "bench@ren.local"))).scalars().first()
        if user is None:
            user = User(email="bench@ren.local", name="Ren Bench", password_hash=hash_password(os.urandom(16).hex()),
                        timezone="UTC")
            db.add(user)
            await db.flush()
        org = (await db.execute(select(Organization).where(Organization.slug == f"bench-{slug}"))).scalars().first()
        if org is None:
            org = Organization(name=f"Bench · {slug}", slug=f"bench-{slug}", mode="business", settings={})
            db.add(org)
            await db.flush()
            db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
        prov = (await db.execute(select(Provider).where(Provider.org_id == org.id))).scalars().first()
        if spec is None:
            if prov is not None:
                raise SystemExit(f"bench workspace {slug!r} already has a model provider; use another --workdir")
            org.settings = dict(settings or {})
            await ensure_default_collections(db, org.id)
            return BenchOrg(org.id, user.id, "")
        base_url = spec.base_url or ("mock://local" if spec.kind == "mock" else "")
        if prov is None:
            prov = Provider(org_id=org.id, name=f"Bench {spec.kind}", kind=spec.ren_kind, base_url=base_url,
                            default_model=spec.model or "mock-1", is_default=True)
            db.add(prov)
        prov.kind, prov.base_url, prov.default_model = spec.ren_kind, base_url, spec.model or "mock-1"
        prov.api_key_enc = encrypt_secret(spec.api_key) if spec.api_key else None
        prov.extra = spec.provider_extra()
        prov.is_default = True
        await db.flush()
        org.settings = {**(settings or {}), "default_provider_id": prov.id}
        await ensure_default_collections(db, org.id)
        return BenchOrg(org.id, user.id, prov.id)


async def llm_for(org: BenchOrg, purpose: str = "bench"):
    from app.core.db import session_scope
    from app.llm.providers import get_llm

    async with session_scope() as db:
        return await get_llm(db, org.org_id, purpose=purpose)


# ----------------------------------------------------------------------------------------- preflight
WEATHER_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


async def preflight(org: BenchOrg) -> dict:
    """Prove the endpoint does what Ren needs: chat, native tool calls, constrained choice, JSON; Laya health."""
    from app.policy.laya_client import laya

    llm = await llm_for(org)
    out: dict[str, Any] = {"model": llm.model}

    async def step(name: str, coro, check) -> None:
        t = time.perf_counter()
        try:
            res = await coro
            ok, detail = check(res)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
        out[name] = {"ok": ok, "ms": int((time.perf_counter() - t) * 1000), "detail": detail}
        log(f"preflight {name:<12} {'ok ' if ok else 'FAIL'} {out[name]['ms']:>6} ms  {detail}")

    await step("chat", llm.chat([{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=20, temperature=0),
               lambda r: ("ok" in (r.content or "").lower(), (r.content or "").strip()[:60]))
    await step("tool_call", llm.chat([{"role": "user", "content": "What's the weather in Paris right now?"}],
                                     tools=[WEATHER_TOOL], temperature=0, max_tokens=200),
               lambda r: (bool(r.tool_calls) and r.tool_calls[0].name == "get_weather",
                          f"{r.tool_calls[0].name}({r.tool_calls[0].arguments})" if r.tool_calls else (r.content or "")[:60]))
    await step("choose", llm.choose([{"role": "user", "content": "Which is a fruit? A) carrot B) apple"}], ["A", "B"]),
               lambda r: (r[0][:1] == ["B"], f"{r[0]} via {r[1]}"))
    await step("json", llm.json([{"role": "user", "content": "Give the capital of France."}],
                                {"type": "object", "properties": {"capital": {"type": "string"}}, "required": ["capital"]},
                                max_tokens=100),
               lambda r: (isinstance(r, dict) and "paris" in str(r.get("capital", "")).lower(), str(r)[:60]))
    filler = "The quarterly operations log records routine maintenance, inventory counts and shift handovers. " * 900
    await step("context", llm.chat([{"role": "user", "content": (
        f"Remember this code: ZEBRA-7731.\n\n{filler}\n\nWhat was the code at the very start of this message? "
        "Reply with the code only.")}], max_tokens=20, temperature=0),
        lambda r: ("7731" in (r.content or ""),
                   "~13k-token prompt kept intact" if "7731" in (r.content or "") else
                   "the server truncated a ~13k-token prompt — raise its context window (Ollama: "
                   "OLLAMA_CONTEXT_LENGTH=32768; vLLM: --max-model-len)"))
    if laya.configured:
        await step("laya", laya.status(force=True), lambda r: (bool(r.get("reachable")), str(r)[:100]))
    else:
        out["laya"] = {"ok": None, "detail": "not configured (pass --laya-url to benchmark Laya)"}
        log("preflight laya         —  not configured (pass --laya-url to include Laya)")
    return out
