"""Tool calling (model-dependent): Berkeley Function-Calling Leaderboard v3, non-live AST categories.

Every Ren agent action is a native tool call made through Ren's LLM client, so this measures the model under test
*through Ren's tool-calling path* (vLLM tool parser → `LLMClient.chat(tools=…)` → parsed `ToolCall`s).

Categories: simple (1 function, 1 call), multiple (pick 1 of several functions), parallel (several calls of one
function), parallel_multiple (several calls across functions) and irrelevance (no function fits → must not call).
Scoring follows BFCL's AST checker: function name, no unexpected parameters, required parameters present, every
value among the allowed ground-truth values (strings compared case/punctuation-insensitively, ints accepted for
floats), parallel calls matched order-independently.
"""
from __future__ import annotations

import itertools
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench import data
from bench.common import ResultStore, run_pool, seeded_order
from bench.runtime import ModelSpec, ensure_org, llm_for

SUITE = "bfcl"
_TYPE_MAP = {"dict": "object", "float": "number", "tuple": "array", "any": "string", "int": "integer",
             "bool": "boolean", "list": "array", "str": "string", "double": "number", "long": "integer"}
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def tool_name(name: str) -> str:
    return _NAME_OK.sub("_", name)[:64]


def _schema(node: Any) -> Any:
    """BFCL's Python-flavoured JSON schema → strict JSON schema (dict→object, float→number, tuple→array, …)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = _TYPE_MAP.get(v, v)
            elif k in ("properties",) and isinstance(v, dict):
                out[k] = {pk: _schema(pv) for pk, pv in v.items()}
            elif k == "optional":
                continue
            else:
                out[k] = _schema(v)
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
        return out
    if isinstance(node, list):
        return [_schema(x) for x in node]
    return node


def to_tools(functions: list[dict]) -> tuple[list[dict], dict[str, str]]:
    tools, names = [], {}
    for f in functions:
        safe = tool_name(f["name"])
        names[safe] = f["name"]
        params = _schema(f.get("parameters") or {"type": "object", "properties": {}})
        params["type"] = "object"
        tools.append({"type": "function", "function": {"name": safe, "description": f.get("description", "")[:1024],
                                                       "parameters": params}})
    return tools, names


# ----------------------------------------------------------------------------------------- AST checker
_BFCL_STRIP = re.compile(r"[ ,./\-_*^]")


def _std(v: str) -> str:
    """BFCL's string standardisation: drop spaces and , . / - _ * ^, lowercase, ' → "."""
    return _BFCL_STRIP.sub("", str(v)).lower().replace("'", '"')


def value_ok(value: Any, allowed: list) -> bool:
    for a in allowed:
        if a == "" and value in (None, ""):
            return True
        if isinstance(a, bool) or isinstance(value, bool):
            if value is a or (isinstance(value, str) and str(a).lower() == value.lower()):
                return True
            continue
        if isinstance(a, (int, float)) and isinstance(value, (int, float)):
            if float(a) == float(value):
                return True
            continue
        if isinstance(a, (int, float)) and isinstance(value, str):
            try:
                if float(value) == float(a):
                    return True
            except ValueError:
                pass
            continue
        if isinstance(a, str) and isinstance(value, str):
            if _std(a) == _std(value):
                return True
            continue
        if isinstance(a, list) and isinstance(value, (list, tuple)):
            if len(a) == len(value) and all(value_ok(v, [x]) for v, x in zip(value, a, strict=True)):
                return True
            continue
        if isinstance(a, dict) and isinstance(value, dict):
            if set(value) <= set(a) and all(value_ok(value[k], a[k] if isinstance(a[k], list) else [a[k]])
                                            for k in value) and \
                    all(k in value or "" in (a[k] if isinstance(a[k], list) else [a[k]]) for k in a):
                return True
            continue
    return False


def call_ok(call: tuple[str, dict], expected: dict) -> bool:
    """`expected` = {func_name: {param: [allowed values, "" = optional]}}."""
    (fname, params), = expected.items()
    name, args = call
    if name != fname:
        return False
    for k in args:
        if k not in params:
            return False  # hallucinated parameter
    for k, allowed in params.items():
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if k not in args:
            if "" not in allowed:
                return False
            continue
        if not value_ok(args[k], allowed):
            return False
    return True


def check(category: str, calls: list[tuple[str, dict]], ground_truth: list[dict]) -> tuple[bool, str]:
    if category == "irrelevance":
        return (not calls), ("ok" if not calls else "called a tool for an irrelevant request")
    if len(calls) != len(ground_truth):
        return False, f"expected {len(ground_truth)} call(s), got {len(calls)}"
    if len(ground_truth) == 1:
        return call_ok(calls[0], ground_truth[0]), "ok" if call_ok(calls[0], ground_truth[0]) else "wrong call"
    for perm in itertools.permutations(range(len(calls))):
        if all(call_ok(calls[i], gt) for i, gt in zip(perm, ground_truth, strict=True)):
            return True, "ok"
    return False, "parallel calls don't match"


# ----------------------------------------------------------------------------------------- run
async def run(spec: ModelSpec, *, run_dir: Path, deadline: float | None, concurrency: int, seed: int,
              per_category: int | None) -> dict:
    org = await ensure_org("bfcl", spec)
    llm = await llm_for(org, "bench-bfcl")
    items = []
    for cat in data.BFCL_CATEGORIES:
        cat_items = seeded_order(data.load_bfcl(cat), seed)
        items.extend(cat_items[:per_category] if per_category else cat_items)
    items = seeded_order(items, seed)
    store = ResultStore(run_dir / "bfcl.jsonl")

    async def worker(it: dict) -> dict:
        tools, names = to_tools(it["function"])
        turns = it["question"][0] if it["question"] and isinstance(it["question"][0], list) else it["question"]
        t = time.perf_counter()
        res = await llm.chat(list(turns), tools=tools, temperature=0.0, max_tokens=1024)
        calls = [(names.get(tc.name, tc.name), tc.arguments if isinstance(tc.arguments, dict) else {})
                 for tc in res.tool_calls]
        ok, why = check(it["category"], calls, it.get("ground_truth") or [])
        return {"category": it["category"], "ok": ok, "why": why, "ms": int((time.perf_counter() - t) * 1000),
                "calls": json.dumps(calls)[:500], "content": (res.content or "")[:200] if not calls else ""}

    await run_pool(items, worker, label="bfcl", concurrency=concurrency, deadline=deadline, store=store,
                   key=lambda it: it["id"])
    rows = [r for r in store.rows() if "error" not in r]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    acc = {c: {"n": len(v), "accuracy": sum(r["ok"] for r in v) / len(v)} for c, v in by_cat.items()}
    ast = [acc[c]["accuracy"] for c in ("simple", "multiple", "parallel", "parallel_multiple") if c in acc]
    store.close()
    return {"dataset": "BFCL v3 (non-live AST)", "by_category": acc,
            "ast_summary": sum(ast) / len(ast) if ast else None,
            "irrelevance": acc.get("irrelevance", {}).get("accuracy"),
            "overall": sum(r["ok"] for r in rows) / len(rows) if rows else None, "n": len(rows),
            "errors": sum(1 for r in store.rows() if "error" in r)}
