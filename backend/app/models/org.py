"""Identity & tenancy: organizations (workspaces), users, memberships, invites, LLM providers."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType, new_id, utcnow

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(sa.String(200))
    slug: Mapped[str] = mapped_column(sa.String(120), unique=True, index=True)
    mode: Mapped[str] = mapped_column(sa.String(20), default="business")  # business | personal
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    secrets_enc: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # encrypted json (e.g. web search key)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="org", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    password_hash: Mapped[str] = mapped_column(sa.String(200))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)  # IANA, from the browser (X-Timezone)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (sa.UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(sa.String(20), default="member")
    title: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(sa.String(320), index=True)
    role: Mapped[str] = mapped_column(sa.String(20), default="member")
    token: Mapped[str] = mapped_column(sa.String(100), unique=True, index=True)
    invited_by: Mapped[str | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    accepted: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class Provider(Base):
    """An OpenAI-compatible LLM endpoint (OpenRouter, vLLM, OpenAI, custom)."""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    kind: Mapped[str] = mapped_column(sa.String(30))  # openrouter | vllm | openai | custom | mock
    base_url: Mapped[str] = mapped_column(sa.String(500))
    api_key_enc: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    default_model: Mapped[str] = mapped_column(sa.String(300))
    is_default: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    supports_tools: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    extra: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
