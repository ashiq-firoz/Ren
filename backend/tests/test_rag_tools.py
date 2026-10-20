"""Agent tools (rag group): scoped search with citations, structure navigation, cite, answer."""
import asyncio

import pytest

from app.tools.base import Risk, ToolContext, ToolError, ToolResult, registry

DOC = """# Security Handbook

## Passwords
Passwords must be at least fourteen characters long. A password manager is mandatory for all staff.

## Devices
Laptops must use full disk encryption. Lost devices must be reported to IT within one hour.
"""


async def _ingest(org_id: str, user_id: str, collection_name: str, filename: str, text: str):
    from app.core.db import session_scope
    from app.models import Collection, Document
    from app.rag import service

    async with session_scope() as db:
        col = Collection(org_id=org_id, name=collection_name, access="org")
        db.add(col)
        await db.flush()
        doc = await service.create_document_from_bytes(db, org_id=org_id, user_id=user_id, collection_id=col.id,
                                                       filename=filename, data=text.encode())
        col_id, doc_id = col.id, doc.id
    for _ in range(600):
        async with session_scope() as db:
            d = await db.get(Document, doc_id)
            if d.status in ("ready", "failed"):
                assert d.status == "ready", d.error
                return col_id, doc_id
        await asyncio.sleep(0.05)
    raise AssertionError("ingestion timed out")


def _tool(name: str):
    spec = registry.get(name)
    assert spec is not None, name
    return spec


def test_tool_group_registration():
    import app.tools.builtin.rag  # noqa: F401

    group = registry.group("rag")
    assert group is not None and group.prefix == "rag" and group.icon == "library" and group.category == "knowledge"
    names = {t.name for t in group.tools}
    assert names == {"rag_search", "rag_search_structured", "rag_search_semantic", "rag_search_lexical",
                     "rag_answer", "rag_get_document", "rag_get_section", "rag_get_parent", "rag_get_children",
                     "rag_list_documents", "rag_cite"}
    assert all(t.risk == Risk.LOW and t.scope == "read" for t in group.tools)
    assert _tool("rag_search").parameters["required"] == ["query"]


async def test_tools_end_to_end(org_ctx):
    import app.tools.builtin.rag  # noqa: F401

    org_id, user_id = org_ctx["org_id"], org_ctx["user_id"]
    col_id, doc_id = await _ingest(org_id, user_id, "Tools test", "security_tools.md", DOC)
    ctx = ToolContext(org_id=org_id, user_id=user_id, role="member", collection_ids=[col_id])

    res = await _tool("rag_search").handler(ctx, query="disk encryption for laptops", top_k=3)
    assert isinstance(res, ToolResult) and res.citations
    assert "[1] Security Handbook › Devices (§2)" in res.content and "Cite these sources" in res.content
    assert res.citations[0]["section_title"] == "Devices" and res.citations[0]["id"] == 1

    res = await _tool("rag_search_structured").handler(ctx, query="password manager length")
    assert "STAIR selected" in res.content and "Security Handbook › Passwords (§1" in res.content
    res = await _tool("rag_search_lexical").handler(ctx, query="fourteen characters")
    assert res.citations[0]["section_title"] == "Passwords"
    res = await _tool("rag_search_semantic").handler(ctx, query="report lost devices")
    assert res.citations[0]["section_title"] == "Devices"

    info = await _tool("rag_get_document").handler(ctx, document_id=doc_id)
    assert info["title"] == "Security Handbook" and "• 2 Devices" in info["toc"]
    sec = await _tool("rag_get_section").handler(ctx, section_id="2", document_id=doc_id)
    assert "full disk encryption" in sec.content and sec.citations[0]["section_path"] == "2"
    parent = await _tool("rag_get_parent").handler(ctx, section_id="2", document_id=doc_id)
    assert parent["parent"] is None and [s["path"] for s in parent["top_level_sections"]] == ["1", "2"]
    kids = await _tool("rag_get_children").handler(ctx, section_id="1", document_id=doc_id)
    assert kids["children"] == [] and kids["note"]
    listing = await _tool("rag_list_documents").handler(ctx)
    assert [d["document_id"] for d in listing["documents"]] == [doc_id]

    chunk_ids = [c for c in (await _search_chunk_ids(ctx, "encryption"))]
    cited = await _tool("rag_cite").handler(ctx, chunk_ids=chunk_ids[:1])
    assert cited.citations[0]["id"] == 1 and cited.content.startswith("[1] Security Handbook")

    ans = await _tool("rag_answer").handler(ctx, question="How long must passwords be?")
    assert isinstance(ans, ToolResult) and ans.content
    assert all(c["document_id"] == doc_id for c in ans.citations)

    # an agent scoped to another collection sees nothing from this one
    other = ToolContext(org_id=org_id, user_id=user_id, role="member", collection_ids=["some-other-collection"])
    res = await _tool("rag_search").handler(other, query="disk encryption")
    assert res.citations == [] and "No matching passages" in res.content
    with pytest.raises(ToolError):
        await _tool("rag_get_document").handler(other, document_id=doc_id)
    with pytest.raises(ToolError):
        await _tool("rag_cite").handler(other, chunk_ids=chunk_ids[:1])


async def _search_chunk_ids(ctx: ToolContext, query: str) -> list[str]:
    from app.rag import service

    async with ctx.session() as db:
        res = await service.search(db, org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role, query=query,
                                   mode="lexical", agent_collection_ids=ctx.collection_ids)
    return [r["chunk_id"] for r in res["results"]]
