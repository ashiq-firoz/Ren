"""PostgreSQL code paths (compiled against the asyncpg dialect with a capturing fake session — no server needed)
and the reranker providers."""
import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Captures statements; returns canned rows."""

    def __init__(self, rows=None):
        self.statements = []
        self.rows = rows or []

    async def execute(self, stmt, params=None):
        self.statements.append((stmt, params or {}))
        return _Result(self.rows)


def _sql(stmt, params) -> str:
    compiled = stmt.bindparams(**{k: v for k, v in params.items()}).compile(
        dialect=postgresql.asyncpg.dialect(), compile_kwargs={"render_postcompile": True})
    return str(compiled)


async def test_pg_lexical_sql(monkeypatch):
    from app.rag import lexical

    monkeypatch.setattr(lexical, "IS_POSTGRES", True)
    db = FakeSession(rows=[("c1", "d1", "s1", 0.42)])
    hits = await lexical.lexical_search(db, org_id="o1", query="Parental leave & (weeks) | café!",
                                        collection_ids=["a", "b"], document_ids=["d1"])
    assert [(h.chunk_id, h.score) for h in hits] == [("c1", 0.42)]
    stmt, params = db.statements[0]
    assert params["tsq"] == "parental | leave | weeks | café"  # operators stripped, OR-query
    sql = _sql(stmt, params)
    assert "ts_rank_cd(c.tsv, q)" in sql and "to_tsquery('english'" in sql and "c.tsv @@ q" in sql
    assert "c.collection_id IN ($" in sql and "c.document_id IN ($" in sql and "d.status = 'ready'" in sql
    # empty filter lists short-circuit without touching the database
    db2 = FakeSession()
    assert await lexical.lexical_search(db2, org_id="o1", query="leave", collection_ids=[]) == []
    assert db2.statements == []


async def test_pg_vector_sql(monkeypatch):
    from app.rag import vector

    monkeypatch.setattr(vector, "IS_POSTGRES", True)
    q = np.ones(settings.embedding_dim, dtype=np.float32) / np.sqrt(settings.embedding_dim)
    db = FakeSession(rows=[("c1", "d1", "s1", 0.9)])
    hits = await vector.vector_search(db, org_id="o1", qvec=q, collection_ids=["a"], section_ids=["s1"], limit=5)
    assert hits[0].chunk_id == "c1" and hits[0].score == 0.9
    stmt, params = db.statements[0]
    assert params["qv"].startswith("[") and params["qv"].count(",") == settings.embedding_dim - 1
    sql = _sql(stmt, params)
    assert "c.embedding <=> CAST(CAST($1" in sql and "as text) as vector)" in sql.lower()
    assert sql.count("$1") == 2  # the query vector is bound once and reused for score + ORDER BY
    assert "ORDER BY c.embedding <=>" in sql and "c.section_id IN ($" in sql

    db = FakeSession(rows=[("q1", "d1", "s1", "What is X?", 0.8)])
    qh = await vector.question_search(db, org_id="o1", qvec=q, collection_ids=["a"])
    assert qh[0].question == "What is X?"
    assert "section_questions q JOIN documents d" in _sql(*db.statements[0])

    db = FakeSession(rows=[("d1", 0.7)])
    assert await vector.document_similarity(db, org_id="o1", qvec=q, collection_ids=["a"]) == [("d1", 0.7)]
    assert "d.doc_embedding <=>" in _sql(*db.statements[0])

    db = FakeSession(rows=[("c1", "d1", "s1", 0.5, 0), ("c2", "d1", "s1", 0.9, 1), ("c3", "d1", "s2", None, 0)])
    scoped = await vector.scoped_chunks(db, org_id="o1", qvec=q, section_ids=["s1", "s2"], per_section=1)
    assert [h.chunk_id for h in scoped["s1"]] == ["c2"] and [h.chunk_id for h in scoped["s2"]] == ["c3"]


async def test_llm_listwise_reranker(monkeypatch):
    from app.llm.mock import MockLLM
    from app.rag import rerank

    monkeypatch.setattr(settings, "reranker_provider", "llm")
    passages = ["Office plants are watered on Fridays.", "Parental leave lasts sixteen weeks for caregivers.",
                "The cafeteria opens at noon."]
    scores = await rerank.rerank("how long is parental leave", passages, llm=MockLLM())
    assert len(scores) == 3 and scores[1] == max(scores) and all(0 <= s <= 1 for s in scores)
    assert rerank.reranker_info() == {"provider": "llm", "model": "org LLM (listwise)"}
    assert await rerank.rerank("q", passages, llm=None) is None


async def test_cross_encoder_failure_disables_reranking(monkeypatch):
    from app.rag import rerank

    monkeypatch.setattr(settings, "reranker_provider", "fastembed")
    monkeypatch.setattr(rerank, "_failed", False)

    def boom(query, passages):
        raise RuntimeError("model download failed (offline)")

    monkeypatch.setattr(rerank, "_cross_encode", boom)
    assert rerank.reranker_enabled()
    assert await rerank.rerank("q", ["a", "b"]) is None
    assert not rerank.reranker_enabled()  # don't retry a broken model on every query
    monkeypatch.setattr(settings, "reranker_provider", "none")
    assert await rerank.rerank("q", ["a"]) is None


@pytest.mark.parametrize("text,expected", [("Hello, World!", ["hello", "world"]), ("the of and", []),
                                           ("ünïcode wörds", ["ünïcode", "wörds"])])
def test_query_terms(text, expected):
    from app.rag.textutil import query_terms

    assert query_terms(text) == expected
