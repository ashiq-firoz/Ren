"""In-app notifications (bell menu) pushed live over the event stream."""
from __future__ import annotations

from sqlalchemy import select

from app.core.db import session_scope
from app.core.events import bus
from app.models import Membership, Notification
from app.services.serialize import notification_out


async def notify_user(org_id: str, user_id: str, kind: str, title: str, body: str = "",
                      link: str | None = None) -> dict:
    async with session_scope() as db:
        n = Notification(org_id=org_id, user_id=user_id, kind=kind, title=title[:300], body=body[:4000], link=link)
        db.add(n)
        await db.flush()
        out = notification_out(n)
    await bus.emit(org_id, "notification.created", {"notification": out}, user_id=user_id, admin_visible=False)
    return out


async def notify_admins(org_id: str, kind: str, title: str, body: str = "", link: str | None = None) -> int:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Membership.user_id).where(Membership.org_id == org_id, Membership.role.in_(("admin", "owner")))
        )).scalars().all()
    for uid in rows:
        await notify_user(org_id, uid, kind, title, body, link)
    return len(rows)
