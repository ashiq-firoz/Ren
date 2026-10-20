"""Conversations with Ren. Posting a message never blocks — replies stream over /api/stream."""
from __future__ import annotations

import mimetypes
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.agents.orchestrator import active_task_counts, hub
from app.core.config import settings
from app.core.db import utcnow
from app.core.deps import Ctx, get_ctx
from app.core.events import bus
from app.models import Agent, Artifact, Conversation, Message, Task
from app.services.serialize import conversation_out, message_out
from app.agents.common import task_snapshot  # noqa: E402

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class ConvIn(BaseModel):
    title: str | None = None


class ConvPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    attachment_ids: list[str] | None = None
    mention_agent_id: str | None = None


async def _get(ctx: Ctx, conv_id: str) -> Conversation:
    c = await ctx.db.get(Conversation, conv_id)
    if c is None or c.org_id != ctx.org_id or c.user_id != ctx.user_id:
        raise HTTPException(404, "Conversation not found")
    return c


async def _out(ctx: Ctx, c: Conversation, preview: str | None = None, active: int | None = None) -> dict:
    if preview is None:
        last = (await ctx.db.execute(select(Message.content).where(Message.conversation_id == c.id,
                                                                    Message.role.in_(("user", "assistant")))
                                     .order_by(Message.created_at.desc()).limit(1))).scalar()
        preview = (last or "")[:160] or None
    if active is None:
        active = (await active_task_counts(ctx.db, ctx.org_id, [c.id])).get(c.id, 0)
    status, _queued = hub.state(c.id)
    return conversation_out(c, preview, active, status)


@router.get("")
async def list_conversations(archived: bool = False, q: str | None = None, ctx: Ctx = Depends(get_ctx)):
    query = select(Conversation).where(Conversation.org_id == ctx.org_id, Conversation.user_id == ctx.user_id,
                                       Conversation.archived.is_(archived))
    if q:
        like = f"%{q.strip()}%"
        sub = select(Message.conversation_id).where(Message.org_id == ctx.org_id, Message.content.ilike(like))
        query = query.where(or_(Conversation.title.ilike(like), Conversation.id.in_(sub)))
    rows = (await ctx.db.execute(query.order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
                                 .limit(300))).scalars().all()
    ids = [c.id for c in rows]
    actives = await active_task_counts(ctx.db, ctx.org_id, ids)
    previews: dict[str, str] = {}
    if ids:
        sub = (select(Message.conversation_id, func.max(Message.created_at).label("mx"))
               .where(Message.conversation_id.in_(ids), Message.role.in_(("user", "assistant")))
               .group_by(Message.conversation_id).subquery())
        prow = (await ctx.db.execute(select(Message.conversation_id, Message.content).join(
            sub, (Message.conversation_id == sub.c.conversation_id) & (Message.created_at == sub.c.mx)))).all()
        previews = {cid: (content or "")[:160] for cid, content in prow}
    items = [await _out(ctx, c, previews.get(c.id, ""), actives.get(c.id, 0)) for c in rows]
    return {"items": items, "total": len(items)}


@router.post("")
async def create_conversation(body: ConvIn, ctx: Ctx = Depends(get_ctx)):
    c = Conversation(org_id=ctx.org_id, user_id=ctx.user_id, title=(body.title or "").strip()[:300] or "New conversation")
    ctx.db.add(c)
    await ctx.db.flush()
    out = conversation_out(c, None, 0, "idle")
    await bus.emit(ctx.org_id, "conversation.created", {"conversation": out}, user_id=ctx.user_id, admin_visible=False)
    return out


