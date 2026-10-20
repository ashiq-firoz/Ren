"""Model-free measurements (`python -m bench offline`): the parts of Ren that run without a served LLM.

For when no model endpoint is available. Same datasets, seeds and item order as `bench run`; everything that needs
the model (answers, hallucination, the LLM judge, tool calls) is left out.

- rag: MultiHop-RAG retrieval over an index built *without* an LLM: parsing, heading structure, chunks and bge-small
  embeddings, but no synthetic questions, LLM summaries or STAIR leaf selection. Each answerable query goes through
  `rag.service.search` with the answer path's settings (hybrid BM25 + vectors → cross-encoder rerank → top-k chunks),
  so the numbers are a floor for the full pipeline. Evidence coverage (how many of a question's gold articles reach
  the answer context) is what bounds any model's answer accuracy. Plus the BM25 / vector / hybrid ablation.
- align: Laya alone on DynaBench and BeaverTails (needs --laya-url): precision / recall / F1 at Ren's escalate
  threshold, ROC-AUC, the best-F1 threshold, and the share of responses triage clears without the LLM judge.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from bench import align, data
from bench.common import ResultStore, mean, percentile, run_pool, seeded_order
from bench.metrics import average_dicts, binary_metrics, dedupe, retrieval_metrics
from bench.rag_multihop import TYPES, _gold_docs, _title_key, ingest, qid, run_ablation, title_index
from bench.runtime import ensure_org

ANSWERABLE = tuple(t for t in TYPES if t != "null_query")


# ----------------------------------------------------------------------------------------- rag
def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def fact_in_context(fact: str, context_words: str, context_shingles: set[tuple[str, ...]]) -> bool:
    """MultiHop-RAG evidence facts are verbatim sentences from the articles: present if the fact appears in the
    context (punctuation and whitespace ignored) or ≥ 70% of its 5-word shingles do (a fact cut by a chunk boundary)."""
    words = _words(fact)
    if not words:
        return False
    if " ".join(words) in context_words:
        return True
    shingles = [tuple(words[i:i + 5]) for i in range(max(1, len(words) - 4))]
    return sum(1 for s in shingles if s in context_shingles) >= 0.7 * len(shingles)


def coverage(rows: list[dict], have: str = "evidence_in_context", need: str = "gold_docs") -> dict:
    """Share of queries whose answer context holds all / some / none of their gold evidence (articles, or with
    have="facts_in_context", need="facts": the evidence sentences themselves)."""
    n = len(rows)
    if not n:
        return {"n": 0}
    full = sum(1 for r in rows if r[have] >= r[need])
    none = sum(1 for r in rows if r[have] == 0)
    return {"n": n, "full": full / n, "partial": (n - full - none) / n, "none": none / n}


def _coverage_block(rows: list[dict], by_type: dict, have: str, need: str) -> dict:
    by_count: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_count[r["gold_docs"]].append(r)
    return {"all": coverage(rows, have, need),
            **{t: coverage(by_type[t], have, need) for t in ANSWERABLE if by_type.get(t)},
            "yes_no": coverage([r for r in rows if r["yes_no"]], have, need),
            "entity": coverage([r for r in rows if not r["yes_no"]], have, need),
            "by_gold_articles": {str(k): coverage(v, have, need) for k, v in sorted(by_count.items())}}


async def run_rag(*, run_dir: Path, deadline: float | None, max_queries: int | None, concurrency: int,
                  ingest_concurrency: int, ablation_n: int, seed: int, top_k: int) -> dict:
    from app.core.db import session_scope
    from app.rag.service import search

    corpus, queries = data.load_multihop()
    items = seeded_order([(qid(i), q) for i, q in enumerate(queries)], seed)
    if max_queries:
        items = items[:max_queries]
    items = [(k, q) for k, q in items if q["question_type"] != "null_query"]  # no gold evidence to retrieve
    org = await ensure_org("rag-offline", None, settings={"retrieval": {"top_k": top_k}})
    ing = await ingest(org, corpus, concurrency=ingest_concurrency, deadline=deadline)
    title_to_doc = title_index(corpus, ing["mapping"])
    store = ResultStore(run_dir / "rag_retrieval.jsonl")

    async def worker(item) -> dict:
        _key, q = item
        gold = _gold_docs(q, title_to_doc)
        expected = {_title_key(ev.get("title") or "") for ev in q.get("evidence_list") or []}
        t = time.perf_counter()
        async with session_scope() as db:
            ret = await search(db, org_id=org.org_id, user_id=org.user_id, role="owner", query=q["query"],
                               mode="hybrid", top_k=None, rerank=True)  # the settings `rag.service.answer` uses
        ranked = dedupe(r.get("document_id") for r in ret.get("results") or [])
        row = {"type": q["question_type"], "ms": int((time.perf_counter() - t) * 1000),
               "timings": ret.get("timings"), "gold_docs": len(gold), "gold_unindexed": len(gold) < len(expected),
               "context_docs": len(ranked), "reranked": bool(ret.get("reranked")),
               "yes_no": (q.get("answer") or "").strip().lower() in ("yes", "no")}
        if gold:
            row["evidence_in_context"] = len(gold & set(ranked))
            row["retrieval"] = retrieval_metrics(ranked, gold, ks=(1, 3, 5))
            row["retrieval"]["context_recall"] = row["evidence_in_context"] / len(gold)
            ctx = _words(ret.get("context") or "")
            ctx_shingles = {tuple(ctx[i:i + 5]) for i in range(max(0, len(ctx) - 4))}
            ctx_text = " ".join(ctx)
            facts = [ev.get("fact") or "" for ev in q.get("evidence_list") or [] if (ev.get("fact") or "").strip()]
            row["facts"] = len(facts)
            row["facts_in_context"] = sum(1 for f in facts if fact_in_context(f, ctx_text, ctx_shingles))
            row["context_tokens"] = len(ctx)
        return row

    stats = await run_pool(items, worker, label="rag-retrieval", concurrency=concurrency, deadline=deadline,
                           store=store, key=lambda it: it[0])
    store.close()
    rows = [r for r in store.rows() if "error" not in r and not r.get("gold_unindexed") and r.get("retrieval")]
    errors = [r for r in store.rows() if "error" in r]
    ablation = await run_ablation(org, items, title_to_doc, run_dir=run_dir, n=ablation_n, concurrency=concurrency,
                                  deadline=deadline) if ablation_n else {}
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    return {
        "dataset": "MultiHop-RAG (yixuantt/MultiHopRAG)",
        "index": "built without an LLM: heading structure + chunks + bge-small embeddings; no synthetic questions, "
                 "LLM summaries or STAIR",
        "ingestion": {**ing["stats"], "indexed_docs": len(ing["mapping"])},
        "queries": {"evaluated": len(rows), "errors": len(errors), "by_type": {t: len(by_type[t]) for t in ANSWERABLE},
                    "not_run_time_budget": stats.get("timed_out", 0),
                    "excluded_gold_not_indexed": sum(1 for r in store.rows() if r.get("gold_unindexed")),
                    "reranked_rate": mean(float(r["reranked"]) for r in rows)},
        "retrieval": {"n": len(rows), **average_dicts(r["retrieval"] for r in rows),
                      **{t: average_dicts(r["retrieval"] for r in by_type[t]) for t in ANSWERABLE if by_type.get(t)}},
        "evidence_coverage": _coverage_block(rows, by_type, "evidence_in_context", "gold_docs"),
        "fact_coverage": _coverage_block([r for r in rows if r.get("facts")], by_type, "facts_in_context", "facts"),
        "context_words_p50": percentile((r.get("context_tokens") for r in rows), 0.5),
        "latency_ms": {"p50": percentile((r["ms"] for r in rows), 0.5), "p95": percentile((r["ms"] for r in rows), 0.95),
                       "rerank_p50": percentile(((r.get("timings") or {}).get("rerank") for r in rows), 0.5)},
        "ablation": ablation,
        "error_samples": [e.get("error") for e in errors[:5]],
    }


# ----------------------------------------------------------------------------------------- align (Laya only)
def _laya_summary(rows: list[dict], errors: int) -> dict:
    if not rows:
        return {"n": 0, "errors": errors}
    return {"n": len(rows), "positives": sum(r["label"] for r in rows), **align.laya_metrics(rows), "errors": errors}


async def run_align(*, run_dir: Path, deadline: float | None, concurrency: int, seed: int, dynabench_n: int | None,
                    beavertails_n: int) -> dict:
    from app.policy.laya_client import laya

    if not laya.configured:
        raise RuntimeError("the align suite measures Laya: pass --laya-url")
    dstore = ResultStore(run_dir / "align_dynabench.jsonl")
    bstore = ResultStore(run_dir / "align_beavertails.jsonl")
    safety = align.safety_policy()

    async def dyn_worker(ex: dict) -> dict:
        row, pol = align.dynabench_case(ex)
        row.update(await align._layers(None, pol, ex["transcript"], None, transcript=True))
        return row

    async def bt_worker(ex: dict) -> dict:
        row = align.beavertails_row(ex)
        row.update(await align._layers(None, safety, ex.get("response") or "", ex.get("prompt") or "",
                                       transcript=False))
        return row

    per = max(1, concurrency // 2)  # both datasets at once, so a time budget cuts both to random samples
    jobs = [run_pool(align.dynabench_items(seed, dynabench_n), dyn_worker, label="laya-dynabench", concurrency=per,
                     deadline=deadline, store=dstore, key=lambda ex: f"dyn{ex['idx']}")]
    if beavertails_n:
        jobs.append(run_pool(align.beavertails_items(seed, beavertails_n), bt_worker, label="laya-beavertails",
                             concurrency=per, deadline=deadline, store=bstore, key=lambda ex: f"bt{ex['idx']}"))
    await asyncio.gather(*jobs)
    dstore.close()
    bstore.close()

    out: dict = {}
    drows = [r for r in dstore.rows() if "error" not in r]
    out["dynabench"] = _laya_summary(drows, sum(1 for r in dstore.rows() if "error" in r))
    by_mode: dict[str, list[dict]] = defaultdict(list)
    for r in drows:
        by_mode[r.get("failure_mode") or "unknown"].append(r)
    out["dynabench"]["laya_by_failure_mode"] = {
        k: {"n": len(v), **{m: binary_metrics([r["label"] for r in v],
                                              [int(r["laya_max"] >= align.ESCALATE_T) for r in v])[m]
                            for m in ("recall", "accuracy")}}
        for k, v in sorted(by_mode.items(), key=lambda kv: -len(kv[1]))}
    if beavertails_n:
        brows = [r for r in bstore.rows() if "error" not in r]
        out["beavertails"] = _laya_summary(brows, sum(1 for r in bstore.rows() if "error" in r))
        cat_hits: dict[str, list[int]] = defaultdict(list)
        for r in brows:
            if r["label"]:
                for c in r.get("categories") or []:
                    cat_hits[c].append(int(r["laya_max"] >= align.ESCALATE_T))
        out["beavertails"]["laya_recall_by_category"] = {
            c: {"n": len(v), "recall": sum(v) / len(v)} for c, v in sorted(cat_hits.items(), key=lambda kv: -len(kv[1]))}
    out["errors_sample"] = [r["error"] for r in dstore.rows() + bstore.rows() if "error" in r][:5]
    out["error_kinds"] = dict(Counter(r["error"].split(":")[0] for r in dstore.rows() + bstore.rows() if "error" in r))
    return out
