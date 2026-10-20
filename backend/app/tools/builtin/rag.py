"""Knowledge-base tools (STAIR structure-aware hybrid RAG).

Every tool is read-only (LOW risk) and access-filtered: the agent's knowledge scope (`ctx.collection_ids`) is
intersected with what the acting user may read (`ctx.user_id` / `ctx.role`). Results carry `[n]` source labels
with breadcrumbs and section paths so agents can cite, and return `ToolResult(citations=[...])`.
"""
from __future__ import annotations

from typing import Literal

import sqlalchemy as sa

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult

rag = ToolGroup(
    "rag", "Knowledge base",
    "Search, read and cite the organization's documents with structure-aware (STAIR) hybrid retrieval.",
    prefix="rag", icon="library", category="knowledge",
)

TOOL_CONTEXT_BUDGET = 2600
MAX_SECTION_CHARS = 12000


async def _allowed(ctx: ToolContext, db) -> list[str]:
    from app.rag import service

    return await service.accessible_collection_ids(db, ctx.org_id, ctx.user_id, ctx.role, ctx.collection_ids)


async def _document(ctx: ToolContext, db, document_id: str):
    from app.models import Document

    doc = await db.get(Document, (document_id or "").strip())
    if doc is None or doc.org_id != ctx.org_id or doc.collection_id not in await _allowed(ctx, db):
        raise ToolError(f"Document '{document_id}' was not found or is not accessible. Use rag_list_documents to "
                        "see available documents.")
    return doc


async def _section(ctx: ToolContext, db, section: str, document_id: str | None):
    from app.rag import service

    sec = await service.find_section(db, document_id.strip() if document_id else None, section)
    if sec is None:
        hint = "" if document_id else " (pass document_id when using a section path like '4.1.2')"
        raise ToolError(f"Section '{section}' was not found{hint}.")
    await _document(ctx, db, sec.document_id)
    return sec


def _format_results(res: dict, title: str, *, show_stair: bool = True) -> ToolResult:
    results = res.get("results") or []
    citations = res.get("citations") or []
    if not results:
        warn = "; ".join(res.get("warnings") or [])
        return ToolResult(content=f"No matching passages found in the knowledge base for: {res.get('query')!r}."
                          + (f" ({warn})" if warn else ""), display="No results", citations=[])
    docs = {r["document_id"] for r in results}
    lines = [f"{title} for {res.get('query')!r} — {len(citations)} sources from {len(docs)} document(s)."]
    stair_sel = (res.get("stair") or {}).get("selected") or []
    if show_stair and stair_sel:
        lines.append("STAIR targeted sections: " + "; ".join(
            f"{s['breadcrumb']} (§{s['path']})" for s in stair_sel[:3]))
    lines.append("")
    lines.append(res.get("context") or "")
    lines.append("")
    lines.append("Cite these sources inline as [n]. Only state facts supported by them.")
    return ToolResult(content="\n".join(lines), citations=citations,
                      display=f"{len(citations)} sources from {len(docs)} document(s)")


async def _run_search(ctx: ToolContext, query: str, mode: str, top_k: int, collection_ids, document_ids) -> dict:
    from app.rag import service

    if not (query or "").strip():
        raise ToolError("query must not be empty")
    async with ctx.session() as db:
        return await service.search(
            db, org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role, query=query,
            collection_ids=collection_ids or None, document_ids=document_ids or None, mode=mode,
            top_k=max(1, min(int(top_k or 6), 20)), rerank=True, agent_collection_ids=ctx.collection_ids,
            context_budget=TOOL_CONTEXT_BUDGET)


