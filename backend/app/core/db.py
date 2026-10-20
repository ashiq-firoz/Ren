"""Database engine, session factory and portable column types.

Primary target: PostgreSQL + pgvector (relational + vector + full-text in one store).
Fallback: SQLite (zero-ops single-user installs) — vectors stored as float32 blobs and
searched with numpy; lexical search uses an in-process BM25 index.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.core.config import settings


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


class Vector(TypeDecorator):
    """pgvector `vector(dim)` on PostgreSQL, float32 bytes elsewhere."""

    impl = sa.LargeBinary
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector as PGVector

            return dialect.type_descriptor(PGVector(self.dim))
        return dialect.type_descriptor(sa.LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(map(float, value))
        return np.asarray(value, dtype=np.float32).tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return np.asarray(value, dtype=np.float32)
        return np.frombuffer(value, dtype=np.float32)


NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING)


def _make_engine():
    url = settings.resolved_database_url
    if url.startswith("sqlite"):
        from sqlalchemy.pool import NullPool

        # NullPool: SQLite connections are cheap, and this avoids sharing aiosqlite connections across loops.
        eng = create_async_engine(url, connect_args={"timeout": 30}, poolclass=NullPool)

        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return eng
    return create_async_engine(url, pool_size=20, max_overflow=20, pool_pre_ping=True, pool_recycle=1800)


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

IS_POSTGRES = not settings.is_sqlite


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session for background work: commits on success, rolls back on error."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _add_missing_columns(sync_conn) -> list[str]:
    """Additive schema migration: `create_all` never alters existing tables, so after an upgrade any column
    added to a model is created here (as nullable, with a backfill of simple scalar defaults)."""
    import logging

    insp = sa.inspect(sync_conn)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing or col.computed is not None:
                continue
            ddl_type = col.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
            if col.default is not None and getattr(col.default, "is_scalar", False):
                sync_conn.execute(table.update().values({col.name: col.default.arg}))
            added.append(f"{table.name}.{col.name}")
    if added:
        logging.getLogger("ren.db").warning("schema upgraded: added columns %s", ", ".join(added))
    return added


async def init_db() -> None:
    from app import models  # noqa: F401  (register mappers)

    async with engine.begin() as conn:
        if IS_POSTGRES:
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        if IS_POSTGRES:
            # Full-text column + indexes for lexical retrieval (idempotent).
            await conn.execute(sa.text(
                "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english', coalesce(search_text, ''))) STORED"
            ))
            await conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING GIN (tsv)"))
            await conn.execute(sa.text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
            ))
            await conn.execute(sa.text(
                "CREATE INDEX IF NOT EXISTS ix_section_questions_embedding ON section_questions "
                "USING hnsw (embedding vector_cosine_ops)"
            ))
            await conn.execute(sa.text(
                "CREATE INDEX IF NOT EXISTS ix_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops)"
            ))
