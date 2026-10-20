"""Human-in-the-loop approvals for risky tool calls.

The executor calls `broker.request(...)`, which persists an Approval, surfaces it (chat card, approvals queue,
notification), marks the task `waiting_approval` and suspends that tool call until a person decides. Decisions
come from the API (`broker.decide`). Durable: after a restart the task resumes from its checkpoint and re-attaches
to the same approval (matched by task + tool-call id).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents.common import emit_task, post_event, update_event_messages_for
from app.core.db import as_utc, session_scope, utcnow
from app.core.events import bus
from app.models import Agent, Approval, Task, User
from app.services.audit import audit
from app.services.notifications import notify_user
from app.services.serialize import approval_out

log = logging.getLogger("ren.approvals")

APPROVAL_TTL_HOURS = 72
APPROVAL_THRESHOLD = {"supervised": "medium", "balanced": "high", "autonomous": "critical"}


async def approval_snapshot(db, a: Approval) -> dict:
    task = await db.get(Task, a.task_id) if a.task_id else None
    agent = await db.get(Agent, a.agent_id) if a.agent_id else None
    decider = await db.get(User, a.decided_by) if a.decided_by else None
    from app.tools.base import registry

    spec = registry.get(a.tool)
    return approval_out(a, task.title if task else None, agent.name if agent else None,
                        decider.name if decider else None, spec.display_name if spec else None)


class ApprovalBroker:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def reset(self) -> None:
        self._futures = {}

    async def request(self, *, ctx: Any, tool: str, tool_display: str, args: dict, risk: str, reason: str,
                      preview: str, call_id: str | None) -> dict:
        """Create (or re-attach to) an approval and wait for the decision.

        Returns {"status": "approved"|"rejected"|"expired", "comment": str|None, "modified_args": dict|None}.
        """
        async with session_scope() as db:
            existing = None
            if ctx.task_id and call_id:
                existing = (await db.execute(
                    select(Approval).where(Approval.task_id == ctx.task_id, Approval.call_id == call_id)
                )).scalars().first()
            if existing is not None and existing.status != "pending":
                return {"status": existing.status, "comment": existing.comment, "modified_args": existing.modified_args}
            if existing is None:
                appr = Approval(org_id=ctx.org_id, task_id=ctx.task_id, conversation_id=ctx.conversation_id,
                                agent_id=ctx.agent_id, requested_for_user_id=ctx.user_id, call_id=call_id, tool=tool,
                                args=args, risk=risk, reason=reason, preview=preview,
                                expires_at=utcnow() + timedelta(hours=APPROVAL_TTL_HOURS))
                db.add(appr)
                await db.flush()
                created = True
            else:
                appr = existing
                created = False
            approval_id = appr.id
            snap = await approval_snapshot(db, appr)
            if ctx.task_id:
                task = await db.get(Task, ctx.task_id)
                if task is not None and task.status not in ("cancelled", "completed", "failed"):
                    task.status = "waiting_approval"
                    task.current_step = f"Waiting for approval: {tool_display}"

        loop = asyncio.get_running_loop()
        fut = self._futures.get(approval_id)
        if fut is None or fut.done():
            fut = loop.create_future()
            self._futures[approval_id] = fut

        if created:
            await bus.emit(ctx.org_id, "approval.requested", {"approval": snap}, user_id=ctx.user_id, admin_visible=True)
            await post_event(ctx.org_id, ctx.conversation_id, {
                "kind": "approval_requested", "approval_id": approval_id, "task_id": ctx.task_id,
                "agent_id": ctx.agent_id, "agent_name": ctx.agent_name, "title": tool_display, "risk": risk,
                "tool": tool_display, "summary": reason, "status": "pending",
            }, content=preview, task_id=ctx.task_id, agent_id=ctx.agent_id)
            if ctx.user_id:
                await notify_user(ctx.org_id, ctx.user_id, "approval",
                                  f"Approval needed: {tool_display}",
                                  f"{ctx.agent_name or 'An agent'} wants to run {tool_display} ({risk} risk).",
                                  "/approvals")
            await audit(ctx.org_id, "approval.requested", user_id=ctx.user_id, agent_id=ctx.agent_id,
                        task_id=ctx.task_id, target_type="approval", target_id=approval_id, risk=risk,
                        details={"tool": tool})
        if ctx.task_id:
            await emit_task(ctx.task_id)

        try:
            decision = await fut
        except asyncio.CancelledError:
            # Task cancelled (or shutdown) while waiting: shutdown keeps it pending for resume; a user cancel expires it.
            from app.agents.task_manager import task_manager

            if ctx.task_id and task_manager.cancel_requested(ctx.task_id):
                self._futures.pop(approval_id, None)
                asyncio.ensure_future(self.decide(approval_id, approve=False, user_id="system",
                                                  comment="Task was cancelled", status="expired"))
            raise
        finally:
            self._futures.pop(approval_id, None)

        if ctx.task_id:
            async with session_scope() as db:
                task = await db.get(Task, ctx.task_id)
                if task is not None and task.status == "waiting_approval":
                    task.status = "running"
                    task.current_step = (f"Approved: {tool_display}" if decision["status"] == "approved"
                                         else f"Not approved: {tool_display}")
            await emit_task(ctx.task_id)
        return decision

    async def decide(self, approval_id: str, *, approve: bool, user_id: str, comment: str | None = None,
                     modified_args: dict | None = None, status: str | None = None) -> dict | None:
        async with session_scope() as db:
            appr = await db.get(Approval, approval_id)
            if appr is None or appr.status != "pending":
                return None
            appr.status = status or ("approved" if approve else "rejected")
            appr.decided_by = user_id
            appr.decided_at = utcnow()
            appr.comment = comment
            if approve and modified_args:
                appr.modified_args = modified_args
            snap = await approval_snapshot(db, appr)
            org_id, owner, task_id = appr.org_id, appr.requested_for_user_id, appr.task_id
            decision = {"status": appr.status, "comment": comment, "modified_args": appr.modified_args}
        fut = self._futures.get(approval_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        await bus.emit(org_id, "approval.resolved", {"approval": snap}, user_id=owner, admin_visible=True)
        await update_event_messages_for(task_id=task_id, approval_id=approval_id, kinds=("approval_requested",),
                                        event_patch={"status": decision["status"], "decision": decision["status"]})
        await audit(org_id, f"approval.{decision['status']}", user_id=user_id, task_id=task_id,
                    target_type="approval", target_id=approval_id, risk=snap.get("risk"),
                    details={"tool": snap.get("tool"), "comment": comment})
        return snap

    def is_waiting(self, approval_id: str) -> bool:
        f = self._futures.get(approval_id)
        return f is not None and not f.done()

    async def expire_loop(self, interval: float = 300.0) -> None:
        while True:
            try:
                async with session_scope() as db:
                    rows = (await db.execute(select(Approval).where(Approval.status == "pending"))).scalars().all()
                    now = utcnow()
                    expired = [a.id for a in rows if a.expires_at and as_utc(a.expires_at) < now]
                for aid in expired:
                    await self.decide(aid, approve=False, user_id="system", comment="Expired without a decision",
                                      status="expired")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("approval sweeper failed")
            await asyncio.sleep(interval)


broker = ApprovalBroker()
