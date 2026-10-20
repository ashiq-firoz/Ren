"""Knowledge API (STAIR hybrid RAG): collections, documents, structure explorer, search, answers, training data."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.deps import Ctx, get_ctx, require_admin
from app.models import Collection, Document, Membership
from app.rag import service
from app.rag.parsers import detect_format, unsupported_message
from app.services.audit import audit

router = APIRouter(prefix="/api", tags=["knowledge"])


# --------------------------------------------------------------------------- schemas
class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    access: Literal["org", "restricted"] = "org"
    member_ids: list[str] = Field(default_factory=list)


class CollectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    access: Literal["org", "restricted"] | None = None
    member_ids: list[str] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    collection_ids: list[str] | None = None
    document_ids: list[str] | None = None
    mode: Literal["hybrid", "semantic", "lexical", "structure"] = "hybrid"
    top_k: int | None = Field(default=None, ge=1, le=50)
    rerank: bool = True


class AnswerRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    collection_ids: list[str] | None = None
    document_ids: list[str] | None = None


# --------------------------------------------------------------------------- helpers
async def _collection_or_404(ctx: Ctx, collection_id: str, *, check_access: bool = True) -> Collection:
    col = await ctx.db.get(Collection, collection_id)
    if col is None or col.org_id != ctx.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    if check_access:
        allowed = await service.accessible_collection_ids(ctx.db, ctx.org_id, ctx.user_id, ctx.role)
        if col.id not in allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    return col


async def _document_or_404(ctx: Ctx, document_id: str) -> Document:
    doc = await ctx.db.get(Document, document_id)
    if doc is None or doc.org_id != ctx.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    allowed = await service.accessible_collection_ids(ctx.db, ctx.org_id, ctx.user_id, ctx.role)
    if doc.collection_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


async def _valid_member_ids(ctx: Ctx, member_ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(m for m in member_ids if m))
    if not ids:
        return []
    rows = (await ctx.db.execute(
        sa.select(Membership.user_id).where(Membership.org_id == ctx.org_id, Membership.user_id.in_(ids)))).all()
    valid = {r[0] for r in rows}
    unknown = [m for m in ids if m not in valid]
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not members of this workspace: {', '.join(unknown[:5])}")
    return ids


async def _name_taken(ctx: Ctx, name: str, exclude_id: str | None = None) -> bool:
    q = sa.select(Collection.id).where(Collection.org_id == ctx.org_id,
                                       sa.func.lower(Collection.name) == name.strip().lower())
    if exclude_id:
        q = q.where(Collection.id != exclude_id)
    return (await ctx.db.execute(q.limit(1))).first() is not None


async def _read_limited(file: UploadFile) -> bytes:
    limit = int(settings.max_upload_mb) * 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB upload limit.")
    return bytes(buf)


# --------------------------------------------------------------------------- collections
@router.get("/collections")
async def list_collections(ctx: Ctx = Depends(get_ctx)) -> dict:
    await service.ensure_default_collections(ctx.db, ctx.org_id)
    ids = await service.accessible_collection_ids(ctx.db, ctx.org_id, ctx.user_id, ctx.role)
    cols = []
    if ids:
        cols = list((await ctx.db.execute(
            sa.select(Collection).where(Collection.id.in_(ids))
            .order_by(Collection.system, Collection.created_at))).scalars().all())
    items = await service.collections_out(ctx.db, cols)
    return {"items": items, "total": len(items)}


@router.post("/collections")
async def create_collection(body: CollectionCreate, ctx: Ctx = Depends(require_admin)) -> dict:
    if await _name_taken(ctx, body.name):
        raise HTTPException(status.HTTP_409_CONFLICT, "A collection with this name already exists")
    members = await _valid_member_ids(ctx, body.member_ids)
    col = Collection(org_id=ctx.org_id, name=body.name.strip(), description=body.description or "",
                     access=body.access, member_ids=members, system=False)
    ctx.db.add(col)
    await ctx.db.flush()
    await audit(ctx.org_id, "collection.created", user_id=ctx.user_id, target_type="collection", target_id=col.id,
                details={"name": col.name, "access": col.access, "members": len(members)}, db=ctx.db)
    return (await service.collections_out(ctx.db, [col]))[0]


@router.patch("/collections/{collection_id}")
async def update_collection(collection_id: str, body: CollectionUpdate, ctx: Ctx = Depends(require_admin)) -> dict:
    col = await _collection_or_404(ctx, collection_id, check_access=False)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        name = changes["name"].strip()
        if col.system and name != col.name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "System collections can't be renamed")
        if await _name_taken(ctx, name, exclude_id=col.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "A collection with this name already exists")
        col.name = name
    if changes.get("description") is not None:
        col.description = changes["description"]
    if changes.get("access") is not None:
        col.access = changes["access"]
    if changes.get("member_ids") is not None:
        col.member_ids = await _valid_member_ids(ctx, changes["member_ids"])
    await ctx.db.flush()
    await audit(ctx.org_id, "collection.updated", user_id=ctx.user_id, target_type="collection", target_id=col.id,
                details={k: v for k, v in changes.items() if k != "member_ids"}, db=ctx.db)
    return (await service.collections_out(ctx.db, [col]))[0]


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str, ctx: Ctx = Depends(require_admin)) -> dict:
    col = await _collection_or_404(ctx, collection_id, check_access=False)
    if col.system:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "System collections can't be deleted")
    doc_ids = (await ctx.db.execute(sa.select(Document.id).where(Document.collection_id == col.id))).scalars().all()
    for did in doc_ids:
        await service.delete_document(ctx.db, did)
    name = col.name
    await ctx.db.delete(col)
    await ctx.db.flush()
    await audit(ctx.org_id, "collection.deleted", user_id=ctx.user_id, target_type="collection",
                target_id=collection_id, risk="medium", details={"name": name, "documents": len(doc_ids)}, db=ctx.db)
    return {"ok": True}


# --------------------------------------------------------------------------- documents
@router.get("/documents")
async def list_documents(collection_id: str | None = None, q: str | None = None,
                         status_: str | None = Query(default=None, alias="status"),
                         limit: int = Query(default=500, ge=1, le=2000), offset: int = Query(default=0, ge=0),
                         ctx: Ctx = Depends(get_ctx)) -> dict:
    allowed = await service.accessible_collection_ids(ctx.db, ctx.org_id, ctx.user_id, ctx.role)
    if collection_id:
        allowed = [c for c in allowed if c == collection_id]
    if not allowed:
        return {"items": [], "total": 0}
    stmt = sa.select(Document).where(Document.org_id == ctx.org_id, Document.collection_id.in_(allowed))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(sa.or_(sa.func.lower(Document.title).like(like), sa.func.lower(Document.filename).like(like)))
    if status_:
        stmt = stmt.where(Document.status == status_)
    total = int((await ctx.db.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar() or 0)
    docs = list((await ctx.db.execute(stmt.order_by(Document.created_at.desc()).offset(offset).limit(limit)))
                .scalars().all())
    return {"items": await service.documents_out(ctx.db, docs), "total": total}


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), collection_id: str | None = Form(default=None),
                          ctx: Ctx = Depends(get_ctx)) -> dict:
    ctx.require("member")
    filename = Path(file.filename or "document").name
    if detect_format(filename, file.content_type) is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, unsupported_message(filename))
    data = await _read_limited(file)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty.")
    if collection_id:
        col = await _collection_or_404(ctx, collection_id)
    else:
        col = await service.get_default_collection(ctx.db, ctx.org_id)
    doc = await service.create_document_from_bytes(ctx.db, org_id=ctx.org_id, user_id=ctx.user_id,
                                                   collection_id=col.id, filename=filename, data=data,
                                                   mime=file.content_type)
    await audit(ctx.org_id, "document.uploaded", user_id=ctx.user_id, target_type="document", target_id=doc.id,
                details={"filename": doc.filename, "collection_id": col.id, "collection": col.name,
                         "size": doc.size}, db=ctx.db)
    return await service.document_dict(ctx.db, doc)


@router.get("/documents/{document_id}")
async def get_document(document_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    doc = await _document_or_404(ctx, document_id)
    out = await service.document_dict(ctx.db, doc)
    out["summary"] = doc.summary
    out["toc"] = await service.get_toc(ctx.db, doc.id)
    return out


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    ctx.require("member")
    doc = await _document_or_404(ctx, document_id)
    if not ctx.is_admin and doc.uploaded_by != ctx.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins or the uploader can delete this document")
    details = {"filename": doc.filename, "title": doc.title, "collection_id": doc.collection_id}
    await service.delete_document(ctx.db, doc.id)
    await audit(ctx.org_id, "document.deleted", user_id=ctx.user_id, target_type="document", target_id=document_id,
                risk="medium", details=details, db=ctx.db)
    return {"ok": True}


@router.post("/documents/{document_id}/reindex")
async def reindex_document(document_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    ctx.require("member")
    doc = await _document_or_404(ctx, document_id)
    doc.status, doc.progress, doc.error = "uploaded", 0, None
    await ctx.db.flush()
    service.schedule_ingestion(doc.id)
    await audit(ctx.org_id, "document.reindexed", user_id=ctx.user_id, target_type="document", target_id=doc.id,
                details={"filename": doc.filename}, db=ctx.db)
    return await service.document_dict(ctx.db, doc)


@router.get("/documents/{document_id}/download")
async def download_document(document_id: str, ctx: Ctx = Depends(get_ctx)):
    doc = await _document_or_404(ctx, document_id)
    path = Path(doc.storage_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The stored file is missing")
    return FileResponse(str(path), media_type=doc.mime or "application/octet-stream", filename=doc.filename)


@router.get("/documents/{document_id}/sections/{section_id}")
async def get_section(document_id: str, section_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    doc = await _document_or_404(ctx, document_id)
    detail = await service.get_section(ctx.db, doc.id, section_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Section not found")
    return detail


# --------------------------------------------------------------------------- retrieval
@router.post("/knowledge/search")
async def knowledge_search(body: SearchRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    await ctx.db.commit()  # release any write lock before retrieval (LLM calls, usage records)
    return await service.search(ctx.db, org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role, query=body.query,
                                collection_ids=body.collection_ids, document_ids=body.document_ids, mode=body.mode,
                                top_k=body.top_k, rerank=body.rerank)


@router.post("/knowledge/answer")
async def knowledge_answer(body: AnswerRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    await ctx.db.commit()
    return await service.answer(ctx.db, org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role, query=body.query,
                                collection_ids=body.collection_ids, document_ids=body.document_ids)


@router.get("/knowledge/training-data")
async def training_data(collection_id: str | None = None, ctx: Ctx = Depends(get_ctx)):
    allowed = await service.accessible_collection_ids(ctx.db, ctx.org_id, ctx.user_id, ctx.role)
    if collection_id and collection_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    org_id = ctx.org_id

    async def lines():
        async with SessionLocal() as db:
            async for line in service.export_training_data(db, org_id, collection_id, collection_ids=allowed):
                yield line

    fname = "stair-training-data.ndjson" if not collection_id else f"stair-training-{collection_id[:8]}.ndjson"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/knowledge/stats")
async def knowledge_stats(ctx: Ctx = Depends(get_ctx)) -> dict:
    return await service.stats(ctx.db, ctx.org_id)
