"""Ren benchmark suite.

  python -m bench prepare                          # before the GPU hour: datasets + embedding/reranker models
  python -m bench check  [model options]           # preflight against the served model (chat, tools, choice, JSON, Laya)
  python -m bench run    [model options] [--minutes 45] [--suites rag,align,bfcl] ...
  python -m bench offline [--laya-url …]          # no model served: measure retrieval + Laya only
  python -m bench report bench_runs/runs/<run-id>  # rebuild report.md from summary.json

Model options (or BENCH_* env vars): --provider ollama|vllm|openrouter|openai|mock [--base-url …] [--model …]
[--api-key …] [--laya-url http://127.0.0.1:8001]. See bench/README.md for the H200 runbook.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RUNS = BACKEND / "bench_runs"


def _model_args(sp: argparse.ArgumentParser) -> None:
    env = os.environ.get
    sp.add_argument("--provider", default=env("BENCH_PROVIDER", "vllm"),
                    choices=["vllm", "ollama", "openrouter", "openai", "mock"])
    sp.add_argument("--base-url", default=env("BENCH_BASE_URL"),
                    help="default: vLLM http://127.0.0.1:8000/v1, Ollama http://127.0.0.1:11434/v1")
    sp.add_argument("--model", default=env("BENCH_MODEL"),
                    help="served model name (default: vLLM qwen3.6-35b-a3b, Ollama qwen3.6:35b-a3b-mtp-q8_0)")
    sp.add_argument("--api-key", default=env("BENCH_API_KEY"), help="vLLM --api-key / provider key")
    sp.add_argument("--thinking", action="store_true", help="enable the model's thinking mode (slower; default off)")
    sp.add_argument("--laya-url", default=env("BENCH_LAYA_URL"), help="Laya server (omit to skip Laya)")
    sp.add_argument("--workdir", type=Path, default=RUNS / "work",
                    help="isolated Ren instance (DB, uploads, index) — reused across runs")


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="download datasets and embedding/reranker models")
    sp.add_argument("--workdir", type=Path, default=RUNS / "work")

    _model_args(sub.add_parser("check", help="preflight the served model"))

    sp = sub.add_parser("run", help="run benchmark suites")
    _model_args(sp)
    sp.add_argument("--suites", default="rag,align,bfcl")
    sp.add_argument("--minutes", type=float, default=45, help="overall time budget (suites run concurrently)")
    sp.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"), help="reuse an id to resume a run")
    sp.add_argument("--concurrency", type=int, default=24, help="parallel items per suite")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--skip-preflight", action="store_true")
    g = sp.add_argument_group("rag (MultiHop-RAG)")
    g.add_argument("--rag-queries", type=int, default=None, help="max queries (default: all 2,556)")
    g.add_argument("--ingest-concurrency", type=int, default=12)
    g.add_argument("--faithfulness", type=int, default=300, help="answers to check claim-by-claim (0 = off)")
    g.add_argument("--ablation", type=int, default=300, help="queries for the retrieval ablation (0 = off)")
    g.add_argument("--top-k", type=int, default=8, help="chunks in the answer context (Ren default 8)")
    g.add_argument("--corpus-limit", type=int, default=None, help="dry runs: index only gold + N articles")
    g = sp.add_argument_group("align (DynaBench + BeaverTails)")
    g.add_argument("--dynabench", type=int, default=None, help="max DynaBench examples (default: all 543)")
    g.add_argument("--beavertails", type=int, default=600, help="balanced BeaverTails sample (0 = off)")
    g.add_argument("--guard-sample", type=int, default=25, help="transcripts re-checked through check_text")
    g = sp.add_argument_group("bfcl (tool calling)")
    g.add_argument("--bfcl-per-category", type=int, default=None, help="max items per category (default: all)")

    sp = sub.add_parser("offline", help="no model served: measure the model-free parts (retrieval, Laya)")
    sp.add_argument("--suites", default="rag,align")
    sp.add_argument("--laya-url", default=os.environ.get("BENCH_LAYA_URL"), help="Laya server (needed for align)")
    sp.add_argument("--workdir", type=Path, default=RUNS / "work-offline",
                    help="isolated Ren instance with no model provider (keep it apart from model runs)")
    sp.add_argument("--minutes", type=float, default=120)
    sp.add_argument("--run-id", default="offline-" + time.strftime("%Y%m%d-%H%M%S"))
    sp.add_argument("--concurrency", type=int, default=4)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--rag-queries", type=int, default=None)
    sp.add_argument("--ingest-concurrency", type=int, default=4)
    sp.add_argument("--ablation", type=int, default=300)
    sp.add_argument("--top-k", type=int, default=8)
    sp.add_argument("--dynabench", type=int, default=None)
    sp.add_argument("--beavertails", type=int, default=600)

    sp = sub.add_parser("report", help="rebuild report.md for a run")
    sp.add_argument("run_dir", type=Path)
    return p.parse_args(argv)


def _spec(a: argparse.Namespace):
    from bench.runtime import DEFAULT_BASE_URLS, ModelSpec

    base_url = "" if a.provider == "mock" else (a.base_url or DEFAULT_BASE_URLS.get(a.provider, ""))
    model = a.model or {"ollama": "qwen3.6:35b-a3b-mtp-q8_0", "mock": "mock-1"}.get(a.provider, "qwen3.6-35b-a3b")
    return ModelSpec(kind=a.provider, base_url=base_url, model=model, api_key=a.api_key, thinking=a.thinking)


# ----------------------------------------------------------------------------------------- commands
def _seed_models(workdir: Path) -> None:
    """Reuse the embedding/reranker models already downloaded for the dev backend."""
    from bench.common import log

    src, dst = BACKEND / "data" / "models" / "fastembed", workdir / "data" / "models" / "fastembed"
    if src.exists() and not dst.exists():
        log(f"copying cached embedding/reranker models from {src}")
        shutil.copytree(src, dst)


async def cmd_prepare(a: argparse.Namespace) -> None:
    from bench import data
    from bench.common import log
    from bench.runtime import bootstrap

    data.prepare()
    bootstrap(a.workdir)
    _seed_models(a.workdir)
    from app.rag.embeddings import embed_texts
    from app.rag.rerank import rerank

    t = time.perf_counter()
    await embed_texts(["warm-up"])
    await rerank("warm-up", ["a passage"])
    log(f"embedding + reranker models ready ({time.perf_counter() - t:.1f}s)")


async def cmd_check(a: argparse.Namespace) -> dict:
    from bench import runtime

    runtime.bootstrap(a.workdir, laya_url=a.laya_url)
    await runtime.init()
    org = await runtime.ensure_org("preflight", _spec(a))
    return await runtime.preflight(org)


async def cmd_run(a: argparse.Namespace) -> Path:
    from bench import runtime

    runtime.bootstrap(a.workdir, laya_url=a.laya_url)
    from bench import align, rag_multihop, report, tools_bfcl
    from bench.common import log

    suites = [s.strip() for s in a.suites.split(",") if s.strip()]
    run_dir = RUNS / "runs" / a.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started, t0 = time.strftime("%Y-%m-%d %H:%M:%S"), time.monotonic()
    deadline = t0 + a.minutes * 60
    await runtime.init()
    spec = _spec(a)
    pre = None
    if not a.skip_preflight:
        pre = await runtime.preflight(await runtime.ensure_org("preflight", spec))
        if not pre.get("chat", {}).get("ok"):
            raise SystemExit("preflight: the model endpoint doesn't answer chat requests — fix that first.")
        if pre.get("context", {}).get("ok") is False:
            raise SystemExit("preflight: " + pre["context"]["detail"] + " — results would be silently wrong.")
    config = {"model": spec.label(), "base_url": spec.base_url, "laya_url": a.laya_url, "thinking": a.thinking,
              "suites": suites, "minutes": a.minutes, "concurrency": a.concurrency, "seed": a.seed,
              "rag_queries": a.rag_queries, "faithfulness": a.faithfulness, "ablation": a.ablation, "top_k": a.top_k,
              "dynabench": a.dynabench, "beavertails": a.beavertails, "bfcl_per_category": a.bfcl_per_category,
              "corpus_limit": a.corpus_limit}
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    log(f"run {a.run_id}: {', '.join(suites)} · {spec.label()} · budget {a.minutes:.0f} min → {run_dir}")

    jobs = {}
    if "rag" in suites:
        jobs["rag"] = rag_multihop.run(
            spec, run_dir=run_dir, deadline=deadline, ingest_deadline=t0 + a.minutes * 60 * 0.45,
            max_queries=a.rag_queries, concurrency=a.concurrency, ingest_concurrency=a.ingest_concurrency,
            faith_n=a.faithfulness, ablation_n=a.ablation, seed=a.seed, top_k=a.top_k, corpus_limit=a.corpus_limit)
    if "align" in suites:
        jobs["align"] = align.run(spec, run_dir=run_dir, deadline=deadline, concurrency=max(4, a.concurrency // 2),
                                  seed=a.seed, dynabench_n=a.dynabench, beavertails_n=a.beavertails,
                                  guard_sample=a.guard_sample)
    if "bfcl" in suites:
        jobs["bfcl"] = tools_bfcl.run(spec, run_dir=run_dir, deadline=deadline, concurrency=max(4, a.concurrency // 2),
                                      seed=a.seed, per_category=a.bfcl_per_category)
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    results, errors = {}, {}
    for name, out in zip(jobs, outcomes, strict=True):
        if isinstance(out, BaseException):
            errors[name] = f"{type(out).__name__}: {out}"
            log(f"suite {name} failed: {errors[name]}")
        else:
            results[name] = out
    summary = {"config": config, "started_at": started, "minutes": (time.monotonic() - t0) / 60, "preflight": pre,
               "suites": results, "suite_errors": errors}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    report.build(run_dir)
    log(f"done in {summary['minutes']:.1f} min → {run_dir / 'report.md'}")
    return run_dir


async def cmd_offline(a: argparse.Namespace) -> Path:
    from bench import runtime

    runtime.bootstrap(a.workdir, laya_url=a.laya_url)
    from bench import offline, report
    from bench.common import log

    suites = [s.strip() for s in a.suites.split(",") if s.strip()]
    run_dir = RUNS / "runs" / a.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started, t0 = time.strftime("%Y-%m-%d %H:%M:%S"), time.monotonic()
    deadline = t0 + a.minutes * 60
    if "rag" in suites:
        _seed_models(a.workdir)
        await runtime.init()
    config = {"mode": "offline", "model": "none (model-free measurement)", "laya_url": a.laya_url, "suites": suites,
              "minutes": a.minutes, "concurrency": a.concurrency, "seed": a.seed, "rag_queries": a.rag_queries,
              "ablation": a.ablation, "top_k": a.top_k, "dynabench": a.dynabench, "beavertails": a.beavertails}
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    log(f"offline run {a.run_id}: {', '.join(suites)} · no model · budget {a.minutes:.0f} min → {run_dir}")
    jobs = {}
    if "rag" in suites:
        jobs["rag"] = offline.run_rag(run_dir=run_dir, deadline=deadline, max_queries=a.rag_queries,
                                      concurrency=a.concurrency, ingest_concurrency=a.ingest_concurrency,
                                      ablation_n=a.ablation, seed=a.seed, top_k=a.top_k)
    if "align" in suites:
        jobs["align"] = offline.run_align(run_dir=run_dir, deadline=deadline, concurrency=a.concurrency, seed=a.seed,
                                          dynabench_n=a.dynabench, beavertails_n=a.beavertails)
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    results, errors = {}, {}
    for name, out in zip(jobs, outcomes, strict=True):
        if isinstance(out, BaseException):
            errors[name] = f"{type(out).__name__}: {out}"
            log(f"suite {name} failed: {errors[name]}")
        else:
            results[name] = out
    summary = {"mode": "offline", "config": config, "started_at": started, "minutes": (time.monotonic() - t0) / 60,
               "suites": results, "suite_errors": errors}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    report.build(run_dir)
    log(f"done in {summary['minutes']:.1f} min → {run_dir / 'report.md'}")
    return run_dir


def main(argv: list[str] | None = None) -> None:
    a = parse(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):  # legacy Windows consoles can't print → · — in progress lines
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if a.cmd == "prepare":
        asyncio.run(cmd_prepare(a))
    elif a.cmd == "check":
        out = asyncio.run(cmd_check(a))
        ok = all(v.get("ok") is not False for v in out.values() if isinstance(v, dict))
        raise SystemExit(0 if ok else 1)
    elif a.cmd == "run":
        asyncio.run(cmd_run(a))
    elif a.cmd == "offline":
        asyncio.run(cmd_offline(a))
    elif a.cmd == "report":
        from bench import report

        print(report.build(a.run_dir))


if __name__ == "__main__":
    main()
