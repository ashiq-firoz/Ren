"""Knowledge service — STAIR-style structure-aware hybrid RAG.

    Query ─┬─ BM25 (chunks) ──────────────┐
           ├─ Vector (chunks) ─────────────┤
           ├─ Synthetic-question match ────┼─► RRF fusion ─► scoped boost (chunks in STAIR-selected leaves)
           └─ STAIR: query + ToC ─► constrained leaf selection ┘     ─► rerank ─► context builder ─► grounded answer

Public API (used by routes, agent tools, chat attachments):
    ensure_default_collections, accessible_collection_ids, create_document_from_bytes, schedule_ingestion,
    ingest_document, search, answer, get_toc, get_section, export_training_data, stats, delete_document
Ingestion: parsing → structuring → embedding → questions → ready (or failed), persisted and emitted as
`document.updated` events; re-indexing is idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.core.config import settings
from app.core.db import SessionLocal, new_id, session_scope, utcnow
from app.core.events import bus
from app.models import Chunk, Collection, Document, Organization, Section, SectionQuestion, User
from app.rag import cache, lexical, stair, vector
from app.rag.chunker import chunk_leaves
from app.rag.context_builder import DEFAULT_BUDGET_TOKENS, EXPAND_LEAF_TOKENS, build_context, used_citations
from app.rag.embeddings import embed_query, embed_texts, embedding_info
from app.rag.fusion import max_score, rrf, scoped_boost, source_ranks
from app.rag.llmutil import UsageMeter, rag_llm
from app.rag.parsers import ParseError, default_title, guess_mime, parse_file
from app.rag.questions import export_training_data as _export_training_data
from app.rag.questions import generate_questions
from app.rag.rerank import rerank as rerank_passages
from app.rag.rerank import reranker_enabled, reranker_info
from app.rag.structure import StructureResult, build_structure, document_summary
from app.services.serialize import DEFAULT_ORG_SETTINGS, collection_out, deep_merge, document_out

log = logging.getLogger("ren.rag")

GENERAL_COLLECTION = "General"
CHAT_UPLOADS_COLLECTION = "Chat Uploads"
MODES = ("hybrid", "semantic", "lexical", "structure")
MAX_CONCURRENT_INGESTIONS = 2
MIN_DENSE_SCORE = 0.05  # cosine below this is noise (never reached by real embedding models for related text)
NO_ANSWER = ("I couldn't find information about this in the knowledge base. Try rephrasing the question, or "
             "upload the document that covers it.")
ANSWER_SYSTEM = (
    "You are Ren's knowledge assistant. Answer the question using only the numbered sources provided.\n"
    "Rules:\n"
    "- Support every factual statement with the sources and cite them inline with their numbers in square "
    "brackets right after the statement, e.g. [1] or [2][3].\n"
    "- If the sources do not contain the answer, say plainly that the knowledge base does not cover it (you may "
    "mention related information that is there). Never guess and never use outside knowledge.\n"
    "- If sources conflict, say so and cite each of them.\n"
    "- Be concise and specific: keep exact figures, dates, names, conditions and exceptions from the sources.\n"
    "- Answer in the language of the question."
)

_tasks: set[asyncio.Task] = set()
_pending_dirs: dict[str, Path] = {}
_inflight: dict[str, asyncio.AbstractEventLoop] = {}  # document id → loop running its ingestion
_rerun: set[str] = set()
_sem_state: dict[str, Any] = {}
_locks = cache.LoopLocks()


# =========================================================================== settings
def retrieval_config(org: Any) -> dict:
    s = deep_merge(DEFAULT_ORG_SETTINGS, (org.settings or {}) if org is not None else {})
    r = dict(s.get("retrieval") or {})

    def _int(key: str, default: int) -> int:
        try:
            return int(r.get(key) or default)
        except (TypeError, ValueError):
            return default

    return {
        "stair_enabled": bool(r.get("stair_enabled", True)),
        "generate_questions": bool(r.get("generate_questions", True)),
        "structure_augmentation": bool(r.get("structure_augmentation", True)),
        "rerank": bool(r.get("rerank", True)),
        "top_k": max(1, min(_int("top_k", 8), 50)),
        "leaf_summaries": bool(r.get("leaf_summaries", False)),
        "stair_model": r.get("stair_model") or None,
        "stair_provider_id": r.get("stair_provider_id") or None,
        "stair_prompt": str(r.get("stair_prompt") or "auto"),
        "stair_max_docs": max(1, min(_int("stair_max_docs", 4), 12)),
        "stair_toc_budget": max(800, _int("stair_toc_budget", stair.DEFAULT_BUDGET_TOKENS)),
        "stair_k": max(1, min(_int("stair_k", 3), 10)),
    }


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


# =========================================================================== collections & access
async def ensure_default_collections(db, org_id: str) -> None:
    """Create "General" (org access) and the system collection "Chat Uploads" if missing."""
    async with _locks.get(("collections", org_id)):
        rows = (await db.execute(
            sa.select(Collection.id, Collection.name, Collection.system)
            .where(Collection.org_id == org_id).order_by(Collection.created_at))).all()
        generals = [cid for cid, name, system in rows if not system and name == GENERAL_COLLECTION]
        systems = [cid for cid, _, system in rows if system]
        if not generals:
            db.add(Collection(org_id=org_id, name=GENERAL_COLLECTION, access="org", member_ids=[], system=False,
                              description="Shared knowledge for everyone in the workspace."))
        if not systems:
            db.add(Collection(org_id=org_id, name=CHAT_UPLOADS_COLLECTION, access="org", member_ids=[], system=True,
                              description="Files attached in chat conversations."))
        # self-heal duplicates created by concurrent first requests: drop newer *empty* copies
        for dupes in (generals[1:], systems[1:]):
            for cid in dupes:
                has_docs = (await db.execute(
                    sa.select(Document.id).where(Document.collection_id == cid).limit(1))).first()
                if has_docs is None:
                    await db.execute(sa.delete(Collection).where(Collection.id == cid))
        await db.flush()


async def get_default_collection(db, org_id: str, *, system: bool = False) -> Collection:
    """The "General" collection (or the system "Chat Uploads" collection with `system=True`)."""
    await ensure_default_collections(db, org_id)
    q = sa.select(Collection).where(Collection.org_id == org_id, Collection.system.is_(system))
    if not system:
        q = q.where(Collection.name == GENERAL_COLLECTION)
    return (await db.execute(q.order_by(Collection.created_at))).scalars().first()


async def accessible_collection_ids(db, org_id: str, user_id: str | None, role: str,
                                    agent_collection_ids: list[str] | None = None) -> list[str]:
    """Org-access collections + restricted ones listing the user (admins/owners: all), ∩ the agent's scope."""
    rows = (await db.execute(
        sa.select(Collection.id, Collection.access, Collection.member_ids)
        .where(Collection.org_id == org_id).order_by(Collection.created_at))).all()
    is_admin = role in ("admin", "owner")
    ids = [cid for cid, access, members in rows
           if is_admin or access != "restricted" or (user_id is not None and user_id in (members or []))]
    if agent_collection_ids:
        scope = set(agent_collection_ids)
        ids = [c for c in ids if c in scope]
    return ids


