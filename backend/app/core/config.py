"""Application settings (environment-driven)."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

VERSION = "1.0.0"


def _env_files() -> tuple[str, ...]:
    # REN_NO_DOTENV=1 (set by the test suite) → never read .env files, so tests can't pick up real credentials.
    import os

    if os.environ.get("REN_NO_DOTENV"):
        return ()
    return (str(BACKEND_DIR / ".env"), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_files(), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Ren"
    env: str = "development"
    log_level: str = "INFO"
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))

    # --- storage -----------------------------------------------------------------
    database_url: str = ""  # default resolved in validator (sqlite under data_dir)
    redis_url: str | None = None
    data_dir: Path = BACKEND_DIR / "data"

    # --- web ---------------------------------------------------------------------
    frontend_url: str = "http://localhost:3000"
    public_api_url: str = "http://localhost:8000"
    # NoDecode: accept "a,b" (comma list) as well as JSON from env / .env files
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    access_token_expire_minutes: int = 60 * 24 * 7

    # --- default LLM provider bootstrapped from env (optional) --------------------
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4.1-mini"
    # comma list; OpenRouter tries these in order only when the primary model is unavailable / rate-limited
    openrouter_fallback_models: str = ""
    vllm_base_url: str | None = None
    vllm_model: str | None = None
    vllm_api_key: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    # Google AI Studio (Gemini API, OpenAI-compatible endpoint). Model ids have no "google/" prefix or ":free" suffix.
    google_ai_studio_key: str | None = None
    google_ai_studio_model: str = "gemma-4-26b-a4b-it"
    llm_timeout_seconds: float = 180.0

    # --- retrieval ---------------------------------------------------------------
    embedding_provider: str = "fastembed"  # fastembed | openai | hash
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    reranker_provider: str = "fastembed"  # fastembed | llm | none
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    max_upload_mb: int = 100

    # --- alignment (Laya) ----------------------------------------------------------
    laya_url: str | None = None  # e.g. http://laya:8000 (laya-serve)
    laya_api_key: str | None = None
    laya_model: str = "auto"  # auto | english | multilingual | typed-decisions
    laya_local: bool = False  # load laya in-process (requires `pip install laya`)
    laya_timeout_seconds: float = 10.0

    # --- execution sandbox ---------------------------------------------------------
    sandbox_url: str | None = None  # remote sandbox exec server (docker deployments)
    sandbox_token: str | None = None
    shell_timeout_seconds: int = 120
    allow_private_network: bool = False  # let web/browser tools reach localhost / private IPs (SSRF guard)

    # --- tools ---------------------------------------------------------------------
    web_search_provider: str = "duckduckgo"
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    searxng_url: str | None = None

    # --- runtime ---------------------------------------------------------------------
    max_concurrent_tasks: int = 6
    max_delegation_depth: int = 3
    seed_demo: bool = False

    @model_validator(mode="before")
    @classmethod
    def _empty_means_default(cls, data):
        """`KEY=` in .env means "use the default", never an empty value (e.g. an empty SECRET_KEY)."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
        return data

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            url = self.database_url
            if url.startswith("postgres://"):
                url = "postgresql+asyncpg://" + url[len("postgres://"):]
            elif url.startswith("postgresql://"):
                url = "postgresql+asyncpg://" + url[len("postgresql://"):]
            return url
        db_path = (self.data_dir / "ren.db").resolve()
        return f"sqlite+aiosqlite:///{db_path.as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir = Path(s.data_dir).resolve()
    for d in (s.data_dir, s.uploads_dir, s.workspaces_dir, s.models_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Persist an auto-generated secret so tokens/encrypted secrets survive restarts in dev.
    if "secret_key" not in s.model_fields_set:
        key_file = s.data_dir / ".secret_key"
        if key_file.exists():
            s.secret_key = key_file.read_text().strip()
        else:
            key_file.write_text(s.secret_key)
    return s


settings = get_settings()
