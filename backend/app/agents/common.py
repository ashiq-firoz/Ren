"""Helpers shared by the orchestrator, task manager and approval broker."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.db import session_scope, utcnow
from app.core.events import bus
from app.models import Agent, Artifact, Conversation, Message, Task
from app.services.serialize import message_out, task_out

TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"queued", "running", "waiting_approval", "paused"}


async def task_snapshot(db, task: Task) -> dict:
    agent = await db.get(Agent, task.agent_id)
    children = (await db.execute(select(func.count()).select_from(Task).where(Task.parent_id == task.id))).scalar() or 0
    arts = (await db.execute(select(Artifact).where(Artifact.task_id == task.id).order_by(Artifact.created_at))).scalars().all()
    return task_out(task, agent, children, list(arts))


async def emit_task(task_id: str, type_: str = "task.updated") -> dict | None:
    async with session_scope() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return None
        snap = await task_snapshot(db, task)
        org_id, owner = task.org_id, task.created_by
    await bus.emit(org_id, type_, {"task": snap}, user_id=owner, admin_visible=True)
    return snap


async def conversation_owner(db, conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    conv = await db.get(Conversation, conversation_id)
    return conv.user_id if conv else None


async def post_message(
    *,
    org_id: str,
    conversation_id: str,
    role: str,
    content: str = "",
    meta: dict | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    status: str = "complete",
    user_id: str | None = None,
) -> dict:
    async with session_scope() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return {}
        msg = Message(conversation_id=conversation_id, org_id=org_id, role=role, content=content, meta=meta or {},
                      agent_id=agent_id, task_id=task_id, status=status, user_id=user_id)
        db.add(msg)
        conv.updated_at = utcnow()
        await db.flush()
        out = message_out(msg)
        owner = conv.user_id
    await bus.emit(org_id, "message.created", {"message": out}, user_id=owner, admin_visible=False)
    return out


async def post_event(org_id: str, conversation_id: str | None, event: dict, *, content: str = "",
                     task_id: str | None = None, agent_id: str | None = None) -> dict:
    if not conversation_id:
        return {}
    return await post_message(org_id=org_id, conversation_id=conversation_id, role="event", content=content,
                              meta={"event": event}, task_id=task_id, agent_id=agent_id)


async def update_message_meta(message_id: str, patch: dict[str, Any], *, event_patch: dict | None = None,
                              content: str | None = None) -> dict | None:
    async with session_scope() as db:
        msg = await db.get(Message, message_id)
        if msg is None:
            return None
        meta = dict(msg.meta or {})
        meta.update(patch)
        if event_patch:
            ev = dict(meta.get("event") or {})
            ev.update(event_patch)
            meta["event"] = ev
        msg.meta = meta
        if content is not None:
            msg.content = content
        await db.flush()
        out = message_out(msg)
        conv = await db.get(Conversation, msg.conversation_id)
        owner = conv.user_id if conv else None
        org_id = msg.org_id
    await bus.emit(org_id, "message.updated", {"message": out}, user_id=owner, admin_visible=False)
    return out


async def update_event_messages_for(*, task_id: str | None = None, approval_id: str | None = None,
                                    kinds: tuple[str, ...] = (), event_patch: dict) -> None:
    """Patch the event cards in chat that refer to a task/approval (e.g. live status on a task_created card)."""
    async with session_scope() as db:
        q = select(Message).where(Message.role == "event")
        if task_id:
            q = q.where(Message.task_id == task_id)
        rows = (await db.execute(q.order_by(Message.created_at.desc()).limit(20))).scalars().all()
        ids = []
        for m in rows:
            ev = (m.meta or {}).get("event") or {}
            if kinds and ev.get("kind") not in kinds:
                continue
            if approval_id and ev.get("approval_id") != approval_id:
                continue
            ids.append(m.id)
    for mid in ids:
        await update_message_meta(mid, {}, event_patch=event_patch)


def short(text: str | None, n: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"
