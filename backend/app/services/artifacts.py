"""Artifacts: files produced by agents, surfaced in chat/task views."""
from __future__ import annotations

from typing import Any

from app.core.db import session_scope
from app.core.events import bus
from app.models import Artifact, Task, TaskEvent
from app.services.serialize import artifact_out


async def register_artifact(ctx: Any, info: dict) -> dict:
    async with session_scope() as db:
        art = Artifact(org_id=ctx.org_id, user_id=ctx.user_id, task_id=ctx.task_id,
                       conversation_id=ctx.conversation_id, name=info["name"], path=info["path"],
                       mime=info["mime"], size=info["size"])
        db.add(art)
        await db.flush()
        out = artifact_out(art)
        owner = ctx.user_id
        if ctx.task_id:
            task = await db.get(Task, ctx.task_id)
            ev = TaskEvent(task_id=ctx.task_id, kind="artifact", content={"artifact": out})
            db.add(ev)
            await db.flush()
            owner = task.created_by if task else owner
            from app.services.serialize import task_event_out

            await bus.emit(ctx.org_id, "task.event", {"task_id": ctx.task_id, "event": task_event_out(ev)},
                           user_id=owner, admin_visible=True)
    return out
