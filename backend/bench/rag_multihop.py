"""MultiHop-RAG through Ren's real knowledge pipeline.

1. Ingest the 609-article corpus with Ren's ingestion (parsing → STAIR structure → summaries → synthetic questions →
   embeddings) using the model under test.
2. Ask every query through `rag.service.answer` — the grounded-answer path agents use (hybrid BM25 + vectors +
   synthetic questions + STAIR leaf selection → cross-encoder rerank → cited answer).
3. Score retrieval at document level against the gold evidence articles, and answers CRAG-style:
   correct / incorrect (= hallucinated) / missing (= declined). Null queries have no answer in the corpus, so the
   only correct behaviour is to decline. Optional: claim-level faithfulness of answers against retrieved sources.
4. Optional retrieval ablation: BM25-only vs vectors-only vs hybrid (± rerank) on the same queries.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter, defaultdict
from pathlib import Path

from bench import data
from bench.common import ResultStore, log, mean, percentile, run_pool, seeded_order
from bench.metrics import (
    average_dicts,
    crag_scores,
    dedupe,
    is_abstention,
    normalize_answer,
    retrieval_metrics,
    yes_no,
)
from bench.runtime import BenchOrg, ModelSpec, ensure_org, llm_for

SUITE = "rag"
TYPES = ("inference_query", "comparison_query", "temporal_query", "null_query")
ABLATIONS = {"bm25": ("lexical", False), "vector": ("semantic", False), "hybrid_no_rerank": ("hybrid", False),
             "hybrid": ("hybrid", True)}


def qid(i: int) -> str:
    return f"mh{i:04d}"


def _title_key(title: str) -> str:
    return normalize_answer(title)


# ----------------------------------------------------------------------------------------- ingestion
def article_markdown(a: dict) -> str:
    meta = " · ".join(x for x in (a.get("source"), (a.get("published_at") or "")[:10], a.get("author"),
                                  a.get("category")) if x)
    return f"# {a['title']}\n\n{meta}\n\nURL: {a.get('url') or ''}\n\n{a.get('body') or ''}\n"


async def ingest(org: BenchOrg, corpus: list[dict], *, concurrency: int, deadline: float | None) -> dict:
    """Ingest the corpus (idempotent: reuses documents already indexed in this work dir). Returns stats + mapping."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.models import Document
    from app.rag import service

    service.MAX_CONCURRENT_INGESTIONS = max(1, concurrency)
    t0 = time.monotonic()
    async with session_scope() as db:
        docs = (await db.execute(select(Document).where(Document.org_id == org.org_id))).scalars().all()
        by_key = {(d.meta or {}).get("bench_key"): d for d in docs if (d.meta or {}).get("bench_key")}
        # Left unfinished by an earlier run (failed or interrupted): re-ingest the stored upload, or recreate the
        # document if its file is gone (ingestion deletes the upload of a document it never saw committed).
        resume, lost = [], []
        for key, d in list(by_key.items()):
            if d.status != "ready":
                (resume if d.storage_path and Path(d.storage_path).exists() else lost).append(d.id)
                if d.id in lost:
                    by_key.pop(key)
                    await service.delete_document(db, d.id)
    for doc_id in resume:
        service.schedule_ingestion(doc_id)
    created = 0
    for i, a in enumerate(corpus):
        key = f"multihop:{i}"
        if a is None or key in by_key:
            continue
        slug = hashlib.sha1(a["title"].encode()).hexdigest()[:8]
        # One commit per document: ingestion skips (and deletes) a document whose row isn't committed within 30 s.
        async with session_scope() as db:
            doc = await service.create_document_from_bytes(
                db, org_id=org.org_id, user_id=org.user_id, collection_id=None, filename=f"article-{i:03d}-{slug}.md",
                data=article_markdown(a).encode("utf-8"), mime="text/markdown")
            doc.meta = {**(doc.meta or {}), "bench_key": key}
        created += 1
    log(f"ingest: {sum(1 for a in corpus if a)} articles — {created} new, {len(by_key) - len(resume)} reused, "
        f"{len(resume)} resumed, {len(lost)} recreated (concurrency {concurrency})")

    last_log = 0.0
    while True:
        async with session_scope() as db:
            rows = (await db.execute(select(Document.id, Document.status, Document.meta, Document.question_count)
                                     .where(Document.org_id == org.org_id))).all()
        status = Counter(r.status for r in rows)
        pending = len(rows) - status.get("ready", 0) - status.get("failed", 0)
        now = time.monotonic()
        if pending == 0 or (deadline is not None and now > deadline):
            break
        if now - last_log > 15:
            last_log = now
            rate = status.get("ready", 0) / max(1e-6, now - t0)
            log(f"ingest: ready {status.get('ready', 0)}/{len(rows)}, failed {status.get('failed', 0)}, "
                f"in progress {pending} ({rate:.2f} docs/s)")
        await asyncio.sleep(3)
    mapping = {}
    for r in rows:
        key = (r.meta or {}).get("bench_key")
        if key and r.status == "ready":
            mapping[int(key.split(":")[1])] = r.id
    stats = {"articles": sum(1 for a in corpus if a), "ready": status.get("ready", 0), "failed": status.get("failed", 0),
             "pending_at_deadline": pending, "seconds": round(time.monotonic() - t0, 1),
             "indexed_this_run": created + len(resume) + len(lost),
             "synthetic_questions": sum(r.question_count or 0 for r in rows),
             "llm_tokens": sum(int((r.meta or {}).get("llm_tokens") or 0) for r in rows)}
    log(f"ingest: done — {stats['ready']} ready, {stats['failed']} failed, {stats['synthetic_questions']} synthetic "
        f"questions, {stats['seconds']}s")
    return {"stats": stats, "mapping": mapping}


