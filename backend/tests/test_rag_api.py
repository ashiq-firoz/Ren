"""End-to-end via the Knowledge router: upload → ingest → ToC → search (all modes) → answer → training data,
plus access filtering for restricted collections and upload validation."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HANDBOOK_MD = """# Acme Employee Handbook

Welcome to Acme. This handbook explains the policies that apply to every employee, contractor and intern.
Please read it carefully and ask the people team if anything is unclear.

## Leave Policy

### Annual Leave
Employees receive 25 days of paid annual leave per calendar year. Unused days may be carried over up to a
maximum of five days. Requests must be submitted through the HR portal at least two weeks in advance.

### Parental Leave
Primary caregivers receive sixteen weeks of fully paid parental leave. Secondary caregivers receive four weeks.
Parental leave can be taken within twelve months of the birth or adoption.

## Expenses

### Travel Expenses
Economy class flights are reimbursed for trips under six hours. Business class requires director approval.

### Meal Allowance
The daily meal allowance while travelling is sixty euros, and receipts are required for every claim.
"""


def _client() -> TestClient:
    from app.api.knowledge import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _headers(user_id: str, org_id: str) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}", "X-Org-Id": org_id}


def _wait_ready(client: TestClient, doc_id: str, headers: dict, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/documents/{doc_id}", headers=headers)
        assert r.status_code == 200, r.text
        d = r.json()
        if d["status"] in ("ready", "failed"):
            return d
        time.sleep(0.1)
    raise AssertionError("ingestion did not finish in time")


def _add_user(org_id: str, email: str, role: str) -> str:
    from app.core.db import session_scope
    from app.core.security import hash_password
    from app.models import Membership, User

    async def make():
        async with session_scope() as db:
            u = User(email=email, name=email.split("@")[0].title(), password_hash=hash_password("password123"))
            db.add(u)
            await db.flush()
            db.add(Membership(org_id=org_id, user_id=u.id, role=role))
            return u.id

    return asyncio.run(make())


def test_end_to_end_ingest_search_answer(org_ctx):
    org_id, owner = org_ctx["org_id"], org_ctx["user_id"]
    h = _headers(owner, org_id)
    with _client() as client:
        cols = client.get("/api/collections", headers=h).json()
        names = {c["name"]: c for c in cols["items"]}
        assert "General" in names and "Chat Uploads" in names and cols["total"] == len(cols["items"])

        r = client.post("/api/collections", headers=h, json={"name": "HR Handbook E2E", "description": "HR"})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        assert r.json()["access"] == "org" and r.json()["document_count"] == 0

        r = client.post("/api/documents", headers=h, data={"collection_id": cid},
                        files={"file": ("acme_handbook.md", HANDBOOK_MD.encode(), "text/markdown")})
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["status"] == "uploaded" and doc["collection_name"] == "HR Handbook E2E"
        assert doc["uploaded_by_name"] == "Test Owner" and doc["filename"] == "acme_handbook.md"

        d = _wait_ready(client, doc["id"], h)
        assert d["status"] == "ready", d["error"]
        assert d["title"] == "Acme Employee Handbook" and d["structure_source"] == "headings"
        assert d["leaf_count"] == 5 and d["section_count"] == 7 and d["chunk_count"] == 5
        assert d["question_count"] >= 15 and d["progress"] == 100
        toc = d["toc"]
        assert [(n["path"], n["title"], n["is_leaf"]) for n in toc] == [
            ("0", "Overview", True), ("1", "Leave Policy", False), ("2", "Expenses", False)]
        leave = toc[1]
        assert [(c["path"], c["title"]) for c in leave["children"]] == [("1.1", "Annual Leave"),
                                                                        ("1.2", "Parental Leave")]
        parental_id = leave["children"][1]["id"]

        sec = client.get(f"/api/documents/{doc['id']}/sections/{parental_id}", headers=h).json()
        assert sec["breadcrumb"] == "Acme Employee Handbook › Leave Policy › Parental Leave"
        assert "sixteen weeks" in sec["content"] and sec["is_leaf"] and sec["questions"]
        parent = client.get(f"/api/documents/{doc['id']}/sections/{leave['id']}", headers=h).json()
        assert not parent["is_leaf"] and "### 1.2 Parental Leave" in parent["content"]
        assert [c["path"] for c in parent["children"]] == ["1.1", "1.2"]

        q = "How many weeks of parental leave do primary caregivers get?"
        r = client.post("/api/knowledge/search", headers=h, json={"query": q, "collection_ids": [cid]})
        assert r.status_code == 200, r.text
        res = r.json()
        top = res["results"][0]
        assert top["section_title"] == "Parental Leave" and top["section_path"] == "1.2"
        assert top["sources"]["bm25"] == 1 and top["sources"]["vector"] == 1 and top["sources"]["stair"] == 1
        assert set(top["sources"]) == {"bm25", "vector", "stair", "questions"}
        assert res["stair"]["enabled"] and res["stair"]["selected"][0]["title"] == "Parental Leave"
        assert res["stair"]["method"].startswith("single") and res["stair"]["candidates_considered"] == 5
        assert 0 < res["stair"]["selected"][0]["confidence"] <= 1
        for stage in ("bm25", "vector", "questions", "stair", "fusion", "total"):
            assert stage in res["timings"]
        assert res["context"].startswith("[1] Acme Employee Handbook › Leave Policy › Parental Leave (§1.2)")
        assert top["citation"] == 1 and res["citations"][0]["section_id"] == parental_id

        lex = client.post("/api/knowledge/search", headers=h,
                          json={"query": "meal allowance receipts", "collection_ids": [cid], "mode": "lexical"}).json()
        assert lex["results"][0]["section_title"] == "Meal Allowance"
        assert all(r_["sources"]["vector"] is None and r_["sources"]["stair"] is None for r_ in lex["results"])
        assert not lex["stair"]["enabled"]
        sem = client.post("/api/knowledge/search", headers=h,
                          json={"query": "economy flights reimbursed", "collection_ids": [cid], "mode": "semantic",
                                "top_k": 2}).json()
        assert 1 <= len(sem["results"]) <= 2 and sem["results"][0]["section_title"] == "Travel Expenses"
        assert all(r_["sources"]["bm25"] is None for r_ in sem["results"])
        struct = client.post("/api/knowledge/search", headers=h,
                             json={"query": "travel flights business class", "collection_ids": [cid],
                                   "mode": "structure"}).json()
        assert struct["stair"]["selected"][0]["title"] == "Travel Expenses"
        assert struct["results"][0]["section_title"] == "Travel Expenses"
        assert all(r_["sources"]["bm25"] is None for r_ in struct["results"])

        r = client.post("/api/knowledge/answer", headers=h, json={"query": q, "collection_ids": [cid]})
        assert r.status_code == 200, r.text
        ans = r.json()
        assert set(ans) >= {"answer", "citations", "alignment", "retrieval"}
        assert ans["answer"] and ans["retrieval"]["results"]
        valid_ids = {c["id"] for c in ans["retrieval"]["citations"]}
        assert all(c["id"] in valid_ids for c in ans["citations"])
        assert ans["citations"] and ans["citations"][0]["section_path"] == "1.2"
        assert ans["alignment"]["verdict"] in ("pass", "warn", "block", "skipped")

        r = client.get("/api/knowledge/training-data", headers=h, params={"collection_id": cid})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
        assert len(lines) == d["question_count"]
        leaf_paths = {"0", "1.1", "1.2", "2.1", "2.2"}
        for ln in lines:
            assert set(ln) == {"messages", "document_id", "leaf_id", "query"}
            roles = [m["role"] for m in ln["messages"]]
            assert roles == ["system", "user", "assistant"]
            assert ln["messages"][1]["content"].startswith(f"QUERY: {ln['query']}\n\nTABLE OF CONTENTS:\n")
            assert "• 1.2 Parental Leave" in ln["messages"][1]["content"]
            assert ln["messages"][2]["content"] in leaf_paths and ln["document_id"] == doc["id"]
        assert any(ln["leaf_id"] == parental_id and ln["messages"][2]["content"] == "1.2" for ln in lines)

        st = client.get("/api/knowledge/stats", headers=h).json()
        assert st["documents"] >= 1 and st["leaves"] >= 5 and st["chunks"] >= 5 and st["questions"] >= 15
        assert st["embedding_dim"] > 0 and st["reranker"] == "none" and st["embedding_model"]

        listing = client.get("/api/documents", headers=h, params={"collection_id": cid}).json()
        assert listing["total"] == 1 and listing["items"][0]["id"] == doc["id"]
        assert client.get("/api/documents", headers=h, params={"collection_id": cid, "q": "nomatch"}).json()["total"] == 0
        dl = client.get(f"/api/documents/{doc['id']}/download", headers=h)
        assert dl.status_code == 200 and dl.content == HANDBOOK_MD.encode()

        r = client.post(f"/api/documents/{doc['id']}/reindex", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "uploaded"
        d2 = _wait_ready(client, doc["id"], h)
        assert d2["status"] == "ready" and d2["leaf_count"] == 5 and d2["title"] == "Acme Employee Handbook"
        assert d2["toc"][1]["children"][1]["id"] == parental_id  # stable section ids across re-index
        res2 = client.post("/api/knowledge/search", headers=h, json={"query": q, "collection_ids": [cid]}).json()
        assert res2["results"][0]["section_title"] == "Parental Leave"
        assert len({r_["chunk_id"] for r_ in res2["results"]}) == len(res2["results"])  # no stale duplicates

        assert client.delete(f"/api/documents/{doc['id']}", headers=h).json() == {"ok": True}
        assert client.get(f"/api/documents/{doc['id']}", headers=h).status_code == 404
        res3 = client.post("/api/knowledge/search", headers=h, json={"query": q, "collection_ids": [cid]}).json()
        assert res3["results"] == []
        ans3 = client.post("/api/knowledge/answer", headers=h, json={"query": q, "collection_ids": [cid]}).json()
        assert ans3["citations"] == [] and "couldn't find" in ans3["answer"]
        assert client.delete(f"/api/collections/{cid}", headers=h).json() == {"ok": True}


def test_restricted_collections_and_permissions(org_ctx, monkeypatch):
    from app.core.config import settings
    from app.core.db import session_scope
    from app.rag import service

    org_id, owner = org_ctx["org_id"], org_ctx["user_id"]
    member = _add_user(org_id, "member.rag@test.dev", "member")
    viewer = _add_user(org_id, "viewer.rag@test.dev", "viewer")
    ho, hm, hv = _headers(owner, org_id), _headers(member, org_id), _headers(viewer, org_id)
    with _client() as client:
        r = client.post("/api/collections", headers=ho,
                        json={"name": "Board Only", "access": "restricted", "member_ids": []})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        assert client.post("/api/collections", headers=hm, json={"name": "Nope"}).status_code == 403
        assert client.post("/api/collections", headers=ho, json={"name": "board only"}).status_code == 409
        assert client.post("/api/collections", headers=ho,
                           json={"name": "Bad", "member_ids": ["not-a-member"]}).status_code == 400

        doc = client.post("/api/documents", headers=ho, data={"collection_id": rid},
                          files={"file": ("board.txt", b"Board minutes: the acquisition of Globex was approved "
                                                       b"for 40 million euros.", "text/plain")}).json()
        assert _wait_ready(client, doc["id"], ho)["status"] == "ready"

        # the member can't see the restricted collection or its document
        assert rid not in {c["id"] for c in client.get("/api/collections", headers=hm).json()["items"]}
        assert client.get(f"/api/documents/{doc['id']}", headers=hm).status_code == 404
        assert client.post("/api/documents", headers=hm, data={"collection_id": rid},
                           files={"file": ("x.txt", b"hello there", "text/plain")}).status_code == 404
        res = client.post("/api/knowledge/search", headers=hm, json={"query": "Globex acquisition"}).json()
        assert all(r_["document_id"] != doc["id"] for r_ in res["results"])
        assert client.post("/api/knowledge/search", headers=ho,
                           json={"query": "Globex acquisition", "collection_ids": [rid]}).json()["results"]
        lines = client.get("/api/knowledge/training-data", headers=hm).text
        assert doc["id"] not in lines

        async def scopes():
            async with session_scope() as db:
                m = await service.accessible_collection_ids(db, org_id, member, "member")
                o = await service.accessible_collection_ids(db, org_id, owner, "owner")
                agent_scoped = await service.accessible_collection_ids(db, org_id, owner, "owner", [rid])
                agent_member = await service.accessible_collection_ids(db, org_id, member, "member", [rid])
                return m, o, agent_scoped, agent_member

        m, o, agent_scoped, agent_member = asyncio.run(scopes())
        assert rid not in m and rid in o and agent_scoped == [rid] and agent_member == []

        # granting membership opens it up
        r = client.patch(f"/api/collections/{rid}", headers=ho, json={"member_ids": [member]})
        assert r.status_code == 200 and r.json()["member_ids"] == [member]
        assert client.get(f"/api/documents/{doc['id']}", headers=hm).status_code == 200
        assert client.post("/api/knowledge/search", headers=hm,
                           json={"query": "Globex acquisition", "collection_ids": [rid]}).json()["results"]
        # members may not delete documents they didn't upload; viewers are read-only
        assert client.delete(f"/api/documents/{doc['id']}", headers=hm).status_code == 403
        assert client.post("/api/documents", headers=hv,
                           files={"file": ("v.txt", b"viewer upload", "text/plain")}).status_code == 403
        assert client.post("/api/knowledge/search", headers=hv, json={"query": "anything"}).status_code == 200

        # upload validation
        r = client.post("/api/documents", headers=hm, files={"file": ("tool.exe", b"MZ\x90\x00", "application/x-msdownload")})
        assert r.status_code == 415
        r = client.post("/api/documents", headers=hm, files={"file": ("legacy.doc", b"\xd0\xcf\x11\xe0", "application/msword")})
        assert r.status_code == 415 and "Word 97-2003" in r.json()["detail"]
        monkeypatch.setattr(settings, "max_upload_mb", 0)
        r = client.post("/api/documents", headers=hm, files={"file": ("big.txt", b"too large", "text/plain")})
        assert r.status_code == 413
        monkeypatch.undo()

        # system collections can't be deleted or renamed
        system_id = next(c["id"] for c in client.get("/api/collections", headers=ho).json()["items"] if c["name"] == "Chat Uploads")
        assert client.delete(f"/api/collections/{system_id}", headers=ho).status_code == 400
        assert client.patch(f"/api/collections/{system_id}", headers=ho, json={"name": "Other"}).status_code == 400
        assert client.delete(f"/api/collections/{rid}", headers=ho).json() == {"ok": True}


def test_failed_document_reports_clear_error(org_ctx):
    org_id, owner = org_ctx["org_id"], org_ctx["user_id"]
    h = _headers(owner, org_id)
    with _client() as client:
        doc = client.post("/api/documents", headers=h,
                          files={"file": ("broken.pdf", b"%PDF-1.4 definitely not a pdf", "application/pdf")}).json()
        d = _wait_ready(client, doc["id"], h)
        assert d["status"] == "failed" and d["error"] and "PDF" in d["error"]
        assert client.delete(f"/api/documents/{doc['id']}", headers=h).json() == {"ok": True}


@pytest.mark.parametrize("missing", ["documents/unknown-id", "documents/unknown-id/sections/x"])
def test_unknown_ids_404(org_ctx, missing):
    h = _headers(org_ctx["user_id"], org_ctx["org_id"])
    with _client() as client:
        assert client.get(f"/api/{missing}", headers=h).status_code == 404
