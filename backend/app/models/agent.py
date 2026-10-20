"""Agents, policies, skills."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType, new_id, utcnow


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (sa.UniqueConstraint("org_id", "slug", name="uq_agent_org_slug"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    slug: Mapped[str] = mapped_column(sa.String(120))
    role: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    system_prompt: Mapped[str] = mapped_column(sa.Text, default="")
    provider_id: Mapped[str | None] = mapped_column(sa.ForeignKey("providers.id", ondelete="SET NULL"), nullable=True)
    model: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    temperature: Mapped[float] = mapped_column(sa.Float, default=0.3)
    tool_groups: Mapped[list] = mapped_column(JSONType, default=list)
    policy_ids: Mapped[list] = mapped_column(JSONType, default=list)
    collection_ids: Mapped[list] = mapped_column(JSONType, default=list)
    autonomy: Mapped[str] = mapped_column(sa.String(20), default="balanced")
    max_steps: Mapped[int] = mapped_column(sa.Integer, default=24)
    color: Mapped[str] = mapped_column(sa.String(20), default="#56665D")
    icon: Mapped[str] = mapped_column(sa.String(60), default="bot")
    is_orchestrator: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    category: Mapped[str] = mapped_column(sa.String(60), default="custom")
    rules: Mapped[list] = mapped_column(JSONType, default=list)
    patterns: Mapped[list] = mapped_column(JSONType, default=list)
    severity: Mapped[str] = mapped_column(sa.String(10), default="warn")  # block | warn
    scope: Mapped[str] = mapped_column(sa.String(10), default="org")  # org | agent
    applies_to: Mapped[str] = mapped_column(sa.String(20), default="all")  # responses | outbound | all
    agent_ids: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (sa.UniqueConstraint("org_id", "slug", name="uq_skill_org_slug"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    slug: Mapped[str] = mapped_column(sa.String(120))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    category: Mapped[str] = mapped_column(sa.String(60), default="general")
    icon: Mapped[str] = mapped_column(sa.String(60), default="sparkles")
    instructions: Mapped[str] = mapped_column(sa.Text, default="")
    agent_id: Mapped[str | None] = mapped_column(sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    tool_groups: Mapped[list] = mapped_column(JSONType, default=list)
    inputs: Mapped[list] = mapped_column(JSONType, default=list)
    builtin: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
