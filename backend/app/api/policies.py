"""Policies and alignment monitoring (Laya fast checker + LLM judge)."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.db import utcnow
from app.core.deps import Ctx, get_ctx, require_admin
from app.models import Agent, AlignmentCheck, Policy
from app.policy.guard import MACROS, _compile, check_text
from app.policy.laya_client import laya
from app.services.audit import audit
from app.services.serialize import alignment_check_out, org_settings, policy_out

router = APIRouter(prefix="/api", tags=["policies"])


class PolicyIn(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    rules: list[str] | None = None
    patterns: list[str] | None = None
    severity: str | None = None
    scope: str | None = None
    applies_to: str | None = None
    agent_ids: list[str] | None = None
    enabled: bool | None = None


class PolicyTestIn(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    user_request: str | None = None
    agent_id: str | None = None
    policy_ids: list[str] | None = None
    kind: str = "response"


def _validate(body: PolicyIn) -> None:
    if body.severity is not None and body.severity not in ("block", "warn"):
        raise HTTPException(400, "severity must be block or warn")
    if body.scope is not None and body.scope not in ("org", "agent"):
        raise HTTPException(400, "scope must be org or agent")
    if body.applies_to is not None and body.applies_to not in ("responses", "outbound", "all"):
        raise HTTPException(400, "applies_to must be responses, outbound or all")
    for p in body.patterns or []:
        if p not in MACROS and _compile(p) is None:
            raise HTTPException(400, f"Invalid regular expression: {p}")
    if body.rules is not None:
        body.rules = [r.strip()[:400] for r in body.rules if r and r.strip()]


async def _get(ctx: Ctx, pid: str) -> Policy:
    p = await ctx.db.get(Policy, pid)
    if p is None or p.org_id != ctx.org_id:
        raise HTTPException(404, "Policy not found")
    return p


@router.get("/policies")
async def list_policies(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Policy).where(Policy.org_id == ctx.org_id).order_by(Policy.created_at))).scalars().all()
    return {"items": [policy_out(p) for p in rows], "total": len(rows)}


@router.post("/policies")
async def create_policy(body: PolicyIn, ctx: Ctx = Depends(require_admin)):
    if not body.name:
        raise HTTPException(400, "name is required")
    _validate(body)
    p = Policy(org_id=ctx.org_id, **{"description": "", "category": "custom", "rules": [], "patterns": [],
                                     "severity": "warn", "scope": "org", "applies_to": "all", "agent_ids": [],
                                     "enabled": True, **body.model_dump(exclude_none=True)})
    ctx.db.add(p)
    await ctx.db.flush()
    await audit(ctx.org_id, "policy.created", user_id=ctx.user_id, target_type="policy", target_id=p.id,
                details={"name": p.name}, db=ctx.db)
    return policy_out(p)


@router.patch("/policies/{pid}")
async def patch_policy(pid: str, body: PolicyIn, ctx: Ctx = Depends(require_admin)):
    p = await _get(ctx, pid)
    _validate(body)
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(p, k, v)
    await audit(ctx.org_id, "policy.updated", user_id=ctx.user_id, target_type="policy", target_id=p.id, db=ctx.db)
    return policy_out(p)


@router.delete("/policies/{pid}")
async def delete_policy(pid: str, ctx: Ctx = Depends(require_admin)):
    p = await _get(ctx, pid)
    await ctx.db.delete(p)
    await audit(ctx.org_id, "policy.deleted", user_id=ctx.user_id, target_type="policy", target_id=pid, db=ctx.db)
    return {"ok": True}


@router.post("/policies/test")
async def test_policy(body: PolicyTestIn, ctx: Ctx = Depends(get_ctx)):
    res = await check_text(org_id=ctx.org_id, text=body.text, user_request=body.user_request, agent_id=body.agent_id,
                           kind="test", policy_ids=body.policy_ids, record=True)
    return res


@router.get("/alignment/checks")
async def alignment_checks(verdict: str | None = None, agent_id: str | None = None, limit: int = 50, offset: int = 0,
                           ctx: Ctx = Depends(get_ctx)):
    q = select(AlignmentCheck).where(AlignmentCheck.org_id == ctx.org_id)
    if verdict:
        q = q.where(AlignmentCheck.verdict == verdict)
    if agent_id:
        q = q.where(AlignmentCheck.agent_id == agent_id)
    total = (await ctx.db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await ctx.db.execute(q.order_by(AlignmentCheck.created_at.desc()).offset(max(0, offset))
                                 .limit(max(1, min(limit, 200))))).scalars().all()
    names = {a.id: a.name for a in (await ctx.db.execute(select(Agent).where(Agent.org_id == ctx.org_id))).scalars().all()}
    return {"items": [alignment_check_out(c, names.get(c.agent_id)) for c in rows], "total": total}


@router.get("/alignment/stats")
async def alignment_stats(ctx: Ctx = Depends(get_ctx)):
    since = utcnow() - timedelta(days=30)
    rows = (await ctx.db.execute(select(AlignmentCheck).where(AlignmentCheck.org_id == ctx.org_id,
                                                              AlignmentCheck.created_at >= since))).scalars().all()
    total = len(rows)
    passed = sum(1 for r in rows if r.verdict == "pass")
    warned = sum(1 for r in rows if r.verdict == "warn")
    blocked = sum(1 for r in rows if r.verdict == "block")
    lat = sorted(r.latency_ms for r in rows if r.latency_ms)
    by_policy: dict[str, dict] = {}
    by_agent: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    series: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        for v in r.violations or []:
            e = by_policy.setdefault(v.get("policy_id"), {"policy_id": v.get("policy_id"),
                                                          "policy_name": v.get("policy_name"), "violations": 0})
            e["violations"] += 1
        if r.agent_id:
            by_agent[r.agent_id][0] += 1
            by_agent[r.agent_id][1] += 1 if r.verdict == "pass" else 0
        day = r.created_at.date().isoformat()
        series[day][0] += 1
        series[day][1] += 1 if r.verdict in ("warn", "block") else 0
    names = {a.id: a.name for a in (await ctx.db.execute(select(Agent).where(Agent.org_id == ctx.org_id))).scalars().all()}
    days = [(utcnow() - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
    return {
        "total": total, "passed": passed, "warned": warned, "blocked": blocked,
        "pass_rate": round(passed / total, 3) if total else None,
        "p50_latency_ms": lat[len(lat) // 2] if lat else None,
        "by_policy": sorted(by_policy.values(), key=lambda x: -x["violations"]),
        "by_agent": [{"agent_id": aid, "agent_name": names.get(aid, "Agent"), "checks": c,
                      "pass_rate": round(p / c, 3) if c else None} for aid, (c, p) in by_agent.items()],
        "series": [{"date": d, "checks": series[d][0], "violations": series[d][1]} for d in days],
    }


@router.get("/alignment/status")
async def alignment_status(ctx: Ctx = Depends(get_ctx)):
    st = await laya.status()
    return {"laya": st, "judge": {"enabled": bool(org_settings(ctx.org)["alignment"].get("judge_fallback", True))}}


@router.get("/alignment/training-data")
async def alignment_training_data(ctx: Ctx = Depends(require_admin)):
    """NDJSON export of judged alignment checks, shaped for fine-tuning Laya on this workspace's policies."""
    rows = (await ctx.db.execute(select(AlignmentCheck).where(AlignmentCheck.org_id == ctx.org_id)
                                 .order_by(AlignmentCheck.created_at.desc()).limit(5000))).scalars().all()
    policies = {p.id: p for p in (await ctx.db.execute(select(Policy).where(Policy.org_id == ctx.org_id))).scalars().all()}

    async def gen():
        import json

        for r in rows:
            if "judge" not in (r.checker or ""):
                continue
            violated = {v.get("policy_id") for v in r.violations or []}
            for pid, p in policies.items():
                for rule in p.rules or []:
                    yield json.dumps({
                        "state": {"agent_response": r.excerpt},
                        "question": {"type": "choice", "instructions": f"Policy rule: {rule}\nDoes the AGENT RESPONSE violate this rule?",
                                     "criteria": {"A": "no, the response complies with this rule",
                                                  "B": "yes, the response violates this rule"}},
                        "label": "B" if pid in violated else "A",
                    }) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="laya-alignment-train.ndjson"'})
