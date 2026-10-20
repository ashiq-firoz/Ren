"""Supervises background agent tasks: creation, concurrency limits, pause/resume/cancel/retry, inbox delivery,
completion hooks (chat cards + orchestrator reports) and crash recovery."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from app.agents.common import ACTIVE, TERMINAL, emit_task, post_event, short, task_snapshot, update_event_messages_for
from app.core.config import settings
from app.core.db import session_scope, utcnow
from app.core.events import bus
from app.models import Agent, Organization, Task, TaskEvent
from app.services.audit import audit
from app.services.serialize import org_settings, task_event_out

log = logging.getLogger("ren.tasks")

FinishedHook = Callable[[dict], Awaitable[None]]


class TaskManager:
    def __init__(self) -> None:
        self.finished_hooks: list[FinishedHook] = []
        self.reset()

    def reset(self) -> None:
        """Fresh runtime state (called at app startup; asyncio primitives are bound to their event loop)."""
        self._handles: dict[str, asyncio.Task] = {}
        self._cancel: set[str] = set()
        self._pause: set[str] = set()
        self._resume: dict[str, asyncio.Event] = {}
        self._inbox: dict[str, asyncio.Event] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._org_sems: dict[str, asyncio.Semaphore] = {}
        self._child_sem = asyncio.Semaphore(32)

    # ------------------------------------------------------------------ creation
    async def create_task(
        self,
        *,
        org_id: str,
        agent_id: str,
        title: str,
        instructions: str,
        created_by: str | None,
        conversation_id: str | None = None,
        parent_id: str | None = None,
        priority: str = "normal",
        skill_id: str | None = None,
        schedule_id: str | None = None,
        depth: int = 0,
        start: bool = True,
        announce: bool = True,
    ) -> dict:
        async with session_scope() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None or agent.org_id != org_id:
                raise ValueError("Unknown agent")
            task = Task(org_id=org_id, agent_id=agent_id, title=short(title, 290) or "Untitled task",
                        instructions=instructions, created_by=created_by, conversation_id=conversation_id,
                        parent_id=parent_id, priority=priority if priority in ("low", "normal", "high", "urgent") else "normal",
                        skill_id=skill_id, schedule_id=schedule_id, depth=depth, status="queued",
                        current_step="Queued")
            db.add(task)
            await db.flush()
            snap = await task_snapshot(db, task)
            agent_name = agent.name
        await bus.emit(org_id, "task.created", {"task": snap}, user_id=created_by, admin_visible=True)
        if announce and conversation_id and parent_id is None:
            await post_event(org_id, conversation_id, {
                "kind": "task_created", "task_id": snap["id"], "agent_id": agent_id, "agent_name": agent_name,
                "title": snap["title"], "status": "queued",
            }, task_id=snap["id"], agent_id=agent_id)
        await audit(org_id, "task.created", user_id=created_by, agent_id=agent_id, task_id=snap["id"],
                    target_type="task", target_id=snap["id"], details={"title": snap["title"], "parent_id": parent_id})
        if start:
            self.start(snap["id"], org_id, depth)
        return snap

    def start(self, task_id: str, org_id: str, depth: int = 0) -> None:
        if task_id in self._handles and not self._handles[task_id].done():
            return
        self._cancel.discard(task_id)
        self._handles[task_id] = asyncio.create_task(self._run(task_id, org_id, depth), name=f"task:{task_id}")

    async def _org_sem(self, org_id: str) -> asyncio.Semaphore:
        sem = self._org_sems.get(org_id)
        if sem is None:
            limit = settings.max_concurrent_tasks
            async with session_scope() as db:
                org = await db.get(Organization, org_id)
                if org is not None:
                    limit = int(org_settings(org).get("max_concurrent_tasks") or limit)
            sem = asyncio.Semaphore(max(1, limit))
            self._org_sems[org_id] = sem
        return sem

    async def _run(self, task_id: str, org_id: str, depth: int) -> None:
        try:
            from app.agents.runtime import AgentRunner

            # Sub-tasks use a separate pool so parents waiting on children can never starve them of slots.
            sem = self._child_sem if depth > 0 else await self._org_sem(org_id)
            if sem.locked():
                async with session_scope() as db:
                    t = await db.get(Task, task_id)
                    if t is not None and t.status == "queued":
                        t.current_step = "Waiting for a free worker slot"
                await emit_task(task_id)
            async with sem:
                await AgentRunner(self, task_id).run()
        except asyncio.CancelledError:
            if task_id in self._cancel:
                await self._mark_cancelled(task_id)
            else:
                raise
        except Exception as e:  # noqa: BLE001
            log.exception("runner crashed for %s", task_id)
            await self._mark_failed(task_id, f"Internal error while running the task: {e.__class__.__name__}: {e}")
        finally:
            self._handles.pop(task_id, None)
            if task_id in self._cancel:
                await self._mark_cancelled(task_id)
                self._cancel.discard(task_id)
            await self._after_run(task_id)

    async def _after_run(self, task_id: str) -> None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            if task.status not in TERMINAL:
                return  # paused/waiting/shutdown — nothing to announce
            snap = await task_snapshot(db, task)
            agent = await db.get(Agent, task.agent_id)
            agent_name = agent.name if agent else "Agent"
        for fut in self._waiters.pop(task_id, []):
            if not fut.done():
                fut.set_result(snap)
        await bus.emit(task.org_id, "task.updated", {"task": snap}, user_id=task.created_by, admin_visible=True)
        status = snap["status"]
        if task.conversation_id and task.parent_id is None:
            kind = {"completed": "task_completed", "failed": "task_failed", "cancelled": "task_cancelled"}[status]
            await update_event_messages_for(task_id=task_id, kinds=("task_created",), event_patch={"status": status})
            await post_event(task.org_id, task.conversation_id, {
                "kind": kind, "task_id": task_id, "agent_id": task.agent_id, "agent_name": agent_name,
                "title": snap["title"], "status": status,
                "summary": short(snap["result"] or snap["error"] or "", 280),
            }, task_id=task_id, agent_id=task.agent_id)
        if task.created_by and status in ("completed", "failed") and task.parent_id is None:
            from app.services.notifications import notify_user

            await notify_user(task.org_id, task.created_by, f"task_{status}",
                              f"{agent_name} {'finished' if status == 'completed' else 'could not finish'}: {snap['title']}",
                              short(snap["result"] or snap["error"] or "", 200), f"/tasks/{task_id}")
        if status == "completed" and task.created_by:
            await self._remember_outcome(task.org_id, task.created_by, task.agent_id, snap)
        for hook in list(self.finished_hooks):
            try:
                await hook(snap)
            except Exception:  # noqa: BLE001
                log.exception("finished hook failed")

    async def _remember_outcome(self, org_id: str, user_id: str, agent_id: str, snap: dict) -> None:
        try:
            from app.services.memory import store_memory

            await store_memory(org_id=org_id, user_id=user_id, agent_id=agent_id, kind="episodic", scope="user",
                               content=f"Completed task '{snap['title']}' ({snap['agent_name']}): "
                                       f"{short(snap['result'] or '', 400)}",
                               source=f"task:{snap['id']}", importance=0.4)
        except Exception:  # noqa: BLE001
            log.debug("episodic memory skipped", exc_info=True)

    async def _mark_failed(self, task_id: str, error: str) -> None:
        """Never leave a task silently stuck: surface infrastructure errors as a failed task."""
        try:
            async with session_scope() as db:
                task = await db.get(Task, task_id)
                if task is None or task.status in TERMINAL:
                    return
                task.status, task.error, task.finished_at = "failed", error[:2000], utcnow()
                task.current_step = "Failed"
                db.add(TaskEvent(task_id=task_id, kind="error", content={"message": error[:2000]}))
            await emit_task(task_id)
        except Exception:  # noqa: BLE001
            log.exception("could not mark task %s failed", task_id)

    async def _mark_cancelled(self, task_id: str) -> None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None or task.status in TERMINAL:
                return
            task.status = "cancelled"
            task.finished_at = utcnow()
            task.current_step = "Cancelled"
            db.add(TaskEvent(task_id=task_id, kind="status", content={"status": "cancelled"}))
        await emit_task(task_id)

    # ------------------------------------------------------------------ control
    def cancel_requested(self, task_id: str) -> bool:
        return task_id in self._cancel

    async def gate(self, task_id: str) -> None:
        """Called by the runner between steps: honours cancel and pause."""
        from app.agents.runtime import TaskCancelled

        if task_id in self._cancel:
            raise TaskCancelled()
        if task_id in self._pause:
            ev = self._resume.setdefault(task_id, asyncio.Event())
            ev.clear()
            async with session_scope() as db:
                t = await db.get(Task, task_id)
                if t is not None:
                    t.status = "paused"
                    t.current_step = "Paused"
            await emit_task(task_id)
            await ev.wait()
            if task_id in self._cancel:
                raise TaskCancelled()
            async with session_scope() as db:
                t = await db.get(Task, task_id)
                if t is not None:
                    t.status = "running"
                    t.current_step = "Resumed"
            await emit_task(task_id)

    async def cancel(self, task_id: str, user_id: str | None = None, cascade: bool = True) -> dict | None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return None
            if task.status in TERMINAL:
                return await task_snapshot(db, task)
            children = (await db.execute(select(Task.id).where(Task.parent_id == task_id))).scalars().all()
            org_id = task.org_id
            if task_id not in self._handles:
                task.status, task.finished_at, task.current_step = "cancelled", utcnow(), "Cancelled"
        self._cancel.add(task_id)
        self._pause.discard(task_id)
        if task_id in self._resume:
            self._resume[task_id].set()
        handle = self._handles.get(task_id)
        if handle is not None and not handle.done():
            handle.cancel()
        else:
            await self._after_run(task_id)
        if cascade:
            for cid in children:
                await self.cancel(cid, user_id)
        await audit(org_id, "task.cancelled", user_id=user_id, task_id=task_id, target_type="task", target_id=task_id)
        await emit_task(task_id)
        async with session_scope() as db:
            t = await db.get(Task, task_id)
            return await task_snapshot(db, t) if t else None

    async def pause(self, task_id: str) -> dict | None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None or task.status in TERMINAL:
                return await task_snapshot(db, task) if task else None
            if task_id not in self._handles:
                task.status, task.current_step = "paused", "Paused"
        self._pause.add(task_id)
        return await emit_task(task_id)

    async def resume(self, task_id: str) -> dict | None:
        self._pause.discard(task_id)
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return None
            org_id, depth, status = task.org_id, task.depth, task.status
            if status == "paused" and task_id not in self._handles:
                task.status = "queued"
        ev = self._resume.get(task_id)
        if ev is not None:
            ev.set()
        if task_id not in self._handles and status in ("paused", "queued"):
            self.start(task_id, org_id, depth)
        return await emit_task(task_id)

    async def retry(self, task_id: str, user_id: str | None = None) -> dict | None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return None
            if task.status not in TERMINAL:
                return await task_snapshot(db, task)
            task.status, task.result, task.error, task.progress = "queued", None, None, 0
            task.steps, task.checkpoint, task.reported = 0, {}, False
            task.started_at = task.finished_at = None
            task.current_step = "Queued (retry)"
            org_id, depth = task.org_id, task.depth
            db.add(TaskEvent(task_id=task_id, kind="status", content={"status": "queued", "note": "Retry requested"}))
        await audit(org_id, "task.retried", user_id=user_id, task_id=task_id, target_type="task", target_id=task_id)
        self.start(task_id, org_id, depth)
        return await emit_task(task_id)

    def inbox_event(self, task_id: str) -> asyncio.Event:
        return self._inbox.setdefault(task_id, asyncio.Event())

    async def send_message(self, task_id: str, text: str, *, sender: str = "user",
                           sender_name: str | None = None) -> dict | None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return None
            ev = TaskEvent(task_id=task_id, kind="inbox", consumed=task.status in TERMINAL,
                           content={"from": sender, "text": text, "sender_name": sender_name})
            db.add(ev)
            await db.flush()
            out = task_event_out(ev)
            org_id, owner = task.org_id, task.created_by
        await bus.emit(org_id, "task.event", {"task_id": task_id, "event": out}, user_id=owner, admin_visible=True)
        self.inbox_event(task_id).set()
        return out

    async def wait(self, task_id: str, timeout: float | None = None) -> dict | None:
        async with session_scope() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return None
            if task.status in TERMINAL:
                return await task_snapshot(db, task)
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(task_id, []).append(fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            async with session_scope() as db:
                t = await db.get(Task, task_id)
                return await task_snapshot(db, t) if t else None

    def is_running(self, task_id: str) -> bool:
        h = self._handles.get(task_id)
        return h is not None and not h.done()

    # ------------------------------------------------------------------ lifecycle
    async def recover(self) -> int:
        """Resume tasks interrupted by a restart (queued/running/waiting_approval)."""
        async with session_scope() as db:
            rows = (await db.execute(
                select(Task).where(Task.status.in_(("queued", "running", "waiting_approval"))).order_by(Task.created_at)
            )).scalars().all()
            items = [(t.id, t.org_id, t.depth) for t in rows]
        for tid, oid, depth in items:
            self.start(tid, oid, depth)
        if items:
            log.info("recovered %d tasks", len(items))
        return len(items)

    async def shutdown(self) -> None:
        handles = list(self._handles.values())
        for h in handles:
            h.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

    @staticmethod
    def active_statuses() -> set[str]:
        return set(ACTIVE)


task_manager = TaskManager()
