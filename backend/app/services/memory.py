"""Long-term memory: user preferences, facts, project context, episodic task outcomes, org knowledge.

Embedding-backed semantic recall (numpy cosine — memory sets are small), with keyword fallback when the
embedding backend is unavailable. Near-duplicates are merged instead of stored twice.
"""
from __future__ import annotations

import logging
import re

import numpy as np
from sqlalchemy import or_, select

from app.core.db import session_scope, utcnow
from app.models import Memory
from app.services.serialize import memory_out

log = logging.getLogger("ren.memory")

KINDS = ("episodic", "semantic", "preference", "project", "org")


async def _embed(texts: list[str]) -> list[np.ndarray] | None:
    try:
        from app.rag.embeddings import embed_texts

        return await embed_texts(texts)
    except Exception:  # noqa: BLE001
        log.debug("memory embeddings unavailable", exc_info=True)
        return None


def _visible(org_id: str, user_id: str | None):
    cond = [Memory.org_id == org_id]
    if user_id:
        cond.append(or_(Memory.user_id == user_id, Memory.scope == "org"))
    else:
        cond.append(Memory.scope == "org")
    return cond


async def store_memory(*, org_id: str, user_id: str | None, content: str, kind: str = "semantic",
                       scope: str = "user", agent_id: str | None = None, source: str | None = None,
                       importance: float = 0.5) -> dict:
    content = (content or "").strip()[:4000]
    if not content:
        raise ValueError("empty memory")
    kind = kind if kind in KINDS else "semantic"
    scope = "org" if scope == "org" else "user"
    vecs = await _embed([content])
    vec = vecs[0] if vecs else None
    async with session_scope() as db:
        if vec is not None:
            rows = (await db.execute(select(Memory).where(*_visible(org_id, user_id), Memory.kind == kind)
                                     .order_by(Memory.updated_at.desc()).limit(300))).scalars().all()
            for r in rows:
                if r.embedding is not None and len(r.embedding) == len(vec) and float(np.dot(r.embedding, vec)) > 0.95:
                    r.content = content
                    r.importance = max(r.importance, importance)
                    r.updated_at = utcnow()
                    r.embedding = vec
                    await db.flush()
                    return memory_out(r)
        m = Memory(org_id=org_id, user_id=user_id if scope == "user" else None, agent_id=agent_id, scope=scope,
                   kind=kind, content=content, importance=importance, source=source, embedding=vec)
        db.add(m)
        await db.flush()
        return memory_out(m)


async def search_memories(*, org_id: str, user_id: str | None, query: str = "", limit: int = 8,
                          kinds: list[str] | None = None, scope: str | None = None) -> list[dict]:
    async with session_scope() as db:
        q = select(Memory).where(*_visible(org_id, user_id))
        if kinds:
            q = q.where(Memory.kind.in_(kinds))
        if scope in ("user", "org"):
            q = q.where(Memory.scope == scope)
        rows = (await db.execute(q.order_by(Memory.updated_at.desc()).limit(2000))).scalars().all()
    if not rows:
        return []
    if not query.strip():
        return [memory_out(r) for r in rows[:limit]]
    vecs = await _embed([query])
    scored: list[tuple[float, Memory]] = []
    if vecs:
        qv = vecs[0]
        for r in rows:
            if r.embedding is not None and len(r.embedding) == len(qv):
                scored.append((float(np.dot(r.embedding, qv)) + 0.05 * r.importance, r))
    if not scored:
        terms = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower())}
        for r in rows:
            words = set(re.findall(r"[a-z0-9]{3,}", r.content.lower()))
            overlap = len(terms & words)
            if overlap:
                scored.append((overlap + 0.1 * r.importance, r))
    scored.sort(key=lambda x: -x[0])
    return [memory_out(r) for _, r in scored[:limit]]


async def context_memories(org_id: str, user_id: str | None, query: str, limit: int = 6) -> list[dict]:
    """Preferences always, plus the most relevant other memories for the current message."""
    prefs = await search_memories(org_id=org_id, user_id=user_id, kinds=["preference"], limit=6)
    rel = await search_memories(org_id=org_id, user_id=user_id, query=query, limit=limit,
                                kinds=["semantic", "project", "org", "episodic"])
    seen, out = set(), []
    for m in prefs + rel:
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
    return out
