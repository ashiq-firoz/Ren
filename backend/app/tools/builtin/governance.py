"""Governance tools: explicit approvals, identity & permissions, memory, skills, scheduling."""
from __future__ import annotations

from sqlalchemy import select

from app.tools.base import RISK_ORDER, Risk, ToolContext, ToolError, ToolGroup, registry

approval_group = ToolGroup(
    "approval", "Approvals",
    "Ask a human to sign off on a plan or decision before proceeding, and check approval status.",
    prefix="approval", icon="shield-check", category="agents",
)
identity_group = ToolGroup(
    "identity", "Identity & permissions",
    "Who am I acting for, which role they have, and whether an action is permitted or needs approval.",
    prefix="identity", icon="id-card", category="agents",
)
memory_group = ToolGroup(
    "memory", "Memory",
    "Long-term memory: store and recall preferences, facts, project context and past outcomes.",
    prefix="memory", icon="brain", category="agents",
)
skills_group = ToolGroup(
    "skills", "Skills",
    "Reusable playbooks (multi-tool workflows) — list, load to follow inline, or execute as a background task.",
    prefix="skill", icon="sparkles", category="agents",
)
schedule_group = ToolGroup(
    "schedule", "Scheduling",
    "Schedule agent work for later or on a recurring cron (e.g. every Monday 9:00), list and cancel schedules.",
    prefix="schedule", icon="clock", category="agents",
)


# ---------------------------------------------------------------- approvals
@approval_group.tool(risk=Risk.LOW, timeout=7 * 24 * 3600)
async def request(ctx: ToolContext, summary: str, details: str = "") -> dict:
    """Ask the user to approve a plan, decision or deliverable before you proceed. Blocks until they decide.

    Args:
        summary: One-line description of what needs sign-off.
        details: Markdown details (the plan, the draft, the numbers) the approver should review.
    """
    from app.agents.approvals import broker

    decision = await broker.request(ctx=ctx, tool="approval_request", tool_display="approval.request",
                                    args={"summary": summary}, risk="medium", reason=summary,
                                    preview=f"**{summary}**\n\n{details}".strip(), call_id=None)
    return {"decision": decision["status"], "comment": decision.get("comment")}


@approval_group.tool(name="status", risk=Risk.LOW)
async def approval_status(ctx: ToolContext, approval_id: str) -> dict:
    """Check the status of an approval request.

    Args:
        approval_id: The approval id.
    """
    from app.models import Approval

    async with ctx.session() as db:
        a = await db.get(Approval, approval_id)
    if a is None or a.org_id != ctx.org_id:
        raise ToolError("Approval not found.")
    return {"status": a.status, "tool": a.tool, "comment": a.comment}


# ---------------------------------------------------------------- identity
@identity_group.tool(risk=Risk.LOW)
async def whoami(ctx: ToolContext) -> dict:
    """Identity context: the user you act for, their role, the workspace, and your agent identity."""
    from app.models import Organization, User

    async with ctx.session() as db:
        user = await db.get(User, ctx.user_id) if ctx.user_id else None
        org = await db.get(Organization, ctx.org_id)
    return {"user": {"name": user.name, "email": user.email} if user else None, "role": ctx.role,
            "workspace": {"name": org.name, "mode": org.mode} if org else None,
            "agent": ctx.agent_name, "task_id": ctx.task_id}


@identity_group.tool(risk=Risk.LOW)
async def roles(ctx: ToolContext) -> dict:
    """Explain workspace roles and what each may do."""
    return {"viewer": "read-only; agents get read-scoped tools only", "member": "chat, run tasks, upload documents",
            "admin": "manage agents, policies, connectors, members, settings", "owner": "everything incl. deleting the workspace",
            "current_role": ctx.role}


@identity_group.tool(risk=Risk.LOW)
async def permissions(ctx: ToolContext) -> dict:
    """Your effective permissions: role, autonomy level and which risk levels require human approval."""
    from app.agents.approvals import APPROVAL_THRESHOLD
    from app.models import Agent

    async with ctx.session() as db:
        agent = await db.get(Agent, ctx.agent_id) if ctx.agent_id else None
    autonomy = agent.autonomy if agent else "balanced"
    threshold = APPROVAL_THRESHOLD.get(autonomy, "high")
    return {"role": ctx.role, "autonomy": autonomy, "approval_required_for_risk_at_or_above": threshold,
            "can_write": ctx.can_write, "is_admin": ctx.is_admin}


