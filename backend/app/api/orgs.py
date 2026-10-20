"""Workspaces: settings, members, invitations."""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.validation import Email
from app.core.db import get_db, utcnow
from app.core.deps import Ctx, get_ctx, get_current_user, require_admin, require_role
from app.core.security import decrypt_json, encrypt_json
from app.models import Invite, Membership, User
from app.services.audit import audit
from app.services.seed import create_org
from app.services.serialize import deep_merge, invite_out, member_out, org_out, org_summary

router = APIRouter(prefix="/api/orgs", tags=["orgs"])

ROLES = ("owner", "admin", "member", "viewer")


class OrgCreate(BaseModel):
    name: str
    mode: str = "business"


class OrgPatch(BaseModel):
    name: str | None = None
    mode: str | None = None
    settings: dict | None = None


class MemberPatch(BaseModel):
    role: str | None = None
    title: str | None = None


class InviteIn(BaseModel):
    email: Email
    role: str = "member"


async def _org_out(ctx: Ctx) -> dict:
    count = (await ctx.db.execute(select(func.count()).select_from(Membership)
                                  .where(Membership.org_id == ctx.org_id))).scalar() or 0
    has_key = bool(decrypt_json(ctx.org.secrets_enc).get("web_search_api_key"))
    return org_out(ctx.org, count, has_key)


@router.get("")
async def list_orgs(user: User = Depends(get_current_user), db=Depends(get_db)):
    from app.api.auth import user_orgs

    items = await user_orgs(db, user.id)
    return {"items": items, "total": len(items)}


@router.post("")
async def create(body: OrgCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    org = await create_org(db, name=body.name, mode=body.mode, owner_id=user.id)
    await audit(org.id, "org.created", user_id=user.id, target_type="org", target_id=org.id, db=db)
    return org_summary(org, "owner")


@router.get("/current")
async def current(ctx: Ctx = Depends(get_ctx)):
    return await _org_out(ctx)


@router.patch("/current")
async def patch_current(body: OrgPatch, ctx: Ctx = Depends(require_admin)):
    org = ctx.org
    if body.name:
        org.name = body.name.strip()[:200]
    if body.mode in ("business", "personal"):
        org.mode = body.mode
    if body.settings:
        incoming = dict(body.settings)
        key = incoming.pop("web_search_api_key", None)
        if "web_search" in incoming and isinstance(incoming["web_search"], dict):
            incoming["web_search"] = {k: v for k, v in incoming["web_search"].items() if k != "has_key"}
        org.settings = deep_merge(org.settings or {}, incoming)
        if key is not None:
            secrets_ = decrypt_json(org.secrets_enc)
            if key:
                secrets_["web_search_api_key"] = key
            else:
                secrets_.pop("web_search_api_key", None)
            org.secrets_enc = encrypt_json(secrets_)
        if "max_concurrent_tasks" in incoming:
            from app.agents.task_manager import task_manager

            task_manager._org_sems.pop(org.id, None)  # re-read the limit on next task
    await audit(org.id, "org.updated", user_id=ctx.user_id, target_type="org", target_id=org.id,
                details={"fields": [k for k, v in body.model_dump().items() if v is not None]}, db=ctx.db)
    return await _org_out(ctx)


@router.delete("/current")
async def delete_current(ctx: Ctx = Depends(require_role("owner"))):
    await ctx.db.delete(ctx.org)
    return {"ok": True}


@router.get("/current/members")
async def members(ctx: Ctx = Depends(get_ctx)):
    rows = (await ctx.db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                                 .where(Membership.org_id == ctx.org_id).order_by(Membership.created_at))).all()
    items = [member_out(m, u) for m, u in rows]
    return {"items": items, "total": len(items)}


async def _owner_count(ctx: Ctx) -> int:
    return (await ctx.db.execute(select(func.count()).select_from(Membership)
                                 .where(Membership.org_id == ctx.org_id, Membership.role == "owner"))).scalar() or 0


@router.patch("/current/members/{user_id}")
async def patch_member(user_id: str, body: MemberPatch, ctx: Ctx = Depends(require_admin)):
    row = (await ctx.db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                                .where(Membership.org_id == ctx.org_id, Membership.user_id == user_id))).first()
    if row is None:
        raise HTTPException(404, "Member not found")
    m, u = row
    if body.role:
        if body.role not in ROLES:
            raise HTTPException(400, "Invalid role")
        if (body.role == "owner" or m.role == "owner") and ctx.role != "owner":
            raise HTTPException(403, "Only owners can grant or change the owner role")
        if m.role == "owner" and body.role != "owner" and await _owner_count(ctx) <= 1:
            raise HTTPException(400, "A workspace needs at least one owner")
        m.role = body.role
    if body.title is not None:
        m.title = body.title.strip()[:200] or None
    await audit(ctx.org_id, "member.updated", user_id=ctx.user_id, target_type="user", target_id=user_id,
                details=body.model_dump(exclude_none=True), db=ctx.db)
    return member_out(m, u)


@router.delete("/current/members/{user_id}")
async def remove_member(user_id: str, ctx: Ctx = Depends(require_admin)):
    m = (await ctx.db.execute(select(Membership).where(Membership.org_id == ctx.org_id,
                                                       Membership.user_id == user_id))).scalars().first()
    if m is None:
        raise HTTPException(404, "Member not found")
    if m.role == "owner" and (ctx.role != "owner" or await _owner_count(ctx) <= 1):
        raise HTTPException(400, "Cannot remove this owner")
    await ctx.db.delete(m)
    await audit(ctx.org_id, "member.removed", user_id=ctx.user_id, target_type="user", target_id=user_id, db=ctx.db)
    return {"ok": True}


@router.get("/current/invites")
async def invites(ctx: Ctx = Depends(require_admin)):
    rows = (await ctx.db.execute(select(Invite).where(Invite.org_id == ctx.org_id, Invite.accepted.is_(False))
                                 .order_by(Invite.created_at.desc()))).scalars().all()
    names = {}
    for i in rows:
        if i.invited_by and i.invited_by not in names:
            u = await ctx.db.get(User, i.invited_by)
            names[i.invited_by] = u.name if u else None
    items = [invite_out(i, names.get(i.invited_by)) for i in rows]
    return {"items": items, "total": len(items)}


@router.post("/current/invites")
async def create_invite(body: InviteIn, ctx: Ctx = Depends(require_admin)):
    if body.role not in ROLES or (body.role == "owner" and ctx.role != "owner"):
        raise HTTPException(400, "Invalid role")
    email = body.email.lower()
    already = (await ctx.db.execute(select(Membership).join(User, User.id == Membership.user_id)
                                    .where(Membership.org_id == ctx.org_id, func.lower(User.email) == email))).first()
    if already:
        raise HTTPException(409, "This person is already a member")
    inv = Invite(org_id=ctx.org_id, email=email, role=body.role, token=secrets.token_urlsafe(24),
                 invited_by=ctx.user_id, expires_at=utcnow() + timedelta(days=7))
    ctx.db.add(inv)
    await ctx.db.flush()
    await audit(ctx.org_id, "member.invited", user_id=ctx.user_id, target_type="invite", target_id=inv.id,
                details={"email": email, "role": body.role}, db=ctx.db)
    return invite_out(inv, ctx.user.name)


@router.delete("/current/invites/{invite_id}")
async def revoke_invite(invite_id: str, ctx: Ctx = Depends(require_admin)):
    inv = await ctx.db.get(Invite, invite_id)
    if inv is None or inv.org_id != ctx.org_id:
        raise HTTPException(404, "Invite not found")
    await ctx.db.delete(inv)
    return {"ok": True}
