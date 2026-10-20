"""Audit trail."""
from __future__ import annotations

import logging
from typing import Any

from app.core.db import session_scope
from app.models import AuditLog

log = logging.getLogger("ren.audit")

_SECRET_HINTS = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential")


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            k: ("••••" if any(h in str(k).lower() for h in _SECRET_HINTS) else redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


async def audit(
    org_id: str,
    action: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    risk: str | None = None,
    details: dict | None = None,
    db=None,
) -> None:
    entry = AuditLog(org_id=org_id, action=action, user_id=user_id, agent_id=agent_id, task_id=task_id,
                     target_type=target_type, target_id=target_id, risk=risk, details=redact(details or {}))
    try:
        if db is not None:
            db.add(entry)
            return
        async with session_scope() as s:
            s.add(entry)
    except Exception:  # noqa: BLE001
        log.exception("audit write failed")
