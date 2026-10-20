"""End-to-end: register → mock provider → chat → delegation → concurrent follow-up → completion report → SSE."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(db_ready):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _auth(client, email="e2e@ren.dev", mode="business"):
    r = client.post("/api/auth/register", json={"email": email, "password": "supersecret1", "name": "Ada Lovelace",
                                                "org_name": "Analytical Engines", "mode": mode})
    assert r.status_code == 200, r.text
    data = r.json()
    org = data["orgs"][0]
    return {"Authorization": f"Bearer {data['access_token']}", "X-Org-Id": org["id"]}, data


def _wait(pred, timeout=25.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def test_full_flow(client):
    h, data = _auth(client)
    assert data["user"]["name"] == "Ada Lovelace"

    # seeded swarm, policies, skills
    agents = client.get("/api/agents", headers=h).json()["items"]
    assert any(a["is_orchestrator"] for a in agents)
    assert len(agents) >= 8
    assert client.get("/api/policies", headers=h).json()["total"] >= 5
    assert client.get("/api/skills", headers=h).json()["total"] >= 20

    # no provider yet → add the offline mock provider
    r = client.post("/api/providers", headers=h, json={"name": "Mock", "kind": "mock", "default_model": "mock-1"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["is_default"] is True
    t = client.post(f"/api/providers/{pid}/test", headers=h).json()
    assert t["ok"] is True

    conv = client.post("/api/conversations", headers=h, json={}).json()
    cid = conv["id"]
    r = client.post(f"/api/conversations/{cid}/messages", headers=h,
                    json={"content": "Please research the top competitors of Acme Corp and draft a short brief."})
    assert r.status_code == 202, r.text

    def task_created():
        msgs = client.get(f"/api/conversations/{cid}", headers=h).json()["messages"]
        return [m for m in msgs if m["role"] == "event" and m["metadata"].get("event", {}).get("kind") == "task_created"]

    created = _wait(task_created)
    assert created, "orchestrator did not delegate"
    task_id = created[0]["task_id"]

    # follow-up while work is in flight — must be accepted immediately (202) and answered
    r2 = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"content": "How is it going? any status?"})
    assert r2.status_code == 202

    def task_done():
        t = client.get(f"/api/tasks/{task_id}", headers=h).json()
        return t if t["status"] in ("completed", "failed") else None

    task = _wait(task_done, timeout=40)
    assert task is not None and task["status"] == "completed", task
    assert task["result"]
    kinds = {e["kind"] for e in task["events"]}
    assert {"status", "tool_call", "tool_result", "policy"} <= kinds, kinds

    def reported():
        msgs = client.get(f"/api/conversations/{cid}", headers=h).json()["messages"]
        done_cards = [m for m in msgs if m["role"] == "event" and m["metadata"]["event"].get("kind") == "task_completed"]
        assistants = [m for m in msgs if m["role"] == "assistant" and m["status"] == "complete"]
        return (done_cards, assistants) if done_cards and len(assistants) >= 3 else None

    rep = _wait(reported, timeout=30)
    assert rep, "no completion card / proactive report"
    _, assistants = rep
    assert any("Update" in a["content"] or "completed" in a["content"].lower() for a in assistants)
    # alignment verdicts attached
    assert _wait(lambda: all(a["metadata"].get("alignment", {}).get("verdict") not in (None, "pending")
                             for a in client.get(f"/api/conversations/{cid}", headers=h).json()["messages"]
                             if a["role"] == "assistant"), timeout=15)

    # dashboards & listings
    dash = client.get("/api/dashboard", headers=h).json()
    assert dash["stats"]["completed_today"] >= 1
    assert client.get("/api/tasks", headers=h).json()["total"] >= 1
    assert client.get("/api/alignment/stats", headers=h).json()["total"] >= 1
    assert client.get("/api/audit", headers=h).json()["total"] >= 1
    assert client.get("/api/tools/catalog", headers=h).json()["total"] >= 5
    convs = client.get("/api/conversations", headers=h).json()["items"]
    assert convs[0]["id"] == cid


def test_direct_task_and_controls(client):
    h, _ = _auth(client, "ctl@ren.dev", "personal")
    client.post("/api/providers", headers=h, json={"name": "Mock", "kind": "mock", "default_model": "mock-1"})
    agent = next(a for a in client.get("/api/agents", headers=h).json()["items"] if not a["is_orchestrator"])
    t = client.post("/api/tasks", headers=h, json={"agent_id": agent["id"], "title": "Summarize notes",
                                                   "instructions": "Summarize my notes"}).json()
    done = _wait(lambda: client.get(f"/api/tasks/{t['id']}", headers=h).json()["status"] in ("completed", "failed"), 30)
    assert done
    r = client.post(f"/api/tasks/{t['id']}/retry", headers=h)
    assert r.status_code == 200
    ev = client.post(f"/api/tasks/{t['id']}/message", headers=h, json={"content": "also add a TL;DR"})
    assert ev.status_code == 200 and ev.json()["kind"] == "inbox"


def test_policy_test_console(client):
    h, _ = _auth(client, "pol@ren.dev")
    res = client.post("/api/policies/test", headers=h, json={"text": "Here is the key: sk-or-v1-abcdefghijklmnopqrstuvwxyz1234"}).json()
    assert res["verdict"] == "block"
    assert any("Confidentiality" in v["policy_name"] for v in res["violations"])
    ok = client.post("/api/policies/test", headers=h, json={"text": "Our Q3 revenue grew 12% according to [1]."}).json()
    assert ok["verdict"] in ("pass", "warn")


def test_rbac_and_invites(client):
    h, _ = _auth(client, "boss@ren.dev")
    inv = client.post("/api/orgs/current/invites", headers=h, json={"email": "viewer@ren.dev", "role": "viewer"}).json()
    info = client.get(f"/api/auth/invite/{inv['token']}").json()
    assert info["role"] == "viewer" and info["user_exists"] is False
    acc = client.post("/api/auth/accept-invite", json={"token": inv["token"], "name": "Vee", "password": "viewerpass1"})
    assert acc.status_code == 200
    vh = {"Authorization": f"Bearer {acc.json()['access_token']}", "X-Org-Id": h["X-Org-Id"]}
    assert client.post("/api/agents", headers=vh, json={"name": "X", "role": "Y"}).status_code == 403
    assert client.get("/api/orgs/current/members", headers=h).json()["total"] == 2


def test_sse_stream_live(db_ready):
    """Real uvicorn server + streaming HTTP client: the event stream carries the chat lifecycle."""
    import asyncio
    import socket

    import httpx
    import uvicorn

    from app.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    assert _wait(lambda: server.started, timeout=20)
    try:
        with httpx.Client(base_url=base, timeout=30) as c:
            r = c.post("/api/auth/register", json={"email": "sse@ren.dev", "password": "supersecret1", "name": "Sam",
                                                   "mode": "business"})
            d = r.json()
            h = {"Authorization": f"Bearer {d['access_token']}", "X-Org-Id": d["orgs"][0]["id"]}
            c.post("/api/providers", headers=h, json={"name": "Mock", "kind": "mock", "default_model": "mock-1"})
            cid = c.post("/api/conversations", headers=h, json={}).json()["id"]
            events: list[dict] = []
            stop = threading.Event()

            def listen():
                with httpx.Client(base_url=base, timeout=None) as sc:
                    with sc.stream("GET", "/api/stream", headers=h) as resp:
                        for line in resp.iter_lines():
                            if line.startswith("data:"):
                                events.append(json.loads(line[5:].strip()))
                            if stop.is_set():
                                break

            lt = threading.Thread(target=listen, daemon=True)
            lt.start()
            assert _wait(lambda: any(e["type"] == "ready" for e in events), timeout=10)
            c.post(f"/api/conversations/{cid}/messages", headers=h,
                   json={"content": "Research the market for solar inverters and write a summary."})
            need = {"message.created", "message.delta", "message.completed", "task.created", "task.updated",
                    "task.event", "conversation.status"}
            got = _wait(lambda: need <= {e["type"] for e in events}, timeout=40)
            stop.set()
            assert got, {e["type"] for e in events}
    finally:
        server.should_exit = True
        th.join(timeout=20)
