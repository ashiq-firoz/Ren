"""Real-time event bus.

One channel per org. Every event carries a visibility envelope:
  user_id        – owner of the underlying object (conversation owner, task creator, approval target)
  admin_visible  – whether admins/owners of the org may also see it (tasks/approvals yes, private chat no)
The SSE endpoint filters events per connection using these fields.

Backends: in-process fan-out (single instance) or Redis pub/sub (multi-instance API replicas).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import settings

log = logging.getLogger("ren.events")


class _MemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, channel: str, payload: dict) -> None:
        for q in list(self._subs.get(channel, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs[channel].add(q)
        try:
            yield q
        finally:
            self._subs[channel].discard(q)
            if not self._subs[channel]:
                self._subs.pop(channel, None)

    async def close(self) -> None:
        self._subs.clear()


class _RedisBus:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)
        self._local = _MemoryBus()
        self._listeners: dict[str, asyncio.Task] = {}

    async def publish(self, channel: str, payload: dict) -> None:
        await self._redis.publish(f"ren:{channel}", json.dumps(payload, default=str))

    async def _listen(self, channel: str) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"ren:{channel}")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    await self._local.publish(channel, json.loads(msg["data"]))
                except Exception:  # noqa: BLE001
                    log.exception("bad redis event")
        finally:
            await pubsub.unsubscribe(f"ren:{channel}")
            await pubsub.aclose()

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue]:
        if channel not in self._listeners or self._listeners[channel].done():
            self._listeners[channel] = asyncio.create_task(self._listen(channel))
        async with self._local.subscribe(channel) as q:
            yield q

    async def close(self) -> None:
        for t in self._listeners.values():
            t.cancel()
        await self._redis.aclose()


class EventBus:
    def __init__(self) -> None:
        self._impl: _MemoryBus | _RedisBus = _MemoryBus()
        self.shutdown_event = asyncio.Event()

    def configure(self) -> None:
        self.shutdown_event = asyncio.Event()  # fresh per app start (events bind to their loop)
        self._impl = _MemoryBus()
        if settings.redis_url:
            try:
                self._impl = _RedisBus(settings.redis_url)
                log.info("event bus: redis")
            except Exception:  # noqa: BLE001
                log.exception("redis unavailable; falling back to in-memory bus")
                self._impl = _MemoryBus()

    async def emit(
        self,
        org_id: str,
        type_: str,
        data: dict[str, Any],
        *,
        user_id: str | None = None,
        admin_visible: bool = True,
    ) -> None:
        payload = {"type": type_, "data": data, "user_id": user_id, "admin_visible": admin_visible}
        try:
            await self._impl.publish(f"org:{org_id}", payload)
        except Exception:  # noqa: BLE001
            log.exception("event publish failed")

    @asynccontextmanager
    async def subscribe(self, org_id: str) -> AsyncIterator[asyncio.Queue]:
        async with self._impl.subscribe(f"org:{org_id}") as q:
            yield q

    async def close(self) -> None:
        self.shutdown_event.set()
        await self._impl.close()


bus = EventBus()


def visible_to(event: dict, user_id: str, role: str) -> bool:
    owner = event.get("user_id")
    if owner is None or owner == user_id:
        return True
    return role in ("admin", "owner") and bool(event.get("admin_visible"))
