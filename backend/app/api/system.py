"""Health, system settings, dashboard and the real-time event stream."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from app.agents.common import ACTIVE, task_snapshot
from app.core.config import VERSION, settings
from app.core.db import IS_POSTGRES, SessionLocal, utcnow
from app.core.deps import Ctx, _bearer, get_ctx, resolve_ctx, user_from_token
from app.core.events import bus, visible_to
from app.models import Agent, AlignmentCheck, Approval, AuditLog, Conversation, Document, Task, UsageRecord, User
from app.policy.laya_client import laya
from app.services.serialize import audit_out

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok", "version": VERSION, "db": "postgresql" if IS_POSTGRES else "sqlite"}


@router.get("/settings/system")
async def system_settings(ctx: Ctx = Depends(get_ctx)):
    try:
        from app.rag.embeddings import embedding_info

        emb = embedding_info()
    except Exception:  # noqa: BLE001
        emb = {"provider": settings.embedding_provider, "model": settings.embedding_model, "dim": settings.embedding_dim}
    import importlib.util

    st = await laya.status()
    return {
        "version": VERSION, "db": "postgresql" if IS_POSTGRES else "sqlite",
        "embedding": {"provider": emb.get("provider"), "model": emb.get("model"), "dim": emb.get("dim")},
        "reranker": {"provider": settings.reranker_provider,
                     "model": settings.reranker_model if settings.reranker_provider != "none" else None},
        "sandbox": "remote" if settings.sandbox_url else "local",
        "browser": importlib.util.find_spec("playwright") is not None,
        "laya": {"mode": st["mode"], "url": st["url"], "reachable": st["reachable"]},
        "mcp_endpoint": f"{settings.public_api_url.rstrip('/')}/mcp",
    }


@router.get("/dashboard")
async def dashboard(ctx: Ctx = Depends(get_ctx)):
    db, org_id = ctx.db, ctx.org_id
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = [Task.org_id == org_id] + ([] if ctx.is_admin else [Task.created_by == ctx.user_id])

    async def count(q) -> int:
        return (await db.execute(q)).scalar() or 0

    active = await count(select(func.count()).select_from(Task).where(*scope, Task.status.in_(tuple(ACTIVE))))
    completed_today = await count(select(func.count()).select_from(Task).where(
        *scope, Task.status == "completed", Task.finished_at >= day_start))
    failed_today = await count(select(func.count()).select_from(Task).where(
        *scope, Task.status == "failed", Task.finished_at >= day_start))
    approvals_q = select(func.count()).select_from(Approval).where(Approval.org_id == org_id, Approval.status == "pending")
    if not ctx.is_admin:
        approvals_q = approvals_q.where(Approval.requested_for_user_id == ctx.user_id)
    pending = await count(approvals_q)
    documents = await count(select(func.count()).select_from(Document).where(Document.org_id == org_id))
    agents = await count(select(func.count()).select_from(Agent).where(Agent.org_id == org_id, Agent.enabled.is_(True),
                                                                        Agent.is_orchestrator.is_(False)))
    convs = await count(select(func.count()).select_from(Conversation).where(
        Conversation.org_id == org_id, Conversation.user_id == ctx.user_id))
    tokens_today = await count(select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
                               .where(UsageRecord.org_id == org_id, UsageRecord.created_at >= day_start))
    since = now - timedelta(days=30)
    checks = (await db.execute(select(AlignmentCheck.verdict, func.count()).where(
        AlignmentCheck.org_id == org_id, AlignmentCheck.created_at >= since).group_by(AlignmentCheck.verdict))).all()
    total_checks = sum(n for _, n in checks)
    passed = sum(n for v, n in checks if v == "pass")

    recent = (await db.execute(select(Task).where(*scope, Task.parent_id.is_(None))
                               .order_by(Task.created_at.desc()).limit(8))).scalars().all()
    activity = (await db.execute(select(AuditLog).where(AuditLog.org_id == org_id)
                                 .order_by(AuditLog.created_at.desc()).limit(12))).scalars().all() if ctx.is_admin else []
    uids = {a.user_id for a in activity if a.user_id}
    aids = {a.agent_id for a in activity if a.agent_id}
    unames = {u.id: u.name for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    anames = {a.id: a.name for a in (await db.execute(select(Agent).where(Agent.id.in_(aids)))).scalars().all()} if aids else {}

    start = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    usage_rows = (await db.execute(select(UsageRecord.created_at, UsageRecord.prompt_tokens + UsageRecord.completion_tokens)
                                   .where(UsageRecord.org_id == org_id, UsageRecord.created_at >= start))).all()
    task_rows = (await db.execute(select(Task.created_at).where(*scope, Task.created_at >= start))).all()
    days = [(start + timedelta(days=i)).date().isoformat() for i in range(14)]
    tok = dict.fromkeys(days, 0)
    tsk = dict.fromkeys(days, 0)
    for ts, n in usage_rows:
        d = ts.date().isoformat()
        if d in tok:
            tok[d] += int(n or 0)
    for (ts,) in task_rows:
        d = ts.date().isoformat()
        if d in tsk:
            tsk[d] += 1

    agent_rows = (await db.execute(select(Agent).where(Agent.org_id == org_id, Agent.is_orchestrator.is_(False))
                                   .order_by(Agent.name))).scalars().all()
    util_rows = (await db.execute(select(Task.agent_id, func.count(), func.coalesce(func.sum(Task.tokens), 0))
                                  .where(Task.org_id == org_id, Task.created_at >= since).group_by(Task.agent_id))).all()
    util = {aid: (n, int(t or 0)) for aid, n, t in util_rows}
    act_rows = (await db.execute(select(Task.agent_id, func.count()).where(Task.org_id == org_id,
                                                                           Task.status.in_(tuple(ACTIVE)))
                                 .group_by(Task.agent_id))).all()
    act = dict(act_rows)
    return {
        "stats": {"active_tasks": active, "completed_today": completed_today, "failed_today": failed_today,
                  "pending_approvals": pending, "documents": documents, "agents": agents,
                  "alignment_pass_rate": round(passed / total_checks, 3) if total_checks else None,
                  "tokens_today": tokens_today, "conversations": convs},
        "recent_tasks": [await task_snapshot(db, t) for t in recent],
        "recent_activity": [audit_out(a, unames.get(a.user_id), anames.get(a.agent_id)) for a in activity],
        "usage_series": [{"date": d, "tokens": tok[d], "tasks": tsk[d]} for d in days],
        "agent_utilization": [{"agent_id": a.id, "name": a.name, "color": a.color, "tasks": util.get(a.id, (0, 0))[0],
                               "tokens": util.get(a.id, (0, 0))[1], "active": act.get(a.id, 0)} for a in agent_rows],
    }


@router.get("/stream")
async def stream(request: Request, authorization: str | None = Header(default=None),
                 x_org_id: str | None = Header(default=None)):
    """Server-Sent Events for one workspace, filtered to what this user may see."""
    async with SessionLocal() as db:
        user = await user_from_token(db, _bearer(authorization))
        if user is None:
            raise HTTPException(401, "Not authenticated")
        ctx = await resolve_ctx(db, user, x_org_id)
        user_id, role, org_id = user.id, ctx.role, ctx.org_id

    async def gen():
        async with bus.subscribe(org_id) as q:
            yield {"data": json.dumps({"type": "ready", "data": {"org_id": org_id}})}
            stopper = asyncio.ensure_future(bus.shutdown_event.wait())
            getter: asyncio.Future | None = None
            try:
                while not bus.shutdown_event.is_set():
                    if await request.is_disconnected():
                        break
                    getter = asyncio.ensure_future(q.get())
                    done, _ = await asyncio.wait({getter, stopper}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                    if getter not in done:
                        getter.cancel()
                        if stopper in done:
                            break
                        yield {"data": json.dumps({"type": "ping", "data": {}})}
                        continue
                    ev = getter.result()
                    if not visible_to(ev, user_id, role):
                        continue
                    yield {"data": json.dumps({"type": ev["type"], "data": ev["data"]}, default=str)}
            finally:
                stopper.cancel()
                if getter is not None and not getter.done():
                    getter.cancel()

    return EventSourceResponse(gen(), ping=None, headers={"Cache-Control": "no-cache, no-transform",
                                                          "X-Accel-Buffering": "no"})
