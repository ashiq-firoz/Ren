"""summary.json → report.md (tables per suite, with the caveats that matter when reading the numbers)."""
from __future__ import annotations

import json
from pathlib import Path


def pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def num(x, d: int = 3) -> str:
    if x is None:
        return "—"
    return f"{x:.{d}f}" if isinstance(x, float) else str(x)


def ms(x) -> str:
    return "—" if x is None else (f"{x / 1000:.2f} s" if x >= 1000 else f"{x:.0f} ms")


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _index_time(ing: dict) -> str:
    if ing.get("indexed_this_run", 1) == 0:
        return "reused from an earlier run"
    return f"indexed in {num(ing.get('seconds'), 0)} s"


def _rag(s: dict) -> list[str]:
    lines = ["## 1 · RAG — MultiHop-RAG (retrieval + hallucination)", ""]
    ing = s.get("ingestion", {})
    lines.append(f"Corpus: {ing.get('ready', '—')}/{ing.get('articles', '—')} articles indexed by Ren "
                 f"({ing.get('failed', 0)} failed), {_index_time(ing)} · "
                 f"{ing.get('synthetic_questions', 0):,} synthetic questions · "
                 f"{ing.get('llm_tokens', 0):,} ingestion LLM tokens.")
    q = s.get("queries", {})
    types = ", ".join(f"{k.split('_')[0]} {v}" for k, v in (q.get("by_type") or {}).items())
    lines.append(f"Queries evaluated: **{q.get('evaluated')}** ({types}); errors {q.get('errors')}; not run (time "
                 f"budget) {q.get('not_run_time_budget')}; excluded because a gold article failed to index "
                 f"{q.get('excluded_gold_not_indexed')}.")
    lines += ["", "### Hallucination & answer quality (CRAG scoring)", "",
              "Correct = right answer · Hallucinated = confident wrong answer · Missing = declined. "
              "Truthfulness = correct − hallucinated. Null queries have **no answer in the corpus** — declining is the "
              "only correct behaviour.", ""]
    gen = s.get("generation", {})
    rows = []
    for key, label in (("overall", "All queries"), ("answerable", "Answerable"), ("inference_query", "Inference"),
                       ("comparison_query", "Comparison"), ("temporal_query", "Temporal"),
                       ("null_query", "Null (unanswerable)")):
        g = gen.get(key)
        if g and g.get("n"):
            rows.append([label, g["n"], pct(g["accuracy"]), pct(g["hallucination"]), pct(g["missing"]),
                         num(g["truthfulness"])])
    lines += [table(["Queries", "n", "Correct", "Hallucinated", "Missing", "Truthfulness"], rows), ""]
    f = s.get("faithfulness", {})
    if f.get("n"):
        lines.append(f"Claim-level faithfulness (LLM judge, {f['n']} answers, {f.get('claims', 0)} claims): "
                     f"**{pct(f.get('mean_supported'))}** of claims supported by the retrieved sources; "
                     f"{pct(f.get('fully_supported_rate'))} of answers fully supported.")
    lines.append(f"Grading: {', '.join(f'{k} {v}' for k, v in (s.get('grading') or {}).items())} "
                 "(heuristic = exact entity / yes-no / refusal match; judge = constrained LLM grader).")
    r = s.get("retrieval", {})
    lines += ["", "### Retrieval (document level, Ren's answer path: hybrid + STAIR + rerank, top-k chunks)", ""]
    rrows = [["All answerable", r.get("n"), pct(r.get("recall@1")), pct(r.get("recall@3")), pct(r.get("recall@5")),
              pct(r.get("hit@5")), pct(r.get("all@5")), num(r.get("mrr")), num(r.get("ndcg@5")),
              pct(r.get("context_recall"))]]
    for t, label in (("inference_query", "Inference"), ("comparison_query", "Comparison"), ("temporal_query", "Temporal")):
        rt = r.get(t) or {}
        if rt:
            rrows.append([label, "", pct(rt.get("recall@1")), pct(rt.get("recall@3")), pct(rt.get("recall@5")),
                          pct(rt.get("hit@5")), pct(rt.get("all@5")), num(rt.get("mrr")), num(rt.get("ndcg@5")),
                          pct(rt.get("context_recall"))])
    lines += [table(["Queries", "n", "Recall@1", "Recall@3", "Recall@5", "Hit@5", "All-evidence@5", "MRR",
                     "nDCG@5", "Gold docs in context"], rrows), ""]
    st = s.get("stair", {})
    if st.get("n"):
        lines.append(f"STAIR leaf selection: a selected section came from a gold article in "
                     f"**{pct(st.get('doc_hit_rate'))}** of queries · avg {num(st.get('avg_selected'), 1)} sections · "
                     f"errors {pct(st.get('error_rate'))} · methods {st.get('methods')}.")
    lines += _ablation(s.get("ablation") or {})
    lat = s.get("latency_ms", {})
    lines += ["", f"Latency per question (retrieval + answer + grading excluded): p50 {ms(lat.get('p50'))}, "
                  f"p95 {ms(lat.get('p95'))} · retrieval p50 {ms(lat.get('retrieval_p50'))} · "
                  f"answer generation p50 {ms(lat.get('answer_p50'))}.", ""]
    return lines


