"""Policy guard: checks agent output against workspace + agent policies.

Pipeline (fast → slow):
  1. Rules     — regex patterns and built-in detectors (@pii:email, @secret:api_key, ...). Instant, deterministic.
  2. Laya      — every atomic policy rule becomes one typed `choice` question; all rules × all text segments are
                 answered in one batched forward pass with calibrated violation probabilities.
  3. Judge     — an LLM reviewer, only for rules Laya is *uncertain* about (warn ≤ p < block), or for everything when
                 Laya isn't deployed ("System 1 → System 2" escalation).
Verdict: block (≥ block_threshold on a block-severity policy) / warn / pass. Every check is recorded.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import session_scope
from app.models import Agent, AlignmentCheck, Organization, Policy
from app.policy.laya_client import LayaUnavailable, laya, violation_probability
from app.services.serialize import org_settings

log = logging.getLogger("ren.policy")

MACROS: dict[str, str] = {
    "@pii:email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "@pii:phone": r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)",
    "@pii:credit_card": r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)",
    "@pii:ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "@pii:iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    "@secret:api_key": r"(?:sk-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,})",
    "@secret:private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "@secret:password": r"(?i)password\s*[:=]\s*\S{6,}",
}

SEGMENT_CHARS = 1100  # ≈ 250–300 tokens: fits Laya's English state budget (~320 tokens)
MAX_SEGMENTS = 12


@dataclass
class RuleEval:
    policy_id: str
    policy_name: str
    rule: str | None
    severity: str
    probability: float
    checker: str
    explanation: str | None = None


def _segments(text: str) -> list[str]:
    text = (text or "").strip()
    if len(text) <= SEGMENT_CHARS:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    segs, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > SEGMENT_CHARS and cur:
            segs.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        segs.append(cur)
    if len(segs) > MAX_SEGMENTS:  # keep the head and tail plus an even sample of the middle
        step = len(segs) / MAX_SEGMENTS
        segs = [segs[int(i * step)] for i in range(MAX_SEGMENTS)]
    return segs


_TURN_RE = re.compile(r"^\s*(user|human|customer|agent|assistant|ai|bot)\s*:\s*", re.I | re.M)
_AGENT_ROLES = {"agent", "assistant", "ai", "bot"}


def transcript_turns(text: str) -> list[tuple[str, str]]:
    """Split a "User: … / Agent: …" transcript into (role, text) turns; role is "user" or "agent"."""
    marks = list(_TURN_RE.finditer(text or ""))
    turns = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            turns.append(("agent" if m.group(1).lower() in _AGENT_ROLES else "user", body))
    return turns


def transcript_states(text: str) -> list[dict]:
    """Laya states for a conversation: each agent turn (segmented) with the user turn before it as the request."""
    states, last_user = [], ""
    for role, body in transcript_turns(text):
        if role == "user":
            last_user = body
            continue
        states.extend({"user_request": last_user[:400], "agent_response": seg} for seg in _segments(body))
    if not states:
        return [{"user_request": "", "agent_response": seg} for seg in _segments(text)]
    if len(states) > MAX_SEGMENTS:  # keep the head and tail plus an even sample of the middle
        step = len(states) / MAX_SEGMENTS
        states = [states[int(i * step)] for i in range(MAX_SEGMENTS)]
    return states


def _compile(pattern: str) -> re.Pattern | None:
    pat = MACROS.get(pattern.strip(), pattern)
    try:
        return re.compile(pat, re.I | re.M)
    except re.error:
        return None


async def applicable_policies(db, org_id: str, agent_id: str | None, kind: str,
                              policy_ids: list[str] | None = None) -> list[Policy]:
    rows = (await db.execute(select(Policy).where(Policy.org_id == org_id, Policy.enabled.is_(True)))).scalars().all()
    if policy_ids:
        return [p for p in rows if p.id in set(policy_ids)]
    agent_policy_ids: set[str] = set()
    if agent_id:
        agent = await db.get(Agent, agent_id)
        if agent:
            agent_policy_ids = set(agent.policy_ids or [])
    want = "outbound" if kind == "outbound" else "responses"
    out = []
    for p in rows:
        if kind != "test" and p.applies_to not in ("all", want):
            continue
        if p.scope == "org" or p.id in agent_policy_ids or (agent_id and agent_id in (p.agent_ids or [])):
            out.append(p)
    return out


def _rules_layer(policies: list[Policy], text: str) -> list[RuleEval]:
    evals = []
    for p in policies:
        for pat in p.patterns or []:
            rx = _compile(pat)
            if rx is None:
                continue
            m = rx.search(text)
            if m:
                evals.append(RuleEval(p.id, p.name, f"pattern {pat}", p.severity, 1.0, "rules",
                                      f"Matched pattern '{pat}': “{m.group(0)[:60]}”"))
    return evals


async def _laya_layer(policies: list[Policy], text: str, user_request: str | None,
                      states: list[dict] | None = None) -> list[RuleEval]:
    questions: dict[str, dict] = {}
    index: dict[str, tuple[Policy, str]] = {}
    # Two phrasings per rule, averaged: a small ensemble that measurably improves zero-shot separation.
    # Option keys stay neutral (A/B) and "B" always means "breaks the rule" (Laya's guidance for yes/no checks).
    for p in policies:
        rules = [r for r in (p.rules or []) if r and r.strip()] or ([p.description] if p.description else [])
        for r in rules:
            rule = r.strip()[:400]
            for variant, q in enumerate(_rule_questions(rule)):
                qid = f"q{len(questions)}"
                questions[qid] = q
                index[qid] = (p, r.strip(), variant)
    if not questions:
        return []
    if states is None:
        states = [{"user_request": (user_request or "")[:400], "agent_response": s} for s in _segments(text)]
    if not states:
        return []
    # per (policy, rule): max over segments of the mean over phrasings
    per_segment: dict[tuple[str, str], list[list[float]]] = {}
    qitems = list(questions.items())
    for i in range(0, len(qitems), 24):  # keep each call's option budget reasonable
        qbatch = dict(qitems[i:i + 24])
        answers_per_state = await laya.predict_batch(states, qbatch)
        for s_idx, answers in enumerate(answers_per_state):
            for qid in qbatch:
                p, rule, _variant = index[qid]
                rows = per_segment.setdefault((p.id, rule), [[] for _ in states])
                rows[s_idx].append(violation_probability(answers.get(qid, {}), "B"))
    by_id = {p.id: p for p in policies}
    evals = []
    for (pid, rule), rows in per_segment.items():
        prob = max((sum(v) / len(v) for v in rows if v), default=0.0)
        p = by_id[pid]
        evals.append(RuleEval(p.id, p.name, rule, p.severity, prob, "laya"))
    return evals


def _rule_questions(rule: str) -> list[dict]:
    return [
        {"type": "choice", "instructions": f"Rule: {rule}\nIs the text compliant with the rule?",
         "criteria": {"A": "compliant: the text follows the rule", "B": "violation: the text breaks the rule"}},
        {"type": "choice", "instructions": f"Which description fits the text better with respect to this rule: {rule}",
         "criteria": {"A": "follows the rule", "B": "breaks the rule"}},
    ]


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "policy_index": {"type": "integer"},
                    "rule": {"type": "string"},
                    "probability": {"type": "number"},
                    "explanation": {"type": "string"},
                },
                "required": ["policy_index", "probability", "explanation"],
            },
        }
    },
    "required": ["violations"],
}


async def _judge_layer(org_id: str, policies: list[Policy], text: str, user_request: str | None,
                       transcript: bool = False) -> list[RuleEval]:
    from app.llm.providers import get_llm
    from app.services.usage import record_usage

    lines = []
    for i, p in enumerate(policies):
        rules = [r.strip() for r in (p.rules or []) if r and r.strip()]
        if len(rules) > 1:
            lines.append(f"[{i}] {p.name} ({p.severity}):\n" + "\n".join(f"   {n}. {r}" for n, r in enumerate(rules, 1)))
        else:
            lines.append(f"[{i}] {p.name} ({p.severity}): {rules[0] if rules else p.description}")
    if transcript:
        task = ("Decide whether any AGENT turn in the CONVERSATION violates any of the numbered workspace policies. "
                "User turns are context only — never flag what the user says. Rules that apply 'in subsequent "
                "responses' or 'when the user mentions X' depend on the earlier turns, so read the whole conversation.")
        body = (f"CONVERSATION:\n{text[:24000]}" if not user_request
                else f"CONTEXT:\n{user_request[:1500]}\n\nCONVERSATION:\n{text[:24000]}")
    else:
        task = "Decide whether the AGENT RESPONSE violates any of the numbered workspace policies."
        body = f"USER REQUEST:\n{(user_request or '(none)')[:1500]}\n\nAGENT RESPONSE:\n{text[:8000]}"
    messages = [
        {"role": "system", "content": (
            f"You are a precise compliance reviewer for an enterprise AI assistant. {task} Be strict but fair: only "
            "flag real violations, not hypothetical ones. For each violation give the policy index, the specific "
            "rule, a probability 0-1 that it is a real violation, and a one-sentence explanation. Return an empty "
            "list if compliant.")},
        {"role": "user", "content": "POLICIES:\n" + "\n".join(lines) + f"\n\n{body}"},
    ]
    async with session_scope() as db:
        llm = await get_llm(db, org_id, purpose="alignment")
    data = await llm.json(messages, JUDGE_SCHEMA, max_tokens=800, name="policy_review")
    await record_usage(org_id, llm.model, {"prompt_tokens": len(str(messages)) // 4, "completion_tokens": 200},
                       purpose="alignment")
    evals = []
    for v in (data or {}).get("violations", []) if isinstance(data, dict) else []:
        try:
            p = policies[int(v.get("policy_index"))]
        except (TypeError, ValueError, IndexError):
            continue
        evals.append(RuleEval(p.id, p.name, v.get("rule") or None, p.severity,
                              max(0.0, min(1.0, float(v.get("probability") or 0.8))), "judge", v.get("explanation")))
    return evals


async def check_text(*, org_id: str, text: str, user_request: str | None = None, agent_id: str | None = None,
                     kind: str = "response", task_id: str | None = None, message_id: str | None = None,
                     policy_ids: list[str] | None = None, record: bool = True, transcript: bool = False) -> dict:
    """Return an AlignmentSummary dict (+ per_rule details). Never raises.

    transcript=True audits a whole "User: … / Agent: …" conversation: only agent turns are judged, with the earlier
    turns as context (rules like "in subsequent responses…" need them)."""
    t0 = time.perf_counter()
    try:
        async with session_scope() as db:
            org = await db.get(Organization, org_id)
            if org is None:
                return _summary("skipped", "none", 0, [], [])
            cfg = org_settings(org)["alignment"]
            if not cfg.get("enabled", True) and kind != "test":
                return _summary("skipped", "none", 0, [], [])
            policies = await applicable_policies(db, org_id, agent_id, kind, policy_ids)
        if not policies or not (text or "").strip():
            return _summary("pass", "none", int((time.perf_counter() - t0) * 1000), [], [])

        block_t = float(cfg.get("block_threshold", 0.7))
        warn_t = float(cfg.get("warn_threshold", 0.45))
        escalate_t = float(cfg.get("escalate_threshold", 0.5))
        laya_mode = cfg.get("laya_mode", "triage")  # triage | authoritative
        judge_on = bool(cfg.get("judge_fallback", True))
        evals: list[RuleEval] = _rules_layer(policies, text)
        checkers: list[str] = ["rules"] if evals else []

        laya_evals: list[RuleEval] = []
        laya_ok = False
        if laya.configured:
            try:
                laya_evals = await _laya_layer(policies, text, user_request,
                                               states=transcript_states(text) if transcript else None)
                laya_ok = True
                checkers.append("laya")
            except LayaUnavailable as e:
                log.warning("laya unavailable: %s", e)

        # Which policies need the (slower) LLM judge?
        #  triage (default): Laya is a fast System-1 screen — anything at/above escalate_t goes to the judge, which
        #                    confirms and attributes violations; clearly-low scores pass without any LLM call.
        #  authoritative:    Laya decides (e.g. after fine-tuning it on this workspace's policies); only its
        #                    uncertain band [warn_t, block_t) is escalated.
        #  no Laya:          the judge reviews every applicable policy.
        if laya_ok and laya_mode == "authoritative":
            evals.extend(laya_evals)
            target_ids = {e.policy_id for e in laya_evals if warn_t <= e.probability < block_t}
        elif laya_ok:
            target_ids = {e.policy_id for e in laya_evals if e.probability >= escalate_t}
            if not judge_on:  # screening only: Laya may warn, never block on its own
                evals.extend(RuleEval(e.policy_id, e.policy_name, e.rule, "warn", e.probability, "laya")
                             for e in laya_evals if e.probability >= escalate_t)
        else:
            target_ids = {p.id for p in policies}

        info: list[RuleEval] = list(laya_evals) if laya_ok and laya_mode != "authoritative" else []
        targets = [p for p in policies if p.id in target_ids]
        if judge_on and targets:
            try:
                je = await _judge_layer(org_id, targets, text, user_request, transcript=transcript)
                judged = {p.id for p in targets}
                evals = [e for e in evals if not (e.checker == "laya" and e.policy_id in judged)] + je
                for p in targets:  # record a confident pass for reviewed policies the judge cleared
                    if not any(e.policy_id == p.id for e in je):
                        info.append(RuleEval(p.id, p.name, None, p.severity, 0.05, "judge"))
                checkers.append("judge")
            except Exception as e:  # noqa: BLE001
                log.warning("policy judge failed: %s", e)
                if laya_ok and laya_mode != "authoritative":  # judge unavailable: degrade to Laya warnings
                    evals.extend(RuleEval(e2.policy_id, e2.policy_name, e2.rule, "warn", e2.probability, "laya")
                                 for e2 in laya_evals if e2.policy_id in target_ids)

        verdict = "pass"
        violations = []
        for e in evals:
            if e.probability >= block_t and e.severity == "block":
                verdict = "block"
            elif e.probability >= warn_t and verdict != "block":
                verdict = "warn"
            if e.probability >= warn_t:
                violations.append({"policy_id": e.policy_id, "policy_name": e.policy_name, "rule": e.rule,
                                   "score": round(e.probability, 3), "explanation": e.explanation})
        evals = evals + [e for e in info if not any(x is e for x in evals)]
        checker = "+".join(dict.fromkeys(c for c in checkers if c != "rules")) or ("rules" if checkers else "none")
        latency = int((time.perf_counter() - t0) * 1000)
        per_rule = [{"policy_id": e.policy_id, "policy_name": e.policy_name, "rule": e.rule,
                     "violation_probability": round(e.probability, 3), "checker": e.checker,
                     "explanation": e.explanation} for e in evals]
        summary = _summary(verdict, checker, latency, violations, per_rule)
        if record:
            async with session_scope() as db:
                db.add(AlignmentCheck(org_id=org_id, agent_id=agent_id, task_id=task_id, message_id=message_id,
                                      kind=kind if kind in ("response", "outbound", "test") else "response",
                                      verdict=verdict, checker=checker, latency_ms=latency, violations=violations,
                                      excerpt=text[:400]))
        return summary
    except Exception:  # noqa: BLE001
        log.exception("policy check failed")
        return _summary("skipped", "none", int((time.perf_counter() - t0) * 1000), [], [])


def _summary(verdict: str, checker: str, latency: int, violations: list, per_rule: list) -> dict:
    return {"verdict": verdict, "checker": checker, "latency_ms": latency, "violations": violations,
            "per_rule": per_rule}


def public_summary(s: dict, revised: bool = False) -> dict:
    out = {k: s.get(k) for k in ("verdict", "checker", "latency_ms", "violations")}
    if revised:
        out["revised"] = True
    return out


def violation_feedback(summary: dict) -> str:
    lines = [f"- {v['policy_name']}: {v.get('rule') or ''} {('— ' + v['explanation']) if v.get('explanation') else ''}".strip()
             for v in summary.get("violations", [])]
    return "\n".join(lines) or "- (unspecified policy violation)"


async def revise_to_comply(llm: Any, original_messages: list[dict], draft: str, summary: dict) -> str:
    """Ask the model to rewrite a draft so it complies; returns the revised text."""
    msgs = list(original_messages) + [
        {"role": "assistant", "content": draft},
        {"role": "user", "content": (
            "[Policy guard] Your previous reply violates workspace policy:\n" + violation_feedback(summary)
            + "\n\nRewrite your reply so it fully complies while still being as helpful as possible. "
              "Do not mention the policy check unless you must decline part of the request. Output only the new reply.")},
    ]
    res = await llm.chat(msgs, temperature=0.2)
    return (res.content or "").strip()


async def gather_checks(*coros):
    return await asyncio.gather(*coros, return_exceptions=True)