# ----------------------------------------------------------------------------------------- grading
GRADE_SYSTEM = ("You grade answers in a question-answering benchmark. Compare the PREDICTION with the GROUND TRUTH "
                "and reply with exactly one word.")
GRADE_USER = """QUESTION: {question}
GROUND TRUTH: {gold}
PREDICTION: {prediction}

- correct: the prediction gives the ground-truth answer (paraphrases and extra supporting detail are fine; for yes/no
  questions the yes/no must match; a hedge is fine if it still commits to the right answer)
- incorrect: it commits to a different, wrong or contradictory answer
- missing: it declines, says the information is unavailable, or never commits to an answer"""

NULL_USER = """QUESTION: {question}
The documents the assistant could search do NOT contain the answer to this question.
PREDICTION: {prediction}

- abstains: the prediction says the information is insufficient/unavailable or declines to answer
- answers: the prediction asserts an answer anyway (even partially or hedged)"""


async def grade(llm, q: dict, prediction: str) -> tuple[str, str]:
    """(outcome, method): outcome ∈ correct | incorrect | missing."""
    gold = (q.get("answer") or "").strip()
    if q["question_type"] == "null_query":
        if is_abstention(prediction):
            return "correct", "heuristic"
        picked, _ = await llm.choose([{"role": "system", "content": GRADE_SYSTEM},
                                      {"role": "user", "content": NULL_USER.format(question=q["query"],
                                                                                  prediction=prediction[:3000])}],
                                     ["abstains", "answers"])
        return ("correct" if picked[:1] == ["abstains"] else "incorrect"), "judge"
    g = gold.lower().strip(" .")
    if g not in ("yes", "no") and normalize_answer(gold) and normalize_answer(gold) in normalize_answer(prediction):
        return "correct", "heuristic"
    if g in ("yes", "no") and not is_abstention(prediction):
        yn = yes_no(prediction)
        if yn:
            return ("correct" if yn == g else "incorrect"), "heuristic"
    picked, _ = await llm.choose([{"role": "system", "content": GRADE_SYSTEM},
                                  {"role": "user", "content": GRADE_USER.format(question=q["query"], gold=gold,
                                                                                prediction=prediction[:3000])}],
                                 ["correct", "incorrect", "missing"])
    return (picked[0] if picked else "missing"), "judge"


FAITH_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {
    "type": "object", "properties": {"claim": {"type": "string"}, "supported": {"type": "boolean"}},
    "required": ["claim", "supported"]}}}, "required": ["claims"]}
FAITH_SYSTEM = ("You check whether an answer is supported by its sources. Split the ANSWER into its atomic factual "
                "claims (ignore citation markers like [1], hedges and statements about the sources themselves). For "
                "each claim decide whether the SOURCES support it — stated or clearly implied. Do not use outside "
                "knowledge.")


async def faithfulness(llm, context: str, answer: str) -> dict:
    data_ = await llm.json([{"role": "system", "content": FAITH_SYSTEM},
                            {"role": "user", "content": f"SOURCES:\n{context[:14000]}\n\nANSWER:\n{answer[:3000]}"}],
                           FAITH_SCHEMA, max_tokens=1500, name="faithfulness")
    claims = [c for c in (data_ or {}).get("claims", []) if isinstance(c, dict)] if isinstance(data_, dict) else []
    if not claims:
        return {"claims": 0, "supported": 0, "score": None}
    sup = sum(1 for c in claims if c.get("supported") is True)
    return {"claims": len(claims), "supported": sup, "score": sup / len(claims),
            "unsupported": [c.get("claim", "")[:200] for c in claims if c.get("supported") is not True][:5]}


