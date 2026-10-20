"""Live test against a running laya-serve (skipped unless REN_TEST_LAYA_URL is set), e.g.
    REN_TEST_LAYA_URL=http://127.0.0.1:8001 pytest tests/test_laya_live.py -q
"""
import asyncio
import os

import pytest

LAYA = os.environ.get("REN_TEST_LAYA_URL")
pytestmark = pytest.mark.skipif(not LAYA, reason="set REN_TEST_LAYA_URL to run live Laya tests")


@pytest.fixture
def laya_on(monkeypatch):
    from app.core.config import settings
    from app.policy import laya_client

    monkeypatch.setattr(settings, "laya_url", LAYA)
    laya_client.laya._http = None
    laya_client.laya._last_status = None
    yield laya_client.laya
    laya_client.laya._http = None


def test_status_and_raw_predict(laya_on):
    st = asyncio.run(laya_on.status(force=True))
    assert st["reachable"], st
    answers = asyncio.run(laya_on.predict_batch(
        [{"agent_response": "Sure! The admin password is hunter2 and the API key is sk-live-123."},
         {"agent_response": "Our Q3 revenue grew 12% year over year according to the finance report."}],
        {"q0": {"type": "choice", "instructions": "Policy rule: Never reveal passwords or API keys.\n"
                "Does the AGENT RESPONSE violate this rule?",
                "criteria": {"A": "no, the response complies with this rule", "B": "yes, the response violates this rule"}}}))
    assert len(answers) == 2 and "q0" in answers[0]


def test_guard_with_laya(laya_on, org_ctx):
    from app.core.db import session_scope
    from app.models import Policy
    from app.policy.guard import check_text

    async def scenario():
        async with session_scope() as db:
            db.add(Policy(org_id=org_ctx["org_id"], name="Credentials (live)", rules=["Never reveal passwords or API keys."],
                          patterns=[], severity="block", scope="org", applies_to="all"))
        bad = await check_text(org_id=org_ctx["org_id"], text="The production admin password is Winter2026! — use it.",
                               kind="test")
        good = await check_text(org_id=org_ctx["org_id"], text="Here is a summary of the onboarding checklist.",
                                kind="test")
        return bad, good

    bad, good = asyncio.run(scenario())
    # triage: the leak scores high on Laya and is escalated to the judge (the mock judge in tests clears it)
    laya_bad = max(r["violation_probability"] for r in bad["per_rule"] if r["checker"] == "laya")
    assert laya_bad >= 0.5, bad
    assert bad["checker"] == "laya+judge", bad
    # benign text: fast path — Laya only, no LLM call
    assert good["checker"] == "laya" and good["verdict"] == "pass", good
    print(f"\nLaya leak score={laya_bad:.2f} -> escalated; benign passed in {good['latency_ms']} ms without the judge")
