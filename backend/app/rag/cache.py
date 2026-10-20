"""Process-local caches for the in-process retrieval indexes (SQLite deployments) and ToCs.

A cache entry is keyed by a *corpus fingerprint* per org: a local version counter (bumped by `invalidate()` after
ingestion / deletion in this process) + a cheap aggregate over the org's ready documents (count, total chunks,
latest update) so changes made by other processes are picked up too.
"""
from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa

_versions: dict[str, int] = defaultdict(int)
_hooks: list[Callable[[str | None], None]] = []
_lock = threading.Lock()


def invalidate(org_id: str | None = None) -> None:
    """Invalidate the lexical/vector/ToC caches of an org (all orgs when None)."""
    with _lock:
        if org_id is None:
            for k in list(_versions):
                _versions[k] += 1
        else:
            _versions[org_id] += 1
    for hook in list(_hooks):
        try:
            hook(org_id)
        except Exception:  # noqa: BLE001
            pass


def on_invalidate(fn: Callable[[str | None], None]) -> None:
    _hooks.append(fn)


def local_version(org_id: str) -> int:
    return _versions[org_id]


async def corpus_fingerprint(db, org_id: str) -> tuple:
    from app.models import Document

    row = (await db.execute(
        sa.select(sa.func.count(Document.id), sa.func.coalesce(sa.func.sum(Document.chunk_count), 0),
                  sa.func.coalesce(sa.func.sum(Document.question_count), 0), sa.func.max(Document.updated_at))
        .where(Document.org_id == org_id, Document.status == "ready")
    )).one()
    return (local_version(org_id), int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), str(row[3]))


class LRU:
    """Tiny thread-safe LRU mapping."""

    def __init__(self, maxsize: int = 64) -> None:
        self.maxsize = maxsize
        self._d: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            if key not in self._d:
                return None
            self._d.move_to_end(key)
            return self._d[key]

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def pop(self, key: Any) -> None:
        with self._lock:
            self._d.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def keys(self) -> list:
        with self._lock:
            return list(self._d)


class LoopLocks:
    """asyncio.Lock per (event loop, key) — safe when tests or workers run several loops."""

    def __init__(self) -> None:
        self._locks: dict[Any, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    def get(self, key: Any) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        entry = self._locks.get(key)
        if entry is None or entry[0] is not loop:
            if len(self._locks) > 512:
                self._locks.clear()
            entry = self._locks[key] = (loop, asyncio.Lock())
        return entry[1]
