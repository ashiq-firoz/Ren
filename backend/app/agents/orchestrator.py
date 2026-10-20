"""Ren — the conversational orchestrator.

Design goals (the core product promise):
  * The user can message Ren at ANY time. Posting never blocks; messages land in a per-conversation inbox.
  * Ren replies fast because heavy work is delegated to background worker agents (TaskManager).
  * Every turn sees a live task board (running / waiting / finished work), pending approvals and memories, so Ren
    answers the newest message while staying aware of everything in flight.
  * When tasks finish / fail, the hub enqueues a task trigger and Ren proactively reports — batched with any
    new user messages into a single coherent reply.
Turns are serialized per conversation (ordering stays sane) and short (tool loops are capped).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select

from app.agents.common import ACTIVE, TERMINAL, post_message, short, update_message_meta
from app.agents.prompts import ORCHESTRATOR_PROMPT, format_policies, today
from app.agents.task_manager import task_manager
from app.core.db import as_utc, iso, session_scope, utcnow
from app.core.events import bus
from app.llm.client import LLMError, NoProviderError
from app.llm.providers import get_llm
from app.models import Agent, Approval, Conversation, Membership, Message, Organization, Skill, Task, User
from app.policy.guard import applicable_policies, check_text, public_summary, revise_to_comply
from app.services.memory import context_memories, search_memories, store_memory
from app.services.serialize import conversation_out, message_out, org_settings
from app.services.usage import record_usage

log = logging.getLogger("ren.orchestrator")

MAX_TOOL_ROUNDS = 6
HISTORY_LIMIT = 40
HISTORY_CHARS = 60_000
IDLE_GC_SECONDS = 1800


@dataclass
class Trigger:
    kind: str  # user | task
    id: str


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}


ORCH_TOOLS = [
    _tool("delegate_task",
          "Assign work to a specialist agent; it runs in the background while you keep talking with the user. "
          "Instructions must be complete and self-contained (the agent cannot see this chat).",
          {"agent": {"type": "string", "description": "Agent name from the swarm roster."},
           "title": {"type": "string", "description": "Short task title (max ~8 words)."},
           "instructions": {"type": "string", "description": "Goal, context, constraints, inputs (file paths / "
                            "document ids) and the expected deliverable."},
           "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]}},
          ["agent", "title", "instructions"]),
    _tool("message_task", "Send new instructions or information to a running task (delivered before its next step).",
          {"task_id": {"type": "string", "description": "Task ref from the board, e.g. t:1a2b3c4d."},
           "message": {"type": "string"}}, ["task_id", "message"]),
    _tool("cancel_task", "Stop a running task.",
          {"task_id": {"type": "string"}, "reason": {"type": "string"}}, ["task_id"]),
    _tool("pause_task", "Pause a running task.", {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("resume_task", "Resume a paused task.", {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("get_task_status", "Detailed status of one task (or all of this conversation's tasks if no id).",
          {"task_id": {"type": "string"}}, []),
    _tool("get_task_result", "Full result text and artifacts of a finished task.",
          {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("search_knowledge",
          "Search the company knowledge base (structure-aware hybrid retrieval). Returns numbered sources; cite them "
          "inline as [n] exactly as given.",
          {"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]),
    _tool("remember", "Store a durable fact or preference about the user or workspace for future conversations.",
          {"content": {"type": "string"},
           "kind": {"type": "string", "enum": ["preference", "semantic", "project", "org"]},
           "scope": {"type": "string", "enum": ["user", "org"], "description": "org = shared with the whole workspace"}},
          ["content"]),
    _tool("recall", "Search long-term memory about the user/workspace.", {"query": {"type": "string"}}, ["query"]),
    _tool("run_skill", "Run a saved playbook (skill) as a background task.",
          {"skill": {"type": "string", "description": "Skill name"},
           "inputs": {"type": "object", "description": "Values for the skill's inputs"}}, ["skill"]),
    _tool("schedule_task",
          "Schedule work for later or on a recurring basis (e.g. a weekly report every Monday 09:00).",
          {"agent": {"type": "string"}, "title": {"type": "string"}, "instructions": {"type": "string"},
           "run_at": {"type": "string", "description": "ISO-8601 local date/time for a one-off run, e.g. 2026-10-06T09:00"},
           "cron": {"type": "string", "description": "Cron expression for recurring runs, e.g. '0 9 * * 1' (Mondays 09:00)"},
           "timezone": {"type": "string",
                        "description": "IANA time zone that run_at / cron are in (default: the user's time zone)"}},
          ["agent", "title", "instructions"]),
    _tool("manage_schedule",
          "Change, pause, resume, run now or cancel one of the user's schedules (ids are listed under 'Schedules').",
          {"schedule": {"type": "string", "description": "Schedule id (or its first 8 characters) or exact title"},
           "action": {"type": "string", "enum": ["update", "pause", "resume", "run_now", "cancel"]},
           "title": {"type": "string"}, "instructions": {"type": "string"},
           "cron": {"type": "string", "description": "New cron expression (recurring)"},
           "run_at": {"type": "string", "description": "New ISO-8601 local date/time (one-off)"},
           "timezone": {"type": "string", "description": "IANA time zone for cron / run_at"}},
          ["schedule", "action"]),
]

TOOL_LABELS = {
    "delegate_task": "Delegating", "message_task": "Updating a task", "cancel_task": "Cancelling a task",
    "pause_task": "Pausing a task", "resume_task": "Resuming a task", "get_task_status": "Checking tasks",
    "get_task_result": "Reading a result", "search_knowledge": "Searching knowledge", "remember": "Remembering",
    "recall": "Recalling", "run_skill": "Starting a playbook", "schedule_task": "Scheduling",
    "manage_schedule": "Updating a schedule",
}


def ref(task_id: str) -> str:
    return f"t:{task_id[:8]}"


def ago(dt) -> str:
    if dt is None:
        return ""
    secs = int((utcnow() - as_utc(dt)).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class ConversationOrchestrator:
    def __init__(self, hub: OrchestratorHub, conv_id: str, org_id: str, user_id: str):
        self.hub = hub
        self.conv_id = conv_id
        self.org_id = org_id
        self.user_id = user_id
        self.queue: asyncio.Queue[Trigger] = asyncio.Queue()
        self.status = "idle"
        self.detail: str | None = None
        self.busy = False
        self.user_tz: str | None = None
        self.stop_event = asyncio.Event()
        self.last_active = time.time()
        self.worker = asyncio.create_task(self._loop(), name=f"orch:{conv_id}")

    @property
    def queued(self) -> int:
        return self.queue.qsize()

    async def set_status(self, status: str, detail: str | None = None) -> None:
        self.status, self.detail = status, detail
        await bus.emit(self.org_id, "conversation.status",
                       {"conversation_id": self.conv_id, "status": status, "detail": detail, "queued": self.queued},
                       user_id=self.user_id, admin_visible=False)

    async def enqueue(self, trig: Trigger) -> None:
        self.last_active = time.time()
        await self.queue.put(trig)
        if self.busy:
            await self.set_status(self.status, self.detail)  # broadcast the new queued count

    async def _loop(self) -> None:
        while True:
            trig = await self.queue.get()
            batch = [trig]
            await asyncio.sleep(0.05)  # coalesce bursts (e.g. several tasks finishing together)
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            self.busy = True
            try:
                await self._turn(batch)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("orchestrator turn failed (%s)", self.conv_id)
                await post_message(org_id=self.org_id, conversation_id=self.conv_id, role="assistant",
                                   content="Sorry — something went wrong on my side while replying. Please try again.",
                                   status="error")
            finally:
                self.busy = False
                self.last_active = time.time()
                if self.queue.empty():
                    await self.set_status("idle")

    # ------------------------------------------------------------------ the turn
    async def _turn(self, batch: list[Trigger]) -> None:
        self.stop_event.clear()
        await self.set_status("thinking")
        user_msg_ids = [t.id for t in batch if t.kind == "user"]
        task_ids = list(dict.fromkeys(t.id for t in batch if t.kind == "task"))

        async with session_scope() as db:
            conv = await db.get(Conversation, self.conv_id)
            if conv is None:
                return
            org = await db.get(Organization, self.org_id)
            user = await db.get(User, self.user_id)
            member = (await db.execute(select(Membership).where(Membership.org_id == self.org_id,
                                                                Membership.user_id == self.user_id))).scalars().first()
            role = member.role if member else "viewer"
            orch = (await db.execute(select(Agent).where(Agent.org_id == self.org_id,
                                                         Agent.is_orchestrator.is_(True)))).scalars().first()
            changed = []
            if user_msg_ids:
                for m in (await db.execute(select(Message).where(Message.id.in_(user_msg_ids)))).scalars().all():
                    m.processed = True
                    if m.status == "queued":
                        m.status = "complete"
                        changed.append(message_out(m))
            history = await self._history(db, conv)
            board = await self._task_board(db)
            approvals = await self._approvals(db)
            roster, agents = await self._roster(db)
            schedules = await self._schedules(db)
            skills = (await db.execute(select(Skill.name).where(Skill.org_id == self.org_id))).scalars().all()
            policies = await applicable_policies(db, self.org_id, orch.id if orch else None, "response")
            updates = await self._updates_notice(db, task_ids)
            cfg = org_settings(org)
            try:
                llm = await get_llm(db, self.org_id, agent=orch, purpose="orchestrator")
            except NoProviderError as e:
                llm, provider_error = None, str(e)
            else:
                provider_error = None
            conv_summary = conv.summary
            is_new_title = conv.title in ("New conversation", "", None)
        for m in changed:
            await bus.emit(self.org_id, "message.updated", {"message": m}, user_id=self.user_id, admin_visible=False)

        if llm is None:
            await post_message(org_id=self.org_id, conversation_id=self.conv_id, role="assistant",
                               content=f"⚠️ {provider_error}", status="error", agent_id=orch.id if orch else None)
            return

        last_user_text = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        memories = await context_memories(self.org_id, self.user_id, last_user_text)
        system = ORCHESTRATOR_PROMPT.format(
            org_name=org.name, mode=org.mode, user_name=user.name if user else "the user", user_role=role,
            roster=roster + (f"\n\nSkills (playbooks for run_skill): {', '.join(skills[:40])}" if skills else ""),
            task_board=board, approvals=approvals,
            memories="\n".join(f"- ({m['kind']}) {m['content']}" for m in memories) or "- (nothing stored yet)",
            policies=format_policies(policies), schedules=schedules, timezone=self.user_tz or "UTC",
            today=today(self.user_tz))
        if conv_summary:
            system += f"\n\n# Summary of the earlier conversation\n{conv_summary}"
        messages: list[dict] = [{"role": "system", "content": system}] + history
        if updates:
            messages.append({"role": "user", "content": updates})
        if len(messages) == 1:
            return

        align = cfg["alignment"]
        align_on = bool(align.get("enabled", True))
        pre_mode = align_on and align.get("mode") == "pre"
        msg = await post_message(org_id=self.org_id, conversation_id=self.conv_id, role="assistant", content="",
                                 status="streaming", agent_id=orch.id if orch else None,
                                 meta={"alignment": {"verdict": "pending", "checker": "none", "latency_ms": None,
                                                     "violations": []}} if align_on else {})
        msg_id = msg["id"]
        state: dict[str, Any] = {"content": "", "tool_calls": [], "citations": []}

        async def on_delta(delta: str) -> None:
            state["content"] += delta
            if not pre_mode:
                await bus.emit(self.org_id, "message.delta",
                               {"conversation_id": self.conv_id, "message_id": msg_id, "delta": delta},
                               user_id=self.user_id, admin_visible=False)

        reasoning_seen = {"flag": False}

        async def on_reasoning(_delta: str) -> None:
            if not reasoning_seen["flag"]:
                reasoning_seen["flag"] = True
                await self.set_status("thinking", "Reasoning…")

        stopped = False
        try:
            for _round in range(MAX_TOOL_ROUNDS):
                reasoning_seen["flag"] = False
                if self.stop_event.is_set():
                    stopped = True
                    break
                await self.set_status("responding" if _round == 0 else "thinking")
                last_round = _round == MAX_TOOL_ROUNDS - 1
                res = await llm.stream(messages, on_delta=on_delta, tools=None if last_round else ORCH_TOOLS,
                                       temperature=0.4, stop_event=self.stop_event, on_reasoning=on_reasoning,
                                       reasoning_effort=(llm.extra or {}).get("orchestrator_reasoning_effort", "low"))
                await record_usage(self.org_id, res.model, res.usage, purpose="orchestrator",
                                   agent_id=orch.id if orch else None)
                if res.finish_reason == "stopped":
                    stopped = True
                    break
                if not res.tool_calls:
                    break
                messages.append(res.assistant_message())
                for tc in res.tool_calls:
                    state["tool_calls"].append({"id": tc.id, "name": tc.name, "display": TOOL_LABELS.get(tc.name, tc.name),
                                                "args": tc.arguments, "status": "running"})
                await update_message_meta(msg_id, {"tool_calls": state["tool_calls"]})
                label = TOOL_LABELS.get(res.tool_calls[0].name, "Working")
                if res.tool_calls[0].name == "delegate_task":
                    label = f"Delegating to {res.tool_calls[0].arguments.get('agent', 'a specialist')}"
                await self.set_status("using_tool", label)
                outs = await asyncio.gather(*(self._exec(tc.name, tc.arguments, agents, state) for tc in res.tool_calls),
                                            return_exceptions=True)
                for tc, out in zip(res.tool_calls, outs):
                    text = out if isinstance(out, str) else f"Error: {out}"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": text})
                    for meta in state["tool_calls"]:
                        if meta["id"] == tc.id:
                            meta["status"] = "error" if text.startswith("Error") else "done"
                            meta["result_preview"] = short(text, 240)
                patch: dict[str, Any] = {"tool_calls": state["tool_calls"]}
                if state["citations"]:
                    patch["citations"] = state["citations"]
                await update_message_meta(msg_id, patch)
                if state["content"] and not state["content"].endswith("\n"):
                    await on_delta("\n\n")
        except LLMError as e:
            state["content"] += f"\n\n⚠️ The model endpoint returned an error: {e}"
            log.warning("orchestrator LLM error: %s", e)

        content = state["content"].strip()
        if not content:
            content = "Stopped." if stopped else self._fallback_reply(state["tool_calls"])
        final_meta: dict[str, Any] = {"tool_calls": state["tool_calls"]} if state["tool_calls"] else {}
        if state["citations"]:
            final_meta["citations"] = state["citations"]
        if align_on:
            final_meta["alignment"] = {"verdict": "pending", "checker": "none", "latency_ms": None, "violations": []}

        if pre_mode:
            verdict = await check_text(org_id=self.org_id, text=content, user_request=last_user_text,
                                       agent_id=orch.id if orch else None, kind="response", message_id=msg_id)
            revised = False
            if verdict["verdict"] == "block":
                content = await self._revise(llm, messages, content, verdict)
                revised = True
            final_meta["alignment"] = public_summary(verdict, revised)
        await self._complete_message(msg_id, content, final_meta)

        if align_on and not pre_mode:
            asyncio.create_task(self._post_check(llm, messages, msg_id, content, last_user_text,
                                                 orch.id if orch else None))

        if task_ids:
            async with session_scope() as db:
                for t in (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all():
                    t.reported = True
        if is_new_title and last_user_text:
            asyncio.create_task(self._title(llm, last_user_text))
        asyncio.create_task(self._maybe_summarize(llm))

    # ------------------------------------------------------------------ helpers
    async def _complete_message(self, msg_id: str, content: str, meta: dict) -> None:
        async with session_scope() as db:
            m = await db.get(Message, msg_id)
            if m is None:
                return
            m.content = content
            m.status = "complete"
            merged = dict(m.meta or {})
            merged.update(meta)
            m.meta = merged
            await db.flush()
            out = message_out(m)
        await bus.emit(self.org_id, "message.completed", {"message": out}, user_id=self.user_id, admin_visible=False)

    async def _post_check(self, llm, messages: list[dict], msg_id: str, content: str, user_text: str,
                          agent_id: str | None) -> None:
        verdict = await check_text(org_id=self.org_id, text=content, user_request=user_text, agent_id=agent_id,
                                   kind="response", message_id=msg_id)
        if verdict["verdict"] == "block":
            revised = await self._revise(llm, messages, content, verdict)
            await update_message_meta(msg_id, {"alignment": public_summary(verdict, True)}, content=revised)
        else:
            await update_message_meta(msg_id, {"alignment": public_summary(verdict)})

    async def _revise(self, llm, messages: list[dict], content: str, verdict: dict) -> str:
        base = [m for m in messages if m.get("role") in ("system", "user", "assistant") and not m.get("tool_calls")]
        try:
            revised = await revise_to_comply(llm, base, content, verdict)
        except LLMError:
            revised = ""
        if not revised:
            names = ", ".join(sorted({v["policy_name"] for v in verdict["violations"]}))
            revised = f"I can't share that response because it conflicts with workspace policy ({names})."
        return revised

    @staticmethod
    def _fallback_reply(tool_calls: list[dict]) -> str:
        if any(t["name"] == "delegate_task" and t["status"] == "done" for t in tool_calls):
            return "On it — I've assigned this to the team and will report back here as soon as there's progress."
        return "Done."

    async def _title(self, llm, first_text: str) -> None:
        title = short(first_text, 60)
        try:
            res = await llm.chat([
                {"role": "system", "content": "Write a concise 3-6 word title for this conversation. Reply with the title only."},
                {"role": "user", "content": first_text[:1500]}], temperature=0.2, max_tokens=24)
            cand = (res.content or "").strip().strip('"').strip("'").strip()
            if 2 <= len(cand) <= 80 and not cand.startswith("(mock)"):
                title = cand
        except Exception:  # noqa: BLE001
            pass
        async with session_scope() as db:
            conv = await db.get(Conversation, self.conv_id)
            if conv is None or conv.title not in ("New conversation", "", None):
                return
            conv.title = title
            out = conversation_out(conv, status=self.status)
        await bus.emit(self.org_id, "conversation.updated", {"conversation": out}, user_id=self.user_id,
                       admin_visible=False)

    async def _maybe_summarize(self, llm) -> None:
        """Rolling summary so long conversations keep early context without blowing the window."""
        try:
            async with session_scope() as db:
                conv = await db.get(Conversation, self.conv_id)
                q = select(Message).where(Message.conversation_id == self.conv_id,
                                          Message.role.in_(("user", "assistant"))).order_by(Message.created_at)
                rows = (await db.execute(q)).scalars().all()
                if conv is None or len(rows) <= HISTORY_LIMIT + 20:
                    return
                upto_idx = 0
                if conv.summary_upto:
                    ids = [r.id for r in rows]
                    upto_idx = ids.index(conv.summary_upto) + 1 if conv.summary_upto in ids else 0
                cut = len(rows) - HISTORY_LIMIT
                if cut - upto_idx < 20:
                    return
                chunk = rows[upto_idx:cut]
                transcript = "\n".join(f"{r.role.upper()}: {short(r.content, 600)}" for r in chunk)
                prev = conv.summary or ""
                last_id = chunk[-1].id
            res = await llm.chat([
                {"role": "system", "content": "Update the running summary of a conversation between a user and Ren "
                 "(an AI orchestrator). Keep decisions, requirements, facts, open questions and task outcomes. "
                 "Max 250 words."},
                {"role": "user", "content": f"Current summary:\n{prev or '(none)'}\n\nNew transcript:\n{transcript}"}],
                temperature=0.1, max_tokens=500)
            if res.content:
                async with session_scope() as db:
                    conv = await db.get(Conversation, self.conv_id)
                    if conv is not None:
                        conv.summary = res.content.strip()
                        conv.summary_upto = last_id
        except Exception:  # noqa: BLE001
            log.debug("summary update failed", exc_info=True)

    async def _history(self, db, conv: Conversation) -> list[dict]:
        q = (select(Message).where(Message.conversation_id == conv.id, Message.role.in_(("user", "assistant")),
                                   Message.status != "streaming")
             .order_by(Message.created_at.desc()).limit(HISTORY_LIMIT))
        rows = list(reversed((await db.execute(q)).scalars().all()))
        out: list[dict] = []
        total = 0
        agent_names: dict[str, str] = {}
        for m in reversed(rows):
            text = m.content or ""
            if m.role == "user":
                meta = m.meta or {}
                atts = meta.get("attachments") or []
                if atts:
                    text += "\n\n(Attached files: " + "; ".join(
                        f"{a.get('name')} at workspace path `{a.get('path')}`"
                        + (f", knowledge document id {a.get('document_id')}" if a.get("document_id") else "")
                        for a in atts) + ")"
                if meta.get("mention_agent_id"):
                    aid = meta["mention_agent_id"]
                    if aid not in agent_names:
                        ag = await db.get(Agent, aid)
                        agent_names[aid] = ag.name if ag else "that agent"
                    text += f"\n\n(The user addressed this to @{agent_names[aid]} — delegate it to that agent.)"
            total += len(text)
            if total > HISTORY_CHARS and out:
                break
            out.append({"role": m.role, "content": text})
        return list(reversed(out))

    async def _task_board(self, db) -> str:
        conv_tasks = (await db.execute(
            select(Task).where(Task.conversation_id == self.conv_id, Task.parent_id.is_(None))
            .order_by(Task.created_at.desc()).limit(15))).scalars().all()
        other_active = (await db.execute(
            select(Task).where(Task.org_id == self.org_id, Task.created_by == self.user_id,
                               or_(Task.conversation_id.is_(None), Task.conversation_id != self.conv_id),
                               Task.status.in_(tuple(ACTIVE)), Task.parent_id.is_(None))
            .order_by(Task.created_at.desc()).limit(8))).scalars().all()
        agent_ids = {t.agent_id for t in list(conv_tasks) + list(other_active)}
        names = {}
        if agent_ids:
            for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all():
                names[a.id] = a.name

        def line(t: Task) -> str:
            base = f"- [{ref(t.id)}] \"{t.title}\" — {names.get(t.agent_id, 'agent')} — {t.status}"
            if t.status in ("running", "queued", "paused"):
                base += f" {t.progress}% — now: {short(t.current_step, 120) or '…'} — started {ago(t.started_at or t.created_at)}"
            elif t.status == "waiting_approval":
                base += f" — {short(t.current_step, 120)}"
            elif t.status == "completed":
                base += f" {ago(t.finished_at)} — result: {short(t.result, 220)}"
                base += "" if t.reported else " (not yet reported to the user)"
            elif t.status == "failed":
                base += f" {ago(t.finished_at)} — error: {short(t.error, 160)}"
            return base

        parts = []
        if conv_tasks:
            parts.append("This conversation:\n" + "\n".join(line(t) for t in reversed(conv_tasks)))
        if other_active:
            parts.append("Other active work (other chats):\n" + "\n".join(line(t) for t in other_active))
        return "\n\n".join(parts) or "No tasks yet."

    async def _approvals(self, db) -> str:
        rows = (await db.execute(
            select(Approval).where(Approval.org_id == self.org_id, Approval.status == "pending",
                                   Approval.requested_for_user_id == self.user_id)
            .order_by(Approval.created_at).limit(10))).scalars().all()
        if not rows:
            return "None."
        return "\n".join(f"- {a.tool.replace('_', '.', 1)} ({a.risk} risk) for task [{ref(a.task_id or '')}] — "
                         f"{short(a.reason, 140)} (the user can approve/reject in the approval card)" for a in rows)

    async def _roster(self, db) -> tuple[str, list[Agent]]:
        agents = (await db.execute(select(Agent).where(Agent.org_id == self.org_id, Agent.enabled.is_(True),
                                                       Agent.is_orchestrator.is_(False)).order_by(Agent.name))).scalars().all()
        lines = []
        for a in agents:
            tools = ", ".join((a.tool_groups or [])[:8])
            lines.append(f"- **{a.name}** — {a.role}. {short(a.description, 200)} (tools: {tools or 'none'})")
        return ("\n".join(lines) or "- (no specialist agents are enabled — answer directly)"), list(agents)

    async def _updates_notice(self, db, task_ids: list[str]) -> str:
        if not task_ids:
            return ""
        tasks = (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        lines = []
        for t in tasks:
            agent = await db.get(Agent, t.agent_id)
            name = agent.name if agent else "agent"
            from app.models import Artifact

            arts = (await db.execute(select(Artifact.path).where(Artifact.task_id == t.id))).scalars().all()
            if t.status == "completed":
                lines.append(f"- Task [{ref(t.id)}] '{t.title}' by {name} COMPLETED.\n  Result:\n{(t.result or '')[:3000]}"
                             + (f"\n  Artifacts: {', '.join(arts)}" if arts else ""))
            elif t.status == "failed":
                lines.append(f"- Task [{ref(t.id)}] '{t.title}' by {name} FAILED: {short(t.error, 600)}")
            elif t.status == "cancelled":
                lines.append(f"- Task [{ref(t.id)}] '{t.title}' by {name} was CANCELLED.")
        if not lines:
            return ""
        return ("[System notice — TASK UPDATE, not written by the user]\n" + "\n".join(lines)
                + "\n\nReport these updates to the user now: concise, faithful to the results, mention artifact paths, "
                  "suggest sensible next steps. If the user also sent new messages, answer them in the same reply.")

    # ------------------------------------------------------------------ tool execution
    async def _resolve_task(self, raw: str) -> Task | None:
        key = (raw or "").strip().removeprefix("t:").removeprefix("[").removesuffix("]").removeprefix("t:")
        key = "".join(ch for ch in key.lower() if ch in "0123456789abcdef-")
        if not key:
            return None
        async with session_scope() as db:
            rows = (await db.execute(select(Task).where(Task.org_id == self.org_id, Task.id.like(f"{key}%"))
                                     .limit(2))).scalars().all()
            return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _match_agent(name: str, agents: list[Agent]) -> Agent | None:
        n = (name or "").strip().lower().lstrip("@")
        for a in agents:
            if n in (a.name.lower(), a.slug.lower()):
                return a
        for a in agents:
            if n and (n in a.name.lower() or a.name.lower() in n):
                return a
        words = set(n.replace("-", " ").split())
        best, score = None, 0
        for a in agents:
            s = len(words & set((a.name + " " + a.role).lower().replace("-", " ").split()))
            if s > score:
                best, score = a, s
        return best

    async def _exec(self, name: str, args: dict, agents: list[Agent], state: dict) -> str:
        try:
            if name == "delegate_task":
                agent = self._match_agent(str(args.get("agent", "")), agents)
                if agent is None:
                    return "Error: no matching agent. Available: " + ", ".join(a.name for a in agents)
                snap = await task_manager.create_task(
                    org_id=self.org_id, agent_id=agent.id, title=str(args.get("title") or "Task"),
                    instructions=str(args.get("instructions") or args.get("title") or ""), created_by=self.user_id,
                    conversation_id=self.conv_id, priority=str(args.get("priority") or "normal"))
                return f"Task [{ref(snap['id'])}] '{snap['title']}' assigned to {agent.name}; it is running in the background."
            if name in ("message_task", "cancel_task", "pause_task", "resume_task", "get_task_result"):
                task = await self._resolve_task(str(args.get("task_id", "")))
                if task is None:
                    return "Error: task not found — use a ref from the task board, e.g. t:1a2b3c4d."
                if name == "message_task":
                    await task_manager.send_message(task.id, str(args.get("message", "")), sender="orchestrator",
                                                    sender_name="Ren (orchestrator, relaying the user)")
                    return (f"Message delivered to [{ref(task.id)}]." if task.status not in TERMINAL
                            else f"Task [{ref(task.id)}] already finished ({task.status}); consider delegating a follow-up task.")
                if name == "cancel_task":
                    await task_manager.cancel(task.id, self.user_id)
                    return f"Task [{ref(task.id)}] cancelled."
                if name == "pause_task":
                    await task_manager.pause(task.id)
                    return f"Task [{ref(task.id)}] will pause after its current step."
                if name == "resume_task":
                    await task_manager.resume(task.id)
                    return f"Task [{ref(task.id)}] resumed."
                from app.models import Artifact

                async with session_scope() as db:
                    arts = (await db.execute(select(Artifact.path).where(Artifact.task_id == task.id))).scalars().all()
                return json.dumps({"task": ref(task.id), "title": task.title, "status": task.status,
                                   "result": (task.result or "")[:12000], "error": task.error, "artifacts": arts})
            if name == "get_task_status":
                if args.get("task_id"):
                    task = await self._resolve_task(str(args["task_id"]))
                    if task is None:
                        return "Error: task not found."
                    return json.dumps({"task": ref(task.id), "title": task.title, "status": task.status,
                                       "progress": task.progress, "current_step": task.current_step,
                                       "steps": task.steps, "started": iso(task.started_at),
                                       "finished": iso(task.finished_at), "error": task.error})
                async with session_scope() as db:
                    return await self._task_board(db)
            if name == "search_knowledge":
                return await self._search_knowledge(str(args.get("query", "")), int(args.get("top_k") or 6), state)
            if name == "remember":
                m = await store_memory(org_id=self.org_id, user_id=self.user_id, content=str(args.get("content", "")),
                                       kind=str(args.get("kind") or "preference"), scope=str(args.get("scope") or "user"),
                                       source=f"conversation:{self.conv_id}", importance=0.7)
                return f"Remembered ({m['kind']}, {m['scope']})."
            if name == "recall":
                ms = await search_memories(org_id=self.org_id, user_id=self.user_id, query=str(args.get("query", "")))
                return "\n".join(f"- ({m['kind']}) {m['content']}" for m in ms) or "Nothing relevant in memory."
            if name == "run_skill":
                return await self._run_skill(str(args.get("skill", "")), args.get("inputs") or {}, agents)
            if name == "schedule_task":
                return await self._schedule(args, agents)
            if name == "manage_schedule":
                return await self._manage_schedule(args)
            return f"Error: unknown tool {name}"
        except Exception as e:  # noqa: BLE001
            log.exception("orchestrator tool %s failed", name)
            return f"Error: {e}"

    async def _search_knowledge(self, query: str, top_k: int, state: dict) -> str:
        try:
            from app.rag.service import search
        except Exception:  # noqa: BLE001
            return "Error: the knowledge base is not available."
        async with session_scope() as db:
            member = (await db.execute(select(Membership).where(Membership.org_id == self.org_id,
                                                                Membership.user_id == self.user_id))).scalars().first()
            res = await search(db, org_id=self.org_id, user_id=self.user_id, role=member.role if member else "viewer",
                               query=query, top_k=max(1, min(top_k, 10)))
        results = res.get("results") or []
        if not results:
            return "No relevant documents found in the knowledge base."
        offset = len(state["citations"])
        lines = []
        for i, r in enumerate(results, start=1):
            n = offset + i
            state["citations"].append({
                "id": n, "document_id": r.get("document_id"), "document_title": r.get("document_title"),
                "section_id": r.get("section_id"), "section_path": r.get("section_path"),
                "section_title": r.get("section_title"), "breadcrumb": r.get("breadcrumb"),
                "snippet": short(r.get("content"), 300), "score": r.get("rerank_score") or r.get("score")})
            lines.append(f"[{n}] {r.get('document_title')} › {r.get('breadcrumb') or r.get('section_title') or ''} "
                         f"(§{r.get('section_path') or '-'})\n{(r.get('content') or '')[:1200]}")
        stair = res.get("stair") or {}
        sel = "; ".join(s.get("breadcrumb") or s.get("title") or "" for s in (stair.get("selected") or [])[:3])
        head = f"Structure-aware retrieval selected: {sel}\n\n" if sel else ""
        return head + "\n\n".join(lines) + "\n\nCite these as [n] in your answer. Only state what the sources support."

    async def _run_skill(self, skill_name: str, inputs: dict, agents: list[Agent]) -> str:
        async with session_scope() as db:
            rows = (await db.execute(select(Skill).where(Skill.org_id == self.org_id))).scalars().all()
        n = skill_name.strip().lower()
        skill = next((s for s in rows if n in (s.name.lower(), s.slug.lower())), None) or next(
            (s for s in rows if n and n in s.name.lower()), None)
        if skill is None:
            return "Error: skill not found. Available: " + ", ".join(s.name for s in rows[:30])
        agent = next((a for a in agents if a.id == skill.agent_id), None) or (agents[0] if agents else None)
        if agent is None:
            return "Error: no agent available to run this skill."
        snap = await start_skill_task(self.org_id, self.user_id, skill, agent, inputs, self.conv_id)
        return f"Playbook '{skill.name}' started as task [{ref(snap['id'])}] with {agent.name}."

    async def _schedules(self, db) -> str:
        from app.models import ScheduledJob
        from app.services.scheduler import ACTIVE_STATUSES, describe_schedule

        user = await db.get(User, self.user_id)
        self.user_tz = user.timezone if user is not None else None
        jobs = (await db.execute(select(ScheduledJob).where(
            ScheduledJob.org_id == self.org_id, ScheduledJob.user_id == self.user_id,
            ScheduledJob.status.in_(ACTIVE_STATUSES)).order_by(ScheduledJob.run_at).limit(12))).scalars().all()
        if not jobs:
            return "- (none)"
        lines = []
        for j in jobs:
            state = " — PAUSED" if j.status == "paused" else f" — next {iso(j.run_at)}"
            lines.append(f"- [{j.id[:8]}] \"{j.title}\" ({'task' if j.kind == 'task' else 'reminder'}) — "
                         f"{describe_schedule(j.cron, j.timezone, j.run_at)}{state} — ran {j.run_count or 0}×")
        return "\n".join(lines)

    async def _schedule(self, args: dict, agents: list[Agent]) -> str:
        from app.services.scheduler import parse_when, schedule_job, valid_timezone

        agent = self._match_agent(str(args.get("agent", "")), agents)
        if agent is None:
            return "Error: no matching agent."
        if args.get("timezone") and not valid_timezone(str(args["timezone"])):
            return f"Error: unknown time zone '{args['timezone']}' — use an IANA name like 'Asia/Kolkata'."
        tz = valid_timezone(str(args.get("timezone") or "")) or valid_timezone(self.user_tz) or "UTC"
        run_at = None
        if args.get("run_at"):
            try:
                run_at = parse_when(str(args["run_at"]), tz)
            except ValueError as e:
                return f"Error: {e}"
        cron = args.get("cron") or None
        if run_at is None and not cron:
            run_at = utcnow() + timedelta(minutes=1)
        try:
            job = await schedule_job(org_id=self.org_id, user_id=self.user_id, kind="task", title=str(args.get("title")),
                                     payload={"agent_id": agent.id, "title": args.get("title"),
                                              "instructions": args.get("instructions"), "conversation_id": self.conv_id},
                                     run_at=None if cron else run_at, cron=cron, timezone=tz)
        except ValueError as e:
            return f"Error: {e}"
        return (f"Scheduled [{job['id'][:8]}] '{job['title']}' with {agent.name} — {job['description']}; next run "
                f"{job['run_at']} (UTC). It's listed on the Schedules page.")

    async def _manage_schedule(self, args: dict) -> str:
        from app.models import ScheduledJob
        from app.services.scheduler import ACTIVE_STATUSES, apply_changes, job_out, resume_run_at, run_job_now

        key = str(args.get("schedule") or "").strip()
        action = str(args.get("action") or "")
        if not key:
            return "Error: which schedule? Pass its id or title."
        async with session_scope() as db:
            jobs = (await db.execute(select(ScheduledJob).where(
                ScheduledJob.org_id == self.org_id, ScheduledJob.user_id == self.user_id,
                ScheduledJob.status.in_(ACTIVE_STATUSES)))).scalars().all()
            match = [j for j in jobs if j.id == key or j.id.startswith(key.lower())] or \
                [j for j in jobs if j.title.strip().lower() == key.lower()]
            if len(match) != 1:
                return ("Error: no active schedule matches that." if not match
                        else "Error: several schedules match — use the id.")
            j = match[0]
            if action == "update":
                changes = {k: args[k] for k in ("title", "instructions", "cron", "run_at", "timezone") if k in args}
                if not changes:
                    return "Error: nothing to change."
                try:
                    apply_changes(j, changes)
                except ValueError as e:
                    return f"Error: {e}"
            elif action == "pause":
                j.status = "paused"
            elif action == "resume":
                if j.status == "paused":
                    j.status, j.run_at = "scheduled", resume_run_at(j)
            elif action == "cancel":
                j.status = "cancelled"
            elif action != "run_now":
                return "Error: action must be update, pause, resume, run_now or cancel."
            await db.flush()
            out = job_out(j)
        if action == "run_now":
            snap = await run_job_now(out["id"])
            return f"Started a run of '{out['title']}' now" + (f" as task [{ref(snap['id'])}]." if snap else ".")
        verb = {"update": "Updated", "pause": "Paused", "resume": "Resumed", "cancel": "Cancelled"}[action]
        tail = "" if action == "cancel" else f" — {out['description']}" + (
            "" if out["status"] == "paused" else f"; next run {out['run_at']} (UTC)")
        return f"{verb} [{out['id'][:8]}] '{out['title']}'{tail}."


async def start_skill_task(org_id: str, user_id: str, skill: Skill, agent: Agent, inputs: dict,
                           conversation_id: str | None) -> dict:
    lines = []
    for spec in skill.inputs or []:
        key = spec.get("key")
        if key in inputs and inputs[key] not in (None, ""):
            lines.append(f"- **{spec.get('label') or key}**: {inputs[key]}")
    for k, v in inputs.items():
        if not any(s.get("key") == k for s in (skill.inputs or [])):
            lines.append(f"- **{k}**: {v}")
    instructions = f"Run the '{skill.name}' playbook.\n\n## Inputs\n" + ("\n".join(lines) or "- (none provided)")
    if conversation_id:
        await post_message(org_id=org_id, conversation_id=conversation_id, role="event",
                           meta={"event": {"kind": "skill_started", "agent_id": agent.id, "agent_name": agent.name,
                                           "title": skill.name, "status": "queued"}})
    return await task_manager.create_task(org_id=org_id, agent_id=agent.id, title=skill.name, instructions=instructions,
                                          created_by=user_id, conversation_id=conversation_id, skill_id=skill.id)


class OrchestratorHub:
    def __init__(self) -> None:
        self._convs: dict[str, ConversationOrchestrator] = {}

    def reset(self) -> None:
        self._convs = {}

    def get(self, conv_id: str, org_id: str, user_id: str) -> ConversationOrchestrator:
        o = self._convs.get(conv_id)
        if o is None or o.worker.done():
            o = ConversationOrchestrator(self, conv_id, org_id, user_id)
            self._convs[conv_id] = o
        return o

    def state(self, conv_id: str) -> tuple[str, int]:
        o = self._convs.get(conv_id)
        if o is None:
            return "idle", 0
        return o.status, o.queued

    def is_busy(self, conv_id: str) -> bool:
        o = self._convs.get(conv_id)
        return bool(o and (o.busy or o.queued))

    async def user_message(self, conv_id: str, org_id: str, user_id: str, message_id: str) -> None:
        await self.get(conv_id, org_id, user_id).enqueue(Trigger("user", message_id))

    async def stop(self, conv_id: str) -> None:
        o = self._convs.get(conv_id)
        if o is not None:
            o.stop_event.set()

    async def task_finished(self, snap: dict) -> None:
        """TaskManager hook: report top-level task outcomes in their conversation."""
        if not snap.get("conversation_id") or snap.get("parent_id"):
            return
        if snap.get("status") == "cancelled":  # the chat card already shows it; no need for a report turn
            async with session_scope() as db:
                t = await db.get(Task, snap["id"])
                if t is not None:
                    t.reported = True
            return
        async with session_scope() as db:
            conv = await db.get(Conversation, snap["conversation_id"])
            if conv is None:
                return
            org_id, user_id = conv.org_id, conv.user_id
        await self.get(snap["conversation_id"], org_id, user_id).enqueue(Trigger("task", snap["id"]))

    async def recover(self) -> None:
        """After restart: answer unprocessed user messages and report unreported outcomes (last 24h)."""
        since = utcnow() - timedelta(hours=24)
        async with session_scope() as db:
            msgs = (await db.execute(select(Message).where(Message.role == "user", Message.processed.is_(False),
                                                           Message.created_at >= since))).scalars().all()
            pending = [(m.conversation_id, m.org_id, m.id) for m in msgs]
            tasks = (await db.execute(select(Task).where(Task.status.in_(tuple(TERMINAL)), Task.reported.is_(False),
                                                         Task.parent_id.is_(None), Task.conversation_id.is_not(None),
                                                         Task.finished_at >= since))).scalars().all()
            task_items = [(t.conversation_id, t.id) for t in tasks]
            owners = {}
            for cid in {c for c, _, _ in pending} | {c for c, _ in task_items}:
                conv = await db.get(Conversation, cid)
                if conv:
                    owners[cid] = (conv.org_id, conv.user_id)
            stale = (await db.execute(select(Message).where(Message.status == "streaming"))).scalars().all()
            for m in stale:
                m.status = "complete"
                m.content = (m.content or "") + "\n\n_(interrupted by a server restart)_"
        for cid, _org, mid in pending:
            if cid in owners:
                await self.get(cid, owners[cid][0], owners[cid][1]).enqueue(Trigger("user", mid))
        for cid, tid in task_items:
            if cid in owners:
                await self.get(cid, owners[cid][0], owners[cid][1]).enqueue(Trigger("task", tid))

    async def gc_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for cid, o in list(self._convs.items()):
                if not o.busy and o.queue.empty() and now - o.last_active > IDLE_GC_SECONDS:
                    o.worker.cancel()
                    self._convs.pop(cid, None)

    async def shutdown(self) -> None:
        for o in self._convs.values():
            o.worker.cancel()
        await asyncio.gather(*(o.worker for o in self._convs.values()), return_exceptions=True)


hub = OrchestratorHub()
task_manager.finished_hooks.append(hub.task_finished)


async def active_task_counts(db, org_id: str, conv_ids: list[str]) -> dict[str, int]:
    if not conv_ids:
        return {}
    rows = (await db.execute(
        select(Task.conversation_id, func.count()).where(Task.org_id == org_id, Task.conversation_id.in_(conv_ids),
                                                         Task.status.in_(tuple(ACTIVE)))
        .group_by(Task.conversation_id))).all()
    return {cid: n for cid, n in rows}
