"""LLM helpers for the RAG pipeline: provider resolution, safe structured calls and usage metering.

`LLMClient.json()` / `choose()` don't report token usage, so calls made through these helpers are metered with a
token estimate and recorded once per operation via `app.services.usage.record_usage(..., purpose="rag")`.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from app.rag.textutil import estimate_tokens

log = logging.getLogger("ren.rag")


class UsageMeter:
    """Accumulates (estimated or reported) token usage per model."""

    def __init__(self) -> None:
        self.by_model: dict[str, dict[str, int]] = defaultdict(lambda: {"prompt_tokens": 0, "completion_tokens": 0})
        self.calls = 0

    def add_estimate(self, model: str, messages: list[dict], output: Any) -> None:
        prompt = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
        out = output if isinstance(output, str) else json.dumps(output, default=str) if output is not None else ""
        self.add(model, {"prompt_tokens": prompt, "completion_tokens": estimate_tokens(out)})

    def add(self, model: str, usage: dict | None) -> None:
        if not usage:
            return
        u = self.by_model[model or "unknown"]
        u["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        u["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.calls += 1

    @property
    def total_tokens(self) -> int:
        return sum(u["prompt_tokens"] + u["completion_tokens"] for u in self.by_model.values())

    async def flush(self, org_id: str, *, purpose: str = "rag") -> int:
        total = 0
        try:
            from app.services.usage import record_usage
        except Exception:  # noqa: BLE001
            return 0
        for model, usage in list(self.by_model.items()):
            try:
                total += await record_usage(org_id, model, dict(usage), purpose=purpose)
            except Exception:  # noqa: BLE001
                log.exception("usage recording failed")
        self.by_model.clear()
        return total


async def rag_llm(org_id: str, *, provider_id: str | None = None, model: str | None = None):
    """The org's LLM for RAG work (None when no provider is configured)."""
    from app.core.db import session_scope
    from app.llm.client import NoProviderError
    from app.llm.providers import get_llm

    try:
        async with session_scope() as db:
            return await get_llm(db, org_id, provider_id=provider_id, model=model, purpose="rag")
    except NoProviderError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("could not resolve the RAG LLM")
        return None


async def safe_json(llm, messages: list[dict], schema: dict, *, meter: UsageMeter | None = None,
                    max_tokens: int = 1200, name: str = "output") -> Any:
    """`llm.json(...)` that never raises (returns None on failure) and meters usage."""
    if llm is None:
        return None
    try:
        out = await llm.json(messages, schema, max_tokens=max_tokens, name=name)
    except Exception as e:  # noqa: BLE001
        log.warning("structured LLM call failed: %s", e)
        return None
    if meter is not None:
        meter.add_estimate(getattr(llm, "model", "unknown"), messages, out)
    return out