# =========================================================================== serialization helpers
async def documents_out(db, docs: list[Document]) -> list[dict]:
    coll_ids = {d.collection_id for d in docs}
    user_ids = {d.uploaded_by for d in docs if d.uploaded_by}
    names: dict[str, str] = {}
    users: dict[str, str] = {}
    if coll_ids:
        names = dict((await db.execute(sa.select(Collection.id, Collection.name)
                                       .where(Collection.id.in_(coll_ids)))).all())
    if user_ids:
        users = dict((await db.execute(sa.select(User.id, User.name).where(User.id.in_(user_ids)))).all())
    return [document_out(d, names.get(d.collection_id), users.get(d.uploaded_by)) for d in docs]


async def document_dict(db, doc: Document) -> dict:
    return (await documents_out(db, [doc]))[0]


async def collections_out(db, collections: list[Collection]) -> list[dict]:
    ids = [c.id for c in collections]
    counts: dict[str, int] = {}
    if ids:
        counts = dict((await db.execute(
            sa.select(Document.collection_id, sa.func.count(Document.id))
            .where(Document.collection_id.in_(ids)).group_by(Document.collection_id))).all())
    return [collection_out(c, int(counts.get(c.id, 0))) for c in collections]


async def _update_doc(document_id: str, *, meta_update: dict | None = None, **fields: Any) -> dict | None:
    """Persist document fields in their own transaction and emit `document.updated`."""
    try:
        async with session_scope() as db:
            doc = await db.get(Document, document_id)
            if doc is None:
                return None
            for k, v in fields.items():
                setattr(doc, k, v)
            if meta_update:
                doc.meta = {**(doc.meta or {}), **meta_update}
            doc.updated_at = utcnow()
            await db.flush()
            out = await document_dict(db, doc)
            org_id = doc.org_id
    except Exception:  # noqa: BLE001
        log.exception("could not update document %s", document_id)
        return None
    await bus.emit(org_id, "document.updated", {"document": out}, user_id=None)
    return out


# =========================================================================== upload & ingestion
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = _UNSAFE_RE.sub("_", base).strip(" .")
    if not base:
        return "document"
    p = Path(base)
    stem, ext = p.stem[:160] or "document", p.suffix[:16]
    return f"{stem}{ext}"


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


async def create_document_from_bytes(db, *, org_id: str, user_id: str | None, collection_id: str | None,
                                     filename: str, data: bytes, mime: str | None = None) -> Document:
    """Store the file under `uploads_dir/<org>/<doc_id>/<filename>`, create the row (status `uploaded`) and
    schedule background ingestion (it starts once the caller's transaction has committed)."""
    if not collection_id:
        collection_id = (await get_default_collection(db, org_id)).id
    fname = safe_filename(filename)
    doc_id = new_id()
    folder = settings.uploads_dir / org_id / doc_id
    path = folder / fname
    await asyncio.to_thread(_write_file, path, data)
    doc = Document(id=doc_id, org_id=org_id, collection_id=collection_id, title=default_title(fname),
                   filename=fname, mime=guess_mime(fname, mime), size=len(data), storage_path=str(path),
                   status="uploaded", progress=0, uploaded_by=user_id, meta={})
    db.add(doc)
    await db.flush()
    _pending_dirs[doc_id] = folder
    schedule_ingestion(doc_id)
    return doc