@router.get("/{conv_id}")
async def get_conversation(conv_id: str, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    rows = (await ctx.db.execute(select(Message).where(Message.conversation_id == c.id)
                                 .order_by(Message.created_at.desc()).limit(200))).scalars().all()
    out = await _out(ctx, c)
    out["messages"] = [message_out(m) for m in reversed(rows)]
    return out


@router.get("/{conv_id}/messages")
async def list_messages(conv_id: str, before: str | None = None, limit: int = 50, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    q = select(Message).where(Message.conversation_id == c.id)
    if before:
        ref = await ctx.db.get(Message, before)
        if ref is not None:
            q = q.where(Message.created_at < ref.created_at)
    rows = (await ctx.db.execute(q.order_by(Message.created_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    items = [message_out(m) for m in reversed(rows)]
    return {"items": items, "total": len(items)}


@router.patch("/{conv_id}")
async def patch_conversation(conv_id: str, body: ConvPatch, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    if body.title is not None:
        c.title = body.title.strip()[:300] or c.title
    if body.pinned is not None:
        c.pinned = body.pinned
    if body.archived is not None:
        c.archived = body.archived
    out = await _out(ctx, c)
    await bus.emit(ctx.org_id, "conversation.updated", {"conversation": out}, user_id=ctx.user_id, admin_visible=False)
    return out


@router.delete("/{conv_id}")
async def delete_conversation(conv_id: str, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    await hub.stop(c.id)
    await ctx.db.delete(c)
    return {"ok": True}


@router.post("/{conv_id}/messages", status_code=202)
async def post_message(conv_id: str, body: MessageIn, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    meta: dict = {}
    if body.attachment_ids:
        arts = (await ctx.db.execute(select(Artifact).where(Artifact.id.in_(body.attachment_ids),
                                                            Artifact.org_id == ctx.org_id))).scalars().all()
        meta["attachments"] = [{"id": a.id, "name": a.name, "path": a.path, "size": a.size, "mime": a.mime,
                                "document_id": a.document_id} for a in arts]
    if body.mention_agent_id:
        ag = await ctx.db.get(Agent, body.mention_agent_id)
        if ag is not None and ag.org_id == ctx.org_id:
            meta["mention_agent_id"] = ag.id
    busy = hub.is_busy(c.id)
    m = Message(conversation_id=c.id, org_id=ctx.org_id, user_id=ctx.user_id, role="user", content=body.content,
                meta=meta, status="queued" if busy else "complete", processed=False)
    ctx.db.add(m)
    c.updated_at = utcnow()
    await ctx.db.flush()
    out = message_out(m)
    await ctx.db.commit()  # the orchestrator reads it from its own session
    await bus.emit(ctx.org_id, "message.created", {"message": out}, user_id=ctx.user_id, admin_visible=False)
    await hub.user_message(c.id, ctx.org_id, ctx.user_id, m.id)
    return out


@router.post("/{conv_id}/stop")
async def stop(conv_id: str, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    await hub.stop(c.id)
    return {"ok": True}


@router.post("/{conv_id}/attachments")
async def upload_attachment(conv_id: str, file: UploadFile = File(...), ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    from app.services.workspace import Workspace

    ws = Workspace(ctx.org_id, ctx.user_id)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", file.filename or "upload").strip() or "upload"
    target = ws.resolve(f"uploads/{c.id[:8]}/{safe}", for_write=True)
    target.write_bytes(data)
    vpath = ws.virtual(target)
    mime = file.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    document_id = None
    indexable = (".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm", ".xlsx", ".csv", ".pptx")
    if safe.lower().endswith(indexable):
        try:
            from app.models import Collection
            from app.rag.service import create_document_from_bytes, ensure_default_collections

            await ensure_default_collections(ctx.db, ctx.org_id)
            coll = (await ctx.db.execute(select(Collection).where(Collection.org_id == ctx.org_id,
                                                                  Collection.system.is_(True)))).scalars().first()
            if coll is not None:
                doc = await create_document_from_bytes(ctx.db, org_id=ctx.org_id, user_id=ctx.user_id,
                                                       collection_id=coll.id, filename=safe, data=data, mime=mime)
                document_id = doc.id
        except Exception:  # noqa: BLE001
            document_id = None
    art = Artifact(org_id=ctx.org_id, user_id=ctx.user_id, conversation_id=c.id, name=safe, path=vpath, mime=mime,
                   size=len(data), document_id=document_id)
    ctx.db.add(art)
    await ctx.db.flush()
    return {"id": art.id, "name": safe, "path": vpath, "size": len(data), "mime": mime, "document_id": document_id}


@router.get("/{conv_id}/tasks")
async def conversation_tasks(conv_id: str, ctx: Ctx = Depends(get_ctx)):
    c = await _get(ctx, conv_id)
    rows = (await ctx.db.execute(select(Task).where(Task.conversation_id == c.id, Task.parent_id.is_(None))
                                 .order_by(Task.created_at.desc()).limit(100))).scalars().all()
    items = [await task_snapshot(ctx.db, t) for t in rows]
    return {"items": items, "total": len(items)}