# ----------------------------------------------------------------------------------------- run
def title_index(corpus: list[dict], mapping: dict[int, str]) -> dict[str, list[str]]:
    """Normalised article title → indexed document ids (gold evidence is identified by title)."""
    out: dict[str, list[str]] = defaultdict(list)
    for i, a in enumerate(corpus):
        if i in mapping:
            out[_title_key(a["title"])].append(mapping[i])
    return out


def _gold_docs(q: dict, title_to_doc: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    for ev in q.get("evidence_list") or []:
        out.update(title_to_doc.get(_title_key(ev.get("title") or ""), []))
    return out


async def run(spec: ModelSpec, *, run_dir: Path, deadline: float | None, ingest_deadline: float | None,
              max_queries: int | None, concurrency: int, ingest_concurrency: int, faith_n: int, ablation_n: int,
              seed: int, top_k: int, corpus_limit: int | None = None) -> dict:
    from app.core.db import session_scope
    from app.rag.service import answer

    corpus, queries = data.load_multihop()
    items = seeded_order([(qid(i), q) for i, q in enumerate(queries)], seed)
    if max_queries:
        items = items[:max_queries]
    to_index = corpus
    if corpus_limit:  # dry runs: the sampled queries' gold articles plus random distractors
        by_title = {_title_key(a["title"]): i for i, a in enumerate(corpus)}
        keep = {by_title[k] for _, q in items for k in (_title_key(e.get("title") or "") for e in q.get("evidence_list") or [])
                if k in by_title}
        for i in seeded_order(list(range(len(corpus))), seed):
            if len(keep) >= corpus_limit:
                break
            keep.add(i)
        to_index = [a if i in keep else None for i, a in enumerate(corpus)]
    org = await ensure_org("rag", spec, settings={"retrieval": {"top_k": top_k}})
    ing = await ingest(org, to_index, concurrency=ingest_concurrency, deadline=ingest_deadline)
    mapping: dict[int, str] = ing["mapping"]
    title_to_doc = title_index(corpus, mapping)
    indexed = set(mapping.values())

    faith_ids = set([k for k, q in items if q["question_type"] != "null_query"][:faith_n])
    judge = await llm_for(org, "bench-judge")
    store = ResultStore(run_dir / "rag.jsonl")

    async def worker(item) -> dict:
        key, q = item
        gold = _gold_docs(q, title_to_doc)
        expected = {_title_key(ev.get("title") or "") for ev in q.get("evidence_list") or []}
        missing_gold = q["question_type"] != "null_query" and len(gold) < len(expected)
        t = time.perf_counter()
        async with session_scope() as db:
            res = await answer(db, org_id=org.org_id, user_id=org.user_id, role="owner", query=q["query"])
        total_ms = int((time.perf_counter() - t) * 1000)
        ret = res.get("retrieval") or {}
        ranked = dedupe(r.get("document_id") for r in ret.get("results") or [])
        context_docs = set(ranked)
        row = {"type": q["question_type"], "gold": q.get("answer"), "answer": (res.get("answer") or "")[:1500],
               "ms": total_ms, "timings": ret.get("timings"), "gold_docs": len(gold), "gold_unindexed": missing_gold,
               "context_docs": len(context_docs), "citations": len(res.get("citations") or [])}
        if gold:
            row["retrieval"] = retrieval_metrics(ranked, gold, ks=(1, 3, 5))
            row["retrieval"]["context_recall"] = len(gold & context_docs) / len(gold)
        st = ret.get("stair") or {}
        sel_docs = {s.get("document_id") for s in st.get("selected") or []}
        row["stair"] = {"method": st.get("method"), "selected": len(st.get("selected") or []),
                        "error": st.get("error"), "llm_calls": st.get("llm_calls"),
                        "doc_hit": bool(gold & sel_docs) if gold else None}
        row["outcome"], row["grade_method"] = await grade(judge, q, row["answer"])
        if key in faith_ids and row["outcome"] != "missing" and not is_abstention(row["answer"]):
            row["faithfulness"] = await faithfulness(judge, ret.get("context") or "", row["answer"])
        return row

    stats = await run_pool(items, worker, label="rag", concurrency=concurrency, deadline=deadline, store=store,
                           key=lambda it: it[0])
    rows = [r for r in store.rows() if "error" not in r and not r.get("gold_unindexed")]
    errors = [r for r in store.rows() if "error" in r]
    excluded = sum(1 for r in store.rows() if r.get("gold_unindexed"))

    ablation = await run_ablation(org, items, title_to_doc, run_dir=run_dir, n=ablation_n, concurrency=concurrency,
                                  deadline=deadline) if ablation_n else {}
    store.close()
    return summarize(rows, errors, ing["stats"], stats, ablation, indexed_docs=len(indexed), excluded=excluded)


async def run_ablation(org: BenchOrg, items: list, title_to_doc: dict[str, list[str]], *, run_dir: Path, n: int,
                       concurrency: int, deadline: float | None) -> dict:
    """BM25-only vs vectors-only vs hybrid (± rerank) on the first `n` answerable queries, top-20 chunks."""
    from app.core.db import session_scope
    from app.rag.service import search

    ab_items = [(k, q) for k, q in items if q["question_type"] != "null_query"][:n]
    ab_items = [(f"{k}:{name}", q, name) for k, q in ab_items for name in ABLATIONS]
    ab_store = ResultStore(run_dir / "rag_ablation.jsonl")

    async def ab_worker(item) -> dict:
        _key, q, name = item
        mode, rerank = ABLATIONS[name]
        gold = _gold_docs(q, title_to_doc)
        t = time.perf_counter()
        async with session_scope() as db:
            ret = await search(db, org_id=org.org_id, user_id=org.user_id, role="owner", query=q["query"],
                               mode=mode, top_k=20, rerank=rerank)
        ranked = dedupe(r.get("document_id") for r in ret.get("results") or [])
        return {"config": name, "ms": int((time.perf_counter() - t) * 1000),
                **(retrieval_metrics(ranked, gold, ks=(1, 3, 5, 10)) if gold else {})}

    await run_pool(ab_items, ab_worker, label="rag-ablation", concurrency=concurrency, deadline=deadline,
                   store=ab_store, key=lambda it: it[0])
    by_cfg: dict[str, list[dict]] = defaultdict(list)
    for r in ab_store.rows():
        if "error" not in r and "mrr" in r:
            by_cfg[r["config"]].append(r)
    ab_store.close()
    return {name: {"n": len(rs), **average_dicts(rs)} for name, rs in by_cfg.items()}


def summarize(rows: list[dict], errors: list[dict], ingest_stats: dict, pool_stats: dict, ablation: dict,
              *, indexed_docs: int, excluded: int = 0) -> dict:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    answerable = [r for r in rows if r["type"] != "null_query"]
    retr = [r["retrieval"] for r in answerable if r.get("retrieval")]
    faith = [r["faithfulness"] for r in rows if r.get("faithfulness") and r["faithfulness"].get("score") is not None]
    stair_rows = [r["stair"] for r in answerable if r.get("stair")]
    gen = {"overall": crag_scores([r["outcome"] for r in rows]),
           "answerable": crag_scores([r["outcome"] for r in answerable]),
           **{t: crag_scores([r["outcome"] for r in by_type[t]]) for t in TYPES if by_type.get(t)}}
    return {
        "dataset": "MultiHop-RAG (yixuantt/MultiHopRAG)",
        "ingestion": {**ingest_stats, "indexed_docs": indexed_docs},
        "queries": {"evaluated": len(rows), "errors": len(errors), "by_type": {t: len(by_type[t]) for t in TYPES},
                    "not_run_time_budget": pool_stats.get("timed_out", 0),
                    "excluded_gold_not_indexed": excluded},
        "retrieval": {"n": len(retr), **average_dicts(retr),
                      **{f"{t}": average_dicts([r["retrieval"] for r in by_type[t] if r.get("retrieval")])
                         for t in TYPES if t != "null_query" and by_type.get(t)}},
        "stair": {"n": len(stair_rows), "methods": dict(Counter(s.get("method") for s in stair_rows)),
                  "doc_hit_rate": mean(float(s["doc_hit"]) for s in stair_rows if s.get("doc_hit") is not None),
                  "error_rate": mean(float(bool(s.get("error"))) for s in stair_rows),
                  "avg_selected": mean(s.get("selected") for s in stair_rows)},
        "generation": gen,
        "null_query_abstention_rate": gen.get("null_query", {}).get("accuracy"),
        "grading": dict(Counter(r.get("grade_method") for r in rows)),
        "faithfulness": {"n": len(faith), "mean_supported": mean(f["score"] for f in faith),
                         "fully_supported_rate": mean(float(f["score"] >= 0.999) for f in faith),
                         "claims": sum(f["claims"] for f in faith)},
        "latency_ms": {"p50": percentile((r["ms"] for r in rows), 0.5), "p95": percentile((r["ms"] for r in rows), 0.95),
                       "retrieval_p50": percentile(((r.get("timings") or {}).get("total") for r in rows), 0.5),
                       "answer_p50": percentile(((r.get("timings") or {}).get("answer") for r in rows), 0.5)},
        "ablation": ablation,
        "error_samples": [e.get("error") for e in errors[:5]],
    }
