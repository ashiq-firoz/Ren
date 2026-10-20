"""Deterministic offline LLM used for tests, CI and key-less demos.

It imitates the behaviours Ren relies on: orchestrator delegation via tool calls, worker agents
calling a tool and then summarising, STAIR constrained leaf selection, JSON outputs.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from app.llm.client import ChatResult, LLMClient, ToolCall, snap_to_choice

WORK_VERBS = (
    "research", "analy", "draft", "write", "create", "prepare", "summar", "build", "compare", "report",
    "investigate", "find", "plan", "review", "reconcile", "generate", "design", "compile", "audit", "fix",
)
AGENT_HINTS = {
    "research": ("research", "competitor", "market", "investigate", "find out"),
    "data": ("data", "spreadsheet", "csv", "excel", "chart", "analy", "sql"),
    "finance": ("finance", "budget", "revenue", "invoice", "reconcile", "forecast"),
    "writer": ("draft", "write", "email", "blog", "copy", "memo", "announcement"),
    "engineer": ("code", "bug", "deploy", "api", "repository", "script"),
    "assistant": ("meeting", "calendar", "schedule", "inbox"),
    "legal": ("contract", "legal", "compliance", "policy", "obligation"),
}


def _last(messages: list[dict], role: str) -> dict | None:
    for m in reversed(messages):
        if m.get("role") == role:
            return m
    return None


def _text(m: dict | None) -> str:
    if not m:
        return ""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


class MockLLM(LLMClient):
    def __init__(self, model: str = "mock-1", **kw: Any):
        super().__init__(kind="mock", base_url="mock://local", api_key=None, model=model, name="Mock", **kw)

    async def chat(self, messages, *, model=None, tools=None, tool_choice=None, temperature=None,
                   max_tokens=None, stop=None, response_format=None, timeout=None) -> ChatResult:
        await asyncio.sleep(0.02)
        tool_names = [t["function"]["name"] for t in (tools or [])]
        system = _text(messages[0]) if messages and messages[0].get("role") == "system" else ""
        last_user = _text(_last(messages, "user"))
        tool_results = [m for m in messages if m.get("role") == "tool"]
        last_msg = messages[-1] if messages else {}

        if response_format and response_format.get("type") in ("json_schema", "json_object"):
            schema = (response_format.get("json_schema") or {}).get("schema") or {}
            return self._result(json.dumps(self._fake_json(schema, messages)))

        # ---------------- orchestrator ----------------
        if "delegate_task" in tool_names:
            if last_msg.get("role") == "tool":
                names = [m.get("name") for m in tool_results[-3:]]
                if "delegate_task" in names:
                    return self._result(
                        "On it — I've handed this to the right specialist on the team. "
                        "I'll keep you posted here as it progresses, and you can keep chatting with me meanwhile."
                    )
                return self._result("Done. " + _text(last_msg)[:400])
            if "TASK UPDATE" in last_user or "[System notice]" in last_user:
                titles = re.findall(r"'([^']+)'", last_user)
                status = "completed" if "completed" in last_user.lower() else "updated"
                return self._result(
                    f"Update: **{titles[0] if titles else 'a task'}** has {status}. "
                    + ("Here's the gist of the result:\n\n" + last_user.split("Result:", 1)[1][:600].strip()
                       if "Result:" in last_user else "")
                )
            low = last_user.lower()
            if any(k in low for k in ("status", "progress", "how is it going", "how's it going", "update me")):
                board = system.split("## Live task board", 1)[1][:800] if "## Live task board" in system else ""
                return self._result("Here's where things stand:\n\n" + (board.strip() or "No tasks are running right now."))
            if any(v in low for v in WORK_VERBS) and len(low) > 12:
                agent = self._pick_agent(low, system)
                args = {"agent": agent, "title": last_user.strip().split("\n")[0][:80],
                        "instructions": last_user.strip(), "priority": "normal"}
                return self._result("", [ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name="delegate_task",
                                                  arguments=args, raw_arguments=json.dumps(args))])
            return self._result(
                "Hi — I'm Ren. Tell me what you need and I'll coordinate the swarm: research, analysis, documents, "
                "email drafts, knowledge-base answers and more. You can keep chatting while tasks run."
            )

        # ---------------- worker agents ----------------
        if tools and not tool_results:
            preferred = ["rag_search", "web_search", "fs_list", "memory_search"]
            name = next((p for p in preferred if p in tool_names), tool_names[0])
            args: dict[str, Any] = {}
            if name in ("rag_search", "web_search", "memory_search"):
                args = {"query": last_user[:200]}
            elif name == "fs_list":
                args = {"path": "workspace"}
            return self._result("", [ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name=name, arguments=args,
                                              raw_arguments=json.dumps(args))])
        if tool_results:
            snippet = "\n".join(f"- {(_text(m) or '')[:240]}" for m in tool_results[-3:])
            return self._result(
                "## Result\n\nI completed the task using the available tools.\n\n### Findings\n" + snippet
                + "\n\n### Next steps\n- Review the findings above\n- Ask me to go deeper on any point"
            )
        if "JSON" in system or "JSON" in last_user:
            return self._result("{}")
        return self._result(f"(mock) {last_user[:300]}")

    async def stream(self, messages, *, on_delta, model=None, tools=None, tool_choice=None, temperature=None,
                     max_tokens=None, stop_event=None, on_reasoning=None, reasoning_effort=None) -> ChatResult:
        res = await self.chat(messages, tools=tools)
        for i in range(0, len(res.content), 12):
            if stop_event is not None and stop_event.is_set():
                break
            await on_delta(res.content[i:i + 12])
            await asyncio.sleep(0.005)
        return res

    async def choose(self, messages, choices, *, model=None, multi=1, temperature=0.0, max_tokens=300):
        prompt = "\n".join(_text(m) for m in messages)
        query = ""
        mq = re.search(r"QUERY:\s*(.+)", prompt)
        if mq:
            query = mq.group(1)
        qw = _words(query or _text(_last(messages, "user")))
        scored = []
        for c in choices:
            line = next((ln for ln in prompt.splitlines() if c in ln), c)
            scored.append((len(qw & _words(line)), c))
        scored.sort(key=lambda x: -x[0])
        picked = [c for s, c in scored[:max(1, multi)] if s > 0] or [scored[0][1]]
        return [snap_to_choice(p, choices) or p for p in picked], "mock"

    async def json(self, messages, schema, *, model=None, temperature=0.0, max_tokens=1500, name="output"):
        return self._fake_json(schema, messages)

    async def embed(self, texts, model=None):
        raise NotImplementedError("mock provider has no embedding endpoint")

    async def list_models(self):
        return [{"id": "mock-1", "name": "Mock (offline)", "context_length": 32000}]

    # ------------------------------------------------------------------
    def _result(self, content: str, tool_calls: list[ToolCall] | None = None) -> ChatResult:
        return ChatResult(content=content, tool_calls=tool_calls or [], finish_reason="stop", model=self.model,
                          usage={"prompt_tokens": 50, "completion_tokens": max(1, len(content) // 4)})

    def _pick_agent(self, low: str, system: str) -> str:
        roster = re.findall(r"^- \*\*(.+?)\*\*", system, re.M)
        for key, hints in AGENT_HINTS.items():
            if any(h in low for h in hints):
                for name in roster:
                    if key in name.lower() or (key == "writer" and "writ" in name.lower()):
                        return name
        return roster[0] if roster else "Research Analyst"

    def _fake_json(self, schema: dict, messages: list[dict]) -> Any:
        text = " ".join(_text(m) for m in messages[-2:])
        heads = re.findall(r"(?:SECTION|TITLE|Section):\s*(.+)", text)

        def build(s: dict, key: str = "") -> Any:
            t = s.get("type")
            if "enum" in s:
                return s["enum"][0]
            if t == "object" or "properties" in s:
                return {k: build(v, k) for k, v in (s.get("properties") or {}).items()}
            if t == "array":
                item = s.get("items") or {"type": "string"}
                n = min(3, s.get("maxItems", 3))
                if key in ("questions",) and item.get("type") == "string":
                    topic = heads[0].strip() if heads else "this section"
                    return [f"What does {topic} cover?", f"What are the key points of {topic}?",
                            f"How does {topic} apply in practice?"][:n]
                if key in ("violations", "issues"):
                    return []
                return [build(item, key) for _ in range(n)] if item.get("type") != "object" else []
            if t == "number" or t == "integer":
                return 0
            if t == "boolean":
                return key in ("compliant", "grounded", "ok")
            if key in ("title",):
                return heads[0][:60] if heads else "Section"
            if key in ("summary",):
                return text[:160]
            return ""

        return build(schema)
