"""Synthetic questions per ToC leaf + the STAIR fine-tuning dataset exporter.

Questions (3–5 per leaf, diverse, answerable from that leaf only) serve two purposes:
  1. a doc2query-style retrieval signal (question embeddings matched against the query), and
  2. training pairs (question → leaf) to fine-tune a STAIR retriever, as in the paper: section content →
     synthetic questions → leaf id; this generalizes well even when sections have few examples.

`export_training_data` yields NDJSON lines in chat format, e.g. for LoRA fine-tuning of Mistral/Qwen:
  {"messages": [{"role": "system", ...}, {"role": "user", "content": "QUERY: ...\\n\\nTABLE OF CONTENTS:\\n..."},
                {"role": "assistant", "content": "<leaf path>"}], "document_id": ..., "leaf_id": ..., "query": ...}
The user/system messages are produced by the same code the retriever uses at inference time for a dedicated
(fine-tuned) STAIR model, so training and serving formats match exactly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable

from app.rag.llmutil import UsageMeter, safe_json
from app.rag.textutil import count_tokens, truncate_tokens

log = logging.getLogger("ren.rag.questions")

MIN_LEAF_TOKENS = 8
MAX_LEAVES = 400
LEAF_TOKENS_IN_PROMPT = 1200
DEFAULT_CONCURRENCY = 4

QUESTION_SYSTEM = (
    "You write realistic search questions for an enterprise document retrieval system. Given one part of a "
    "document, write questions a real user might ask that can be answered only from the given text. Make them "
    "diverse (facts, numbers, conditions, procedures, definitions, exceptions), specific and self-contained: name "
    "the subject explicitly instead of saying 'this part' or 'the text'. Phrase them the way employees or customers "
    "actually ask, and do not copy sentences verbatim."
)


def questions_schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {"questions": {"type": "array", "items": {"type": "string"}, "minItems": min(3, n),
                                     "maxItems": n}},
        "required": ["questions"],
    }


def _n_questions(tokens: int) -> int:
    return 3 if tokens < 250 else 4 if tokens < 700 else 5


def _clean_questions(raw, n: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for q in raw:
        if not isinstance(q, str):
            continue
        t = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", q).strip().strip("\"'“”")
        t = re.sub(r"\s+", " ", t)
        if len(t) < 8 or len(t) > 300:
            continue
        if not t.endswith("?"):
            t = t.rstrip(".") + "?"
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= n:
            break
    return out


def _select_leaves(leaves: list, cap: int = MAX_LEAVES) -> list:
    eligible = [lf for lf in leaves if count_tokens(lf.content) >= MIN_LEAF_TOKENS]
    if len(eligible) <= cap:
        return eligible
    step = len(eligible) / cap
    return [eligible[int(i * step)] for i in range(cap)]


async def generate_questions(llm, doc_title: str, leaves: list, *, meter: UsageMeter | None = None,
                             concurrency: int = DEFAULT_CONCURRENCY,
                             progress: Callable[[int, int], Awaitable[None]] | None = None) -> dict[str, list[str]]:
    """Generate questions for leaves (`app.rag.structure.TocNode`s). Returns leaf id → questions."""
    if llm is None:
        return {}
    targets = _select_leaves(leaves)
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, list[str]] = {}
    done = [0]

    async def one(leaf) -> None:
        tokens = count_tokens(leaf.content)
        n = _n_questions(tokens)
        messages = [
            {"role": "system", "content": QUESTION_SYSTEM},
            {"role": "user", "content": (
                f"DOCUMENT: {doc_title}\nSECTION: {leaf.title}\nPATH: {leaf.breadcrumb}\n\n"
                f"TEXT:\n{truncate_tokens(leaf.content, LEAF_TOKENS_IN_PROMPT)}\n\nWrite {n} questions.")},
        ]
        async with sem:
            out = await safe_json(llm, messages, questions_schema(n), meter=meter, max_tokens=60 * n + 80,
                                  name="section_questions")
        qs = _clean_questions(out.get("questions") if isinstance(out, dict) else None, n)
        if qs:
            results[leaf.id] = qs
        done[0] += 1
        if progress is not None:
            try:
                await progress(done[0], len(targets))
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(*(one(lf) for lf in targets))
    return results


async def export_training_data(db, org_id: str, collection_id: str | None = None, *,
                               collection_ids: list[str] | None = None,
                               budget_tokens: int | None = None) -> AsyncIterator[str]:
    """Async generator of NDJSON lines (STAIR fine-tuning pairs: query + ToC → leaf path)."""
    import sqlalchemy as sa

    from app.models import Document, SectionQuestion
    from app.rag import stair

    q = sa.select(Document.id, Document.title, Document.updated_at).where(
        Document.org_id == org_id, Document.status == "ready", Document.question_count > 0)
    if collection_id:
        q = q.where(Document.collection_id == collection_id)
    if collection_ids is not None:
        if not collection_ids:
            return
        q = q.where(Document.collection_id.in_(collection_ids))
    docs = (await db.execute(q.order_by(Document.created_at))).all()
    budget = budget_tokens or stair.DEFAULT_BUDGET_TOKENS
    for doc_id, title, updated_at in docs:
        toc = await stair.load_toc(db, doc_id, title=title, version=str(updated_at))
        if toc is None or not toc.leaves:
            continue
        rows = (await db.execute(
            sa.select(SectionQuestion.section_id, SectionQuestion.question)
            .where(SectionQuestion.document_id == doc_id).order_by(SectionQuestion.section_id, SectionQuestion.id)
        )).all()
        full = stair.render_budgeted(toc, budget)
        rendered: dict[str, tuple[str, str]] = {}
        for section_id, question in rows:
            item = toc.by_id.get(section_id)
            if item is None or not item.is_leaf:
                continue
            if section_id not in rendered:
                rendered[section_id] = (full, item.path) if full is not None else stair.render_for_training(
                    toc, item, budget)
            toc_text, answer = rendered[section_id]
            line = {
                "messages": stair.training_messages(question, toc_text, answer),
                "document_id": doc_id,
                "leaf_id": section_id,
                "query": question,
            }
            yield json.dumps(line, ensure_ascii=False) + "\n"