@identity_group.tool(risk=Risk.LOW)
async def permission_check(ctx: ToolContext, tool: str) -> dict:
    """Check whether a tool is allowed for you and whether it will require human approval.

    Args:
        tool: Tool name, e.g. "email_send" or "email.send".
    """
    from app.agents.approvals import APPROVAL_THRESHOLD
    from app.models import Agent
    from app.tools.executor import scope_allowed

    spec = registry.get(tool.replace(".", "_", 1))
    if spec is None:
        raise ToolError(f"Unknown tool {tool}")
    async with ctx.session() as db:
        agent = await db.get(Agent, ctx.agent_id) if ctx.agent_id else None
    autonomy = agent.autonomy if agent else "balanced"
    granted = agent is None or spec.group in (agent.tool_groups or [])
    needs = RISK_ORDER[spec.risk.value] >= RISK_ORDER[APPROVAL_THRESHOLD.get(autonomy, "high")]
    return {"tool": spec.display_name, "granted_to_agent": granted, "role_allows": scope_allowed(spec, ctx.role),
            "risk": spec.risk.value, "requires_approval": needs}


# ---------------------------------------------------------------- memory
@memory_group.tool(risk=Risk.LOW, scope="write")
async def store(ctx: ToolContext, content: str, kind: str = "semantic", scope: str = "user",
                importance: float = 0.5) -> dict:
    """Store a durable memory (fact, preference, project context).

    Args:
        content: The fact to remember, written as a standalone statement.
        kind: preference | semantic | project | org | episodic
        scope: user (private to the user) or org (shared with the workspace — admins only).
        importance: 0..1
    """
    from app.services.memory import store_memory

    if scope == "org" and not ctx.is_admin:
        scope = "user"
    return await store_memory(org_id=ctx.org_id, user_id=ctx.user_id, content=content, kind=kind, scope=scope,
                              agent_id=ctx.agent_id, source=f"agent:{ctx.agent_name}", importance=importance)


@memory_group.tool(risk=Risk.LOW)
async def search(ctx: ToolContext, query: str, limit: int = 8) -> list[dict]:
    """Semantic search over memories visible to the current user.

    Args:
        query: What to look for.
        limit: Maximum results.
    """
    from app.services.memory import search_memories

    return [{"id": m["id"], "kind": m["kind"], "scope": m["scope"], "content": m["content"]}
            for m in await search_memories(org_id=ctx.org_id, user_id=ctx.user_id, query=query, limit=limit)]


@memory_group.tool(risk=Risk.MEDIUM, scope="write")
async def update(ctx: ToolContext, memory_id: str, content: str) -> dict:
    """Correct an existing memory.

    Args:
        memory_id: Memory id.
        content: New content.
    """
    from app.core.db import utcnow
    from app.models import Memory
    from app.services.serialize import memory_out

    async with ctx.session() as db:
        m = await db.get(Memory, memory_id)
        if m is None or m.org_id != ctx.org_id or (m.user_id and m.user_id != ctx.user_id):
            raise ToolError("Memory not found.")
        if m.scope == "org" and not ctx.is_admin:
            raise ToolError("Only admins can edit workspace memories.")
        m.content = content[:4000]
        m.updated_at = utcnow()
        m.embedding = None
        return memory_out(m)


@memory_group.tool(risk=Risk.MEDIUM, scope="write")
async def delete(ctx: ToolContext, memory_id: str) -> str:
    """Delete a memory that is wrong or no longer relevant.

    Args:
        memory_id: Memory id.
    """
    from app.models import Memory

    async with ctx.session() as db:
        m = await db.get(Memory, memory_id)
        if m is None or m.org_id != ctx.org_id or (m.user_id and m.user_id != ctx.user_id):
            raise ToolError("Memory not found.")
        if m.scope == "org" and not ctx.is_admin:
            raise ToolError("Only admins can delete workspace memories.")
        await db.delete(m)
    return "Deleted."


# ---------------------------------------------------------------- skills
async def _skill(ctx: ToolContext, name: str):
    from app.models import Skill

    async with ctx.session() as db:
        rows = (await db.execute(select(Skill).where(Skill.org_id == ctx.org_id))).scalars().all()
    n = name.strip().lower()
    s = next((s for s in rows if n in (s.name.lower(), s.slug.lower(), s.id)), None) or next(
        (s for s in rows if n and n in s.name.lower()), None)
    if s is None:
        raise ToolError("Skill not found. Available: " + ", ".join(s.name for s in rows[:40]))
    return s


@skills_group.tool(name="list", risk=Risk.LOW)
async def skill_list(ctx: ToolContext, category: str | None = None) -> list[dict]:
    """List available playbooks (skills).

    Args:
        category: Optional category filter.
    """
    from app.models import Skill

    async with ctx.session() as db:
        q = select(Skill).where(Skill.org_id == ctx.org_id)
        if category:
            q = q.where(Skill.category == category)
        rows = (await db.execute(q.order_by(Skill.name))).scalars().all()
    return [{"name": s.name, "category": s.category, "description": s.description[:200]} for s in rows]


