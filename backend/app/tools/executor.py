"""Tool resolution and governed execution.

Every agent tool call passes through `execute()`:
  validate args → role/scope permission → policy pre-check of outbound content → risk-based approval gate
  → run with timeout → audit → compact text result for the model.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.models import ROLE_ORDER, Connector, ToolGroupSetting
from app.tools.base import RISK_ORDER, ToolContext, ToolError, ToolResult, ToolSpec, registry, result_to_text

log = logging.getLogger("ren.executor")


@dataclass
class ExecResult:
    text: str
    ok: bool = True
    display: str | None = None
    citations: list[dict] | None = None
    artifacts: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    risk: str = "low"
    approval: str | None = None  # approved | rejected | expired | None


async def disabled_groups(db, org_id: str) -> set[str]:
    rows = (await db.execute(select(ToolGroupSetting).where(ToolGroupSetting.org_id == org_id))).scalars().all()
    disabled = {r.group_id for r in rows if not r.enabled}
    enabled_explicit = {r.group_id for r in rows if r.enabled}
    for g in registry.groups.values():  # groups that are off by default unless an admin enabled them
        if not g.default_enabled and g.id not in enabled_explicit:
            disabled.add(g.id)
    return disabled


async def connected_kinds(db, org_id: str) -> set[str]:
    rows = (await db.execute(select(Connector.kind).where(Connector.org_id == org_id))).scalars().all()
    return set(rows)


def scope_allowed(spec: ToolSpec, role: str) -> bool:
    if spec.scope == "read":
        return True
    if spec.scope == "write":
        return ROLE_ORDER.get(role, 0) >= ROLE_ORDER["member"]
    return ROLE_ORDER.get(role, 0) >= ROLE_ORDER["admin"]


async def resolve_tools(db, org_id: str, group_ids: list[str], role: str) -> dict[str, ToolSpec]:
    """Tools an agent may use for this user, honoring org enablement, connectors, availability and role."""
    disabled = await disabled_groups(db, org_id)
    kinds = await connected_kinds(db, org_id)
    out: dict[str, ToolSpec] = {}
    for gid in dict.fromkeys(group_ids or []):
        if gid in disabled:
            continue
        if gid.startswith("mcp:"):
            from app.tools.mcp_client import mcp_manager

            for spec in await mcp_manager.tool_specs(org_id, gid.split(":", 1)[1]):
                if scope_allowed(spec, role):
                    out[spec.name] = spec
            continue
        group = registry.group(gid)
        if group is None:
            continue
        ok, _ = group.availability()
        if not ok:
            continue
        if group.connector_kinds and not group.connector_optional and not (set(group.connector_kinds) & kinds):
            continue
        for spec in group.tools:
            if scope_allowed(spec, role):
                out[spec.name] = spec
    return out


def approval_needed(risk: str, autonomy: str) -> bool:
    from app.agents.approvals import APPROVAL_THRESHOLD

    threshold = APPROVAL_THRESHOLD.get(autonomy or "balanced", "high")
    return RISK_ORDER.get(risk, 0) >= RISK_ORDER[threshold]


def describe_call(spec: ToolSpec | None, name: str, args: dict) -> str:
    """Human-readable one-liner for UI status lines ("Searching the web: 'acme pricing'")."""
    label = spec.display_name if spec else name
    key = next((k for k in ("query", "q", "path", "url", "title", "command", "to", "name", "agent") if k in (args or {})), None)
    if key:
        val = args[key]
        if isinstance(val, list):
            val = ", ".join(map(str, val[:3]))
        sval = str(val)
        return f"{label}: {sval[:80]}{'…' if len(sval) > 80 else ''}"
    return label


async def execute(
    ctx: ToolContext,
    spec: ToolSpec | None,
    name: str,
    args: dict,
    *,
    autonomy: str = "balanced",
    call_id: str | None = None,
    on_event: Any = None,
) -> ExecResult:
    t0 = time.perf_counter()
    if spec is None:
        return ExecResult(f"Error: unknown tool '{name}'. Use only the tools you were given.", ok=False)
    try:
        args = spec.validate(args or {})
    except ToolError as e:
        return ExecResult(f"Error: {e}", ok=False)

    if not scope_allowed(spec, ctx.role):
        return ExecResult(f"Error: your user role ({ctx.role}) does not allow '{spec.display_name}'.", ok=False)

    risk = spec.effective_risk(args).value
    approval_reason = None

    # Outbound content is policy-checked before it can leave the system.
    if spec.content_fields:
        content = "\n\n".join(str(args.get(f) or "") for f in spec.content_fields if args.get(f))
        if content.strip():
            from app.policy.guard import check_text, violation_feedback

            verdict = await check_text(org_id=ctx.org_id, text=content, agent_id=ctx.agent_id, kind="outbound",
                                       task_id=ctx.task_id)
            if on_event is not None:
                await on_event("policy", {"verdict": verdict["verdict"], "violations": verdict["violations"],
                                          "action": "blocked" if verdict["verdict"] == "block" else
                                          ("warned" if verdict["verdict"] == "warn" else "passed"),
                                          "tool": spec.display_name})
            if verdict["verdict"] == "block":
                return ExecResult(
                    "Blocked by workspace policy before sending. Violations:\n" + violation_feedback(verdict)
                    + "\nRevise the content so it complies, then try again.", ok=False, risk=risk)
            if verdict["verdict"] == "warn":
                approval_reason = "Policy warning on outbound content: " + "; ".join(
                    v["policy_name"] for v in verdict["violations"])

    decision_status = None
    if approval_needed(risk, autonomy) or approval_reason:
        from app.agents.approvals import broker

        reason = approval_reason or f"{risk.capitalize()}-risk action requires approval under '{autonomy}' autonomy."
        decision = await broker.request(ctx=ctx, tool=spec.name, tool_display=spec.display_name, args=args, risk=risk,
                                        reason=reason, preview=spec.render_preview(args), call_id=call_id)
        decision_status = decision["status"]
        if decision_status != "approved":
            note = f" Reviewer comment: {decision.get('comment')}" if decision.get("comment") else ""
            return ExecResult(f"The user did not approve '{spec.display_name}' (status: {decision_status}).{note} "
                              "Do not retry the same action unless the user asks; continue or explain.",
                              ok=False, risk=risk, approval=decision_status)
        if decision.get("modified_args"):
            try:
                args = spec.validate({**args, **decision["modified_args"]})
            except ToolError as e:
                return ExecResult(f"Error: approved arguments are invalid: {e}", ok=False, risk=risk)

    from app.services.audit import audit

    try:
        value = await asyncio.wait_for(spec.handler(ctx, **args), timeout=spec.timeout)
        ok = True
    except ToolError as e:
        value, ok = f"Error: {e}", False
    except asyncio.TimeoutError:
        value, ok = f"Error: '{spec.display_name}' timed out after {int(spec.timeout)}s.", False
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s crashed", spec.name)
        value, ok = f"Error: '{spec.display_name}' failed: {e.__class__.__name__}: {str(e)[:300]}", False

    duration = int((time.perf_counter() - t0) * 1000)
    res = ExecResult(text=result_to_text(value), ok=ok, duration_ms=duration, risk=risk, approval=decision_status)
    if isinstance(value, ToolResult):
        res.ok = ok and value.ok
        res.display = value.display
        res.citations = value.citations
        res.artifacts = value.artifacts
    if RISK_ORDER.get(risk, 0) >= RISK_ORDER["medium"] or not ok:
        await audit(ctx.org_id, f"tool.{spec.name}", user_id=ctx.user_id, agent_id=ctx.agent_id, task_id=ctx.task_id,
                    target_type="tool", target_id=spec.name, risk=risk,
                    details={"args": args, "ok": res.ok, "duration_ms": duration, "approval": decision_status,
                             "result": res.text[:500]})
    return res
