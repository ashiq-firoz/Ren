"""Lexical retrieval over chunk `search_text` (breadcrumb + content).

- PostgreSQL: `ts_rank_cd` over the generated `chunks.tsv` column (created in `app.core.db.init_db`) with an
  OR-tsquery built from sanitized query terms (only `[a-z0-9]` words reach the tsquery — no syntax injection).
- SQLite: in-process Okapi BM25 (k1=1.5, b=0.75) with stopwords + light stemming; the index is built per org,
  cached by corpus fingerprint and invalidated whenever documents change.
"""
from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
import sqlalchemy as sa

from app.core.db import IS_POSTGRES
from app.rag.cache import LRU, LoopLocks, corpus_fingerprint, on_invalidate
from app.rag.textutil import query_terms, tokenize


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    section_id: str | None
    score: float


class BM25Index:
    """Immutable in-memory BM25 index with numpy posting lists and collection/document/section filters."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.ids: list[str] = []
        self.doc_ids: list[str] = []
        self.sec_ids: list[str | None] = []
        self.coll_codes = np.zeros(0, dtype=np.int32)
        self.doc_codes = np.zeros(0, dtype=np.int32)
        self.sec_codes = np.zeros(0, dtype=np.int32)
        self.coll_index: dict[str, int] = {}
        self.doc_index: dict[str, int] = {}
        self.sec_index: dict[str, int] = {}
        self.postings: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.dl = np.zeros(0, dtype=np.float32)
        self.avgdl = 1.0

    @property
    def size(self) -> int:
        return len(self.ids)

    @classmethod
    def build(cls, rows, k1: float = 1.5, b: float = 0.75) -> BM25Index:
        """rows: iterable of (chunk_id, collection_id, document_id, section_id, text)."""
        idx = cls(k1, b)
        post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        coll_codes, doc_codes, sec_codes, lengths = [], [], [], []
        for i, (cid, coll, doc, sec, text) in enumerate(rows):
            idx.ids.append(cid)
            idx.doc_ids.append(doc)
            idx.sec_ids.append(sec)
            coll_codes.append(idx.coll_index.setdefault(coll, len(idx.coll_index)))
            doc_codes.append(idx.doc_index.setdefault(doc, len(idx.doc_index)))
            sec_codes.append(idx.sec_index.setdefault(sec or "", len(idx.sec_index)))
            toks = tokenize(text or "")
            lengths.append(len(toks))
            for t, c in Counter(toks).items():
                post[t].append((i, c))
        idx.coll_codes = np.asarray(coll_codes, dtype=np.int32)
        idx.doc_codes = np.asarray(doc_codes, dtype=np.int32)
        idx.sec_codes = np.asarray(sec_codes, dtype=np.int32)
        idx.dl = np.asarray(lengths, dtype=np.float32)
        idx.avgdl = float(idx.dl.mean()) if len(lengths) and idx.dl.mean() > 0 else 1.0
        for t, plist in post.items():
            arr = np.asarray(plist, dtype=np.int64)
            idx.postings[t] = (arr[:, 0].astype(np.int32), arr[:, 1].astype(np.float32))
        return idx

    def _mask(self, codes: np.ndarray, index: dict[str, int], wanted: list[str] | None) -> np.ndarray | None:
        if wanted is None:
            return None
        sel = [index[w] for w in wanted if w in index]
        if not sel:
            return np.zeros(len(codes), dtype=bool)
        return np.isin(codes, np.asarray(sel, dtype=np.int32))

    def scores(self, query: str) -> np.ndarray:
        n = self.size
        out = np.zeros(n, dtype=np.float32)
        if n == 0:
            return out
        for t in set(tokenize(query)):
            p = self.postings.get(t)
            if p is None:
                continue
            ix, tf = p
            df = len(ix)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            denom = tf + self.k1 * (1.0 - self.b + self.b * self.dl[ix] / self.avgdl)
            out[ix] += idf * tf * (self.k1 + 1.0) / denom
        return out

    def search(self, query: str, *, collection_ids: list[str] | None = None, document_ids: list[str] | None = None,
               section_ids: list[str] | None = None, limit: int = 50) -> list[Hit]:
        if self.size == 0:
            return []
        s = self.scores(query)
        for m in (self._mask(self.coll_codes, self.coll_index, collection_ids),
                  self._mask(self.doc_codes, self.doc_index, document_ids),
                  self._mask(self.sec_codes, self.sec_index, section_ids)):
            if m is not None:
                s = np.where(m, s, 0.0)
        nz = np.nonzero(s > 0)[0]
        if nz.size == 0:
            return []
        top = nz[np.argsort(-s[nz], kind="stable")[:limit]]
        return [Hit(self.ids[i], self.doc_ids[i], self.sec_ids[i], float(s[i])) for i in top]


_indexes = LRU(maxsize=16)
_locks = LoopLocks()
on_invalidate(lambda org_id: _indexes.pop(org_id) if org_id else _indexes.clear())


async def _sqlite_index(db, org_id: str) -> BM25Index:
    from app.models import Chunk, Document

    fp = await corpus_fingerprint(db, org_id)
    entry = _indexes.get(org_id)
    if entry is not None and entry[0] == fp:
        return entry[1]
    async with _locks.get(("bm25", org_id)):
        entry = _indexes.get(org_id)
        if entry is not None and entry[0] == fp:
            return entry[1]
        rows = (await db.execute(
            sa.select(Chunk.id, Chunk.collection_id, Chunk.document_id, Chunk.section_id, Chunk.search_text)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.org_id == org_id, Document.status == "ready")
            .order_by(Chunk.document_id, Chunk.ordinal)
        )).all()
        index = await asyncio.to_thread(BM25Index.build, [tuple(r) for r in rows])
        _indexes.set(org_id, (fp, index))
        return index


async def _pg_search(db, org_id: str, query: str, collection_ids, document_ids, section_ids, limit: int) -> list[Hit]:
    terms = query_terms(query)
    if not terms:
        return []
    filters, params, expanding = [], {"org": org_id, "tsq": " | ".join(terms), "lim": int(limit)}, []
    for col, name, vals in (("c.collection_id", "cols", collection_ids), ("c.document_id", "docs", document_ids),
                            ("c.section_id", "secs", section_ids)):
        if vals is not None:
            if not vals:
                return []
            filters.append(f"AND {col} IN :{name}")
            params[name] = list(vals)
            expanding.append(sa.bindparam(name, expanding=True))
    sql = sa.text(
        "SELECT c.id, c.document_id, c.section_id, ts_rank_cd(c.tsv, q) AS score "
        "FROM chunks c JOIN documents d ON d.id = c.document_id, to_tsquery('english', :tsq) q "
        "WHERE c.org_id = :org AND d.status = 'ready' AND c.tsv @@ q " + " ".join(filters) +
        " ORDER BY score DESC LIMIT :lim"
    )
    if expanding:
        sql = sql.bindparams(*expanding)
    rows = (await db.execute(sql, params)).all()
    return [Hit(r[0], r[1], r[2], float(r[3] or 0.0)) for r in rows]


async def lexical_search(db, *, org_id: str, query: str, collection_ids: list[str] | None = None,
                         document_ids: list[str] | None = None, section_ids: list[str] | None = None,
                         limit: int = 50) -> list[Hit]:
    """Top chunks by lexical relevance (best first)."""
    if not (query or "").strip():
        return []
    if IS_POSTGRES:
        return await _pg_search(db, org_id, query, collection_ids, document_ids, section_ids, limit)
    index = await _sqlite_index(db, org_id)
    return index.search(query, collection_ids=collection_ids, document_ids=document_ids, section_ids=section_ids,
                        limit=limit)