def schedule_ingestion(document_id: str) -> None:
    """Run `ingest_document` in a background task once the document row is committed (never inside the
    request transaction). At most `MAX_CONCURRENT_INGESTIONS` ingestions run at the same time."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        import threading

        threading.Thread(target=lambda: asyncio.run(_ingest_when_visible(document_id)), daemon=True).start()
        return

    def _start() -> None:
        task = loop.create_task(_ingest_when_visible(document_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    loop.call_soon(_start)


async def _ingest_when_visible(document_id: str, timeout: float = 30.0) -> None:
    await asyncio.sleep(0.05)
    deadline = time.monotonic() + timeout
    while True:
        try:
            async with SessionLocal() as s:
                found = (await s.execute(sa.select(Document.id).where(Document.id == document_id))).first()
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            break
        if time.monotonic() > deadline:
            log.warning("document %s never became visible (transaction rolled back?) — skipping ingestion",
                        document_id)
            folder = _pending_dirs.pop(document_id, None)
            if folder is not None:
                await asyncio.to_thread(shutil.rmtree, folder, True)
            return
        await asyncio.sleep(0.2)
    _pending_dirs.pop(document_id, None)
    await ingest_document(document_id)


def _ingest_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if _sem_state.get("loop") is not loop:
        _sem_state["loop"] = loop
        _sem_state["sem"] = asyncio.Semaphore(MAX_CONCURRENT_INGESTIONS)
    return _sem_state["sem"]


async def ingest_document(document_id: str) -> None:
    """Full ingestion pipeline (idempotent re-index). Never raises: failures mark the document `failed`."""
    loop = asyncio.get_running_loop()
    running = _inflight.get(document_id)
    if running is not None and running is loop and not running.is_closed():
        _rerun.add(document_id)  # a re-index was requested while running: run again afterwards
        return
    _inflight[document_id] = loop
    try:
        while True:
            _rerun.discard(document_id)
            async with _ingest_semaphore():
                await _ingest_once(document_id)
            if document_id not in _rerun:
                break
    finally:
        if _inflight.get(document_id) is loop:
            _inflight.pop(document_id, None)


def _assign_stable_ids(nodes: list, document_id: str) -> None:
    """Deterministic section ids (document + path + title): re-indexing an unchanged document keeps the ids that
    citations stored elsewhere (chat messages, tasks) point to."""
    import uuid

    try:
        ns = uuid.UUID(document_id)
    except ValueError:
        ns = uuid.uuid5(uuid.NAMESPACE_URL, f"ren-document:{document_id}")
    id_map = {n.id: str(uuid.uuid5(ns, f"{n.path}\x1f{n.title}")) for n in nodes}
    for n in nodes:
        n.id = id_map[n.id]
        n.parent_id = id_map.get(n.parent_id) if n.parent_id else None


def _doc_repr(structure: StructureResult, summary: str) -> str:
    tops = [n.title for n in structure.root.children if n.kind != "overview"][:40]
    seconds = [c.title for n in structure.root.children for c in n.children if c.kind != "overview"][:40]
    parts = [structure.title, summary or "", "Sections: " + "; ".join(tops)]
    if seconds:
        parts.append("Subsections: " + "; ".join(seconds))
    return "\n".join(p for p in parts if p)


async def _embed_with_progress(document_id: str, texts: list[str], start: int, end: int) -> list:
    out: list = []
    batch = 256 if embedding_info()["provider"] == "hash" else 64
    last = time.monotonic()
    for i in range(0, len(texts), batch):
        out.extend(await embed_texts(texts[i: i + batch]))
        if time.monotonic() - last > 1.0 and i + batch < len(texts):
            await _update_doc(document_id, progress=start + int((end - start) * (i + batch) / len(texts)))
            last = time.monotonic()
    return out


async def _delete_index_rows(db, document_id: str) -> None:
    await db.execute(sa.delete(SectionQuestion).where(SectionQuestion.document_id == document_id))
    await db.execute(sa.delete(Chunk).where(Chunk.document_id == document_id))
    await db.execute(sa.delete(Section).where(Section.document_id == document_id))


async def _insert_batches(db, model, rows: list[dict], size: int = 400) -> None:
    for i in range(0, len(rows), size):
        await db.execute(sa.insert(model), rows[i: i + size])


async def _ingest_once(document_id: str) -> None:
    t0 = time.perf_counter()
    async with session_scope() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return
        org = await db.get(Organization, doc.org_id)
        cfg = retrieval_config(org)
        org_id, collection_id = doc.org_id, doc.collection_id
        path, filename, mime, current_title = doc.storage_path, doc.filename, doc.mime, doc.title
    meter = UsageMeter()
    try:
        await _update_doc(document_id, status="parsing", progress=5, error=None)
        parsed = await asyncio.to_thread(parse_file, Path(path), filename, mime)
        await _update_doc(document_id, status="structuring", progress=20, page_count=parsed.page_count)

        llm = await rag_llm(org_id)
        user_title = current_title if current_title and current_title != default_title(filename) else None
        structure = await build_structure(parsed, title=user_title, llm=llm,
                                          augment=cfg["structure_augmentation"], summaries=cfg["leaf_summaries"],
                                          meter=meter)
        nodes, leaves = structure.nodes(), structure.leaves()
        if not leaves:
            raise ParseError("No text content found in the document.")
        _assign_stable_ids(nodes, document_id)
        drafts = chunk_leaves(leaves)
        summary = await document_summary(llm, structure.title, structure.root, meter)

        await _update_doc(document_id, status="embedding", progress=35)
        vectors = await _embed_with_progress(document_id, [d.search_text for d in drafts], 35, 70)
        doc_vec = (await embed_texts([_doc_repr(structure, summary)]))[0]

        section_rows = [{
            "id": n.id, "org_id": org_id, "document_id": document_id, "parent_id": n.parent_id,
            "path": n.path[:100], "title": n.title[:500], "level": n.depth, "ordinal": n.ordinal,
            "is_leaf": n.is_leaf, "content": n.content if n.is_leaf else "", "summary": n.summary,
            "breadcrumb": n.breadcrumb, "token_count": n.token_count, "page_start": n.page_start,
            "page_end": n.page_end,
        } for n in nodes]
        chunk_rows = [{
            "id": new_id(), "org_id": org_id, "collection_id": collection_id, "document_id": document_id,
            "section_id": d.section_id, "ordinal": d.ordinal, "content": d.content, "search_text": d.search_text,
            "token_count": d.token_count, "embedding": v, "meta": d.meta,
        } for d, v in zip(drafts, vectors)]
        async with session_scope() as db:
            doc = await db.get(Document, document_id)
            if doc is None:
                return  # deleted while processing
            await _delete_index_rows(db, document_id)
            await _insert_batches(db, Section, section_rows)
            await _insert_batches(db, Chunk, chunk_rows)
            if user_title is None:
                doc.title = structure.title[:500]
            doc.page_count = parsed.page_count
            doc.section_count = len(nodes)
            doc.leaf_count = len(leaves)
            doc.chunk_count = len(chunk_rows)
            doc.question_count = 0
            doc.structure_source = structure.structure_source
            doc.summary = summary
            doc.doc_embedding = doc_vec
            doc.meta = {**(doc.meta or {}), "format": parsed.format,
                        "parser": parsed.meta.get("parser", parsed.format),
                        "native_toc_entries": len(parsed.native_toc or []),
                        "embedding_model": embedding_info()["model"]}
            doc.progress = 70
            doc.updated_at = utcnow()
        cache.invalidate(org_id)

        q_count = 0
        if cfg["generate_questions"] and llm is not None:
            await _update_doc(document_id, status="questions", progress=72)
            last = [time.monotonic()]

            async def progress(done: int, total: int) -> None:
                if time.monotonic() - last[0] > 1.0 and done < total:
                    last[0] = time.monotonic()
                    await _update_doc(document_id, progress=72 + int(23 * done / max(1, total)))

            qmap = await generate_questions(llm, structure.title, leaves, meter=meter, progress=progress)
            pairs = [(sid, q) for sid, qs in qmap.items() for q in qs]
            if pairs:
                qvecs = await embed_texts([q for _, q in pairs])
                async with session_scope() as db:
                    if await db.get(Document, document_id) is None:
                        return
                    await db.execute(sa.delete(SectionQuestion).where(SectionQuestion.document_id == document_id))
                    await _insert_batches(db, SectionQuestion, [
                        {"id": new_id(), "org_id": org_id, "document_id": document_id, "section_id": sid,
                         "question": q, "embedding": v} for (sid, q), v in zip(pairs, qvecs)])
                q_count = len(pairs)
        await _update_doc(document_id, status="ready", progress=100, error=None, question_count=q_count,
                          meta_update={"ingest_ms": int((time.perf_counter() - t0) * 1000),
                                       "llm_tokens": meter.total_tokens})
    except ParseError as e:
        await _update_doc(document_id, status="failed", progress=0, error=str(e)[:1000])
    except Exception as e:  # noqa: BLE001
        log.exception("ingestion failed for document %s", document_id)
        await _update_doc(document_id, status="failed", progress=0,
                          error=f"Ingestion failed: {type(e).__name__}: {str(e)[:400]}")
    finally:
        cache.invalidate(org_id)
        await meter.flush(org_id)


async def resume_pending_ingestions() -> int:
    """Re-schedule documents left mid-ingestion (e.g. by a restart). Call once at application startup."""
    async with session_scope() as db:
        ids = (await db.execute(sa.select(Document.id).where(
            Document.status.in_(("uploaded", "parsing", "structuring", "embedding", "questions"))))).scalars().all()
    for doc_id in ids:
        schedule_ingestion(doc_id)
    return len(ids)


async def delete_document(db, document_id: str) -> None:
    """Delete a document's rows and stored file, and invalidate retrieval caches."""
    doc = await db.get(Document, document_id)
    if doc is None:
        return
    org_id, storage = doc.org_id, doc.storage_path
    await _delete_index_rows(db, document_id)
    await db.delete(doc)
    await db.flush()
    try:
        folder = Path(storage).parent
        root = settings.uploads_dir.resolve()
        if folder.resolve().is_relative_to(root) and folder.resolve() != root:
            await asyncio.to_thread(shutil.rmtree, folder, True)
    except Exception:  # noqa: BLE001
        log.warning("could not remove stored file for %s", document_id, exc_info=True)
    cache.invalidate(org_id)


