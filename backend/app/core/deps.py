"""FastAPI dependencies: authentication, workspace membership and role checks."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db, utcnow
from app.core.security import decode_access_token
from app.models import ROLE_ORDER, Membership, Organization, User


@dataclass
class Ctx:
    """Request context for org-scoped routes."""

    user: User
    org: Organization
    membership: Membership
    db: AsyncSession

    @property
    def role(self) -> str:
        return self.membership.role

    @property
    def org_id(self) -> str:
        return self.org.id

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER["admin"]

    def require(self, min_role: str) -> None:
        if ROLE_ORDER.get(self.role, 0) < ROLE_ORDER[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {min_role} role")


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def user_from_token(db: AsyncSession, token: str | None) -> User | None:
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or payload.get("typ") != "access":
        return None
    user = await db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        return None
    return user


async def get_current_user(
    authorization: str | None = Header(default=None),
    x_timezone: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await user_from_token(db, _bearer(authorization))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    now = utcnow()
    last = user.last_active_at
    if last is None or (now - (last if last.tzinfo else last.replace(tzinfo=now.tzinfo))).total_seconds() > 300:
        user.last_active_at = now
    if x_timezone and x_timezone != user.timezone:
        from app.services.scheduler import valid_timezone

        tz = valid_timezone(x_timezone)
        if tz and tz != user.timezone:
            user.timezone = tz  # the browser's zone: default for schedules, "today" in prompts
    return user


async def resolve_ctx(db: AsyncSession, user: User, org_id: str | None) -> Ctx:
    if not org_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing X-Org-Id header")
    row = (await db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user.id, Membership.org_id == org_id)
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this workspace")
    membership, org = row
    return Ctx(user=user, org=org, membership=membership, db=db)


async def get_ctx(
    user: User = Depends(get_current_user),
    x_org_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Ctx:
    return await resolve_ctx(db, user, x_org_id)


def require_role(min_role: str):
    async def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        ctx.require(min_role)
        return ctx

    return dep


require_admin = require_role("admin")
require_member = require_role("member")
