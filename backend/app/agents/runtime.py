"""Worker agent loop: executes one Task with tool calling until done.

Durable by design: the message history and any in-flight tool calls are checkpointed after every step, so a task
survives restarts (and re-attaches to pending approvals). Between steps the runner honours pause/cancel and reads
its inbox (messages from the user/orchestrator delivered mid-task).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.agents.common import TERMINAL, emit_task, short
from app.agents.prompts import WORKER_PROTOCOL, format_policies, today
from app.core.db import session_scope, utcnow
from app.core.events import bus
from app.llm.client import LLMError, ToolCall
from app.llm.providers import get_llm
from app.models import Agent, Membership, Organization, Skill, Task, TaskEvent, User
from app.policy.guard import applicable_policies, check_text, public_summary, violation_feedback
from app.services.serialize import task_event_out
from app.services.usage import record_usage
from app.tools.base import ToolContext
from app.tools.disclosure import FIND_LOAD_TOOLS, catalog_text, find_tools, initial_tools, load_tools
from app.tools.executor import describe_call, execute, resolve_tools

if TYPE_CHECKING:
    from app.agents.task_manager import TaskManager

log = logging.getLogger("ren.runtime")

CONTEXT_CHAR_BUDGET = 120_000  # ≈30k tokens of history per step
KEEP_RECENT = 6
MAX_REVISIONS = 2

RUNTIME_TOOLS = [
    {"type": "function", "function": {
        "name": "report_progress",
        "description": "Report a meaningful milestone to the user (shown live in the task view).",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "What was just accomplished / what comes next (one sentence)."},
            "percent": {"type": "integer", "description": "Estimated overall completion 0-100."}},
            "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": ("Ask the user one concise question when you are blocked on information only they have. "
                        "The question appears in their chat; you wait up to `wait_minutes` for a reply."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "wait_minutes": {"type": "integer", "description": "How long to wait for the answer (default 15)."}},
            "required": ["question"]}}},
]


class TaskCancelled(Exception):
    pass


class AgentRunner:
    def __init__(self, manager: TaskManager, task_id: str):
        self.manager = manager
        self.task_id = task_id
        self.org_id = ""
        self.owner: str | None = None
        self.progress_reported = False
        self.last_progress = 0
        self.granted: dict = {}
        self.active: set[str] = set()
        self.base_system = ""

    # ------------------------------------------------------------------ events
    async def event(self, kind: str, content: dict) -> None:
        async with session_scope() as db:
            ev = TaskEvent(task_id=self.task_id, kind=kind, content=content)
            db.add(ev)
            await db.flush()
            out = task_event_out(ev)
        await bus.emit(self.org_id, "task.event", {"task_id": self.task_id, "event": out}, user_id=self.owner,
                       admin_visible=True)

    async def update(self, emit: bool = True, **fields: Any) -> None:
        async with session_scope() as db:
            task = await db.get(Task, self.task_id)
            if task is None:
                return
            for k, v in fields.items():
                setattr(task, k, v)
        if emit:
            await emit_task(self.task_id)

    async def checkpoint(self, messages: list[dict], pending: list[dict] | None, extra: dict | None = None) -> None:
        async with session_scope() as db:
            task = await db.get(Task, self.task_id)
            if task is None:
                return
            cp = dict(task.checkpoint or {})
            cp["messages"] = messages
            cp["pending_tool_calls"] = pending or []
            cp["active_tools"] = sorted(self.active)
            if extra:
                cp.update(extra)
            task.checkpoint = cp

    # ------------------------------------------------------------------ main
    async def run(self) -> None:
        async with session_scope() as db:
            task = await db.get(Task, self.task_id)
            if task is None or task.status in TERMINAL:
                return
            self.org_id, self.owner = task.org_id, task.created_by
            agent = await db.get(Agent, task.agent_id)
            org = await db.get(Organization, task.org_id)
            if agent is None or org is None:
                task.status, task.error, task.finished_at = "failed", "Agent no longer exists", utcnow()
                return
            user = await db.get(User, task.created_by) if task.created_by else None
            role = "member"
            if user is not None:
                m = (await db.execute(select(Membership).where(Membership.org_id == org.id,
                                                               Membership.user_id == user.id))).scalars().first()
                role = m.role if m else "viewer"
            skill = await db.get(Skill, task.skill_id) if task.skill_id else None
            groups = list(agent.tool_groups or []) + (list(skill.tool_groups or []) if skill else [])
            tools = await resolve_tools(db, org.id, groups, role)
            policies = await applicable_policies(db, org.id, agent.id, "response")
            llm = await get_llm(db, org.id, agent=agent)
            cp = dict(task.checkpoint or {})
            first_run = not cp.get("messages")
            task.status = "running"
            task.started_at = task.started_at or utcnow()
            if first_run:
                task.current_step = "Planning"
            agent_snapshot = {"id": agent.id, "name": agent.name, "role": agent.role, "autonomy": agent.autonomy,
                              "max_steps": agent.max_steps, "temperature": agent.temperature,
                              "system_prompt": agent.system_prompt}
            ctx = ToolContext(org_id=org.id, user_id=task.created_by, role=role, agent_id=agent.id,
                              agent_name=agent.name, task_id=task.id, conversation_id=task.conversation_id,
                              org_mode=org.mode, collection_ids=list(agent.collection_ids or []) or None,
                              depth=task.depth)
            title, instructions = task.title, task.instructions
            steps_done = task.steps
            tokens_used = task.tokens
            self.granted = tools
            restored = set(cp.get("active_tools") or []) & set(tools)
            self.active = restored or initial_tools(tools)
            self.base_system = self._system_prompt(agent_snapshot, org, user, role, policies)
            system = self._system_with_catalog()
            if first_run:
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": self._task_brief(title, instructions, skill)}]
            else:
                messages = cp["messages"]
                messages[0] = {"role": "system", "content": system}
            pending = cp.get("pending_tool_calls") or []
            revisions = int(cp.get("revisions") or 0)

        ctx.extra["progress_cb"] = self._progress_cb
        await emit_task(self.task_id)
        await self.event("status", {"status": "running", "note": "Started" if first_run else "Resumed"})

        max_steps = max(1, int(agent_snapshot["max_steps"] or 24))
        step = steps_done

        try:
            while True:
                await self.manager.gate(self.task_id)
                inbox = await self._drain_inbox()
                for item in inbox:
                    messages.append({"role": "user", "content": f"[New message from {item['from_label']}]: {item['text']}"})

                if pending:
                    calls = [ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments") or {}) for c in pending]
                    results = await self._run_tools(ctx, tools, calls, agent_snapshot["autonomy"])
                    messages.extend(results)
                    pending = []
                    step += 1
                    self.last_progress = max(self.last_progress, self._estimate(step, max_steps))
                    await self.update(steps=step, progress=self.last_progress, emit=True)
                    await self.checkpoint(messages, [], {"revisions": revisions})
                    continue

                if step >= max_steps:
                    final = await self._wrap_up(llm, messages, agent_snapshot)
                    await self._finish(ctx, final, messages, policies, llm, title, instructions, agent_snapshot,
                                       revisions, truncated=True)
                    return

                messages = _compact(messages)
                messages[0] = {"role": "system", "content": self._system_with_catalog()}
                try:
                    res = await llm.chat(messages, tools=self._schemas(), temperature=agent_snapshot["temperature"])
                except LLMError as e:
                    raise RuntimeError(f"Model error: {e}") from e
                tokens_used += await record_usage(self.org_id, res.model, res.usage, purpose="agent",
                                                  agent_id=agent_snapshot["id"], task_id=self.task_id)
                await self.update(emit=False, tokens=tokens_used)

                if res.tool_calls:
                    if res.content and res.content.strip():
                        await self.event("thought", {"text": res.content.strip()[:4000]})
                    messages.append(res.assistant_message())
                    pending = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in res.tool_calls]
                    await self.checkpoint(messages, pending, {"revisions": revisions})
                    continue

                final = (res.content or "").strip()
                if not final:
                    messages.append({"role": "user", "content": "Please provide your final answer now (no tool calls)."})
                    step += 1
                    continue
                messages.append({"role": "assistant", "content": final})
                done = await self._finish(ctx, final, messages, policies, llm, title, instructions, agent_snapshot,
                                          revisions)
                if done:
                    return
                revisions += 1
                step += 1
        except TaskCancelled:
            return
        except asyncio.CancelledError:
            if self.manager.cancel_requested(self.task_id):
                return
            raise  # shutdown: leave status as-is for recovery
        except Exception as e:  # noqa: BLE001
            log.exception("task %s failed", self.task_id)
            await self.update(status="failed", error=str(e)[:2000], finished_at=utcnow(),
                              current_step="Failed")
            await self.event("error", {"message": str(e)[:2000]})
            await self.event("status", {"status": "failed"})

    # ------------------------------------------------------------------ pieces
    def _system_prompt(self, agent: dict, org: Organization, user: User | None, role: str, policies: list) -> str:
        protocol = WORKER_PROTOCOL.format(
            agent_name=agent["name"], agent_role=agent["role"], org_name=org.name, policies=format_policies(policies),
            today=today(user.timezone if user else None), mode=org.mode, user_name=user.name if user else "the team",
            user_role=role)
        return (agent["system_prompt"] or f"You are {agent['name']}, {agent['role']}.").strip() + "\n\n" + protocol

    def _system_with_catalog(self) -> str:
        if len(self.active) >= len(self.granted):
            return self.base_system
        return (self.base_system + "\n\n# Your tool catalog\nLoaded tools are callable now. To use any other tool listed "
                "under 'Also available', call `load_tools` with its full name (group prefix + name, e.g. sheet_pivot) — "
                "or `find_tools` to search by keyword.\n" + catalog_text(self.granted, self.active))

    def _schemas(self) -> list[dict]:
        out = [self.granted[n].openai_schema() for n in sorted(self.active) if n in self.granted] + RUNTIME_TOOLS
        if len(self.active) < len(self.granted):
            out += FIND_LOAD_TOOLS
        return out

    @staticmethod
    def _task_brief(title: str, instructions: str, skill: Skill | None) -> str:
        brief = f"# Task: {title}\n\n{instructions}".strip()
        if skill is not None:
            brief += f"\n\n# Playbook: {skill.name}\nFollow this playbook:\n\n{skill.instructions}"
        return brief

    @staticmethod
    def _estimate(step: int, max_steps: int) -> int:
        return max(5, min(92, int(100 * (1 - 0.82 ** (step + 1)))))

    async def _progress_cb(self, note: str, progress: int | None) -> None:
        fields: dict[str, Any] = {"current_step": short(note, 200)}
        if progress is not None:
            self.last_progress = max(self.last_progress, max(0, min(99, int(progress))))
            fields["progress"] = self.last_progress
            self.progress_reported = True
        await self.update(**fields)
        await self.event("progress", {"progress": fields.get("progress"), "note": note})

    async def _drain_inbox(self) -> list[dict]:
        async with session_scope() as db:
            rows = (await db.execute(
                select(TaskEvent).where(TaskEvent.task_id == self.task_id, TaskEvent.kind == "inbox",
                                        TaskEvent.consumed.is_(False)).order_by(TaskEvent.created_at)
            )).scalars().all()
            out = []
            for ev in rows:
                ev.consumed = True
                c = ev.content or {}
                label = c.get("sender_name") or {"user": "the user", "orchestrator": "Ren (orchestrator)"}.get(
                    c.get("from"), c.get("from", "someone"))
                out.append({"text": c.get("text", ""), "from_label": label})
        self.manager.inbox_event(self.task_id).clear()
        return out

    async def _run_tools(self, ctx: ToolContext, tools: dict, calls: list[ToolCall], autonomy: str) -> list[dict]:
        async def one(tc: ToolCall) -> dict:
            if tc.name == "report_progress":
                note = str(tc.arguments.get("note") or "")
                pct = tc.arguments.get("percent")
                await self._progress_cb(note, int(pct) if isinstance(pct, (int, float)) else None)
                return {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": "Progress reported."}
            if tc.name == "ask_user":
                answer = await self._ask_user(ctx, str(tc.arguments.get("question") or ""),
                                              int(tc.arguments.get("wait_minutes") or 15))
                return {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": answer}
            if tc.name == "find_tools":
                found = find_tools(self.granted, self.active, str(tc.arguments.get("query") or ""),
                                   tc.arguments.get("group") or None)
                text = ("\n".join(f"- {f['name']} ({f['group']}, {f['risk']} risk): {f['description']}" for f in found)
                        or "No matching unloaded tools. Check the catalog in your instructions.")
                return {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": text}
            if tc.name == "load_tools":
                names = tc.arguments.get("names") or []
                loaded, unknown = load_tools(self.granted, self.active, names if isinstance(names, list) else [names])
                await self.event("thought", {"text": f"Loaded tools: {', '.join(loaded) or 'none'}"})
                msg = f"Loaded: {', '.join(loaded) or 'none'}. They are callable from your next step."
                if unknown:
                    msg += f" Not granted to you: {', '.join(unknown)}."
                return {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": msg}
            if tc.name in self.granted and tc.name not in self.active:
                self.active.add(tc.name)  # called by name from the catalog: load it on the fly
            spec = tools.get(tc.name)
            display = describe_call(spec, tc.name, tc.arguments)
            await self.update(current_step=display)
            await self.event("tool_call", {"call_id": tc.id, "tool": tc.name,
                                           "display": spec.display_name if spec else tc.name,
                                           "args": _clip_args(tc.arguments),
                                           "risk": spec.effective_risk(tc.arguments).value if spec else "low"})

            async def on_event(kind: str, content: dict) -> None:
                await self.event(kind, content)

            res = await execute(ctx, spec, tc.name, tc.arguments, autonomy=autonomy, call_id=tc.id, on_event=on_event)
            await self.event("tool_result", {"call_id": tc.id, "tool": tc.name, "ok": res.ok,
                                             "preview": res.text[:1500], "duration_ms": res.duration_ms})
            return {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": res.text}

        return list(await asyncio.gather(*(one(c) for c in calls)))

    async def _ask_user(self, ctx: ToolContext, question: str, wait_minutes: int) -> str:
        from app.agents.common import post_event

        await post_event(ctx.org_id, ctx.conversation_id, {
            "kind": "info", "task_id": self.task_id, "agent_id": ctx.agent_id, "agent_name": ctx.agent_name,
            "title": f"{ctx.agent_name} has a question", "summary": question, "status": "question",
        }, content=question, task_id=self.task_id, agent_id=ctx.agent_id)
        if ctx.user_id:
            from app.services.notifications import notify_user

            await notify_user(ctx.org_id, ctx.user_id, "question", f"{ctx.agent_name} has a question", question,
                              f"/tasks/{self.task_id}")
        await self.update(current_step=f"Waiting for your reply: {short(question, 120)}")
        await self.event("message", {"text": f"Question for the user: {question}"})
        ev = self.manager.inbox_event(self.task_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(1, wait_minutes) * 60)
        except asyncio.TimeoutError:
            return ("No reply from the user yet. Proceed with your best reasonable assumption, state it clearly in "
                    "the result, and continue.")
        msgs = await self._drain_inbox()
        if not msgs:
            return "No reply yet; proceed with a reasonable assumption."
        return "User reply: " + "\n".join(m["text"] for m in msgs)

    async def _wrap_up(self, llm, messages: list[dict], agent: dict) -> str:
        msgs = _compact(messages) + [{"role": "user", "content": (
            "You have reached the step limit for this task. Without calling any tools, write the final result now: "
            "summarise what you accomplished, the findings so far, artifact paths, and what remains to be done.")}]
        try:
            res = await llm.chat(msgs, temperature=agent["temperature"])
            await record_usage(self.org_id, res.model, res.usage, purpose="agent", agent_id=agent["id"],
                               task_id=self.task_id)
            return (res.content or "").strip() or "Stopped at the step limit."
        except LLMError as e:
            return f"Stopped at the step limit (summary unavailable: {e})."

    async def _finish(self, ctx: ToolContext, final: str, messages: list[dict], policies: list, llm, title: str,
                      instructions: str, agent: dict, revisions: int, truncated: bool = False) -> bool:
        """Policy-check the final answer. Returns True when the task is completed, False to let the agent revise."""
        verdict = await check_text(org_id=self.org_id, text=final, user_request=f"{title}\n{instructions}",
                                   agent_id=agent["id"], kind="response", task_id=self.task_id)
        action = "passed"
        if verdict["verdict"] == "block":
            if revisions < MAX_REVISIONS and not truncated:
                await self.event("policy", {"verdict": "block", "violations": verdict["violations"], "action": "revised"})
                messages.append({"role": "user", "content": (
                    "[Policy guard] Your final answer violates workspace policy:\n" + violation_feedback(verdict)
                    + "\nRevise it so it fully complies (you may use tools if needed), then give the final answer again.")})
                await self.checkpoint(messages, [], {"revisions": revisions + 1})
                return False
            action = "blocked"
            final = ("⚠️ The result was withheld because it conflicts with workspace policy:\n"
                     + violation_feedback(verdict) + "\n\nPlease rephrase the request or ask an admin to review the policy.")
        elif verdict["verdict"] == "warn":
            action = "warned"
        await self.event("policy", {"verdict": verdict["verdict"], "violations": verdict["violations"], "action": action})
        if truncated:
            final += "\n\n> _Stopped at the step limit — ask me to continue if more work is needed._"
        await self.event("message", {"text": final[:8000]})
        await self.checkpoint(messages, [], {"alignment": public_summary(verdict), "revisions": revisions})
        await self.update(status="completed", result=final, progress=100, finished_at=utcnow(),
                          current_step="Completed", error=None)
        await self.event("status", {"status": "completed"})
        return True


def _clip_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 1200:
            out[k] = v[:1200] + "…"
        else:
            out[k] = v
    return out


def _compact(messages: list[dict]) -> list[dict]:
    """Keep the context within budget: shrink old tool outputs, then drop the middle if needed."""
    def size(ms: list[dict]) -> int:
        return sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or "")) for m in ms)

    if size(messages) <= CONTEXT_CHAR_BUDGET:
        return messages
    ms = [dict(m) for m in messages]
    cutoff = max(2, len(ms) - KEEP_RECENT)
    for m in ms[2:cutoff]:
        if m.get("role") == "tool" and len(m.get("content") or "") > 1200:
            m["content"] = m["content"][:1200] + "\n…[older tool output truncated]"
    if size(ms) <= CONTEXT_CHAR_BUDGET:
        return ms
    # drop whole old turns, keeping assistant/tool pairs consistent
    head, tail = ms[:2], ms[2:]
    while tail and size(head + tail) > CONTEXT_CHAR_BUDGET and len(tail) > KEEP_RECENT:
        tail.pop(0)
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
    return head + [{"role": "user", "content": "[Earlier steps were omitted to save space.]"}] + tail
