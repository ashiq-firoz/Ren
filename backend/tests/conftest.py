"""Test isolation: every test session gets its own temp data dir + SQLite DB, hashing embeddings,
and an org/user with a mock LLM provider."""
import asyncio
import os
import pathlib
import tempfile

_tmp = tempfile.mkdtemp(prefix="ren-test-")
os.environ["REN_NO_DOTENV"] = "1"  # never load backend/.env (real keys) in tests
os.environ["DATA_DIR"] = _tmp
# REN_TEST_DATABASE_URL=postgresql+asyncpg://… runs the suite against PostgreSQL (schema is reset per session).
os.environ["DATABASE_URL"] = os.environ.get("REN_TEST_DATABASE_URL") or     f"sqlite+aiosqlite:///{pathlib.Path(_tmp, 'test.db').as_posix()}"
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("RERANKER_PROVIDER", "none")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
for k in ("OPENROUTER_API_KEY", "VLLM_BASE_URL", "OPENAI_API_KEY", "REDIS_URL", "SANDBOX_URL", "LAYA_URL"):
    os.environ.pop(k, None)

import pytest  # noqa: E402

_state: dict = {}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def db_ready():
    from app.core.db import IS_POSTGRES, engine, init_db

    async def reset_and_init():
        if IS_POSTGRES:
            import sqlalchemy as sa

            async with engine.begin() as conn:
                await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
                await conn.execute(sa.text("CREATE SCHEMA public"))
        await init_db()
        await engine.dispose()

    _run(reset_and_init())
    return True


@pytest.fixture(scope="session")
def org_ctx(db_ready):
    """Creates (once) an org + owner user + mock provider. Returns dict(org_id, user_id, provider_id)."""
    if "org" in _state:
        return _state["org"]
    from app.core.db import session_scope
    from app.core.security import hash_password
    from app.models import Membership, Organization, Provider, User

    async def make():
        async with session_scope() as db:
            org = Organization(name="Test Org", slug="test-org", mode="business", settings={})
            user = User(email="owner@test.dev", name="Test Owner", password_hash=hash_password("password123"))
            db.add_all([org, user])
            await db.flush()
            db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
            prov = Provider(org_id=org.id, name="Mock", kind="mock", base_url="mock://local",
                            default_model="mock-1", is_default=True)
            db.add(prov)
            await db.flush()
            org.settings = {"default_provider_id": prov.id}
            return {"org_id": org.id, "user_id": user.id, "provider_id": prov.id}

    _state["org"] = _run(make())
    return _state["org"]


@pytest.fixture
def tool_ctx(org_ctx):
    from app.tools.base import ToolContext

    return ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="owner",
                       agent_name="Test Agent")