@rag.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str, top_k: int = 6, collection_ids: list[str] | None = None,
                 document_ids: list[str] | None = None) -> ToolResult:
    """Search the knowledge base (hybrid: STAIR structure-aware leaf targeting + BM25 + vectors + synthetic
    questions, fused and reranked). Returns numbered sources [n] with breadcrumbs and section paths to cite.

    Args:
        query: Natural-language question or keywords, e.g. "parental leave eligibility for contractors".
        top_k: Number of passages to return (1-20).
        collection_ids: Optional collection ids to restrict the search to.
        document_ids: Optional document ids to restrict the search to.
    """
    res = await _run_search(ctx, query, "hybrid", top_k, collection_ids, document_ids)
    return _format_results(res, "Knowledge search (hybrid)")


@rag.tool(risk=Risk.LOW, scope="read")
async def search_structured(ctx: ToolContext, query: str, top_k: int = 6, collection_ids: list[str] | None = None,
                            document_ids: list[str] | None = None) -> ToolResult:
    """Structure-aware retrieval only (STAIR): the model reads the documents' tables of contents and selects the
    leaf sections that contain the answer (constrained to valid section ids). Returns the selected sections with
    breadcrumbs, confidence and their text.

    Args:
        query: The question to locate in the documents' structure.
        top_k: Maximum passages to return from the selected sections (1-20).
        collection_ids: Optional collection ids to restrict the search to.
        document_ids: Optional document ids to restrict the search to.
    """
    res = await _run_search(ctx, query, "structure", top_k, collection_ids, document_ids)
    sel = (res.get("stair") or {}).get("selected") or []
    if not sel:
        err = (res.get("stair") or {}).get("error")
        return ToolResult(content="STAIR did not select any section" + (f": {err}" if err else
                                                                        " (no matching documents)."),
                          display="No sections", citations=[])
    lines = [f"STAIR selected {len(sel)} leaf section(s) for {query!r} "
             f"(method: {(res.get('stair') or {}).get('method')}):"]
    for i, s in enumerate(sel, start=1):
        conf = f", confidence {s['confidence']:.2f}" if s.get("confidence") is not None else ""
        lines.append(f"{i}. {s['breadcrumb']} (§{s['path']}{conf}) — document_id {s['document_id']}, "
                     f"section_id {s['section_id']}")
    if res.get("context"):
        lines += ["", "Text of the selected sections:", res["context"], "",
                  "Cite these sources inline as [n]."]
    return ToolResult(content="\n".join(lines), citations=res.get("citations") or [],
                      display=f"{len(sel)} sections selected")


@rag.tool(risk=Risk.LOW, scope="read")
async def search_semantic(ctx: ToolContext, query: str, top_k: int = 6, collection_ids: list[str] | None = None,
                          document_ids: list[str] | None = None) -> ToolResult:
    """Semantic (embedding similarity) search of the knowledge base — good for paraphrased or conceptual queries.

    Args:
        query: Natural-language question or description of what you need.
        top_k: Number of passages to return (1-20).
        collection_ids: Optional collection ids to restrict the search to.
        document_ids: Optional document ids to restrict the search to.
    """
    res = await _run_search(ctx, query, "semantic", top_k, collection_ids, document_ids)
    return _format_results(res, "Semantic search", show_stair=False)


@rag.tool(risk=Risk.LOW, scope="read")
async def search_lexical(ctx: ToolContext, query: str, top_k: int = 6, collection_ids: list[str] | None = None,
                         document_ids: list[str] | None = None) -> ToolResult:
    """Keyword (BM25 / full-text) search of the knowledge base — best for exact terms, codes, names and numbers.

    Args:
        query: Keywords or an exact phrase, e.g. "form W-9" or "clause 14.2".
        top_k: Number of passages to return (1-20).
        collection_ids: Optional collection ids to restrict the search to.
        document_ids: Optional document ids to restrict the search to.
    """
    res = await _run_search(ctx, query, "lexical", top_k, collection_ids, document_ids)
    return _format_results(res, "Keyword search", show_stair=False)


