"""Schedules: recurring and one-off agent work and reminders — list, create, edit, pause/resume, run now, cancel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.db import as_utc, utcnow
from app.core.deps import Ctx, get_ctx, require_member
from app.models import Agent, Artifact, ScheduledJob, Task
from app.services.audit import audit
from app.services.scheduler import (
    ACTIVE_STATUSES,
    HISTORY_STATUSES,
    apply_changes,
    describe_schedule,
    job_out,
    parse_when,
    resume_run_at,
    run_job_now,
    schedule_job,
    upcoming_runs,
    valid_timezone,
    validate_cron,
)
from app.services.serialize import artifact_out, task_out

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

STATUS_FILTERS: dict[str, tuple[str, ...] | None] = {
    "active": ACTIVE_STATUSES, "scheduled": ("scheduled",), "paused": ("paused",), "history": HISTORY_STATUSES,
    "all": None,
}


def _tz(ctx: Ctx, tz: str | None) -> str:
    if tz and not valid_timezone(tz):
        raise HTTPException(400, f"Unknown time zone '{tz}'. Use an IANA name such as 'Asia/Kolkata' or 'UTC'.")
    return valid_timezone(tz) or valid_timezone(ctx.user.timezone) or "UTC"


def _future(when, what: str = "run_at"):
    if as_utc(when) < utcnow():
        raise HTTPException(400, f"{what} is in the past.")
    return when


async def _job(ctx: Ctx, job_id: str) -> ScheduledJob:
    j = await ctx.db.get(ScheduledJob, job_id)
    if j is None or j.org_id != ctx.org_id or (not ctx.is_admin and j.user_id != ctx.user_id):
        raise HTTPException(404, "Scheduled job not found")
    return j


async def _agent(ctx: Ctx, agent_id: str) -> Agent:
    a = await ctx.db.get(Agent, agent_id)
    if a is None or a.org_id != ctx.org_id:
        raise HTTPException(400, "Unknown agent")
    if a.is_orchestrator:
        raise HTTPException(400, "Pick a specialist agent — Ren delegates, it doesn't run scheduled tasks itself.")
    return a


async def _enrich(ctx: Ctx, jobs: list[ScheduledJob]) -> list[dict]:
    agent_ids = {(j.payload or {}).get("agent_id") for j in jobs if j.kind == "task"} - {None}
    agents = {a.id: a.name for a in (await ctx.db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars()} \
        if agent_ids else {}
    task_ids = {j.last_task_id for j in jobs if j.last_task_id}
    tasks = {t.id: t for t in (await ctx.db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()} \
        if task_ids else {}
    out = []
    for j in jobs:
        t = tasks.get(j.last_task_id or "")
        last = {"id": t.id, "title": t.title, "status": t.status, "created_at": t.created_at.isoformat().replace("+00:00", "Z"),
                "finished_at": t.finished_at.isoformat().replace("+00:00", "Z") if t.finished_at else None} if t else None
        out.append(job_out(j, agent_name=agents.get((j.payload or {}).get("agent_id")), last_task=last))
    return out


async def _one(ctx: Ctx, j: ScheduledJob) -> dict:
    await ctx.db.flush()
    return (await _enrich(ctx, [j]))[0]


@router.get("")
async def list_schedules(status: str = "active", ctx: Ctx = Depends(get_ctx)):
    if status not in STATUS_FILTERS:
        raise HTTPException(400, f"status must be one of {', '.join(STATUS_FILTERS)}")
    q = select(ScheduledJob).where(ScheduledJob.org_id == ctx.org_id)
    if STATUS_FILTERS[status]:
        q = q.where(ScheduledJob.status.in_(STATUS_FILTERS[status]))
    if not ctx.is_admin:
        q = q.where(ScheduledJob.user_id == ctx.user_id)
    order = (ScheduledJob.run_at,) if status in ("active", "scheduled", "paused") else \
        (ScheduledJob.last_run_at.desc(), ScheduledJob.created_at.desc())
    rows = (await ctx.db.execute(q.order_by(*order).limit(500))).scalars().all()
    return {"items": await _enrich(ctx, list(rows)), "total": len(rows)}


@router.get("/preview")
async def preview_schedule(cron: str | None = None, run_at: str | None = None, timezone: str | None = None,
                           count: int = 5, ctx: Ctx = Depends(get_ctx)):
    try:
        tz = _tz(ctx, timezone)
        if cron:
            expr = validate_cron(cron)
            runs = upcoming_runs(expr, None, tz, count)
            return {"valid": True, "error": None, "description": describe_schedule(expr, tz, None),
                    "next": [r.isoformat().replace("+00:00", "Z") for r in runs]}
        if run_at:
            when = parse_when(run_at, tz)
            if when < utcnow():
                return {"valid": False, "error": "That time is in the past.", "description": describe_schedule(None, tz, when),
                        "next": []}
            return {"valid": True, "error": None, "description": describe_schedule(None, tz, when),
                    "next": [when.isoformat().replace("+00:00", "Z")]}
        return {"valid": False, "error": "Choose when it should run.", "description": "", "next": []}
    except HTTPException as e:
        return {"valid": False, "error": e.detail, "description": "", "next": []}
    except ValueError as e:
        return {"valid": False, "error": str(e), "description": "", "next": []}


class ScheduleIn(BaseModel):
    title: str
    agent_id: str
    instructions: str
    cron: str | None = None
    run_at: str | None = None
    timezone: str | None = None


@router.post("")
async def create_schedule(body: ScheduleIn, ctx: Ctx = Depends(require_member)):
    title, instructions = body.title.strip(), body.instructions.strip()
    if not title or not instructions:
        raise HTTPException(400, "Title and instructions are required.")
    agent = await _agent(ctx, body.agent_id)
    tz = _tz(ctx, body.timezone)
    try:
        cron = validate_cron(body.cron) if body.cron else None
        when = _future(parse_when(body.run_at, tz)) if body.run_at and not cron else None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if cron is None and when is None:
        raise HTTPException(400, "Give a cron expression for recurring runs or run_at for a one-off run.")
    await ctx.db.commit()  # release the request transaction before the job is written (SQLite has one writer)
    job = await schedule_job(org_id=ctx.org_id, user_id=ctx.user_id, kind="task", title=title,
                             payload={"agent_id": agent.id, "title": title, "instructions": instructions},
                             run_at=when, cron=cron, timezone=tz)
    await audit(ctx.org_id, "schedule.created", user_id=ctx.user_id, agent_id=agent.id, target_type="schedule",
                target_id=job["id"], details={"title": title, "cron": cron, "timezone": tz}, db=ctx.db)
    return await _one(ctx, await _job(ctx, job["id"]))


@router.get("/{job_id}")
async def get_schedule(job_id: str, ctx: Ctx = Depends(get_ctx)):
    j = await _job(ctx, job_id)
    tasks = (await ctx.db.execute(select(Task).where(Task.org_id == ctx.org_id, Task.schedule_id == j.id)
                                  .order_by(Task.created_at.desc()).limit(25))).scalars().all()
    arts: dict[str, list] = {}
    if tasks:
        for a in (await ctx.db.execute(select(Artifact).where(Artifact.task_id.in_([t.id for t in tasks]))
                                       .order_by(Artifact.created_at))).scalars():
            arts.setdefault(a.task_id, []).append(a)
    agents = {a.id: a for a in (await ctx.db.execute(select(Agent).where(
        Agent.id.in_({t.agent_id for t in tasks})))).scalars()} if tasks else {}
    runs = []
    for t in tasks:
        snap = task_out(t, agents.get(t.agent_id))
        runs.append({k: snap[k] for k in ("id", "title", "status", "created_at", "started_at", "finished_at", "error")}
                    | {"artifacts": [artifact_out(a, t, agents.get(t.agent_id)) for a in arts.get(t.id, [])]})
    return {"job": await _one(ctx, j), "runs": runs}


class SchedulePatch(BaseModel):
    title: str | None = None
    instructions: str | None = None
    agent_id: str | None = None
    cron: str | None = None
    run_at: str | None = None
    timezone: str | None = None


@router.patch("/{job_id}")
async def update_schedule(job_id: str, body: SchedulePatch, ctx: Ctx = Depends(require_member)):
    j = await _job(ctx, job_id)
    if j.status not in ACTIVE_STATUSES:
        raise HTTPException(400, "Only active or paused schedules can be edited.")
    changes = body.model_dump(exclude_unset=True)
    if changes.pop("agent_id", None):
        if j.kind != "task":
            raise HTTPException(400, "Reminders don't run an agent.")
        j.payload = {**(j.payload or {}), "agent_id": (await _agent(ctx, body.agent_id or "")).id}
    if not changes.get("timezone"):
        changes.pop("timezone", None)
    try:
        apply_changes(j, changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await audit(ctx.org_id, "schedule.updated", user_id=ctx.user_id, target_type="schedule", target_id=j.id,
                details={"fields": sorted(body.model_fields_set)}, db=ctx.db)
    return await _one(ctx, j)


@router.post("/{job_id}/pause")
async def pause_schedule(job_id: str, ctx: Ctx = Depends(require_member)):
    j = await _job(ctx, job_id)
    if j.status not in ACTIVE_STATUSES:
        raise HTTPException(400, "Only active schedules can be paused.")
    if j.status != "paused":
        j.status = "paused"
        await audit(ctx.org_id, "schedule.paused", user_id=ctx.user_id, target_type="schedule", target_id=j.id, db=ctx.db)
    return await _one(ctx, j)


@router.post("/{job_id}/resume")
async def resume_schedule(job_id: str, ctx: Ctx = Depends(require_member)):
    j = await _job(ctx, job_id)
    if j.status not in ACTIVE_STATUSES:
        raise HTTPException(400, "Only paused schedules can be resumed.")
    if j.status == "paused":
        j.status = "scheduled"
        j.run_at = resume_run_at(j)
        await audit(ctx.org_id, "schedule.resumed", user_id=ctx.user_id, target_type="schedule", target_id=j.id, db=ctx.db)
    return await _one(ctx, j)


@router.post("/{job_id}/run")
async def run_schedule_now(job_id: str, ctx: Ctx = Depends(require_member)):
    j = await _job(ctx, job_id)
    if j.kind == "task":
        agent_id = (j.payload or {}).get("agent_id")
        if not agent_id or await ctx.db.get(Agent, agent_id) is None:
            raise HTTPException(400, "The agent for this schedule no longer exists — edit it to pick another.")
    await ctx.db.commit()  # release the request transaction before the job writes (SQLite has one writer)
    try:
        snap = await run_job_now(j.id)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    await audit(ctx.org_id, "schedule.run_now", user_id=ctx.user_id, target_type="schedule", target_id=j.id,
                details={"task_id": snap["id"] if snap else None})
    return {"task": snap}


@router.delete("/{job_id}")
async def cancel_schedule(job_id: str, ctx: Ctx = Depends(require_member)):
    j = await _job(ctx, job_id)
    j.status = "cancelled"
    await audit(ctx.org_id, "schedule.cancelled", user_id=ctx.user_id, target_type="schedule", target_id=job_id, db=ctx.db)
    return {"ok": True}
