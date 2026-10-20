"""Skills, memory, files & artifacts, audit, notifications."""
from __future__ import annotations

import mimetypes
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.agents.orchestrator import start_skill_task
from app.core.config import settings
from app.core.db import utcnow
from app.core.deps import Ctx, get_ctx, require_admin, require_member
from app.core.events import bus
from app.models import Agent, Artifact, AuditLog, Conversation, Memory, Notification, Skill, Task, User
from app.services.audit import audit
from app.services.files import artifact_file, build_preview, kind_filter, resolve_file
from app.services.memory import search_memories, store_memory
from app.services.serialize import (
    artifact_out,
    audit_out,
    conversation_out,
    memory_out,
    notification_out,
    skill_out,
)
from app.services.workspace import Workspace, WorkspaceError

router = APIRouter(prefix="/api", tags=["misc"])


# ------------------------------------------------------------------ skills
class SkillIn(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    icon: str | None = None
    instructions: str | None = None
    agent_id: str | None = None
    tool_groups: list[str] | None = None
    inputs: list[dict] | None = None


class SkillRunIn(BaseModel):
    inputs: dict = {}
    conversation_id: str | None = None


async def _skill(ctx: Ctx, sid: str) -> Skill:
    s = await ctx.db.get(Skill, sid)
    if s is None or s.org_id != ctx.org_id:
        raise HTTPException(404, "Skill not found")
    return s


@router.get("/skills")
async def list_skills(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Skill).where(Skill.org_id == ctx.org_id)
                                 .order_by(Skill.category, Skill.name))).scalars().all()
    return {"items": [skill_out(s) for s in rows], "total": len(rows)}


@router.post("/skills")
async def create_skill(body: SkillIn, ctx: Ctx = Depends(require_admin)):
    if not body.name or not body.instructions:
        raise HTTPException(400, "name and instructions are required")
    base = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:60] or "skill"
    slug, i = base, 1
    while (await ctx.db.execute(select(Skill.id).where(Skill.org_id == ctx.org_id, Skill.slug == slug))).first():
        i += 1
        slug = f"{base}-{i}"
    s = Skill(org_id=ctx.org_id, slug=slug, builtin=False, **{"description": "", "category": "custom", "icon": "sparkles",
                                                              "tool_groups": [], "inputs": [],
                                                              **body.model_dump(exclude_none=True)})
    ctx.db.add(s)
    await ctx.db.flush()
    await audit(ctx.org_id, "skill.created", user_id=ctx.user_id, target_type="skill", target_id=s.id, db=ctx.db)
    return skill_out(s)


@router.patch("/skills/{sid}")
async def patch_skill(sid: str, body: SkillIn, ctx: Ctx = Depends(require_admin)):
    s = await _skill(ctx, sid)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "agent_id":
            s.agent_id = v or None
        elif v is not None:
            setattr(s, k, v)
    return skill_out(s)


@router.delete("/skills/{sid}")
async def delete_skill(sid: str, ctx: Ctx = Depends(require_admin)):
    s = await _skill(ctx, sid)
    if s.builtin:
        raise HTTPException(400, "Built-in skills can be edited but not deleted")
    await ctx.db.delete(s)
    return {"ok": True}


@router.post("/skills/{sid}/run")
async def run_skill(sid: str, body: SkillRunIn, ctx: Ctx = Depends(require_member)):
    s = await _skill(ctx, sid)
    missing = [i.get("label") or i.get("key") for i in (s.inputs or [])
               if i.get("required") and not str(body.inputs.get(i.get("key"), "")).strip()]
    if missing:
        raise HTTPException(400, "Missing inputs: " + ", ".join(missing))
    agent = await ctx.db.get(Agent, s.agent_id) if s.agent_id else None
    if agent is None or not agent.enabled:
        agent = (await ctx.db.execute(select(Agent).where(Agent.org_id == ctx.org_id, Agent.enabled.is_(True),
                                                          Agent.is_orchestrator.is_(False)))).scalars().first()
    if agent is None:
        raise HTTPException(400, "No enabled agent can run this skill")
    conv_id = body.conversation_id
    if conv_id:
        conv = await ctx.db.get(Conversation, conv_id)
        if conv is None or conv.user_id != ctx.user_id:
            raise HTTPException(404, "Conversation not found")
    else:
        conv = Conversation(org_id=ctx.org_id, user_id=ctx.user_id, title=s.name)
        ctx.db.add(conv)
        await ctx.db.flush()
        conv_id = conv.id
        await bus.emit(ctx.org_id, "conversation.created", {"conversation": conversation_out(conv)},
                       user_id=ctx.user_id, admin_visible=False)
    await ctx.db.commit()
    task = await start_skill_task(ctx.org_id, ctx.user_id, s, agent, body.inputs, conv_id)
    return {"task": task, "conversation_id": conv_id}


# ------------------------------------------------------------------ memory
class MemoryIn(BaseModel):
    content: str
    kind: str = "semantic"
    scope: str = "user"


class MemoryPatch(BaseModel):
    content: str | None = None
    kind: str | None = None
    importance: float | None = None