@rag.tool(risk=Risk.LOW, scope="read", timeout=180.0)
async def answer(ctx: ToolContext, question: str, collection_ids: list[str] | None = None,
                 document_ids: list[str] | None = None) -> ToolResult:
    """Answer a question from the knowledge base with a grounded, cited answer (retrieval + answer generation that
    only uses the retrieved sources and says so when the answer isn't there).

    Args:
        question: The question to answer.
        collection_ids: Optional collection ids to restrict retrieval to.
        document_ids: Optional document ids to restrict retrieval to.
    """
    from app.rag import service

    if not (question or "").strip():
        raise ToolError("question must not be empty")
    async with ctx.session() as db:
        res = await service.answer(db, org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role, query=question,
                                   collection_ids=collection_ids or None, document_ids=document_ids or None,
                                   agent_collection_ids=ctx.collection_ids)
    cites = res.get("citations") or []
    lines = [res.get("answer") or ""]
    if cites:
        lines += ["", "Sources:"]
        lines += [f"[{c['id']}] {c.get('breadcrumb') or c.get('document_title')}"
                  + (f" (§{c['section_path']})" if c.get("section_path") else "") for c in cites]
    verdict = (res.get("alignment") or {}).get("verdict")
    if verdict in ("warn", "block"):
        lines += ["", f"(Policy check: {verdict})"]
    return ToolResult(content="\n".join(lines), citations=cites, display=f"Answer with {len(cites)} citation(s)")


@rag.tool(risk=Risk.LOW, scope="read")
async def get_document(ctx: ToolContext, document_id: str, max_lines: int = 250) -> dict:
    """Get a document's metadata and its table of contents (section paths, titles, leaf markers, token counts).
    Use the paths with rag_get_section to read specific sections.

    Args:
        document_id: The document id (from search results or rag_list_documents).
        max_lines: Maximum number of ToC lines to return.
    """
    from app.models import Collection, Section

    async with ctx.session() as db:
        doc = await _document(ctx, db, document_id)
        col = await db.get(Collection, doc.collection_id)
        rows = (await db.execute(
            sa.select(Section.path, Section.title, Section.level, Section.is_leaf, Section.token_count)
            .where(Section.document_id == doc.id).order_by(Section.ordinal))).all()
    lines = []
    for path, title, level, is_leaf, tokens in rows[: max(1, max_lines)]:
        lines.append("  " * max(0, (level or 1) - 1) + ("• " if is_leaf else "") + f"{path} {title}"
                     + (f" ({tokens} tok)" if is_leaf else ""))
    if len(rows) > max_lines:
        lines.append(f"… {len(rows) - max_lines} more sections (truncated)")
    return {
        "id": doc.id, "title": doc.title, "filename": doc.filename, "collection": col.name if col else None,
        "status": doc.status, "pages": doc.page_count, "sections": doc.section_count, "leaves": doc.leaf_count,
        "structure_source": doc.structure_source, "summary": doc.summary,
        "toc": "\n".join(lines) if lines else "(not indexed yet)",
        "note": "Leaves are marked with •. Read one with rag_get_section(section_id=<path>, document_id=<id>).",
    }


def _section_result(detail: dict, doc_title: str, max_chars: int) -> ToolResult:
    content = detail.get("content") or ""
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + f"\n… [truncated {len(detail['content']) - max_chars} chars]"
    cite = {"id": 1, "document_id": detail["document_id"], "document_title": doc_title, "section_id": detail["id"],
            "section_path": detail["path"], "section_title": detail["title"], "breadcrumb": detail["breadcrumb"],
            "snippet": (detail.get("content") or "")[:280], "score": None}
    lines = [f"[1] {detail['breadcrumb']} (§{detail['path']})"
             + (f", pp. {detail['page_start']}–{detail['page_end']}" if detail.get("page_start") else ""),
             f"section_id: {detail['id']} · {'leaf' if detail['is_leaf'] else 'non-leaf'} · "
             f"{detail['token_count']} tokens"]
    if detail.get("children"):
        lines.append("Subsections: " + "; ".join(f"{c['path']} {c['title']}" for c in detail["children"][:30]))
    lines += ["", content or "(no text)"]
    return ToolResult(content="\n".join(lines), citations=[cite], display=f"§{detail['path']} {detail['title']}")


