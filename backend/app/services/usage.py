"""Token usage accounting."""
from __future__ import annotations

import logging

from app.core.db import session_scope
from app.models import UsageRecord

log = logging.getLogger("ren.usage")


async def record_usage(
    org_id: str,
    model: str,
    usage: dict | None,
    *,
    purpose: str = "agent",
    agent_id: str | None = None,
    task_id: str | None = None,
) -> int:
    usage = usage or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if pt == 0 and ct == 0:
        return 0
    try:
        async with session_scope() as db:
            db.add(UsageRecord(org_id=org_id, model=model or "unknown", purpose=purpose, agent_id=agent_id,
                               task_id=task_id, prompt_tokens=pt, completion_tokens=ct))
    except Exception:  # noqa: BLE001
        log.exception("usage record failed")
    return pt + ct