@router.get("/memories")
async def list_memories(q: str | None = None, kind: str | None = None, scope: str | None = None,
                        ctx: Ctx = Depends(get_ctx)):
    items = await search_memories(org_id=ctx.org_id, user_id=ctx.user_id, query=q or "", limit=200,
                                  kinds=[kind] if kind else None, scope=scope)
    return {"items": items, "total": len(items)}


@router.post("/memories")
async def create_memory(body: MemoryIn, ctx: Ctx = Depends(require_member)):
    scope = body.scope if body.scope in ("user", "org") else "user"
    if scope == "org" and not ctx.is_admin:
        raise HTTPException(403, "Only admins can add workspace memories")
    await ctx.db.commit()
    return await store_memory(org_id=ctx.org_id, user_id=ctx.user_id, content=body.content, kind=body.kind,
                              scope=scope, source="manual", importance=0.7)


async def _memory(ctx: Ctx, mid: str) -> Memory:
    m = await ctx.db.get(Memory, mid)
    if m is None or m.org_id != ctx.org_id or (m.user_id and m.user_id != ctx.user_id):
        raise HTTPException(404, "Memory not found")
    if m.scope == "org" and not ctx.is_admin:
        raise HTTPException(403, "Only admins can modify workspace memories")
    return m


@router.patch("/memories/{mid}")
async def patch_memory(mid: str, body: MemoryPatch, ctx: Ctx = Depends(get_ctx)):
    m = await _memory(ctx, mid)
    if body.content:
        m.content = body.content.strip()[:4000]
        try:
            from app.rag.embeddings import embed_texts

            m.embedding = (await embed_texts([m.content]))[0]
        except Exception:  # noqa: BLE001
            m.embedding = None
    if body.kind:
        m.kind = body.kind
    if body.importance is not None:
        m.importance = max(0.0, min(1.0, body.importance))
    m.updated_at = utcnow()
    return memory_out(m)


@router.delete("/memories/{mid}")
async def delete_memory(mid: str, ctx: Ctx = Depends(get_ctx)):
    m = await _memory(ctx, mid)
    await ctx.db.delete(m)
    return {"ok": True}


# ------------------------------------------------------------------ files & artifacts
def _ws(ctx: Ctx) -> Workspace:
    return Workspace(ctx.org_id, ctx.user_id)


@router.get("/files")
async def list_files(path: str = "", ctx: Ctx = Depends(get_ctx)):
    try:
        items = _ws(ctx).list(path)
    except WorkspaceError as e:
        raise HTTPException(400, str(e)) from e
    return {"items": items, "total": len(items)}


