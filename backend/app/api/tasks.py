"""Tasks (background agent work) and approvals."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agents.approvals import approval_snapshot, broker
from app.agents.common import task_snapshot
from app.agents.task_manager import task_manager
from app.core.deps import Ctx, get_ctx, require_member
from app.models import Agent, Approval, Task, TaskEvent
from app.services.serialize import task_event_out

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskIn(BaseModel):
    agent_id: str
    title: str = Field(min_length=1, max_length=300)
    instructions: str = ""
    conversation_id: str | None = None
    priority: str = "normal"


class TaskMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class DecisionIn(BaseModel):
    comment: str | None = None
    modified_args: dict | None = None


async def _task(ctx: Ctx, task_id: str) -> Task:
    t = await ctx.db.get(Task, task_id)
    if t is None or t.org_id != ctx.org_id or (not ctx.is_admin and t.created_by != ctx.user_id):
        raise HTTPException(404, "Task not found")
    return t


@router.get("/tasks")
async def list_tasks(status: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
                     q: str | None = None, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(get_ctx)):
    query = select(Task).where(Task.org_id == ctx.org_id)
    if not ctx.is_admin:
        query = query.where(Task.created_by == ctx.user_id)
    if status:
        statuses = [s for s in status.split(",") if s]
        query = query.where(Task.status.in_(statuses))
    if agent_id:
        query = query.where(Task.agent_id == agent_id)
    if conversation_id:
        query = query.where(Task.conversation_id == conversation_id)
    if q:
        query = query.where(Task.title.ilike(f"%{q.strip()}%"))
    total = (await ctx.db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = (await ctx.db.execute(query.order_by(Task.created_at.desc()).offset(max(0, offset))
                                 .limit(max(1, min(limit, 200))))).scalars().all()
    return {"items": [await task_snapshot(ctx.db, t) for t in rows], "total": total}


@router.post("/tasks")
async def create_task(body: TaskIn, ctx: Ctx = Depends(require_member)):
    agent = await ctx.db.get(Agent, body.agent_id)
    if agent is None or agent.org_id != ctx.org_id or agent.is_orchestrator or not agent.enabled:
        raise HTTPException(400, "Choose an enabled specialist agent")
    await ctx.db.commit()
    return await task_manager.create_task(org_id=ctx.org_id, agent_id=agent.id, title=body.title,
                                          instructions=body.instructions or body.title, created_by=ctx.user_id,
                                          conversation_id=body.conversation_id, priority=body.priority)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, ctx: Ctx = Depends(get_ctx)):
    t = await _task(ctx, task_id)
    snap = await task_snapshot(ctx.db, t)
    events = (await ctx.db.execute(select(TaskEvent).where(TaskEvent.task_id == t.id)
                                   .order_by(TaskEvent.created_at).limit(2000))).scalars().all()
    children = (await ctx.db.execute(select(Task).where(Task.parent_id == t.id).order_by(Task.created_at))).scalars().all()
    approvals = (await ctx.db.execute(select(Approval).where(Approval.task_id == t.id)
                                      .order_by(Approval.created_at))).scalars().all()
    snap["events"] = [task_event_out(e) for e in events]
    snap["children"] = [await task_snapshot(ctx.db, c) for c in children]
    snap["approvals"] = [await approval_snapshot(ctx.db, a) for a in approvals]
    return snap


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, after: str | None = None, ctx: Ctx = Depends(get_ctx)):
    t = await _task(ctx, task_id)
    q = select(TaskEvent).where(TaskEvent.task_id == t.id)
    if after:
        ref = await ctx.db.get(TaskEvent, after)
        if ref is not None:
            q = q.where(TaskEvent.created_at > ref.created_at)
    rows = (await ctx.db.execute(q.order_by(TaskEvent.created_at).limit(2000))).scalars().all()
    return {"items": [task_event_out(e) for e in rows], "total": len(rows)}


@router.post("/tasks/{task_id}/message")
async def task_message(task_id: str, body: TaskMessageIn, ctx: Ctx = Depends(require_member)):
    t = await _task(ctx, task_id)
    await ctx.db.commit()
    ev = await task_manager.send_message(t.id, body.content, sender="user", sender_name=ctx.user.name)
    if ev is None:
        raise HTTPException(404, "Task not found")
    return ev


@router.post("/tasks/{task_id}/{action}")
async def task_action(task_id: str, action: str, ctx: Ctx = Depends(require_member)):
    t = await _task(ctx, task_id)
    await ctx.db.commit()
    if action == "cancel":
        res = await task_manager.cancel(t.id, ctx.user_id)
    elif action == "pause":
        res = await task_manager.pause(t.id)
    elif action == "resume":
        res = await task_manager.resume(t.id)
    elif action == "retry":
        res = await task_manager.retry(t.id, ctx.user_id)
    else:
        raise HTTPException(404, "Unknown action")
    if res is None:
        raise HTTPException(404, "Task not found")
    return res


# ------------------------------------------------------------------ approvals
@router.get("/approvals")
async def list_approvals(status: str = "pending", ctx: Ctx = Depends(get_ctx)):
    q = select(Approval).where(Approval.org_id == ctx.org_id)
    if status and status != "all":
        q = q.where(Approval.status == status)
    if not ctx.is_admin:
        q = q.where(Approval.requested_for_user_id == ctx.user_id)
    rows = (await ctx.db.execute(q.order_by(Approval.created_at.desc()).limit(300))).scalars().all()
    return {"items": [await approval_snapshot(ctx.db, a) for a in rows], "total": len(rows)}


async def _decide(approval_id: str, approve: bool, body: DecisionIn, ctx: Ctx) -> dict:
    a = await ctx.db.get(Approval, approval_id)
    if a is None or a.org_id != ctx.org_id:
        raise HTTPException(404, "Approval not found")
    if not ctx.is_admin and a.requested_for_user_id != ctx.user_id:
        raise HTTPException(403, "Only the requester or an admin can decide this approval")
    if ctx.role == "viewer":
        raise HTTPException(403, "Viewers cannot approve actions")
    if a.status != "pending":
        raise HTTPException(409, f"Approval already {a.status}")
    await ctx.db.commit()
    res = await broker.decide(approval_id, approve=approve, user_id=ctx.user_id, comment=body.comment,
                              modified_args=body.modified_args if approve else None)
    if res is None:
        raise HTTPException(409, "Approval is no longer pending")
    return res


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, body: DecisionIn | None = None, ctx: Ctx = Depends(get_ctx)):
    return await _decide(approval_id, True, body or DecisionIn(), ctx)


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, body: DecisionIn | None = None, ctx: Ctx = Depends(get_ctx)):
    return await _decide(approval_id, False, body or DecisionIn(), ctx)