@rag.tool(risk=Risk.LOW, scope="read")
async def get_section(ctx: ToolContext, section_id: str, document_id: str | None = None,
                      max_chars: int = MAX_SECTION_CHARS) -> ToolResult:
    """Read a section's full text by section id, or by ToC path (e.g. "4.1.2") together with document_id.
    Non-leaf sections return the text of all their leaves.

    Args:
        section_id: Section id, or a ToC path such as "4.1.2" (then document_id is required).
        document_id: The document id (required when section_id is a path).
        max_chars: Maximum characters of text to return.
    """
    from app.models import Document
    from app.rag import service

    async with ctx.session() as db:
        sec = await _section(ctx, db, section_id, document_id)
        detail = await service.get_section(db, sec.document_id, sec.id, max_chars=max_chars * 2)
        doc = await db.get(Document, sec.document_id)
    if detail is None:
        raise ToolError("Section not found.")
    return _section_result(detail, doc.title if doc else "", max(500, max_chars))


@rag.tool(risk=Risk.LOW, scope="read")
async def get_parent(ctx: ToolContext, section_id: str, document_id: str | None = None) -> dict:
    """Get the parent of a section (for wider context): its path, title, breadcrumb and children.

    Args:
        section_id: Section id, or a ToC path such as "4.1.2" (then document_id is required).
        document_id: The document id (required when section_id is a path).
    """
    from app.models import Document, Section

    async with ctx.session() as db:
        sec = await _section(ctx, db, section_id, document_id)
        if not sec.parent_id:
            doc = await db.get(Document, sec.document_id)
            tops = (await db.execute(
                sa.select(Section.id, Section.path, Section.title, Section.is_leaf)
                .where(Section.document_id == sec.document_id, Section.parent_id.is_(None))
                .order_by(Section.ordinal))).all()
            return {"parent": None, "document": {"id": doc.id, "title": doc.title} if doc else None,
                    "note": f"§{sec.path} is a top-level section of the document.",
                    "top_level_sections": [{"section_id": r[0], "path": r[1], "title": r[2], "is_leaf": bool(r[3])}
                                           for r in tops]}
        parent = await db.get(Section, sec.parent_id)
        kids = (await db.execute(
            sa.select(Section.id, Section.path, Section.title, Section.is_leaf)
            .where(Section.parent_id == parent.id).order_by(Section.ordinal))).all()
    return {"parent": {"section_id": parent.id, "path": parent.path, "title": parent.title,
                       "breadcrumb": parent.breadcrumb, "is_leaf": parent.is_leaf,
                       "token_count": parent.token_count},
            "children": [{"section_id": r[0], "path": r[1], "title": r[2], "is_leaf": bool(r[3])} for r in kids]}


@rag.tool(risk=Risk.LOW, scope="read")
async def get_children(ctx: ToolContext, section_id: str, document_id: str | None = None) -> dict:
    """List the direct subsections of a section (path, title, leaf flag, token count).

    Args:
        section_id: Section id, or a ToC path such as "4.1" (then document_id is required).
        document_id: The document id (required when section_id is a path).
    """
    from app.models import Section

    async with ctx.session() as db:
        sec = await _section(ctx, db, section_id, document_id)
        kids = (await db.execute(
            sa.select(Section.id, Section.path, Section.title, Section.is_leaf, Section.token_count)
            .where(Section.parent_id == sec.id).order_by(Section.ordinal))).all()
    return {"section": {"section_id": sec.id, "path": sec.path, "title": sec.title, "breadcrumb": sec.breadcrumb},
            "children": [{"section_id": r[0], "path": r[1], "title": r[2], "is_leaf": bool(r[3]),
                          "token_count": r[4]} for r in kids],
            "note": "This is a leaf section (no children)." if sec.is_leaf else None}