def _ablation(ab: dict) -> list[str]:
    if not ab:
        return []
    rows = [[name, v.get("n"), pct(v.get("recall@1")), pct(v.get("recall@5")), pct(v.get("recall@10")),
             num(v.get("mrr")), num(v.get("ndcg@10")), ms(v.get("ms"))] for name, v in ab.items()]
    return ["", "### Retrieval ablation (top-20 chunks → distinct documents)", "",
            table(["Config", "n", "Recall@1", "Recall@5", "Recall@10", "MRR", "nDCG@10", "Mean latency"], rows)]


def _rag_offline(s: dict) -> list[str]:
    lines = ["## 1 · RAG retrieval — MultiHop-RAG (measured, no LLM)", ""]
    ing = s.get("ingestion", {})
    lines.append(f"Index: {ing.get('ready', '—')}/{ing.get('articles', '—')} articles ({ing.get('failed', 0)} failed), "
                 f"{_index_time(ing)} — {s.get('index', '')}.")
    q = s.get("queries", {})
    types = ", ".join(f"{k.split('_')[0]} {v}" for k, v in (q.get("by_type") or {}).items())
    lines.append(f"Answerable queries evaluated: **{q.get('evaluated')}** ({types}); errors {q.get('errors')}; not run "
                 f"(time budget) {q.get('not_run_time_budget')}; reranked {pct(q.get('reranked_rate'))}.")
    r = s.get("retrieval", {})
    rows = [["All answerable", r.get("n"), pct(r.get("recall@1")), pct(r.get("recall@3")), pct(r.get("recall@5")),
             pct(r.get("hit@5")), pct(r.get("all@5")), num(r.get("mrr")), num(r.get("ndcg@5")),
             pct(r.get("context_recall"))]]
    for t, label in (("inference_query", "Inference"), ("comparison_query", "Comparison"), ("temporal_query", "Temporal")):
        rt = r.get(t) or {}
        if rt:
            rows.append([label, "", pct(rt.get("recall@1")), pct(rt.get("recall@3")), pct(rt.get("recall@5")),
                         pct(rt.get("hit@5")), pct(rt.get("all@5")), num(rt.get("mrr")), num(rt.get("ndcg@5")),
                         pct(rt.get("context_recall"))])
    lines += ["", "### Retrieval (document level, Ren's answer path: hybrid BM25 + vectors → rerank → top-k chunks)", "",
              table(["Queries", "n", "Recall@1", "Recall@3", "Recall@5", "Hit@5", "All-evidence@5", "MRR", "nDCG@5",
                     "Gold docs in context"], rows), ""]
    for key, what in (("evidence_coverage", "gold evidence articles"),
                      ("fact_coverage", "gold evidence facts (the sentences the question was written from)")):
        cov = s.get(key) or {}
        rows = []
        for k, label in (("all", "All answerable"), ("inference_query", "Inference"), ("comparison_query", "Comparison"),
                         ("temporal_query", "Temporal"), ("yes_no", "Yes/no answers"), ("entity", "Entity answers")):
            c = cov.get(k) or {}
            if c.get("n"):
                rows.append([label, c["n"], pct(c["full"]), pct(c["partial"]), pct(c["none"])])
        for k, c in (cov.get("by_gold_articles") or {}).items():
            if c.get("n"):
                rows.append([f"Needs {k} articles", c["n"], pct(c["full"]), pct(c["partial"]), pct(c["none"])])
        if rows:
            lines += [f"### Evidence in the answer context: {what.split(' (')[0]}", "",
                      f"Share of questions whose answer context (the text the model would get) holds all, some or none "
                      f"of their {what}.", "", table(["Queries", "n", "All", "Some", "None"], rows), ""]
    if s.get("context_words_p50"):
        lines.append(f"Answer context size: p50 {s['context_words_p50']:,.0f} words.")
    lines += _ablation(s.get("ablation") or {})
    lat = s.get("latency_ms", {})
    lines += ["", f"Retrieval latency per question: p50 {ms(lat.get('p50'))}, p95 {ms(lat.get('p95'))} "
                  f"(rerank p50 {ms(lat.get('rerank_p50'))}).", ""]
    return lines