@router.get("/files/download")
async def download_file(path: str, ctx: Ctx = Depends(get_ctx)):
    try:
        p = _ws(ctx).resolve(path, must_exist=True)
        if p.is_dir():
            raise HTTPException(400, "Path is a folder")
    except WorkspaceError:
        try:
            p, _, _ = await resolve_file(ctx, path)
        except WorkspaceError as e:
            raise HTTPException(404, str(e)) from e
    return FileResponse(p, filename=p.name, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")


@router.get("/files/preview")
async def preview_file(path: str, ctx: Ctx = Depends(get_ctx)):
    try:
        p, norm, art = await resolve_file(ctx, path)
    except WorkspaceError as e:
        raise HTTPException(404, str(e)) from e
    task, agent = await _task_agent(ctx, art.task_id if art else None)
    return await build_preview(p, name=p.name, path=norm, artifact=art, task=task, agent=agent,
                               download={"path": "/files/download", "query": {"path": norm}})


@router.post("/files/upload")
async def upload_file(file: UploadFile = File(...), path: str = Form("uploads"), ctx: Ctx = Depends(require_member)):
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    ws = _ws(ctx)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", file.filename or "upload").strip() or "upload"
    try:
        target = ws.resolve(f"{path.rstrip('/')}/{safe}", for_write=True)
    except WorkspaceError as e:
        raise HTTPException(400, str(e)) from e
    target.write_bytes(data)
    return ws.info(target)


@router.delete("/files")
async def delete_file(path: str, ctx: Ctx = Depends(require_member)):
    ws = _ws(ctx)
    try:
        p = ws.resolve(path, must_exist=True)
    except WorkspaceError as e:
        raise HTTPException(404, str(e)) from e
    if ws.is_shared(path) and not ctx.is_admin:
        raise HTTPException(403, "Only admins can delete shared files")
    if p in (ws.user_root, ws.shared_root) or p.parent == ws.user_root:
        raise HTTPException(400, "Top-level folders cannot be deleted")
    if p.is_dir():
        import shutil

        shutil.rmtree(p)
    else:
        p.unlink()
    await audit(ctx.org_id, "file.deleted", user_id=ctx.user_id, target_type="file", target_id=path, db=ctx.db)
    return {"ok": True}


async def _task_agent(ctx: Ctx, task_id: str | None) -> tuple[Task | None, Agent | None]:
    task = await ctx.db.get(Task, task_id) if task_id else None
    agent = await ctx.db.get(Agent, task.agent_id) if task is not None else None
    return task, agent


async def _artifact(ctx: Ctx, aid: str) -> Artifact:
    a = await ctx.db.get(Artifact, aid)
    if a is None or a.org_id != ctx.org_id or (not ctx.is_admin and a.user_id not in (None, ctx.user_id)):
        raise HTTPException(404, "Artifact not found")
    return a


@router.get("/artifacts")
async def list_artifacts(task_id: str | None = None, conversation_id: str | None = None, q: str | None = None,
                         kind: str | None = None, source: str = "all", limit: int = 100, offset: int = 0,
                         ctx: Ctx = Depends(get_ctx)):
    query = select(Artifact).where(Artifact.org_id == ctx.org_id)
    if not ctx.is_admin:
        query = query.where(Artifact.user_id == ctx.user_id)
    if task_id:
        query = query.where(Artifact.task_id == task_id)
    if conversation_id:
        query = query.where(Artifact.conversation_id == conversation_id)
    if source == "generated":
        query = query.where(Artifact.task_id.is_not(None))
    elif source == "uploaded":
        query = query.where(Artifact.task_id.is_(None))
    elif source != "all":
        raise HTTPException(400, "source must be generated, uploaded or all")
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Artifact.name.ilike(like), Artifact.path.ilike(like)))
    if kind:
        try:
            query = query.where(kind_filter(Artifact.name, kind))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    total = (await ctx.db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = (await ctx.db.execute(query.order_by(Artifact.created_at.desc()).offset(max(0, offset))
                                 .limit(max(1, min(limit, 500))))).scalars().all()
    task_ids = {a.task_id for a in rows if a.task_id}
    tasks = {t.id: t for t in (await ctx.db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()} \
        if task_ids else {}
    agent_ids = {t.agent_id for t in tasks.values()}
    agents = {a.id: a for a in (await ctx.db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars()} \
        if agent_ids else {}
    items = []
    for a in rows:
        t = tasks.get(a.task_id or "")
        items.append(artifact_out(a, t, agents.get(t.agent_id) if t else None))
    return {"items": items, "total": total}


@router.get("/artifacts/{aid}/download")
async def download_artifact(aid: str, ctx: Ctx = Depends(get_ctx)):
    a = await _artifact(ctx, aid)
    try:
        p = artifact_file(ctx, a)
    except WorkspaceError as e:
        raise HTTPException(404, "File no longer exists") from e
    return FileResponse(p, filename=a.name, media_type=a.mime or "application/octet-stream")


@router.get("/artifacts/{aid}/preview")
async def preview_artifact(aid: str, ctx: Ctx = Depends(get_ctx)):
    a = await _artifact(ctx, aid)
    try:
        p = artifact_file(ctx, a)
    except WorkspaceError as e:
        raise HTTPException(404, "File no longer exists") from e
    task, agent = await _task_agent(ctx, a.task_id)
    return await build_preview(p, name=a.name, path=a.path, artifact=a, task=task, agent=agent,
                               download={"path": f"/artifacts/{a.id}/download", "query": {}})


# ------------------------------------------------------------------ audit & notifications
@router.get("/audit")
async def audit_log(action: str | None = None, user_id: str | None = None, agent_id: str | None = None,
                    risk: str | None = None, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(require_admin)):
    q = select(AuditLog).where(AuditLog.org_id == ctx.org_id)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if agent_id:
        q = q.where(AuditLog.agent_id == agent_id)
    if risk:
        q = q.where(AuditLog.risk == risk)
    total = (await ctx.db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await ctx.db.execute(q.order_by(AuditLog.created_at.desc()).offset(max(0, offset))
                                 .limit(max(1, min(limit, 200))))).scalars().all()
    uids = {r.user_id for r in rows if r.user_id}
    aids = {r.agent_id for r in rows if r.agent_id}
    users = {u.id: u.name for u in (await ctx.db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    agents = {a.id: a.name for a in (await ctx.db.execute(select(Agent).where(Agent.id.in_(aids)))).scalars().all()} if aids else {}
    return {"items": [audit_out(r, users.get(r.user_id), agents.get(r.agent_id)) for r in rows], "total": total}


@router.get("/notifications")
async def notifications(unread: bool = False, ctx: Ctx = Depends(get_ctx)):
    q = select(Notification).where(Notification.org_id == ctx.org_id, Notification.user_id == ctx.user_id)
    if unread:
        q = q.where(Notification.read.is_(False))
    rows = (await ctx.db.execute(q.order_by(Notification.created_at.desc()).limit(100))).scalars().all()
    return {"items": [notification_out(n) for n in rows], "total": len(rows)}


@router.post("/notifications/read-all")
async def read_all(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Notification).where(Notification.org_id == ctx.org_id,
                                                            Notification.user_id == ctx.user_id,
                                                            Notification.read.is_(False)))).scalars().all()
    for n in rows:
        n.read = True
    return {"ok": True}


@router.post("/notifications/{nid}/read")
async def read_one(nid: str, ctx: Ctx = Depends(get_ctx)):
    n = await ctx.db.get(Notification, nid)
    if n is None or n.user_id != ctx.user_id:
        raise HTTPException(404, "Notification not found")
    n.read = True
    return {"ok": True}
