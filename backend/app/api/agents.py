"""Agents (the swarm)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.agents.common import ACTIVE
from app.core.db import as_utc
from app.core.deps import Ctx, get_ctx, require_admin
from app.models import Agent, AlignmentCheck, Task
from app.services.audit import audit
from app.services.serialize import agent_out

router = APIRouter(prefix="/api/agents", tags=["agents"])

AUTONOMY = ("supervised", "balanced", "autonomous")


class AgentIn(BaseModel):
    name: str | None = None
    role: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    provider_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    tool_groups: list[str] | None = None
    policy_ids: list[str] | None = None
    collection_ids: list[str] | None = None
    autonomy: str | None = None
    max_steps: int | None = None
    color: str | None = None
    icon: str | None = None
    enabled: bool | None = None


async def agent_stats(db, org_id: str, agent_ids: list[str]) -> dict[str, dict]:
    stats = {aid: {"active_tasks": 0, "tasks_total": 0, "tasks_completed": 0, "tasks_failed": 0, "success_rate": 0.0,
                   "avg_duration_s": 0.0, "tokens_total": 0, "alignment_pass_rate": 1.0} for aid in agent_ids}
    if not agent_ids:
        return stats
    rows = (await db.execute(
        select(Task.agent_id, Task.status, func.count(), func.coalesce(func.sum(Task.tokens), 0))
        .where(Task.org_id == org_id, Task.agent_id.in_(agent_ids)).group_by(Task.agent_id, Task.status))).all()
    for aid, status, n, tokens in rows:
        s = stats[aid]
        s["tasks_total"] += n
        s["tokens_total"] += int(tokens or 0)
        if status == "completed":
            s["tasks_completed"] += n
        elif status == "failed":
            s["tasks_failed"] += n
        if status in ACTIVE:
            s["active_tasks"] += n
    finished = (await db.execute(
        select(Task.agent_id, Task.started_at, Task.finished_at).where(
            Task.org_id == org_id, Task.agent_id.in_(agent_ids), Task.status == "completed",
            Task.started_at.is_not(None), Task.finished_at.is_not(None)).order_by(Task.finished_at.desc()).limit(500))).all()
    durs: dict[str, list[float]] = {}
    for aid, st, fin in finished:
        durs.setdefault(aid, []).append((as_utc(fin) - as_utc(st)).total_seconds())
    checks = (await db.execute(
        select(AlignmentCheck.agent_id, AlignmentCheck.verdict, func.count())
        .where(AlignmentCheck.org_id == org_id, AlignmentCheck.agent_id.in_(agent_ids))
        .group_by(AlignmentCheck.agent_id, AlignmentCheck.verdict))).all()
    totals: dict[str, list[int]] = {}
    for aid, verdict, n in checks:
        t = totals.setdefault(aid, [0, 0])
        t[0] += n
        if verdict == "pass":
            t[1] += n
    for aid, s in stats.items():
        done = s["tasks_completed"] + s["tasks_failed"]
        s["success_rate"] = round(s["tasks_completed"] / done, 3) if done else 0.0
        if durs.get(aid):
            s["avg_duration_s"] = round(sum(durs[aid]) / len(durs[aid]), 1)
        if totals.get(aid) and totals[aid][0]:
            s["alignment_pass_rate"] = round(totals[aid][1] / totals[aid][0], 3)
    return stats


async def _unique_slug(ctx: Ctx, name: str, exclude: str | None = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "agent"
    slug, i = base, 1
    while True:
        q = select(Agent.id).where(Agent.org_id == ctx.org_id, Agent.slug == slug)
        if exclude:
            q = q.where(Agent.id != exclude)
        if not (await ctx.db.execute(q)).first():
            return slug
        i += 1
        slug = f"{base}-{i}"


def _validate(body: AgentIn) -> None:
    if body.autonomy is not None and body.autonomy not in AUTONOMY:
        raise HTTPException(400, f"autonomy must be one of {AUTONOMY}")
    if body.temperature is not None and not 0 <= body.temperature <= 2:
        raise HTTPException(400, "temperature must be between 0 and 2")
    if body.max_steps is not None and not 1 <= body.max_steps <= 200:
        raise HTTPException(400, "max_steps must be between 1 and 200")


async def _get(ctx: Ctx, agent_id: str) -> Agent:
    a = await ctx.db.get(Agent, agent_id)
    if a is None or a.org_id != ctx.org_id:
        raise HTTPException(404, "Agent not found")
    return a


@router.get("")
async def list_agents(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Agent).where(Agent.org_id == ctx.org_id)
                                 .order_by(Agent.is_orchestrator.desc(), Agent.name))).scalars().all()
    stats = await agent_stats(ctx.db, ctx.org_id, [a.id for a in rows])
    return {"items": [agent_out(a, stats.get(a.id)) for a in rows], "total": len(rows)}


@router.post("")
async def create_agent(body: AgentIn, ctx: Ctx = Depends(require_admin)):
    if not body.name or not body.role:
        raise HTTPException(400, "name and role are required")
    _validate(body)
    data = body.model_dump(exclude_none=True)
    a = Agent(org_id=ctx.org_id, slug=await _unique_slug(ctx, body.name), **{
        "description": "", "system_prompt": "", "tool_groups": ["search", "rag", "documents", "filesystem", "memory"],
        "autonomy": (ctx.org.settings or {}).get("autonomy_default", "balanced"), **data})
    ctx.db.add(a)
    await ctx.db.flush()
    await audit(ctx.org_id, "agent.created", user_id=ctx.user_id, agent_id=a.id, target_type="agent", target_id=a.id,
                details={"name": a.name}, db=ctx.db)
    return agent_out(a, (await agent_stats(ctx.db, ctx.org_id, [a.id]))[a.id])


@router.get("/{agent_id}")
async def get_agent(agent_id: str, ctx: Ctx = Depends(get_ctx)):
    a = await _get(ctx, agent_id)
    return agent_out(a, (await agent_stats(ctx.db, ctx.org_id, [a.id]))[a.id])


@router.patch("/{agent_id}")
async def patch_agent(agent_id: str, body: AgentIn, ctx: Ctx = Depends(require_admin)):
    a = await _get(ctx, agent_id)
    _validate(body)
    data = body.model_dump(exclude_unset=True)
    if a.is_orchestrator:
        data.pop("tool_groups", None)
        data.pop("enabled", None)
    for k, v in data.items():
        if k in ("provider_id", "model") or v is not None:
            setattr(a, k, v or None if k in ("provider_id", "model") else v)
    if body.name and not a.is_orchestrator:
        a.slug = await _unique_slug(ctx, body.name, exclude=a.id)
    await audit(ctx.org_id, "agent.updated", user_id=ctx.user_id, agent_id=a.id, target_type="agent", target_id=a.id,
                details={"fields": list(data)}, db=ctx.db)
    return agent_out(a, (await agent_stats(ctx.db, ctx.org_id, [a.id]))[a.id])


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, ctx: Ctx = Depends(require_admin)):
    a = await _get(ctx, agent_id)
    if a.is_orchestrator:
        raise HTTPException(400, "The orchestrator cannot be deleted")
    history = (await ctx.db.execute(select(func.count()).select_from(Task).where(Task.agent_id == a.id))).scalar() or 0
    if history:
        raise HTTPException(409, f"This agent has {history} task(s) in its history. Disable it instead so the audit "
                                 "trail is preserved.")
    await ctx.db.delete(a)
    await audit(ctx.org_id, "agent.deleted", user_id=ctx.user_id, target_type="agent", target_id=agent_id,
                details={"name": a.name}, db=ctx.db)
    return {"ok": True}