def _align_offline(s: dict) -> list[str]:
    lines = ["## 2 · Alignment — Laya alone (measured, no LLM judge)", ""]
    for key, name, positive in (("dynabench", "DynaBench (user-defined policies, multi-turn transcripts)", "violations"),
                                ("beavertails", "BeaverTails 30k_test (safety, 14 harm categories)", "unsafe")):
        b = s.get(key) or {}
        if not b.get("n"):
            continue
        m = b["laya"]
        lines += [f"### {name}", "", f"n = {b['n']} ({b.get('positives')} {positive}); errors {b.get('errors', 0)}.", ""]
        rows = [["Laya alone (threshold 0.5)", pct(m["precision"]), pct(m["recall"]), f"**{pct(m['f1'])}**",
                 pct(m["accuracy"]), pct(m["false_positive_rate"])]]
        best = b.get("laya_best")
        if best:
            rows.append([f"Laya alone, best threshold {best['threshold']} (oracle reference)", pct(best["precision"]),
                         pct(best["recall"]), pct(best["f1"]), pct(best["accuracy"]), pct(best["false_positive_rate"])])
        lat = b.get("latency_ms") or {}
        lines += [table(["Checker", "Precision", "Recall", "F1", "Accuracy", "False-positive rate"], rows), "",
                  f"Laya ROC-AUC **{num(b.get('laya_auc'))}**. In triage it would clear "
                  f"**{pct(b.get('judge_calls_avoided'))}** of responses without calling the LLM judge, so triage can "
                  f"catch at most the {pct(m['recall'])} of {positive} that Laya escalates. Latency p50 "
                  f"{ms(lat.get('laya_p50'))}, p95 {ms(lat.get('laya_p95'))}.", ""]
        fm = b.get("laya_by_failure_mode") or {}
        if fm:
            lines += ["Laya by failure mode (recall = violations escalated):", "",
                      table(["Failure mode", "n", "Recall", "Accuracy"],
                            [[k, v["n"], pct(v["recall"]), pct(v["accuracy"])] for k, v in list(fm.items())[:12]]), ""]
        cats = b.get("laya_recall_by_category") or {}
        if cats:
            lines += ["Laya recall on unsafe responses by category:", "",
                      table(["Category", "n", "Recall"], [[k, v["n"], pct(v["recall"])] for k, v in cats.items()]), ""]
    if s.get("error_kinds"):
        lines += [f"Errors: {s['error_kinds']}, e.g. {(s.get('errors_sample') or [''])[0]}", ""]
    return lines