@skills_group.tool(name="get", risk=Risk.LOW)
async def skill_get(ctx: ToolContext, skill: str) -> dict:
    """Get a skill's description and inputs.

    Args:
        skill: Skill name.
    """
    s = await _skill(ctx, skill)
    return {"name": s.name, "description": s.description, "inputs": s.inputs, "tool_groups": s.tool_groups}


@skills_group.tool(risk=Risk.LOW)
async def load(ctx: ToolContext, skill: str) -> str:
    """Load a skill's full playbook so you can follow it step by step in your current task.

    Args:
        skill: Skill name.
    """
    s = await _skill(ctx, skill)
    return f"# Playbook: {s.name}\n\n{s.instructions}"


@skills_group.tool(risk=Risk.LOW, scope="write")
async def execute(ctx: ToolContext, skill: str, inputs: dict | None = None) -> dict:
    """Run a skill as a separate background task with its preferred agent.

    Args:
        skill: Skill name.
        inputs: Input values for the skill.
    """
    from app.agents.orchestrator import start_skill_task
    from app.models import Agent

    s = await _skill(ctx, skill)
    async with ctx.session() as db:
        agent = await db.get(Agent, s.agent_id) if s.agent_id else None
        if agent is None:
            agent = (await db.execute(select(Agent).where(Agent.org_id == ctx.org_id, Agent.is_orchestrator.is_(False),
                                                          Agent.enabled.is_(True)))).scalars().first()
    if agent is None:
        raise ToolError("No agent available to run this skill.")
    snap = await start_skill_task(ctx.org_id, ctx.user_id or "", s, agent, inputs or {}, ctx.conversation_id)
    return {"task_id": snap["id"], "ref": f"t:{snap['id'][:8]}", "agent": agent.name}


# ---------------------------------------------------------------- scheduling
async def _user_timezone(ctx: ToolContext) -> str | None:
    from app.models import User

    if not ctx.user_id:
        return None
    async with ctx.session() as db:
        user = await db.get(User, ctx.user_id)
        return user.timezone if user is not None else None


@schedule_group.tool(name="task", risk=Risk.MEDIUM, scope="write")
async def schedule_task(ctx: ToolContext, agent: str, title: str, instructions: str, run_at: str | None = None,
                        cron: str | None = None, timezone: str | None = None) -> dict:
    """Schedule agent work for later (run_at) or on a recurring cron schedule.

    Args:
        agent: Agent name to run the task.
        title: Task title.
        instructions: Complete instructions for the scheduled run.
        run_at: ISO-8601 date/time for a one-off run, e.g. "2026-10-06T09:00" (local time in `timezone`).
        cron: Cron expression for recurring runs, e.g. "0 9 * * 1" (Mondays 09:00 in `timezone`).
        timezone: IANA time zone for run_at and cron, e.g. "Asia/Kolkata" (default: the user's time zone, else UTC).
    """
    from app.services.scheduler import parse_when, schedule_job, valid_timezone
    from app.tools.builtin.agents import _find_agent

    a = await _find_agent(ctx, agent)
    if timezone and not valid_timezone(timezone):
        raise ToolError(f"Unknown time zone '{timezone}' — use an IANA name like 'Asia/Kolkata'.")
    tz = valid_timezone(timezone) or valid_timezone(await _user_timezone(ctx)) or "UTC"
    when = None
    if run_at:
        try:
            when = parse_when(run_at, tz)
        except ValueError as e:
            raise ToolError(str(e)) from e
    if when is None and not cron:
        raise ToolError("Provide run_at or cron.")
    try:
        return await schedule_job(org_id=ctx.org_id, user_id=ctx.user_id, kind="task", title=title,
                                  payload={"agent_id": a.id, "title": title, "instructions": instructions,
                                           "conversation_id": ctx.conversation_id},
                                  run_at=None if cron else when, cron=cron, timezone=tz)
    except (ValueError, KeyError) as e:
        raise ToolError(f"Invalid schedule: {e}") from e


@schedule_group.tool(name="list", risk=Risk.LOW)
async def schedule_list(ctx: ToolContext) -> list[dict]:
    """List active scheduled jobs for the current user."""
    from app.models import ScheduledJob
    from app.services.scheduler import job_out

    async with ctx.session() as db:
        rows = (await db.execute(select(ScheduledJob).where(ScheduledJob.org_id == ctx.org_id,
                                                            ScheduledJob.user_id == ctx.user_id,
                                                            ScheduledJob.status == "scheduled")
                                 .order_by(ScheduledJob.run_at))).scalars().all()
    return [job_out(j) for j in rows]


@schedule_group.tool(name="cancel", risk=Risk.MEDIUM, scope="write")
async def schedule_cancel(ctx: ToolContext, job_id: str) -> str:
    """Cancel a scheduled job.

    Args:
        job_id: Scheduled job id.
    """
    from app.services.scheduler import cancel_job

    if not await cancel_job(ctx.org_id, job_id):
        raise ToolError("Scheduled job not found.")
    return "Cancelled."