# =========================================================================== retrieval
@dataclass
class _Doc:
    id: str
    title: str
    collection_id: str
    updated_at: Any
    leaf_count: int


async def _candidate_documents(db, org_id: str, collection_ids: list[str], document_ids: list[str] | None
                               ) -> list[_Doc]:
    q = sa.select(Document.id, Document.title, Document.collection_id, Document.updated_at, Document.leaf_count).where(
        Document.org_id == org_id, Document.status == "ready", Document.collection_id.in_(collection_ids))
    if document_ids:
        q = q.where(Document.id.in_(list(document_ids)))
    rows = (await db.execute(q.order_by(Document.created_at.desc()))).all()
    return [_Doc(*r) for r in rows]


async def _route_documents(db, org_id: str, docs: list[_Doc], allowed: list[str], doc_filter: list[str] | None,
                           qvec, hit_lists: list[tuple[list, float]], max_docs: int) -> list[_Doc]:
    """Candidate documents for STAIR: RRF of per-document chunk hits (BM25 / vector / questions, top 3 per doc)
    and doc-level embedding similarity."""
    score: dict[str, float] = defaultdict(float)
    for hits, w in hit_lists:
        per_doc: dict[str, int] = defaultdict(int)
        for rank, h in enumerate(hits, start=1):
            if per_doc[h.document_id] >= 3:
                continue
            per_doc[h.document_id] += 1
            score[h.document_id] += w / (60 + rank)
    if qvec is not None and len(docs) > 1:
        sims = await vector.document_similarity(db, org_id=org_id, qvec=qvec, collection_ids=allowed,
                                                document_ids=doc_filter, limit=max(20, max_docs * 5))
        for rank, (did, _) in enumerate(sims, start=1):
            score[did] += 1.0 / (60 + rank)
    order = {d.id: i for i, d in enumerate(docs)}
    ranked = sorted(docs, key=lambda d: (-score.get(d.id, 0.0), order[d.id]))
    return ranked[:max_docs]


