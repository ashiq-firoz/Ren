"""Knowledge base for STAIR-style structure-aware hybrid retrieval.

Document → Section tree (the Table of Contents; every piece of text lives in exactly one *leaf*)
         → Chunks (retrieval units inside leaves, embedded + full-text indexed)
         → SectionQuestions (synthetic questions per leaf: doc2query signal + STAIR fine-tuning data)
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.db import Base, JSONType, Vector, new_id, utcnow

EMBED_DIM = settings.embedding_dim


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    access: Mapped[str] = mapped_column(sa.String(20), default="org")  # org | restricted
    member_ids: Mapped[list] = mapped_column(JSONType, default=list)
    system: Mapped[bool] = mapped_column(sa.Boolean, default=False)  # e.g. "Chat Uploads"
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    collection_id: Mapped[str] = mapped_column(sa.ForeignKey("collections.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(sa.String(500))
    filename: Mapped[str] = mapped_column(sa.String(500))
    mime: Mapped[str] = mapped_column(sa.String(200), default="application/octet-stream")
    size: Mapped[int] = mapped_column(sa.Integer, default=0)
    storage_path: Mapped[str] = mapped_column(sa.String(1000))
    status: Mapped[str] = mapped_column(sa.String(20), default="uploaded")
    progress: Mapped[int] = mapped_column(sa.Integer, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    section_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    leaf_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    question_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    structure_source: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)  # native|headings|augmented|flat
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    doc_embedding = mapped_column(Vector(EMBED_DIM), nullable=True)  # title + summary + top-level ToC
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    uploaded_by: Mapped[str | None] = mapped_column(sa.String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Section(Base):
    """A node of the document's Table of Contents. Leaves are STAIR retrieval targets."""

    __tablename__ = "sections"
    __table_args__ = (sa.Index("ix_sections_doc_ordinal", "document_id", "ordinal"),)

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    document_id: Mapped[str] = mapped_column(sa.ForeignKey("documents.id", ondelete="CASCADE"))
    parent_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    path: Mapped[str] = mapped_column(sa.String(100))  # hierarchical number, e.g. "4.1.1"
    title: Mapped[str] = mapped_column(sa.String(500))
    level: Mapped[int] = mapped_column(sa.Integer, default=1)
    ordinal: Mapped[int] = mapped_column(sa.Integer, default=0)  # document order
    is_leaf: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    content: Mapped[str] = mapped_column(sa.Text, default="")  # leaf text (non-leaves: empty)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    breadcrumb: Mapped[str] = mapped_column(sa.Text, default="")  # "Doc > Chapter > Section"
    token_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    collection_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    document_id: Mapped[str] = mapped_column(sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[str | None] = mapped_column(sa.String(36), nullable=True, index=True)
    ordinal: Mapped[int] = mapped_column(sa.Integer, default=0)
    content: Mapped[str] = mapped_column(sa.Text)
    search_text: Mapped[str] = mapped_column(sa.Text, default="")  # breadcrumb + content (what gets indexed)
    token_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)


class SectionQuestion(Base):
    __tablename__ = "section_questions"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    document_id: Mapped[str] = mapped_column(sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    question: Mapped[str] = mapped_column(sa.Text)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
