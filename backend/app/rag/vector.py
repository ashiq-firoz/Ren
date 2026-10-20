"""Dense retrieval: chunks, synthetic section questions, document embeddings and leaf-scoped chunk ranking.

- PostgreSQL: pgvector cosine distance (`<=>`, HNSW indexes from `init_db`) with org / collection / document /
  section filters (the query vector is bound as text and cast to `vector`).
- SQLite: numpy cosine (dot product of L2-normalized vectors) over float32 matrices cached per org and corpus
  fingerprint (invalidated on every document change).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import sqlalchemy as sa

from app.core.db import IS_POSTGRES
from app.rag.cache import LRU, LoopLocks, corpus_fingerprint, on_invalidate
from app.rag.lexical import Hit


@dataclass
class QuestionHit:
    question_id: str
    document_id: str
    section_id: str
    question: str
    score: float


class _Matrix:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.doc_ids: list[str] = []
        self.sec_ids: list[str | None] = []
        self.extra: list[str | None] = []
        self.coll_codes = np.zeros(0, dtype=np.int32)
        self.doc_codes = np.zeros(0, dtype=np.int32)
        self.sec_codes = np.zeros(0, dtype=np.int32)
        self.coll_index: dict[str, int] = {}
        self.doc_index: dict[str, int] = {}
        self.sec_index: dict[str, int] = {}
        self.M = np.zeros((0, 0), dtype=np.float32)

    @property
    def size(self) -> int:
        return len(self.ids)

    @classmethod
    def build(cls, rows) -> _Matrix:
        """rows: (id, collection_id, document_id, section_id, embedding, extra)."""
        m = cls()
        vecs, cc, dc, sc = [], [], [], []
        for rid, coll, doc, sec, emb, extra in rows:
            if emb is None:
                continue
            v = np.asarray(emb, dtype=np.float32).reshape(-1)
            if v.size == 0:
                continue
            m.ids.append(rid)
            m.doc_ids.append(doc)
            m.sec_ids.append(sec)
            m.extra.append(extra)
            cc.append(m.coll_index.setdefault(coll, len(m.coll_index)))
            dc.append(m.doc_index.setdefault(doc, len(m.doc_index)))
            sc.append(m.sec_index.setdefault(sec or "", len(m.sec_index)))
            vecs.append(v)
        if vecs:
            dim = max(v.shape[0] for v in vecs)
            vecs = [v if v.shape[0] == dim else np.pad(v, (0, dim - v.shape[0])) for v in vecs]
            mat = np.vstack(vecs).astype(np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            m.M = mat / norms
        m.coll_codes = np.asarray(cc, dtype=np.int32)
        m.doc_codes = np.asarray(dc, dtype=np.int32)
        m.sec_codes = np.asarray(sc, dtype=np.int32)
        return m

    def _mask(self, codes, index, wanted):
        if wanted is None:
            return None
        sel = [index[w] for w in wanted if w in index]
        if not sel:
            return np.zeros(len(codes), dtype=bool)
        return np.isin(codes, np.asarray(sel, dtype=np.int32))

    def search(self, q: np.ndarray, *, collection_ids=None, document_ids=None, section_ids=None,
               limit: int = 50) -> list[tuple[int, float]]:
        if self.size == 0 or q is None:
            return []
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.M.shape[1]:
            q = q[: self.M.shape[1]] if q.shape[0] > self.M.shape[1] else np.pad(q, (0, self.M.shape[1] - q.shape[0]))
        sims = self.M @ q
        valid = np.ones(self.size, dtype=bool)
        for m in (self._mask(self.coll_codes, self.coll_index, collection_ids),
                  self._mask(self.doc_codes, self.doc_index, document_ids),
                  self._mask(self.sec_codes, self.sec_index, section_ids)):
            if m is not None:
                valid &= m
        idx = np.nonzero(valid)[0]
        if idx.size == 0:
            return []
        sub = sims[idx]
        k = min(limit, idx.size)
        part = np.argpartition(-sub, k - 1)[:k] if k < idx.size else np.arange(idx.size)
        order = part[np.argsort(-sub[part], kind="stable")]
        return [(int(idx[i]), float(sub[i])) for i in order]


_mats = LRU(maxsize=48)
_locks = LoopLocks()


def _drop(org_id: str | None) -> None:
    if org_id is None:
        _mats.clear()
        return
    for key in _mats.keys():
        if key[1] == org_id:
            _mats.pop(key)


on_invalidate(_drop)


async def _matrix(db, org_id: str, kind: str) -> _Matrix:
    from app.models import Chunk, Document, SectionQuestion

    fp = await corpus_fingerprint(db, org_id)
    key = (kind, org_id)
    entry = _mats.get(key)
    if entry is not None and entry[0] == fp:
        return entry[1]
    async with _locks.get(key):
        entry = _mats.get(key)
        if entry is not None and entry[0] == fp:
            return entry[1]
        if kind == "chunks":
            q = (sa.select(Chunk.id, Chunk.collection_id, Chunk.document_id, Chunk.section_id, Chunk.embedding,
                           sa.null())
                 .join(Document, Document.id == Chunk.document_id)
                 .where(Chunk.org_id == org_id, Document.status == "ready", Chunk.embedding.is_not(None)))
        elif kind == "questions":
            q = (sa.select(SectionQuestion.id, Document.collection_id, SectionQuestion.document_id,
                           SectionQuestion.section_id, SectionQuestion.embedding, SectionQuestion.question)
                 .join(Document, Document.id == SectionQuestion.document_id)
                 .where(SectionQuestion.org_id == org_id, Document.status == "ready",
                        SectionQuestion.embedding.is_not(None)))
        else:  # documents
            q = (sa.select(Document.id, Document.collection_id, Document.id.label("doc_ref"), sa.null(),
                           Document.doc_embedding, Document.title)
                 .where(Document.org_id == org_id, Document.status == "ready", Document.doc_embedding.is_not(None)))
        rows = (await db.execute(q)).all()
        mat = await asyncio.to_thread(_Matrix.build, [tuple(r) for r in rows])
        _mats.set(key, (fp, mat))
        return mat


def _vec_literal(q: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in np.asarray(q, dtype=np.float32).reshape(-1)) + "]"


def _pg_filters(pairs) -> tuple[list[str], dict, list] | None:
    filters, params, expanding = [], {}, []
    for col, name, vals in pairs:
        if vals is None:
            continue
        if not vals:
            return None
        filters.append(f"AND {col} IN :{name}")
        params[name] = list(vals)
        expanding.append(sa.bindparam(name, expanding=True))
    return filters, params, expanding


_QV = "CAST(CAST(:qv AS text) AS vector)"


async def vector_search(db, *, org_id: str, qvec: np.ndarray, collection_ids: list[str] | None = None,
                        document_ids: list[str] | None = None, section_ids: list[str] | None = None,
                        limit: int = 50) -> list[Hit]:
    """Top chunks by cosine similarity to `qvec` (best first)."""
    if qvec is None:
        return []
    if IS_POSTGRES:
        f = _pg_filters((("c.collection_id", "cols", collection_ids), ("c.document_id", "docs", document_ids),
                         ("c.section_id", "secs", section_ids)))
        if f is None:
            return []
        filters, params, expanding = f
        sql = sa.text(
            f"SELECT c.id, c.document_id, c.section_id, 1 - (c.embedding <=> {_QV}) AS score "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.org_id = :org AND d.status = 'ready' AND c.embedding IS NOT NULL " + " ".join(filters) +
            f" ORDER BY c.embedding <=> {_QV} LIMIT :lim")
        if expanding:
            sql = sql.bindparams(*expanding)
        rows = (await db.execute(sql, {**params, "org": org_id, "qv": _vec_literal(qvec), "lim": int(limit)})).all()
        return [Hit(r[0], r[1], r[2], float(r[3] or 0.0)) for r in rows]
    mat = await _matrix(db, org_id, "chunks")
    res = mat.search(qvec, collection_ids=collection_ids, document_ids=document_ids, section_ids=section_ids,
                     limit=limit)
    return [Hit(mat.ids[i], mat.doc_ids[i], mat.sec_ids[i], s) for i, s in res]


async def question_search(db, *, org_id: str, qvec: np.ndarray, collection_ids: list[str] | None = None,
                          document_ids: list[str] | None = None, limit: int = 40) -> list[QuestionHit]:
    """Synthetic questions most similar to the query (doc2query-style signal)."""
    if qvec is None:
        return []
    if IS_POSTGRES:
        f = _pg_filters((("d.collection_id", "cols", collection_ids), ("q.document_id", "docs", document_ids)))
        if f is None:
            return []
        filters, params, expanding = f
        sql = sa.text(
            f"SELECT q.id, q.document_id, q.section_id, q.question, 1 - (q.embedding <=> {_QV}) AS score "
            "FROM section_questions q JOIN documents d ON d.id = q.document_id "
            "WHERE q.org_id = :org AND d.status = 'ready' AND q.embedding IS NOT NULL " + " ".join(filters) +
            f" ORDER BY q.embedding <=> {_QV} LIMIT :lim")
        if expanding:
            sql = sql.bindparams(*expanding)
        rows = (await db.execute(sql, {**params, "org": org_id, "qv": _vec_literal(qvec), "lim": int(limit)})).all()
        return [QuestionHit(r[0], r[1], r[2], r[3], float(r[4] or 0.0)) for r in rows]
    mat = await _matrix(db, org_id, "questions")
    res = mat.search(qvec, collection_ids=collection_ids, document_ids=document_ids, limit=limit)
    return [QuestionHit(mat.ids[i], mat.doc_ids[i], mat.sec_ids[i] or "", mat.extra[i] or "", s) for i, s in res]


async def document_similarity(db, *, org_id: str, qvec: np.ndarray, collection_ids: list[str] | None = None,
                              document_ids: list[str] | None = None, limit: int = 20) -> list[tuple[str, float]]:
    """Documents ranked by similarity of their doc-level embedding (title + summary + top-level ToC)."""
    if qvec is None:
        return []
    if IS_POSTGRES:
        f = _pg_filters((("d.collection_id", "cols", collection_ids), ("d.id", "docs", document_ids)))
        if f is None:
            return []
        filters, params, expanding = f
        sql = sa.text(
            f"SELECT d.id, 1 - (d.doc_embedding <=> {_QV}) AS score FROM documents d "
            "WHERE d.org_id = :org AND d.status = 'ready' AND d.doc_embedding IS NOT NULL " + " ".join(filters) +
            f" ORDER BY d.doc_embedding <=> {_QV} LIMIT :lim")
        if expanding:
            sql = sql.bindparams(*expanding)
        rows = (await db.execute(sql, {**params, "org": org_id, "qv": _vec_literal(qvec), "lim": int(limit)})).all()
        return [(r[0], float(r[1] or 0.0)) for r in rows]
    mat = await _matrix(db, org_id, "documents")
    res = mat.search(qvec, collection_ids=collection_ids, document_ids=document_ids, limit=limit)
    return [(mat.ids[i], s) for i, s in res]


async def scoped_chunks(db, *, org_id: str, qvec: np.ndarray | None, section_ids: list[str],
                        per_section: int = 3) -> dict[str, list[Hit]]:
    """Best chunks inside each given section (leaf), ranked by similarity to the query (or document order when
    no query vector is available)."""
    from app.models import Chunk

    out: dict[str, list[Hit]] = defaultdict(list)
    section_ids = [s for s in dict.fromkeys(section_ids) if s]
    if not section_ids:
        return {}
    if qvec is not None and not IS_POSTGRES:
        mat = await _matrix(db, org_id, "chunks")
        res = mat.search(qvec, section_ids=section_ids, limit=per_section * len(section_ids) * 4 + 10)
        for i, s in res:
            sec = mat.sec_ids[i]
            if sec and len(out[sec]) < per_section:
                out[sec].append(Hit(mat.ids[i], mat.doc_ids[i], sec, s))
        missing = [s for s in section_ids if s not in out]
        if not missing:
            return dict(out)
        section_ids = missing
    if qvec is not None and IS_POSTGRES:
        sql = sa.text(
            f"SELECT c.id, c.document_id, c.section_id, 1 - (c.embedding <=> {_QV}) AS score, c.ordinal "
            "FROM chunks c WHERE c.org_id = :org AND c.section_id IN :secs"
        ).bindparams(sa.bindparam("secs", expanding=True))
        rows = (await db.execute(sql, {"org": org_id, "secs": section_ids, "qv": _vec_literal(qvec)})).all()
        rows = sorted(rows, key=lambda r: (-(float(r[3]) if r[3] is not None else -1.0), r[4]))
    else:
        rows = (await db.execute(
            sa.select(Chunk.id, Chunk.document_id, Chunk.section_id, sa.literal(0.0), Chunk.ordinal)
            .where(Chunk.org_id == org_id, Chunk.section_id.in_(section_ids))
            .order_by(Chunk.ordinal)
        )).all()
    for r in rows:
        sec = r[2]
        if len(out[sec]) < per_section:
            out[sec].append(Hit(r[0], r[1], sec, float(r[3] or 0.0)))
    return dict(out)
