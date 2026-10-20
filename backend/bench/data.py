"""Benchmark datasets, downloaded once from Hugging Face into bench/.cache (no extra dependencies).

- MultiHop-RAG (yixuantt/MultiHopRAG, ODC-BY): 609 news articles + 2,556 multi-hop queries with gold evidence;
  301 "null" queries have no answer in the corpus (the system must decline → hallucination test).
- DynaBench (montehoover/DynaBench, MIT): 543 hand-written dialogues with user-defined policies, PASS/FAIL labels.
- BeaverTails (PKU-Alignment/BeaverTails, CC-BY-NC-4.0): 30k_test QA pairs with is_safe + 14 harm categories.
- BFCL v3 (gorilla-llm/Berkeley-Function-Calling-Leaderboard, Apache-2.0): function-calling questions + answers.
"""
from __future__ import annotations

import gzip
import json
import lzma
import os
import time
from pathlib import Path

import httpx

from bench.common import log

CACHE = Path(os.environ.get("BENCH_CACHE") or Path(__file__).resolve().parent / ".cache")
HF = "https://huggingface.co/datasets"
ROWS_API = "https://datasets-server.huggingface.co/rows"

MULTIHOP = "yixuantt/MultiHopRAG"
DYNABENCH = "montehoover/DynaBench"
BEAVERTAILS = "PKU-Alignment/BeaverTails"
BFCL = "gorilla-llm/Berkeley-Function-Calling-Leaderboard"
BFCL_CATEGORIES = ("simple", "multiple", "parallel", "parallel_multiple", "irrelevance")


def _headers() -> dict:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _retry(fn, what: str, attempts: int = 6):
    """HF endpoints return the odd 502/503 — back off and retry instead of failing the whole prepare."""
    for i in range(attempts):
        try:
            return fn()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            if (status is not None and status < 500 and status != 429) or i == attempts - 1:
                raise
            wait = min(30, 2 ** i)
            log(f"{what}: {status or type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)


def _path(repo: str, name: str) -> Path:
    return CACHE / repo.replace("/", "__") / name


def _download(client: httpx.Client, repo: str, name: str) -> Path:
    dest = _path(repo, name)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HF}/{repo}/resolve/main/{name}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    def get() -> None:
        with client.stream("GET", url, headers=_headers(), follow_redirects=True) as r:
            if r.status_code in (401, 403):
                raise RuntimeError(f"{repo} needs a Hugging Face token (accept its terms, then set HF_TOKEN)")
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 16):
                    fh.write(chunk)

    _retry(get, f"{repo}/{name}")
    tmp.replace(dest)
    return dest


def _rows(client: httpx.Client, repo: str, config: str, split: str) -> Path:
    """All rows of a (small) split via the datasets-server JSON API — avoids a parquet dependency."""
    dest = _path(repo, f"{config}-{split}.json")
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    rows, offset = [], 0
    while True:
        def page(offset: int = offset):
            r = client.get(ROWS_API, params={"dataset": repo, "config": config, "split": split, "offset": offset,
                                             "length": 100}, headers=_headers(), timeout=60)
            r.raise_for_status()
            return r.json()

        data = _retry(page, f"{repo} rows {offset}")
        batch = [row["row"] for row in data.get("rows", [])]
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(data.get("num_rows_total") or 0):
            break
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return dest


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _read_jsonl(p: Path) -> list[dict]:
    opener = gzip.open if p.suffix == ".gz" else lzma.open if p.suffix == ".xz" else open
    with opener(p, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ----------------------------------------------------------------------------------------- download everything
def prepare() -> dict:
    """Download every dataset (idempotent). Returns item counts per dataset."""
    with httpx.Client(timeout=120) as c:
        _download(c, MULTIHOP, "corpus.json")
        _download(c, MULTIHOP, "MultiHopRAG.json")
        _rows(c, DYNABENCH, "DynaBench", "test")
        _download(c, BEAVERTAILS, "round0/30k/test.jsonl.gz")
        for cat in BFCL_CATEGORIES:
            _download(c, BFCL, f"BFCL_v3_{cat}.json")
            if cat != "irrelevance":
                _download(c, BFCL, f"possible_answer/BFCL_v3_{cat}.json")
    corpus, queries = load_multihop()
    counts = {"multihop_corpus": len(corpus), "multihop_queries": len(queries), "dynabench": len(load_dynabench()),
              "beavertails_test": len(load_beavertails()),
              **{f"bfcl_{c}": len(load_bfcl(c)) for c in BFCL_CATEGORIES}}
    log("datasets ready: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


# ----------------------------------------------------------------------------------------- loaders
def load_multihop() -> tuple[list[dict], list[dict]]:
    return _read_json(_path(MULTIHOP, "corpus.json")), _read_json(_path(MULTIHOP, "MultiHopRAG.json"))


def load_dynabench() -> list[dict]:
    rows = _read_json(_path(DYNABENCH, "DynaBench-test.json"))
    for i, r in enumerate(rows):
        r.setdefault("idx", i)
        if isinstance(r.get("metadata"), str):
            try:
                r["metadata"] = json.loads(r["metadata"])
            except json.JSONDecodeError:
                r["metadata"] = {}
    return rows


def load_beavertails() -> list[dict]:
    rows = _read_jsonl(_path(BEAVERTAILS, "round0/30k/test.jsonl.gz"))
    for i, r in enumerate(rows):
        r.setdefault("idx", i)
    return rows


def load_bfcl(category: str) -> list[dict]:
    """BFCL items with `ground_truth` attached (irrelevance items have none — the model must not call a tool)."""
    items = _read_jsonl(_path(BFCL, f"BFCL_v3_{category}.json"))
    if category != "irrelevance":
        answers = {a["id"]: a["ground_truth"] for a in _read_jsonl(_path(BFCL, f"possible_answer/BFCL_v3_{category}.json"))}
        for it in items:
            it["ground_truth"] = answers.get(it["id"], [])
    for it in items:
        it["category"] = category
    return items
