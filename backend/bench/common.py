"""Shared plumbing: resumable JSONL result stores, a time-boxed worker pool with progress, small stats helpers."""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_secs(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def seeded_order(items: list, seed: int) -> list:
    """Deterministic shuffle: a run cut short by its time budget is still a random sample."""
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


def percentile(values: Iterable[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] if lo == hi else vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def mean(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


class ResultStore:
    """Append-only JSONL keyed by `id`: re-running a suite skips items that already have a result."""

    def __init__(self, path: Path):
        self.path = path
        self._rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a line cut off by a crash
                    self._rows[str(row["id"])] = row
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def done(self, item_id: str) -> bool:
        return str(item_id) in self._rows

    def add(self, row: dict) -> None:
        self._rows[str(row["id"])] = row
        self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def rows(self) -> list[dict]:
        return list(self._rows.values())

    def close(self) -> None:
        self._fh.close()


async def run_pool(items: list, worker: Callable[[Any], Awaitable[dict]], *, label: str, concurrency: int,
                   deadline: float | None, store: ResultStore, key: Callable[[Any], str]) -> dict:
    """Run `worker` over items not yet in `store` with bounded concurrency until done or `deadline` (monotonic).

    A worker returns a result row (stored with its id) or raises; errors are stored as rows with `error` so the run
    still reports them. No new item starts after the deadline; in-flight items finish."""
    todo = [it for it in items if not store.done(key(it))]
    total, skipped = len(todo), len(items) - len(todo)
    if skipped:
        log(f"{label}: {skipped} already done (resuming), {total} to go")
    if not todo:
        return {"run": 0, "skipped": skipped, "errors": 0, "timed_out": 0}
    queue: asyncio.Queue = asyncio.Queue()
    for it in todo:
        queue.put_nowait(it)
    stats = {"run": 0, "skipped": skipped, "errors": 0, "timed_out": 0}
    t0 = time.monotonic()
    last = [t0]

    def progress(final: bool = False) -> None:
        now = time.monotonic()
        if not final and now - last[0] < 10:
            return
        last[0] = now
        done = stats["run"]
        rate = done / max(1e-6, now - t0)
        eta = (total - done) / rate if rate > 0 else 0
        log(f"{label}: {done}/{total} ({rate:.2f}/s, errors {stats['errors']}"
            + (", done" if final else f", eta {fmt_secs(eta)}") + ")")

    async def loop() -> None:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                return
            try:
                it = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                row = await worker(it)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — record and keep going
                row = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
                stats["errors"] += 1
            row["id"] = key(it)
            store.add(row)
            stats["run"] += 1
            progress()

    await asyncio.gather(*(loop() for _ in range(max(1, concurrency))))
    stats["timed_out"] = queue.qsize()
    progress(final=True)
    if stats["timed_out"]:
        log(f"{label}: time budget reached — {stats['timed_out']} items not run")
    return stats