async def _stair_llm(db, org_id: str, cfg: dict):
    from app.llm.client import NoProviderError
    from app.llm.providers import get_llm

    pid, model = cfg["stair_provider_id"], cfg["stair_model"]
    dedicated = bool(pid or model)
    try:
        llm = await get_llm(db, org_id, provider_id=pid, model=model, purpose="rag")
    except NoProviderError:
        return None, False
    except Exception:  # noqa: BLE001
        log.exception("could not resolve the STAIR retriever model")
        return None, False
    prompt = cfg["stair_prompt"]
    return llm, prompt == "finetuned" or (prompt == "auto" and dedicated)


async def _load_chunks(db, chunk_ids: list[str]) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    rows = (await db.execute(
        sa.select(Chunk.id, Chunk.document_id, Chunk.section_id, Chunk.content, Chunk.search_text, Chunk.ordinal,
                  Chunk.meta, Section.path, Section.title, Section.breadcrumb, Document.title)
        .outerjoin(Section, Section.id == Chunk.section_id)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(chunk_ids))
    )).all()
    out = {}
    for r in rows:
        meta = r[6] or {}
        out[r[0]] = {"document_id": r[1], "section_id": r[2], "content": r[3], "search_text": r[4] or r[3],
                     "ordinal": r[5], "page_start": meta.get("page_start"), "page_end": meta.get("page_end"),
                     "section_path": r[7], "section_title": r[8], "breadcrumb": r[9], "document_title": r[10]}
    return out


async def _small_leaf_texts(db, section_ids: set[str]) -> dict[str, tuple[str, int]]:
    if not section_ids:
        return {}
    rows = (await db.execute(
        sa.select(Section.id, Section.content, Section.token_count)
        .where(Section.id.in_(list(section_ids)), Section.is_leaf.is_(True),
               Section.token_count <= EXPAND_LEAF_TOKENS))).all()
    return {r[0]: (r[1] or "", int(r[2] or 0)) for r in rows}


def _empty_response(query: str, mode: str) -> dict:
    return {"query": query, "mode": mode, "results": [],
            "stair": {"enabled": False, "selected": [], "method": "off", "latency_ms": 0,
                      "candidates_considered": 0, "documents_considered": 0, "error": None},
            "timings": {}, "context": "", "citations": [], "warnings": []}