def _offline(run_dir: Path, summary: dict) -> str:
    cfg = summary.get("config", {})
    suites = summary.get("suites", {})
    laya = f"on ({cfg['laya_url']})" if cfg.get("laya_url") else "off"
    lines = ["# Ren benchmark — model-free measurements", "",
             f"Run `{run_dir.name}` · {summary.get('started_at', '')} · {num(summary.get('minutes'), 1)} min · "
             f"Laya {laya} · no LLM", "",
             "No model was served for this run, so it measures only the parts of Ren that need none: retrieval over an "
             "index built without an LLM, and Laya alone. Answer quality, hallucination, the LLM judge, triage and "
             "tool calling need `python -m bench run` against a served model.", ""]
    if "rag" in suites:
        lines += _rag_offline(suites["rag"])
    if "align" in suites:
        lines += _align_offline(suites["align"])
    for name, err in (summary.get("suite_errors") or {}).items():
        lines += [f"> ⚠️ Suite `{name}` failed: {err}", ""]
    lines += ["## Reading these numbers", ""]
    if "rag" in suites:
        lines += ["- The index has no LLM-built parts (synthetic questions, LLM summaries, STAIR leaf selection), so "
                  "retrieval here is a floor for Ren's full pipeline.",
                  "- Retrieval is scored at document level against MultiHop-RAG's gold evidence articles; multi-hop "
                  "questions need 2–4 articles in the context at once."]
    if "align" in suites:
        lines.append("- Laya runs zero-shot. Its best-threshold row uses the test labels and is a reference point, not "
                     "a deployable setting.")
    text = "\n".join([*lines, ""])
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


def _align_block(name: str, s: dict, positive: str) -> list[str]:
    lines = [f"### {name}", "", f"n = {s.get('n')} ({s.get('positives')} {positive}); errors {s.get('errors', 0)}.", ""]
    rows = []
    for key, label in (("judge", "LLM judge on every response (Ren without Laya)"),
                       ("triage", "**Ren default — Laya screen ≥ 0.5 → LLM judge confirms**"),
                       ("laya", "Laya alone (threshold 0.5)")):
        m = s.get(key)
        if m:
            rows.append([label, pct(m["precision"]), pct(m["recall"]), f"**{pct(m['f1'])}**", pct(m["accuracy"]),
                         pct(m["false_positive_rate"])])
    best = s.get("laya_best")
    if best:
        rows.append([f"Laya alone, best threshold {best['threshold']} (oracle reference)", pct(best["precision"]),
                     pct(best["recall"]), pct(best["f1"]), pct(best["accuracy"]), pct(best["false_positive_rate"])])
    lines += [table(["Checker", "Precision", "Recall", "F1", "Accuracy", "False-positive rate"], rows), ""]
    lat = s.get("latency_ms", {})
    extra = []
    if s.get("laya_auc") is not None:
        extra.append(f"Laya ROC-AUC **{num(s['laya_auc'])}**")
    if s.get("judge_calls_avoided") is not None:
        extra.append(f"triage skipped the LLM judge for **{pct(s['judge_calls_avoided'])}** of responses")
    extra.append(f"latency p50 — judge {ms(lat.get('judge_p50'))}"
                 + (f", Laya {ms(lat.get('laya_p50'))}, triage {ms(lat.get('triage_p50'))}" if lat.get("laya_p50") else ""))
    lines += ["; ".join(extra) + ".", ""]
    return lines