@rag.tool(risk=Risk.LOW, scope="read")
async def list_documents(ctx: ToolContext, collection_id: str | None = None, query: str | None = None,
                         status: Literal["ready", "all"] = "ready", limit: int = 30) -> dict:
    """List knowledge-base documents the agent can access (title, id, collection, structure, size).

    Args:
        collection_id: Optional collection id to filter by.
        query: Optional text to match in titles / filenames.
        status: "ready" for searchable documents only, "all" to include ones still processing or failed.
        limit: Maximum documents to return (1-200).
    """
    from app.models import Collection, Document

    async with ctx.session() as db:
        allowed = await _allowed(ctx, db)
        if collection_id:
            allowed = [c for c in allowed if c == collection_id]
        if not allowed:
            return {"documents": [], "total": 0}
        stmt = sa.select(Document, Collection.name).join(Collection, Collection.id == Document.collection_id).where(
            Document.org_id == ctx.org_id, Document.collection_id.in_(allowed))
        if status == "ready":
            stmt = stmt.where(Document.status == "ready")
        if query:
            like = f"%{query.strip().lower()}%"
            stmt = stmt.where(sa.or_(sa.func.lower(Document.title).like(like),
                                     sa.func.lower(Document.filename).like(like)))
        rows = (await db.execute(stmt.order_by(Document.created_at.desc()).limit(max(1, min(limit, 200))))).all()
    docs = [{"document_id": d.id, "title": d.title, "collection": cname, "status": d.status,
             "sections": d.section_count, "leaves": d.leaf_count, "pages": d.page_count,
             "structure": d.structure_source, "filename": d.filename} for d, cname in rows]
    return {"documents": docs, "total": len(docs)}


@rag.tool(risk=Risk.LOW, scope="read")
async def cite(ctx: ToolContext, chunk_ids: list[str]) -> ToolResult:
    """Format citations for specific chunk ids (from search results): numbered [n] labels with document title,
    breadcrumb, section path and a snippet — ready to append as a "Sources" list.

    Args:
        chunk_ids: Chunk ids to cite, in the order they should be numbered.
    """
    from app.models import Chunk, Document, Section

    ids = [c for c in dict.fromkeys(chunk_ids or []) if c][:50]
    if not ids:
        raise ToolError("chunk_ids must contain at least one id")
    async with ctx.session() as db:
        allowed = set(await _allowed(ctx, db))
        rows = (await db.execute(
            sa.select(Chunk.id, Chunk.document_id, Chunk.section_id, Chunk.content, Chunk.collection_id,
                      Document.title, Section.path, Section.title, Section.breadcrumb)
            .join(Document, Document.id == Chunk.document_id)
            .outerjoin(Section, Section.id == Chunk.section_id)
            .where(Chunk.id.in_(ids), Chunk.org_id == ctx.org_id))).all()
    by_id = {r[0]: r for r in rows if r[4] in allowed}
    citations, lines, missing = [], [], []
    for cid in ids:
        r = by_id.get(cid)
        if r is None:
            missing.append(cid)
            continue
        n = len(citations) + 1
        snippet = " ".join((r[3] or "").split())[:280]
        citations.append({"id": n, "document_id": r[1], "document_title": r[5], "section_id": r[2],
                          "section_path": r[6], "section_title": r[7], "breadcrumb": r[8], "snippet": snippet,
                          "score": None})
        lines.append(f"[{n}] {r[8] or r[5]}" + (f" (§{r[6]})" if r[6] else "") + f" — “{snippet[:160]}”")
    if missing:
        lines.append(f"(Not found or not accessible: {', '.join(missing[:10])})")
    if not citations:
        raise ToolError("None of the chunk ids were found or accessible.")
    return ToolResult(content="\n".join(lines), citations=citations, display=f"{len(citations)} citation(s)")