async def search(db, *, org_id: str, user_id: str | None, role: str, query: str,
                 collection_ids: list[str] | None = None, document_ids: list[str] | None = None,
                 mode: str = "hybrid", top_k: int | None = 8, rerank: bool = True,
                 agent_collection_ids: list[str] | None = None, context_budget: int | None = None) -> dict:
    """Access-filtered retrieval. Returns the `SearchResponse` shape (+ `citations`, `warnings`, `mode`)."""
    t_all = time.perf_counter()
    query = (query or "").strip()
    mode = mode if mode in MODES else "hybrid"
    resp = _empty_response(query, mode)
    timings: dict[str, float] = resp["timings"]
    org = await db.get(Organization, org_id)
    cfg = retrieval_config(org)
    top_k = max(1, min(int(top_k or cfg["top_k"]), 50))

    allowed = await accessible_collection_ids(db, org_id, user_id, role, agent_collection_ids)
    if collection_ids:
        wanted = set(collection_ids)
        allowed = [c for c in allowed if c in wanted]
    docs = await _candidate_documents(db, org_id, allowed, document_ids) if (query and allowed) else []
    if not docs:
        timings["total"] = _ms(t_all)
        return resp
    doc_filter = [d.id for d in docs] if document_ids else None

    use_stair = mode == "structure" or (mode == "hybrid" and cfg["stair_enabled"])
    use_bm25 = mode in ("hybrid", "lexical")
    use_vec = mode in ("hybrid", "semantic")
    use_q = mode == "hybrid"
    max_docs = cfg["stair_max_docs"]
    need_routing = use_stair and len(docs) > max_docs
    n_cand = max(50, top_k * 6)
    meter = UsageMeter()

    qvec = None
    if use_vec or use_q or use_stair:
        t = time.perf_counter()
        try:
            qvec = await embed_query(query)
        except Exception as e:  # noqa: BLE001
            log.warning("query embedding failed: %s", e)
            resp["warnings"].append(f"Query embedding failed ({type(e).__name__}); semantic signals were skipped.")
        timings["embed"] = _ms(t)

    rankings: dict[str, list[str]] = {}
    chunk_section: dict[str, str | None] = {}
    bm25_hits: list = []
    vec_hits: list = []
    q_hits: list = []
    if use_bm25 or need_routing:
        t = time.perf_counter()
        bm25_hits = await lexical.lexical_search(db, org_id=org_id, query=query, collection_ids=allowed,
                                                 document_ids=doc_filter, limit=n_cand)
        timings["bm25"] = _ms(t)
    if (use_vec or need_routing) and qvec is not None:
        t = time.perf_counter()
        vec_hits = await vector.vector_search(db, org_id=org_id, qvec=qvec, collection_ids=allowed,
                                              document_ids=doc_filter, limit=n_cand)
        vec_hits = [h for h in vec_hits if h.score > MIN_DENSE_SCORE]
        timings["vector"] = _ms(t)
    for h in bm25_hits + vec_hits:
        chunk_section[h.chunk_id] = h.section_id
    if use_bm25:
        rankings["bm25"] = [h.chunk_id for h in bm25_hits]
    if use_vec:
        rankings["vector"] = [h.chunk_id for h in vec_hits]

    if use_q and qvec is not None:
        t = time.perf_counter()
        q_hits = await vector.question_search(db, org_id=org_id, qvec=qvec, collection_ids=allowed,
                                              document_ids=doc_filter, limit=40)
        q_hits = [h for h in q_hits if h.score > MIN_DENSE_SCORE]
        sec_order = list(dict.fromkeys(h.section_id for h in q_hits if h.section_id))[:12]
        scoped = await vector.scoped_chunks(db, org_id=org_id, qvec=qvec, section_ids=sec_order, per_section=2)
        rankings["questions"] = [h.chunk_id for sid in sec_order for h in scoped.get(sid, [])]
        for sid in sec_order:
            for h in scoped.get(sid, []):
                chunk_section[h.chunk_id] = sid
        timings["questions"] = _ms(t)

    stair_conf: dict[str, float] = {}
    if use_stair:
        t = time.perf_counter()
        block = resp["stair"]
        block["enabled"] = True
        try:
            t_r = time.perf_counter()
            routed = await _route_documents(db, org_id, docs, allowed, doc_filter, qvec,
                                            [(bm25_hits, 1.0), (vec_hits, 1.0), (q_hits, 0.7)], max_docs)
            timings["routing"] = _ms(t_r)
            tocs = []
            for d in routed:
                toc = await stair.load_toc(db, d.id, title=d.title, version=str(d.updated_at))
                if toc is not None and toc.leaves:
                    tocs.append(toc)
            llm, finetuned = await _stair_llm(db, org_id, cfg)
            outcome = await stair.select_leaves(llm, query, tocs, k=cfg["stair_k"],
                                                budget_tokens=cfg["stair_toc_budget"], finetuned=finetuned,
                                                meter=meter)
            agree_sets = [s for s in ({h.section_id for h in bm25_hits[:20]}, {h.section_id for h in vec_hits[:20]},
                                      {h.section_id for h in q_hits[:20]}) if s]
            for sel in outcome.selections:
                if agree_sets:
                    agreement = sum(1 for s in agree_sets if sel.section_id in s) / len(agree_sets)
                    sel.confidence = round(0.6 * sel.confidence + 0.4 * agreement, 3)
            block.update(selected=[s.to_api() for s in outcome.selections], method=outcome.method,
                         latency_ms=outcome.latency_ms, candidates_considered=outcome.candidates_considered,
                         documents_considered=len(tocs), error=outcome.error, llm_calls=outcome.llm_calls)
            if outcome.error:
                resp["warnings"].append(f"Structure-aware retrieval: {outcome.error}")
            sel_ids = [s.section_id for s in outcome.selections]
            scoped = await vector.scoped_chunks(db, org_id=org_id, qvec=qvec, section_ids=sel_ids, per_section=4)
            rankings["stair"] = [h.chunk_id for sid in sel_ids for h in scoped.get(sid, [])]
            for sid in sel_ids:
                for h in scoped.get(sid, []):
                    chunk_section[h.chunk_id] = sid
            stair_conf = {s.section_id: s.confidence for s in outcome.selections}
        except Exception as e:  # noqa: BLE001
            log.exception("STAIR stage failed")
            block["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            resp["warnings"].append("Structure-aware retrieval failed; results use the other retrievers.")
        timings["stair"] = _ms(t)
        await meter.flush(org_id)

    # ---- fusion
    t = time.perf_counter()
    active = {k: v for k, v in rankings.items() if v}
    fused = rrf(active)
    if stair_conf and mode == "hybrid":
        fused = scoped_boost(fused, chunk_section, stair_conf)
    norm = max_score(active)
    ranks = source_ranks(active)
    timings["fusion"] = _ms(t)

    do_rerank = bool(rerank) and cfg["rerank"] and reranker_enabled()
    pool = fused[: (max(top_k * 3, 24) if do_rerank else top_k)]
    rows = await _load_chunks(db, [cid for cid, _ in pool])
    pool = [(cid, s) for cid, s in pool if cid in rows]
    rerank_scores: dict[str, float] = {}
    if do_rerank and len(pool) > 1:
        t = time.perf_counter()
        llm_r = await rag_llm(org_id) if reranker_info()["provider"] == "llm" else None
        scores = await rerank_passages(query, [rows[cid]["search_text"] for cid, _ in pool], llm=llm_r)
        if scores is not None and len(scores) == len(pool):
            rerank_scores = {cid: float(s) for (cid, _), s in zip(pool, scores)}
        timings["rerank"] = _ms(t)
    final = []
    for cid, s in pool:
        fs = min(1.0, s / norm) if norm else s
        score = 0.65 * rerank_scores[cid] + 0.35 * fs if rerank_scores else fs
        final.append((cid, score))
    if rerank_scores:
        final.sort(key=lambda x: -x[1])
    final = final[:top_k]

    results = []
    for cid, score in final:
        r = rows[cid]
        src = ranks.get(cid, {})
        results.append({
            "chunk_id": cid, "document_id": r["document_id"], "document_title": r["document_title"],
            "section_id": r["section_id"], "section_path": r["section_path"], "section_title": r["section_title"],
            "breadcrumb": r["breadcrumb"], "content": r["content"], "score": round(float(score), 4),
            "rerank_score": round(rerank_scores[cid], 4) if cid in rerank_scores else None,
            "sources": {"bm25": src.get("bm25"), "vector": src.get("vector"), "stair": src.get("stair"),
                        "questions": src.get("questions")},
            "ordinal": r["ordinal"], "page_start": r["page_start"], "page_end": r["page_end"],
        })
    t = time.perf_counter()
    leaf_texts = await _small_leaf_texts(db, {r["section_id"] for r in results if r["section_id"]})
    built = build_context(results, leaf_texts=leaf_texts, budget_tokens=context_budget or DEFAULT_BUDGET_TOKENS)
    for r in results:
        r["citation"] = built.chunk_to_source.get(r["chunk_id"])
    timings["context"] = _ms(t)
    resp.update(results=results, context=built.text, citations=built.citations, reranked=bool(rerank_scores))
    timings["total"] = _ms(t_all)
    return resp


def answer_messages(query: str, context: str) -> list[dict]:
    return [{"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": f"SOURCES:\n{context}\n\nQUESTION: {query.strip()}"}]


async def _alignment(org_id: str, text: str, query: str) -> dict:
    skipped = {"verdict": "skipped", "checker": "none", "latency_ms": None, "violations": []}
    try:
        from app.policy.guard import check_text
    except Exception:  # noqa: BLE001
        return skipped
    try:
        s = await check_text(org_id=org_id, text=text, user_request=query, kind="response")
    except Exception:  # noqa: BLE001
        log.exception("policy check failed")
        return skipped
    out = {k: s.get(k) for k in ("verdict", "checker", "latency_ms", "violations")}
    if s.get("revised"):
        out["revised"] = True
    return out


async def answer(db, *, org_id: str, user_id: str | None, role: str, query: str,
                 collection_ids: list[str] | None = None, document_ids: list[str] | None = None,
                 agent_collection_ids: list[str] | None = None) -> dict:
    """Retrieval + grounded answer with inline [n] citations; returns {answer, citations, alignment, retrieval}."""
    retrieval = await search(db, org_id=org_id, user_id=user_id, role=role, query=query,
                             collection_ids=collection_ids, document_ids=document_ids, mode="hybrid", top_k=None,
                             rerank=True, agent_collection_ids=agent_collection_ids)
    t = time.perf_counter()
    citations = retrieval.get("citations") or []
    context = retrieval.get("context") or ""
    cited: list[dict] = []
    if not context.strip() or not citations:
        text = NO_ANSWER
    else:
        llm = await rag_llm(org_id)
        if llm is None:
            text = ("No language model is configured for this workspace, so I can't compose an answer. "
                    "The most relevant sources are listed below.")
            cited = citations
        else:
            try:
                res = await llm.chat(answer_messages(query, context), temperature=0.1, max_tokens=1200)
                from app.services.usage import record_usage

                await record_usage(org_id, res.model or llm.model, res.usage, purpose="rag")
                text, used = used_citations((res.content or "").strip(), {c["id"] for c in citations})
                text = text or NO_ANSWER
                used_set = set(used)
                cited = [c for c in citations if c["id"] in used_set]
            except Exception as e:  # noqa: BLE001
                log.warning("answer generation failed: %s", e)
                text = f"The model provider failed to generate an answer ({type(e).__name__}). Please try again."
    retrieval["timings"]["answer"] = _ms(t)
    alignment = await _alignment(org_id, text, query)
    return {"answer": text, "citations": cited, "alignment": alignment, "retrieval": retrieval}


# =========================================================================== structure explorer
async def get_toc(db, document_id: str) -> list[dict]:
    """The document's ToC as a nested `TocNode` tree."""
    rows = (await db.execute(
        sa.select(Section.id, Section.parent_id, Section.path, Section.title, Section.level, Section.is_leaf,
                  Section.token_count, Section.summary)
        .where(Section.document_id == document_id).order_by(Section.ordinal))).all()
    nodes: dict[str, dict] = {}
    roots: list[dict] = []
    for r in rows:
        nodes[r[0]] = {"id": r[0], "path": r[2], "title": r[3], "level": r[4], "is_leaf": bool(r[5]),
                       "token_count": int(r[6] or 0), "summary": r[7], "children": []}
    for r in rows:
        parent = nodes.get(r[1]) if r[1] else None
        (parent["children"] if parent is not None else roots).append(nodes[r[0]])
    return roots


async def find_section(db, document_id: str | None, ref: str) -> Section | None:
    """Resolve a section by id, or by ToC path ("4.1.1") within a document."""
    ref = (ref or "").strip()
    if not ref:
        return None
    sec = await db.get(Section, ref)
    if sec is not None and (document_id is None or sec.document_id == document_id):
        return sec
    if document_id:
        path = ref.lstrip("§").strip()
        return (await db.execute(sa.select(Section).where(Section.document_id == document_id,
                                                          Section.path == path))).scalars().first()
    return None


async def get_section(db, document_id: str, section_id: str, *, max_chars: int = 30000) -> dict | None:
    """`SectionDetail` for a section (by id or path). Non-leaf content = its leaves' text, headed by path/title."""
    sec = await find_section(db, document_id, section_id)
    if sec is None or sec.document_id != document_id:
        return None
    children = (await db.execute(
        sa.select(Section.id, Section.path, Section.title, Section.is_leaf)
        .where(Section.parent_id == sec.id).order_by(Section.ordinal))).all()
    questions = (await db.execute(
        sa.select(SectionQuestion.question).where(SectionQuestion.section_id == sec.id)
        .order_by(SectionQuestion.id))).scalars().all()
    if sec.is_leaf:
        content = sec.content or ""
    else:
        rows = (await db.execute(
            sa.select(Section.path, Section.title, Section.level, Section.is_leaf, Section.content, Section.ordinal)
            .where(Section.document_id == document_id, Section.ordinal > sec.ordinal)
            .order_by(Section.ordinal))).all()
        parts, size = [], 0
        for path, title, level, is_leaf, text, _ in rows:
            if level <= sec.level:
                break
            if not is_leaf:
                continue
            piece = f"### {path} {title}\n{text or ''}".strip()
            parts.append(piece)
            size += len(piece)
            if size > max_chars:
                parts.append("… (truncated)")
                break
        content = "\n\n".join(parts)
    return {"id": sec.id, "document_id": sec.document_id, "path": sec.path, "title": sec.title, "level": sec.level,
            "is_leaf": sec.is_leaf, "breadcrumb": sec.breadcrumb, "content": content, "summary": sec.summary,
            "token_count": sec.token_count, "questions": list(questions),
            "children": [{"id": c[0], "path": c[1], "title": c[2], "is_leaf": bool(c[3])} for c in children],
            "page_start": sec.page_start, "page_end": sec.page_end, "parent_id": sec.parent_id}


async def export_training_data(db, org_id: str, collection_id: str | None = None, *,
                               collection_ids: list[str] | None = None) -> AsyncIterator[str]:
    """NDJSON lines of STAIR fine-tuning pairs (query + ToC → leaf path). See `app.rag.questions`."""
    async for line in _export_training_data(db, org_id, collection_id, collection_ids=collection_ids):
        yield line


async def stats(db, org_id: str) -> dict:
    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    info = embedding_info()
    rr = reranker_info()
    return {
        "documents": await count(sa.select(sa.func.count(Document.id)).where(Document.org_id == org_id)),
        "documents_ready": await count(sa.select(sa.func.count(Document.id))
                                       .where(Document.org_id == org_id, Document.status == "ready")),
        "sections": await count(sa.select(sa.func.count(Section.id)).where(Section.org_id == org_id)),
        "leaves": await count(sa.select(sa.func.count(Section.id))
                              .where(Section.org_id == org_id, Section.is_leaf.is_(True))),
        "chunks": await count(sa.select(sa.func.count(Chunk.id)).where(Chunk.org_id == org_id)),
        "questions": await count(sa.select(sa.func.count(SectionQuestion.id))
                                 .where(SectionQuestion.org_id == org_id)),
        "embedding_model": info["model"],
        "embedding_provider": info["provider"],
        "embedding_dim": info["dim"],
        "reranker": rr["model"] if rr["provider"] != "none" else "none",
    }