def _align(s: dict) -> list[str]:
    lines = ["## 2 · Alignment — policy guard", ""]
    if s.get("dynabench"):
        lines += _align_block("DynaBench (user-defined policies, multi-turn transcripts)", s["dynabench"], "violations")
        fm = s["dynabench"].get("judge_by_failure_mode") or {}
        if fm:
            lines += ["LLM judge by failure mode:", "",
                      table(["Failure mode", "n", "F1", "Accuracy"],
                            [[k, v["n"], pct(v["f1"]), pct(v["accuracy"])] for k, v in list(fm.items())[:12]]), ""]
    if s.get("beavertails"):
        lines += _align_block("BeaverTails 30k_test (safety, 14 harm categories)", s["beavertails"], "unsafe")
        cats = s["beavertails"].get("judge_recall_by_category") or {}
        if cats:
            lines += ["LLM judge recall on unsafe responses by category:", "",
                      table(["Category", "n", "Recall"], [[k, v["n"], pct(v["recall"])] for k, v in cats.items()]), ""]
    pc = s.get("pipeline_check")
    if pc and pc.get("n"):
        lines += [f"End-to-end check: the composed `check_text` pipeline agreed with the derived configuration on "
                  f"{pct(pc.get('agreement'))} of {pc['n']} sampled transcripts (verdicts {pc.get('verdicts')}).", ""]
    return lines


def _bfcl(s: dict) -> list[str]:
    lines = ["## 3 · Tool calling — BFCL v3 (model-dependent)", ""]
    rows = [[c, v["n"], pct(v["accuracy"])] for c, v in (s.get("by_category") or {}).items()]
    lines += [table(["Category", "n", "Accuracy"], rows), "",
              f"AST summary (mean of simple/multiple/parallel/parallel_multiple): **{pct(s.get('ast_summary'))}** · "
              f"irrelevance detection {pct(s.get('irrelevance'))} · errors {s.get('errors', 0)}.", ""]
    return lines


def build(run_dir: Path) -> str:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("mode") == "offline":
        return _offline(run_dir, summary)
    cfg = summary.get("config", {})
    lines = [f"# Ren benchmark — {cfg.get('model', '?')}", "",
             f"Run `{run_dir.name}` · {summary.get('started_at', '')} · {num(summary.get('minutes'), 1)} min · "
             f"Laya {'on (' + cfg.get('laya_url', '') + ')' if cfg.get('laya_url') else 'off'} · thinking "
             f"{'on' if cfg.get('thinking') else 'off'} · concurrency {cfg.get('concurrency')}", ""]
    pf = summary.get("preflight") or {}
    if pf:
        lines.append("Preflight: " + ", ".join(f"{k} {'✓' if v.get('ok') else ('—' if v.get('ok') is None else '✗')}"
                                               for k, v in pf.items() if isinstance(v, dict)))
        lines.append("")
    suites = summary.get("suites", {})
    if "rag" in suites:
        lines += _rag(suites["rag"])
    if "align" in suites:
        lines += _align(suites["align"])
    if "bfcl" in suites:
        lines += _bfcl(suites["bfcl"])
    for name, err in (summary.get("suite_errors") or {}).items():
        lines += [f"> ⚠️ Suite `{name}` failed: {err}", ""]
    lines += ["## Reading these numbers", "",
              "- The LLM judge and the answer grader run on the **model under test** (self-grading). Grading is "
              "constrained (fixed labels) and exact matches skip the judge, but treat small differences between runs "
              "as noise.",
              "- Retrieval is scored at document level against MultiHop-RAG's gold evidence articles; Ren retrieves "
              "sections/chunks, and multi-hop questions need 2–4 articles in context at once (All-evidence@5).",
              "- Laya runs zero-shot (not fine-tuned on these policies). Its best-threshold row uses the test labels "
              "and is a reference point, not a deployable setting.",
              "- A run cut short by its time budget processed items in a seeded random order, so partial results are "
              "still a random sample; compare runs with the same seed and sizes.", ""]
    text = "\n".join(lines)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text
