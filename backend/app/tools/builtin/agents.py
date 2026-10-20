"""Agent-to-agent collaboration (the swarm) and long-running task control."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.core.config import settings
from app.core.db import iso
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup

agents_group = ToolGroup(
    "agents", "Agent collaboration",
    "Delegate sub-tasks to other specialist agents, message them, wait for their results (agent-to-agent).",
    prefix="agent", icon="users", category="agents",
)
tasks_group = ToolGroup(
    "tasks", "Task control",
    "Inspect and control long-running background tasks: status, logs, output, pause, resume, cancel, retry.",
    prefix="task", icon="list-checks", category="agents",
)


async def _agents(ctx: ToolContext):
    from app.models import Agent

    async with ctx.session() as db:
        return (await db.execute(select(Agent).where(Agent.org_id == ctx.org_id, Agent.enabled.is_(True))
                                 .order_by(Agent.name))).scalars().all()


async def _find_agent(ctx: ToolContext, name: str):
    n = (name or "").strip().lower().lstrip("@")
    agents = [a for a in await _agents(ctx) if not a.is_orchestrator]
    for a in agents:
        if n in (a.name.lower(), a.slug.lower(), a.id):
            return a
    for a in agents:
        if n and n in a.name.lower():
            return a
    raise ToolError(f"No agent named '{name}'. Available: {', '.join(a.name for a in agents)}")


async def _own_task(ctx: ToolContext, task_id: str):
    from app.models import Task

    key = "".join(ch for ch in (task_id or "").removeprefix("t:") if ch in "0123456789abcdef-")
    if not key:
        raise ToolError("task_id is required")
    async with ctx.session() as db:
        q = select(Task).where(Task.org_id == ctx.org_id, Task.id.like(f"{key}%"))
        if not ctx.is_admin and ctx.user_id:
            q = q.where(Task.created_by == ctx.user_id)
        rows = (await db.execute(q.limit(2))).scalars().all()
    if len(rows) != 1:
        raise ToolError(f"Task '{task_id}' not found (or ambiguous).")
    return rows[0]


def _brief(t, agent_name: str | None = None) -> dict:
    return {"task_id": t.id, "ref": f"t:{t.id[:8]}", "title": t.title, "agent": agent_name, "status": t.status,
            "progress": t.progress, "current_step": t.current_step, "started_at": iso(t.started_at),
            "finished_at": iso(t.finished_at)}


@agents_group.tool(name="list", risk=Risk.LOW)
async def list_agents(ctx: ToolContext) -> list[dict]:
    """List the specialist agents in this workspace with their roles and tool groups."""
    return [{"name": a.name, "role": a.role, "description": a.description[:200], "tools": a.tool_groups}
            for a in await _agents(ctx) if not a.is_orchestrator]


@agents_group.tool(risk=Risk.LOW)
async def get(ctx: ToolContext, agent: str) -> dict:
    """Describe one agent (role, description, tools, autonomy).

    Args:
        agent: Agent name.
    """
    a = await _find_agent(ctx, agent)
    return {"name": a.name, "role": a.role, "description": a.description, "tools": a.tool_groups,
            "autonomy": a.autonomy}


@agents_group.tool(risk=Risk.LOW, scope="write")
async def delegate(ctx: ToolContext, agent: str, title: str, instructions: str, wait: bool = False,
                   wait_minutes: int = 20) -> dict:
    """Delegate a sub-task to another specialist agent. It runs in parallel; set wait=true to block until it
    finishes and get its result (useful when you need the output to continue).

    Args:
        agent: Name of the agent to delegate to.
        title: Short title for the sub-task.
        instructions: Complete, self-contained instructions (goal, context, inputs, expected output).
        wait: Wait for the sub-task to finish and return its result.
        wait_minutes: Maximum minutes to wait when wait=true.
    """
    from app.agents.task_manager import task_manager

    if ctx.depth + 1 > settings.max_delegation_depth:
        raise ToolError(f"Delegation depth limit ({settings.max_delegation_depth}) reached — do this work yourself.")
    target = await _find_agent(ctx, agent)
    if target.id == ctx.agent_id:
        raise ToolError("You cannot delegate to yourself.")
    snap = await task_manager.create_task(
        org_id=ctx.org_id, agent_id=target.id, title=title, instructions=instructions, created_by=ctx.user_id,
        conversation_id=ctx.conversation_id, parent_id=ctx.task_id, depth=ctx.depth + 1)
    if ctx.task_id:
        from app.core.db import session_scope
        from app.core.events import bus
        from app.models import Task, TaskEvent
        from app.services.serialize import task_event_out

        async with session_scope() as db:
            ev = TaskEvent(task_id=ctx.task_id, kind="delegation",
                           content={"child_task_id": snap["id"], "agent_name": target.name, "title": snap["title"]})
            db.add(ev)
            await db.flush()
            out = task_event_out(ev)
            parent = await db.get(Task, ctx.task_id)
            owner = parent.created_by if parent else ctx.user_id
        await bus.emit(ctx.org_id, "task.event", {"task_id": ctx.task_id, "event": out}, user_id=owner,
                       admin_visible=True)
    if not wait:
        return {"delegated": True, "task_id": snap["id"], "ref": f"t:{snap['id'][:8]}", "agent": target.name,
                "note": "Running in parallel. Use agent_wait or agent_status to collect the result."}
    await ctx.progress(f"Waiting for {target.name}: {snap['title']}")
    done = await task_manager.wait(snap["id"], timeout=max(1, wait_minutes) * 60)
    return {"task_id": snap["id"], "agent": target.name, "status": done["status"] if done else "unknown",
            "result": (done or {}).get("result"), "error": (done or {}).get("error"),
            "artifacts": [a["path"] for a in (done or {}).get("artifacts", [])]}


@agents_group.tool(risk=Risk.LOW, scope="write")
async def message(ctx: ToolContext, task_id: str, message: str) -> str:
    """Send information or new instructions to another agent's running task.

    Args:
        task_id: The task id or ref (t:xxxxxxxx).
        message: The message to deliver.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.send_message(t.id, message, sender="agent", sender_name=ctx.agent_name or "another agent")
    return f"Delivered to t:{t.id[:8]}."


