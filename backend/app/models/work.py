"""Conversations, messages, tasks, task events, approvals, artifacts."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType, new_id, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(sa.String(300), default="New conversation")
    pinned: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    archived: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # rolling summary of older turns
    summary_upto: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, index=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (sa.Index("ix_messages_conv_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(sa.ForeignKey("conversations.id", ondelete="CASCADE"))
    org_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    role: Mapped[str] = mapped_column(sa.String(20))  # user | assistant | event | system
    content: Mapped[str] = mapped_column(sa.Text, default="")
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(sa.String(20), default="complete")
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    processed: Mapped[bool] = mapped_column(sa.Boolean, default=True)  # user msgs: seen by orchestrator
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (sa.Index("ix_tasks_org_status", "org_id", "status"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(sa.ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    skill_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)  # started by a schedule
    created_by: Mapped[str | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(sa.String(300))
    instructions: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[str] = mapped_column(sa.String(30), default="queued")
    priority: Mapped[str] = mapped_column(sa.String(10), default="normal")
    progress: Mapped[int] = mapped_column(sa.Integer, default=0)
    current_step: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    result: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    steps: Mapped[int] = mapped_column(sa.Integer, default=0)
    tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    depth: Mapped[int] = mapped_column(sa.Integer, default=0)
    checkpoint: Mapped[dict] = mapped_column(JSONType, default=dict)  # agent loop state for resume
    reported: Mapped[bool] = mapped_column(sa.Boolean, default=False)  # orchestrator has reported the outcome
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (sa.Index("ix_task_events_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(sa.ForeignKey("tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(sa.String(30))
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    consumed: Mapped[bool] = mapped_column(sa.Boolean, default=True)  # inbox events: picked up by agent
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    requested_for_user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    tool: Mapped[str] = mapped_column(sa.String(200))
    args: Mapped[dict] = mapped_column(JSONType, default=dict)
    risk: Mapped[str] = mapped_column(sa.String(10))
    reason: Mapped[str] = mapped_column(sa.Text, default="")
    preview: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[str] = mapped_column(sa.String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    modified_args: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(300))
    path: Mapped[str] = mapped_column(sa.String(1000))  # workspace-relative path
    mime: Mapped[str] = mapped_column(sa.String(200), default="application/octet-stream")
    size: Mapped[int] = mapped_column(sa.Integer, default=0)
    document_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)  # knowledge doc (chat uploads)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
