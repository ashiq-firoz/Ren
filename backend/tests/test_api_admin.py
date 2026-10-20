"""API coverage for workspace administration surfaces."""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(db_ready):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/auth/register", json={"email": "admin-api@ren.dev", "password": "supersecret1",
                                                "name": "Grace Hopper", "org_name": "Compilers Inc", "mode": "business"})
    assert r.status_code == 200, r.text
    d = r.json()
    headers = {"Authorization": f"Bearer {d['access_token']}", "X-Org-Id": d["orgs"][0]["id"]}
    client.post("/api/providers", headers=headers, json={"name": "Mock", "kind": "mock", "default_model": "mock-1"})
    return headers


def test_auth_edges(client, h):
    assert client.post("/api/auth/register", json={"email": "admin-api@ren.dev", "password": "supersecret1",
                                                   "name": "Dup", "mode": "business"}).status_code == 409
    assert client.post("/api/auth/login", json={"email": "admin-api@ren.dev", "password": "wrong-pass"}).status_code == 401
    ok = client.post("/api/auth/login", json={"email": "ADMIN-API@ren.dev", "password": "supersecret1"})
    assert ok.status_code == 200 and ok.json()["orgs"]
    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/agents", headers={"Authorization": h["Authorization"]}).status_code == 400  # no org header
    me = client.patch("/api/auth/me", headers=h, json={"name": "Rear Admiral Grace Hopper"}).json()
    assert me["name"].startswith("Rear Admiral")
    assert client.patch("/api/auth/me", headers=h, json={"current_password": "nope", "new_password": "newpassword1"}).status_code == 400


def test_org_settings_and_workspaces(client, h):
    org = client.get("/api/orgs/current", headers=h).json()
    assert org["settings"]["alignment"]["laya_mode"] == "triage"
    upd = client.patch("/api/orgs/current", headers=h, json={"settings": {
        "alignment": {"block_threshold": 0.8}, "retrieval": {"top_k": 5}, "web_search_api_key": "tvly-secret"}}).json()
    assert upd["settings"]["alignment"]["block_threshold"] == 0.8
    assert upd["settings"]["alignment"]["warn_threshold"] == 0.45  # deep-merged, untouched
    assert upd["settings"]["retrieval"]["top_k"] == 5
    assert upd["settings"]["web_search"]["has_key"] is True
    assert "tvly-secret" not in str(upd)
    new = client.post("/api/orgs", headers=h, json={"name": "Side Project", "mode": "personal"}).json()
    assert new["role"] == "owner" and new["mode"] == "personal"
    assert client.get("/api/orgs", headers=h).json()["total"] == 2


def test_providers(client, h):
    items = client.get("/api/providers", headers=h).json()["items"]
    assert items[0]["has_key"] is False
    r = client.post("/api/providers", headers=h, json={"name": "Local vLLM", "kind": "vllm", "default_model": "qwen"})
    assert r.status_code == 400  # base_url required
    r = client.post("/api/providers", headers=h, json={"name": "OR", "kind": "openrouter", "api_key": "sk-or-v1-test",
                                                       "default_model": "openai/gpt-4.1-mini"}).json()
    assert r["has_key"] is True and r["base_url"].startswith("https://openrouter.ai") and "sk-or" not in str(r)
    p2 = client.patch(f"/api/providers/{r['id']}", headers=h, json={"is_default": True, "api_key": ""}).json()
    assert p2["is_default"] is True and p2["has_key"] is False
    mock_id = items[0]["id"]
    client.patch(f"/api/providers/{mock_id}", headers=h, json={"is_default": True})
    models = client.get(f"/api/providers/{mock_id}/models", headers=h).json()
    assert models["items"][0]["id"] == "mock-1"
    assert client.delete(f"/api/providers/{r['id']}", headers=h).json() == {"ok": True}


def test_agents_crud(client, h):
    a = client.post("/api/agents", headers=h, json={"name": "Procurement Analyst", "role": "Vendor & spend analysis",
                                                    "tool_groups": ["sheets", "analytics"], "autonomy": "supervised"}).json()
    assert a["slug"] == "procurement-analyst" and a["stats"]["tasks_total"] == 0
    assert client.post("/api/agents", headers=h, json={"name": "X", "role": "Y", "autonomy": "yolo"}).status_code == 400
    up = client.patch(f"/api/agents/{a['id']}", headers=h, json={"temperature": 0.1, "model": "", "max_steps": 40}).json()
    assert up["temperature"] == 0.1 and up["model"] is None and up["max_steps"] == 40
    orch = next(x for x in client.get("/api/agents", headers=h).json()["items"] if x["is_orchestrator"])
    assert client.delete(f"/api/agents/{orch['id']}", headers=h).status_code == 400
    assert client.delete(f"/api/agents/{a['id']}", headers=h).json() == {"ok": True}


def test_policies_crud(client, h):
    bad = client.post("/api/policies", headers=h, json={"name": "Broken", "patterns": ["(unclosed"]})
    assert bad.status_code == 400
    p = client.post("/api/policies", headers=h, json={"name": "No pricing promises", "rules": ["Never quote prices.", " "],
                                                      "patterns": ["@pii:email"], "severity": "block"}).json()
    assert p["rules"] == ["Never quote prices."] and p["severity"] == "block"
    res = client.post("/api/policies/test", headers=h, json={"text": "Contact me at jane@acme.com", "policy_ids": [p["id"]]}).json()
    assert res["verdict"] == "block" and res["per_rule"]
    upd = client.patch(f"/api/policies/{p['id']}", headers=h, json={"enabled": False}).json()
    assert upd["enabled"] is False
    assert client.get("/api/alignment/checks", headers=h).json()["total"] >= 1
    assert client.get("/api/alignment/status", headers=h).json()["judge"]["enabled"] is True
    assert client.delete(f"/api/policies/{p['id']}", headers=h).json() == {"ok": True}


