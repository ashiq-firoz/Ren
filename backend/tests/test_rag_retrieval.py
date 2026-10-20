"""Embeddings (hash + OpenAI-compatible), BM25 ranking, RRF fusion, DB-backed lexical/vector/question search."""
import asyncio
import json

import httpx
import numpy as np
import pytest

from app.core.config import settings
from app.rag.embeddings import embed_query, embed_texts, embedding_info, hash_embed
from app.rag.fusion import max_score, rrf, scoped_boost, source_ranks
from app.rag.lexical import BM25Index


def test_hash_embeddings_are_deterministic_normalized_and_semantic():
    a = hash_embed("Employees receive 25 days of paid annual leave")
    b = hash_embed("Employees receive 25 days of paid annual leave")
    c = hash_embed("How many days of annual leave do employees get?")
    d = hash_embed("Laptops must use full disk encryption")
    assert a.shape == (settings.embedding_dim,) and a.dtype == np.float32
    assert np.allclose(a, b) and abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert float(a @ c) > float(a @ d) + 0.2


async def test_embed_api_contract():
    vecs = await embed_texts(["first passage", "second passage", ""])
    assert len(vecs) == 3 and all(v.shape == (settings.embedding_dim,) for v in vecs)
    q1 = await embed_query("what is the leave policy")
    q2 = await embed_query("what is the leave policy")
    assert q1 is q2  # LRU cache
    assert embedding_info() == {"provider": "hash", "model": "feature-hashing (offline)",
                                "dim": settings.embedding_dim}


async def test_openai_compatible_provider_pads_and_normalizes(monkeypatch):
    import app.llm.client as client_mod
    import app.rag.embeddings as emb

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        data = [{"index": i, "embedding": [float(i + 1)] * 8} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_mod, "http_client", lambda: mock_client)
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_base_url", "http://embed.local/v1")
    monkeypatch.setattr(settings, "embedding_api_key", "sk-test")
    monkeypatch.setattr(emb, "_dim_warned", False)
    vecs = await embed_texts(["a", "b"])
    assert seen["url"] == "http://embed.local/v1/embeddings" and seen["auth"] == "Bearer sk-test"
    assert all(v.shape == (settings.embedding_dim,) for v in vecs)
    assert abs(float(np.linalg.norm(vecs[0])) - 1.0) < 1e-5
    assert np.count_nonzero(vecs[0]) == 8  # padded with zeros up to embedding_dim
    await mock_client.aclose()


def test_bm25_ranking_sanity():
    rows = [
        ("c1", "colA", "d1", "s1", "Annual leave: employees receive 25 days of paid leave per year"),
        ("c2", "colA", "d1", "s2", "Parental leave: primary caregivers receive 16 weeks of paid leave"),
        ("c3", "colB", "d2", "s3", "Laptops must use full disk encryption and screen locks"),
        ("c4", "colB", "d2", "s4", "Passwords must be rotated; leave no laptop unattended"),
    ]
    idx = BM25Index.build(rows)
    hits = idx.search("parental leave for caregivers")
    assert hits[0].chunk_id == "c2"
    hits = idx.search("laptop encryption")
    assert hits[0].chunk_id == "c3" and {h.chunk_id for h in hits} == {"c3", "c4"}
    # rare terms outweigh common ones: "weeks" (1 doc) beats "leave" (3 docs)
    assert idx.search("weeks leave")[0].chunk_id == "c2"
    assert [h.chunk_id for h in idx.search("leave", collection_ids=["colB"])] == ["c4"]
    assert idx.search("leave", document_ids=["nope"]) == []
    assert idx.search("the and of") == []  # stopwords only


def test_rrf_fusion_and_boost():
    rankings = {"bm25": ["x", "y", "z"], "vector": ["y", "z"]}
    fused = rrf(rankings)
    assert [cid for cid, _ in fused] == ["y", "z", "x"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)
    weighted = rrf(rankings, weights={"vector": 0.0})
    assert [cid for cid, _ in weighted] == ["x", "y", "z"]
    assert source_ranks(rankings)["z"] == {"bm25": 3, "vector": 2}
    assert max_score(rankings) == pytest.approx(2 / 61)
    boosted = scoped_boost(fused, {"x": "leafA", "y": "leafB", "z": "leafB"}, {"leafA": 1.0}, strength=2.0)
    assert boosted[0][0] == "x"


# --------------------------------------------------------------------------- DB-backed retrieval
async def _wait_ready(doc_id: str, timeout: float = 30.0):
    from app.core.db import session_scope
    from app.models import Document

    for _ in range(int(timeout / 0.05)):
        async with session_scope() as db:
            d = await db.get(Document, doc_id)
            if d.status in ("ready", "failed"):
                return d
        await asyncio.sleep(0.05)
    raise AssertionError("ingestion timed out")


POLICY_MD = """# Security Handbook

## Passwords
Passwords must be at least fourteen characters long. Password managers are mandatory for all staff.

## Devices
Laptops must use full disk encryption. Lost devices must be reported to IT within one hour.

## Visitors
Visitors must sign in at reception and wear a badge at all times.
"""


async def test_db_lexical_vector_and_question_search(org_ctx):
    import sqlalchemy as sa

    from app.core.db import session_scope
    from app.models import Collection, Section, SectionQuestion
    from app.rag import lexical, service, vector

    org_id, user_id = org_ctx["org_id"], org_ctx["user_id"]
    async with session_scope() as db:
        col = Collection(org_id=org_id, name="Retrieval test", access="org")
        db.add(col)
        await db.flush()
        doc = await service.create_document_from_bytes(db, org_id=org_id, user_id=user_id, collection_id=col.id,
                                                       filename="security.md", data=POLICY_MD.encode())
        col_id, doc_id = col.id, doc.id
    d = await _wait_ready(doc_id)
    assert d.status == "ready", d.error
    assert d.structure_source == "headings" and d.title == "Security Handbook" and d.leaf_count == 3
    assert d.question_count > 0 and d.chunk_count == 3

    async with session_scope() as db:
        hits = await lexical.lexical_search(db, org_id=org_id, query="disk encryption laptops", collection_ids=[col_id])
        assert hits and (await db.get(Section, hits[0].section_id)).title == "Devices"
        qv = await embed_query("how long must passwords be")
        vhits = await vector.vector_search(db, org_id=org_id, qvec=qv, collection_ids=[col_id])
        assert len(vhits) == 3
        top_sec = await db.get(Section, vhits[0].section_id)
        assert top_sec.title == "Passwords"
        qhits = await vector.question_search(db, org_id=org_id, qvec=await embed_query("What does Visitors cover?"),
                                             collection_ids=[col_id])
        assert qhits and qhits[0].question.startswith("What does Visitors cover")
        scoped = await vector.scoped_chunks(db, org_id=org_id, qvec=qv, section_ids=[top_sec.id])
        assert list(scoped) == [top_sec.id]
        # collection filter excludes everything else
        assert await lexical.lexical_search(db, org_id=org_id, query="encryption", collection_ids=["missing"]) == []
        n_q = (await db.execute(sa.select(sa.func.count()).select_from(SectionQuestion)
                                .where(SectionQuestion.document_id == doc_id))).scalar()
        assert n_q == d.question_count
