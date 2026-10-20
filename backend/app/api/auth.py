"""Authentication: register, login, profile, invitations."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.validation import Email
from app.core.db import as_utc, get_db, utcnow
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Invite, Membership, Organization, User
from app.services.audit import audit
from app.services.seed import create_org
from app.services.serialize import org_summary, user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Brute-force protection: at most 8 failed logins per email+IP per 10 minutes (per process).
_FAILED: dict[str, deque] = defaultdict(deque)
_WINDOW, _MAX_FAILS = 600, 8


def _throttle_key(request: Request, email: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{email.lower()}|{ip}"


def _check_throttle(key: str) -> None:
    q = _FAILED[key]
    now = time.time()
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= _MAX_FAILS:
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.")


class RegisterIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    org_name: str | None = None
    mode: str = "business"


class LoginIn(BaseModel):
    email: Email
    password: str


class MePatch(BaseModel):
    name: str | None = None
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class AcceptInviteIn(BaseModel):
    token: str
    name: str | None = None
    password: str | None = None


async def user_orgs(db: AsyncSession, user_id: str) -> list[dict]:
    rows = (await db.execute(
        select(Membership, Organization).join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user_id).order_by(Organization.created_at)
    )).all()
    return [org_summary(o, m.role) for m, o in rows]


async def auth_response(db: AsyncSession, user: User) -> dict:
    return {"access_token": create_access_token(user.id), "token_type": "bearer", "user": user_out(user),
            "orgs": await user_orgs(db, user.id)}


@router.post("/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    email = body.email.lower().strip()
    if (await db.execute(select(User.id).where(func.lower(User.email) == email))).first():
        raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password), last_active_at=utcnow())
    db.add(user)
    await db.flush()
    mode = body.mode if body.mode in ("business", "personal") else "business"
    org_name = (body.org_name or "").strip() or (f"{body.name.split()[0]}'s Workspace" if mode == "personal"
                                                 else f"{body.name.split()[0]}'s Company")
    org = await create_org(db, name=org_name, mode=mode, owner_id=user.id)
    await audit(org.id, "auth.registered", user_id=user.id, target_type="user", target_id=user.id, db=db)
    return await auth_response(db, user)


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    key = _throttle_key(request, body.email)
    _check_throttle(key)
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower().strip()))).scalars().first()
    if user is None or not verify_password(body.password, user.password_hash):
        _FAILED[key].append(time.time())
        raise HTTPException(401, "Invalid email or password")
    _FAILED.pop(key, None)
    if not user.is_active:
        raise HTTPException(403, "Account disabled")
    user.last_active_at = utcnow()
    return await auth_response(db, user)


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return {"user": user_out(user), "orgs": await user_orgs(db, user.id)}


@router.patch("/me")
async def patch_me(body: MePatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user.id)
    if body.name:
        u.name = body.name.strip()[:200]
    if body.new_password:
        if not body.current_password or not verify_password(body.current_password, u.password_hash):
            raise HTTPException(400, "Current password is incorrect")
        u.password_hash = hash_password(body.new_password)
    return user_out(u)


async def _valid_invite(db: AsyncSession, token: str) -> Invite:
    inv = (await db.execute(select(Invite).where(Invite.token == token))).scalars().first()
    if inv is None or inv.accepted:
        raise HTTPException(404, "Invitation not found or already used")
    if as_utc(inv.expires_at) < utcnow():
        raise HTTPException(410, "Invitation expired")
    return inv


@router.get("/invite/{token}")
async def get_invite(token: str, db: AsyncSession = Depends(get_db)):
    inv = await _valid_invite(db, token)
    org = await db.get(Organization, inv.org_id)
    inviter = await db.get(User, inv.invited_by) if inv.invited_by else None
    exists = (await db.execute(select(User.id).where(func.lower(User.email) == inv.email.lower()))).first() is not None
    return {"email": inv.email, "org_name": org.name if org else "", "role": inv.role,
            "inviter_name": inviter.name if inviter else None, "user_exists": exists}


@router.post("/accept-invite")
async def accept_invite(body: AcceptInviteIn, db: AsyncSession = Depends(get_db)):
    inv = await _valid_invite(db, body.token)
    user = (await db.execute(select(User).where(func.lower(User.email) == inv.email.lower()))).scalars().first()
    if user is None:
        if not body.name or not body.password or len(body.password) < 8:
            raise HTTPException(400, "Name and a password of at least 8 characters are required")
        user = User(email=inv.email.lower(), name=body.name.strip(), password_hash=hash_password(body.password))
        db.add(user)
        await db.flush()
    elif not body.password or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Enter the password of your existing account to accept")
    exists = (await db.execute(select(Membership).where(Membership.org_id == inv.org_id,
                                                        Membership.user_id == user.id))).scalars().first()
    if exists is None:
        db.add(Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
    inv.accepted = True
    user.last_active_at = utcnow()
    await db.flush()
    await audit(inv.org_id, "member.joined", user_id=user.id, target_type="user", target_id=user.id,
                details={"role": inv.role}, db=db)
    return await auth_response(db, user)