def test_tool_catalog_and_toggles(client, h):
    cat = client.get("/api/tools/catalog", headers=h).json()["items"]
    ids = {g["id"] for g in cat}
    assert {"agents", "memory", "skills", "approval"} <= ids
    fs = next((g for g in cat if g["id"] == "filesystem"), None)
    if fs:
        off = client.patch("/api/tools/groups/filesystem", headers=h, json={"enabled": False}).json()
        assert off["enabled"] is False
        on = client.patch("/api/tools/groups/filesystem", headers=h, json={"enabled": True}).json()
        assert on["enabled"] is True
    assert client.patch("/api/tools/groups/does-not-exist", headers=h, json={"enabled": True}).status_code == 404


def test_connectors(client, h):
    kinds = client.get("/api/connectors/kinds", headers=h).json()["items"]
    if not kinds:
        pytest.skip("no connector kinds registered yet")
    kind = next((k for k in kinds if k["kind"] == "github"), kinds[0])
    secret_fields = [f["key"] for f in kind["fields"] if f.get("secret")]
    cfg = {f["key"]: "value-123" for f in kind["fields"] if f.get("required")}
    c = client.post("/api/connectors", headers=h, json={"kind": kind["kind"], "name": "Test", "config": cfg})
    assert c.status_code == 200, c.text
    body = c.json()
    for k in secret_fields:
        if k in cfg:
            assert body["config"].get(k) == "••••"
    assert "value-123" not in str({k: v for k, v in body["config"].items() if k in secret_fields})
    assert client.post("/api/connectors", headers=h, json={"kind": "nope", "name": "x", "config": {}}).status_code == 400
    assert client.delete(f"/api/connectors/{body['id']}", headers=h).json() == {"ok": True}


def test_mcp_servers_api(client, h):
    script = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")
    s = client.post("/api/mcp/servers", headers=h, json={"name": "Echo API", "transport": "stdio",
                                                          "command": sys.executable, "args": [script]})
    assert s.status_code == 200, s.text
    srv = s.json()
    assert srv["status"] == "connected", srv
    assert {t["name"] for t in srv["tools"]} >= {"echo", "add"}
    cat = client.get("/api/tools/catalog", headers=h).json()["items"]
    g = next(x for x in cat if x["id"] == f"mcp:{srv['id']}")
    assert any(t["display_name"].endswith(".echo") for t in g["tools"])
    assert client.get("/api/mcp/presets", headers=h).json()["total"] >= 8
    assert client.post("/api/mcp/servers", headers=h, json={"name": "Bad", "transport": "http", "url": "ftp://x"}).status_code == 400
    off = client.patch(f"/api/mcp/servers/{srv['id']}", headers=h, json={"enabled": False}).json()
    assert off["enabled"] is False
    assert client.delete(f"/api/mcp/servers/{srv['id']}", headers=h).json() == {"ok": True}


def test_skills_memory_files_schedules(client, h):
    skills = client.get("/api/skills", headers=h).json()["items"]
    s = next(x for x in skills if x["slug"] == "policy-qa")
    assert client.post(f"/api/skills/{s['id']}/run", headers=h, json={"inputs": {}}).status_code == 400  # required input
    run = client.post(f"/api/skills/{s['id']}/run", headers=h, json={"inputs": {"question": "What is our leave policy?"}}).json()
    assert run["task"]["skill_id"] == s["id"] and run["conversation_id"]
    assert client.delete(f"/api/skills/{s['id']}", headers=h).status_code == 400  # built-in
    custom = client.post("/api/skills", headers=h, json={"name": "Daily digest", "instructions": "Summarize the day."}).json()
    assert custom["builtin"] is False
    assert client.delete(f"/api/skills/{custom['id']}", headers=h).json() == {"ok": True}

    m = client.post("/api/memories", headers=h, json={"content": "Grace prefers bullet-point summaries", "kind": "preference"}).json()
    assert m["kind"] == "preference"
    found = client.get("/api/memories", headers=h, params={"q": "bullet summaries"}).json()["items"]
    assert any(x["id"] == m["id"] for x in found)
    assert client.patch(f"/api/memories/{m['id']}", headers=h, json={"importance": 0.9}).json()["importance"] == 0.9
    assert client.delete(f"/api/memories/{m['id']}", headers=h).json() == {"ok": True}

    up = client.post("/api/files/upload", headers=h, files={"file": ("notes.txt", b"hello ren", "text/plain")},
                     data={"path": "workspace"}).json()
    assert up["path"] == "workspace/notes.txt"
    names = [f["name"] for f in client.get("/api/files", headers=h, params={"path": "workspace"}).json()["items"]]
    assert "notes.txt" in names
    dl = client.get("/api/files/download", headers=h, params={"path": "workspace/notes.txt"})
    assert dl.status_code == 200 and dl.content == b"hello ren"
    assert client.get("/api/files/download", headers=h, params={"path": "../../etc/passwd"}).status_code == 404
    assert client.delete("/api/files", headers=h, params={"path": "workspace"}).status_code == 400  # top-level folder
    assert client.delete("/api/files", headers=h, params={"path": "workspace/notes.txt"}).json() == {"ok": True}

    assert client.get("/api/schedules", headers=h).json()["total"] == 0
    sysinfo = client.get("/api/settings/system", headers=h).json()
    assert sysinfo["db"] in ("sqlite", "postgresql") and sysinfo["mcp_endpoint"].endswith("/mcp")
    notes = client.get("/api/notifications", headers=h).json()
    assert "items" in notes
    assert client.post("/api/notifications/read-all", headers=h).json() == {"ok": True}
    time.sleep(0.2)
