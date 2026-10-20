"""Memory, connectors, MCP servers, tool settings, audit, notifications, alignment checks, usage."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.db import Base, JSONType, Vector, new_id, utcnow


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)  # null => org-wide
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    scope: Mapped[str] = mapped_column(sa.String(10), default="user")  # user | org
    kind: Mapped[str] = mapped_column(sa.String(20), default="semantic")
    content: Mapped[str] = mapped_column(sa.Text)
    importance: Mapped[float] = mapped_column(sa.Float, default=0.5)
    source: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    embedding = mapped_column(Vector(settings.embedding_dim), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Connector(Base):
    """Credentials + config for an external integration (Gmail, Slack, GitHub, ...)."""

    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(60), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    config: Mapped[dict] = mapped_column(JSONType, default=dict)  # non-secret fields
    secrets_enc: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # encrypted json of secret fields
    status: Mapped[str] = mapped_column(sa.String(20), default="unknown")
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    transport: Mapped[str] = mapped_column(sa.String(10))  # stdio | http | sse
    command: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    args: Mapped[list] = mapped_column(JSONType, default=list)
    url: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    env_enc: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    headers_enc: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    env_keys: Mapped[list] = mapped_column(JSONType, default=list)
    header_keys: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    status: Mapped[str] = mapped_column(sa.String(20), default="disconnected")
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tools_cache: Mapped[list] = mapped_column(JSONType, default=list)  # [{name, description, input_schema}]
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class ToolGroupSetting(Base):
    __tablename__ = "tool_group_settings"
    __table_args__ = (sa.UniqueConstraint("org_id", "group_id", name="uq_toolgroup_org_group"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[str] = mapped_column(sa.String(120))
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (sa.Index("ix_audit_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36))
    user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(120), index=True)
    target_type: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    risk: Mapped[str | None] = mapped_column(sa.String(10), nullable=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    user_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    kind: Mapped[str] = mapped_column(sa.String(60))
    title: Mapped[str] = mapped_column(sa.String(300))
    body: Mapped[str] = mapped_column(sa.Text, default="")
    link: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    read: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class AlignmentCheck(Base):
    __tablename__ = "alignment_checks"
    __table_args__ = (sa.Index("ix_alignment_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36))
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    message_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(20), default="response")
    verdict: Mapped[str] = mapped_column(sa.String(10))
    checker: Mapped[str] = mapped_column(sa.String(30))
    latency_ms: Mapped[int] = mapped_column(sa.Integer, default=0)
    violations: Mapped[list] = mapped_column(JSONType, default=list)
    excerpt: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (sa.Index("ix_usage_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36))
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    model: Mapped[str] = mapped_column(sa.String(300))
    purpose: Mapped[str] = mapped_column(sa.String(40), default="agent")  # agent|orchestrator|rag|alignment|embedding
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class ScheduledJob(Base):
    """Deferred / recurring work: reminders (kind=notification) and scheduled agent tasks (kind=task)."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(20))  # notification | task
    title: Mapped[str] = mapped_column(sa.String(300), default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    run_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    cron: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)  # recurring (croniter syntax)
    timezone: Mapped[str] = mapped_column(sa.String(64), default="UTC")  # IANA zone the cron / run_at is read in
    # scheduled | paused | done | cancelled | failed
    status: Mapped[str] = mapped_column(sa.String(20), default="scheduled")
    last_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    run_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    last_task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