@agents_group.tool(risk=Risk.LOW)
async def status(ctx: ToolContext, task_id: str) -> dict:
    """Status of a delegated task.

    Args:
        task_id: The task id or ref.
    """
    t = await _own_task(ctx, task_id)
    return _brief(t)


@agents_group.tool(risk=Risk.LOW)
async def wait(ctx: ToolContext, task_id: str, minutes: int = 20) -> dict:
    """Wait for a delegated task to finish and return its result.

    Args:
        task_id: The task id or ref.
        minutes: Maximum minutes to wait.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    done = await task_manager.wait(t.id, timeout=max(1, minutes) * 60)
    return {"task_id": t.id, "status": done["status"] if done else "unknown", "result": (done or {}).get("result"),
            "error": (done or {}).get("error"), "artifacts": [a["path"] for a in (done or {}).get("artifacts", [])]}


@agents_group.tool(risk=Risk.LOW, scope="write")
async def cancel(ctx: ToolContext, task_id: str) -> str:
    """Cancel a delegated task you no longer need.

    Args:
        task_id: The task id or ref.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.cancel(t.id, ctx.user_id)
    return f"Cancelled t:{t.id[:8]}."


# ---------------------------------------------------------------- task control
@tasks_group.tool(name="list", risk=Risk.LOW)
async def task_list(ctx: ToolContext, status: str | None = None, limit: int = 20) -> list[dict]:
    """List recent tasks of the current user (optionally filtered by status).

    Args:
        status: queued | running | waiting_approval | paused | completed | failed | cancelled
        limit: Maximum number of tasks.
    """
    from app.models import Task

    async with ctx.session() as db:
        q = select(Task).where(Task.org_id == ctx.org_id)
        if ctx.user_id:
            q = q.where(Task.created_by == ctx.user_id)
        if status:
            q = q.where(Task.status == status)
        rows = (await db.execute(q.order_by(Task.created_at.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return [_brief(t) for t in rows]


@tasks_group.tool(name="status", risk=Risk.LOW)
async def task_status(ctx: ToolContext, task_id: str) -> dict:
    """Status and progress of a task.

    Args:
        task_id: Task id or ref.
    """
    return _brief(await _own_task(ctx, task_id))


@tasks_group.tool(risk=Risk.LOW)
async def logs(ctx: ToolContext, task_id: str, limit: int = 30) -> list[dict]:
    """Recent timeline events of a task (tool calls, results, progress).

    Args:
        task_id: Task id or ref.
        limit: Number of most recent events.
    """
    from app.models import TaskEvent

    t = await _own_task(ctx, task_id)
    async with ctx.session() as db:
        rows = (await db.execute(select(TaskEvent).where(TaskEvent.task_id == t.id)
                                 .order_by(TaskEvent.created_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    return [{"kind": e.kind, "at": iso(e.created_at), "content": json.dumps(e.content)[:400]} for e in reversed(rows)]


@tasks_group.tool(risk=Risk.LOW)
async def output(ctx: ToolContext, task_id: str) -> dict:
    """Final result and artifacts of a finished task.

    Args:
        task_id: Task id or ref.
    """
    from app.models import Artifact

    t = await _own_task(ctx, task_id)
    async with ctx.session() as db:
        arts = (await db.execute(select(Artifact.path).where(Artifact.task_id == t.id))).scalars().all()
    return {"status": t.status, "result": t.result, "error": t.error, "artifacts": list(arts)}


@tasks_group.tool(risk=Risk.LOW, scope="write")
async def pause(ctx: ToolContext, task_id: str) -> str:
    """Pause a running task after its current step.

    Args:
        task_id: Task id or ref.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.pause(t.id)
    return f"Pausing t:{t.id[:8]}."


@tasks_group.tool(risk=Risk.LOW, scope="write")
async def resume(ctx: ToolContext, task_id: str) -> str:
    """Resume a paused task.

    Args:
        task_id: Task id or ref.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.resume(t.id)
    return f"Resumed t:{t.id[:8]}."


@tasks_group.tool(name="cancel", risk=Risk.LOW, scope="write")
async def task_cancel(ctx: ToolContext, task_id: str) -> str:
    """Cancel a task.

    Args:
        task_id: Task id or ref.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.cancel(t.id, ctx.user_id)
    return f"Cancelled t:{t.id[:8]}."


@tasks_group.tool(risk=Risk.LOW, scope="write")
async def retry(ctx: ToolContext, task_id: str) -> str:
    """Retry a failed or cancelled task from scratch.

    Args:
        task_id: Task id or ref.
    """
    from app.agents.task_manager import task_manager

    t = await _own_task(ctx, task_id)
    await task_manager.retry(t.id, ctx.user_id)
    return f"Retrying t:{t.id[:8]}."
