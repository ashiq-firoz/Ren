"""Second-stage reranking of fused candidates.

`settings.reranker_provider`:
  - `fastembed`: cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2` by default) — lazy singleton, run in a thread;
    logits are mapped through a sigmoid to [0, 1];
  - `llm`: listwise reranking by the org's LLM using constrained selection over passage ids (never invalid);
  - `none`: disabled.
Failures never break search: `rerank()` returns None and the fused order is kept.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading

from app.core.config import settings
from app.rag.textutil import truncate_tokens

log = logging.getLogger("ren.rag.rerank")

_model = None
_model_lock = threading.Lock()
_run_lock = threading.Lock()
_failed = False


def _provider() -> str:
    return (settings.reranker_provider or "none").strip().lower()


def reranker_info() -> dict:
    p = _provider()
    model = settings.reranker_model if p == "fastembed" else ("org LLM (listwise)" if p == "llm" else None)
    return {"provider": p, "model": model}


def reranker_enabled() -> bool:
    return _provider() in ("fastembed", "llm") and not (_provider() == "fastembed" and _failed)


def _cross_encoder():
    global _model
    with _model_lock:
        if _model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            cache = settings.models_dir / "fastembed"
            cache.mkdir(parents=True, exist_ok=True)
            log.info("loading reranker %s", settings.reranker_model)
            _model = TextCrossEncoder(model_name=settings.reranker_model, cache_dir=str(cache))
    return _model


def _cross_encode(query: str, passages: list[str]) -> list[float]:
    model = _cross_encoder()
    with _run_lock:
        scores = list(model.rerank(query, passages, batch_size=32))
    return [1.0 / (1.0 + math.exp(-float(s))) for s in scores]


async def _llm_listwise(llm, query: str, passages: list[str]) -> list[float] | None:
    ids = [f"P{i + 1}" for i in range(len(passages))]
    listing = "\n\n".join(f"[{pid}] {truncate_tokens(p, 180)}" for pid, p in zip(ids, passages))
    messages = [
        {"role": "system", "content": (
            "You rank retrieved passages for a search engine. Order the passage ids from the passage that best "
            "answers the query to the least useful one. Judge only by whether the passage contains the answer.")},
        {"role": "user", "content": f"QUERY: {query}\n\nPASSAGES:\n{listing}\n\nReturn the passage ids, best first."},
    ]
    picked, _ = await llm.choose(messages, ids, multi=len(ids), max_tokens=20 * len(ids) + 50)
    if not picked:
        return None
    n = len(ids)
    pos = {pid: r for r, pid in enumerate(picked)}
    return [round(1.0 - pos[pid] / n, 4) if pid in pos else 0.0 for pid in ids]


async def rerank(query: str, passages: list[str], *, llm=None) -> list[float] | None:
    """Relevance scores in [0, 1] aligned with `passages`, or None when reranking is off/unavailable."""
    global _failed
    if not passages:
        return []
    p = _provider()
    try:
        if p == "fastembed" and not _failed:
            return await asyncio.to_thread(_cross_encode, query, passages)
        if p == "llm" and llm is not None:
            return await _llm_listwise(llm, query, passages)
    except Exception as e:  # noqa: BLE001
        if p == "fastembed":
            _failed = True  # e.g. offline and the model isn't cached — don't retry on every query
        log.warning("reranker unavailable (%s): %s", p, e)
    return None
