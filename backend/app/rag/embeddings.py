"""Text embeddings (public API used across Ren).

    await embed_texts(["passage", ...]) -> list[np.ndarray]   # L2-normalized float32, dim = settings.embedding_dim
    await embed_query("question")       -> np.ndarray
    embedding_info()                    -> {"provider", "model", "dim"}

Providers (`settings.embedding_provider`):
  - `fastembed` (default): local ONNX model (`BAAI/bge-small-en-v1.5`, 384-d), lazy-loaded singleton cached under
    `settings.models_dir`, batched, run in a worker thread;
  - `openai`: any OpenAI-compatible `/embeddings` endpoint (`settings.embedding_base_url` + key — e.g. a vLLM- or
    TEI-served embedding model, or OpenAI);
  - `hash`: deterministic feature-hashing embeddings (signed unigram + bigram hashing) — offline/tests.
Vectors always have exactly `settings.embedding_dim` dimensions (padded/truncated with a warning on mismatch).
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import zlib
from collections import Counter, OrderedDict

import numpy as np

from app.core.config import settings
from app.rag.textutil import tokenize

log = logging.getLogger("ren.rag.embeddings")

MAX_INPUT_CHARS = 8000
FASTEMBED_BATCH = 32
OPENAI_BATCH = 64

_fe_model = None
_fe_lock = threading.Lock()
_fe_run_lock = threading.Lock()
_dim_warned = False
_query_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
_QUERY_CACHE_SIZE = 512


class EmbeddingError(RuntimeError):
    pass


def _provider() -> str:
    return (settings.embedding_provider or "fastembed").strip().lower()


def embedding_info() -> dict:
    p = _provider()
    model = "feature-hashing (offline)" if p == "hash" else settings.embedding_model
    return {"provider": p, "model": model, "dim": int(settings.embedding_dim)}


def _fit(vec, dim: int) -> np.ndarray:
    global _dim_warned
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    if v.shape[0] != dim:
        if not _dim_warned:
            log.warning("embedding model returned %d dims but EMBEDDING_DIM=%d; %s", v.shape[0], dim,
                        "truncating" if v.shape[0] > dim else "zero-padding")
            _dim_warned = True
        v = v[:dim] if v.shape[0] > dim else np.pad(v, (0, dim - v.shape[0]))
    n = float(np.linalg.norm(v))
    if not math.isfinite(n) or n == 0.0:
        return np.zeros(dim, dtype=np.float32)
    return (v / n).astype(np.float32)


# --------------------------------------------------------------------------- hashing provider
def hash_embed(text: str, dim: int | None = None) -> np.ndarray:
    """Deterministic signed feature hashing of stemmed unigrams (sublinear tf) + bigrams; L2-normalized."""
    dim = int(dim or settings.embedding_dim)
    toks = tokenize(text)
    v = np.zeros(dim, dtype=np.float32)
    feats: dict[str, float] = {}
    for t, c in Counter(toks).items():
        feats[t] = 1.0 + math.log(c)
    for a, b in zip(toks, toks[1:]):
        k = f"{a}\x1f{b}"
        feats[k] = feats.get(k, 0.0) + 0.5
    if not feats:
        raw = (text or "").strip().lower()
        if not raw:
            return v
        feats[raw[:64]] = 1.0
    for f, w in feats.items():
        h = zlib.crc32(f.encode("utf-8"))
        v[h % dim] += w if (h >> 31) & 1 == 0 else -w
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# --------------------------------------------------------------------------- fastembed provider
def _fastembed_model():
    global _fe_model
    with _fe_lock:
        if _fe_model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:  # pragma: no cover
                raise EmbeddingError("fastembed is not installed (pip install fastembed)") from e
            cache = settings.models_dir / "fastembed"
            cache.mkdir(parents=True, exist_ok=True)
            log.info("loading embedding model %s (cache: %s)", settings.embedding_model, cache)
            _fe_model = TextEmbedding(model_name=settings.embedding_model, cache_dir=str(cache))
    return _fe_model


def _fastembed_run(texts: list[str], query: bool) -> list[np.ndarray]:
    model = _fastembed_model()
    with _fe_run_lock:  # one ONNX run at a time (it already uses all cores)
        if query and hasattr(model, "query_embed"):
            return [np.asarray(v) for v in model.query_embed(texts)]
        if hasattr(model, "passage_embed"):
            return [np.asarray(v) for v in model.passage_embed(texts, batch_size=FASTEMBED_BATCH)]
        return [np.asarray(v) for v in model.embed(texts, batch_size=FASTEMBED_BATCH)]


# --------------------------------------------------------------------------- OpenAI-compatible provider
async def _openai_embed(texts: list[str]) -> list[list[float]]:
    from app.llm.client import http_client

    base = (settings.embedding_base_url or "https://api.openai.com/v1").rstrip("/")
    key = settings.embedding_api_key or (settings.openai_api_key if not settings.embedding_base_url else None)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    out: list[list[float]] = []
    for i in range(0, len(texts), OPENAI_BATCH):
        batch = texts[i: i + OPENAI_BATCH]
        payload = {"model": settings.embedding_model, "input": batch}
        delay = 1.0
        for attempt in range(3):
            try:
                resp = await http_client().post(f"{base}/embeddings", headers=headers, json=payload, timeout=120)
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise EmbeddingError(f"embedding endpoint unreachable: {type(e).__name__}: {e}") from e
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise EmbeddingError(f"embedding endpoint HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            if len(items) != len(batch):
                raise EmbeddingError(f"embedding endpoint returned {len(items)} vectors for {len(batch)} inputs")
            out.extend(d["embedding"] for d in items)
            break
    return out


# --------------------------------------------------------------------------- public API
async def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed passages. Returns L2-normalized float32 vectors with exactly `settings.embedding_dim` dims."""
    if not texts:
        return []
    dim = int(settings.embedding_dim)
    clean = [(t or " ")[:MAX_INPUT_CHARS] for t in texts]
    p = _provider()
    if p == "hash":
        if len(clean) > 256:
            return await asyncio.to_thread(lambda: [hash_embed(t, dim) for t in clean])
        return [hash_embed(t, dim) for t in clean]
    if p == "openai":
        raw = await _openai_embed(clean)
    elif p == "fastembed":
        raw = await asyncio.to_thread(_fastembed_run, clean, False)
    else:
        raise EmbeddingError(f"unknown EMBEDDING_PROVIDER '{p}' (use fastembed | openai | hash)")
    return [_fit(v, dim) for v in raw]


async def embed_query(text: str) -> np.ndarray:
    """Embed a search query (uses the model's query instruction where it has one). Small LRU cache."""
    dim = int(settings.embedding_dim)
    p = _provider()
    key = (p, settings.embedding_model, dim, text)
    hit = _query_cache.get(key)
    if hit is not None:
        _query_cache.move_to_end(key)
        return hit
    q = (text or " ")[:MAX_INPUT_CHARS]
    if p == "hash":
        vec = hash_embed(q, dim)
    elif p == "fastembed":
        vec = _fit((await asyncio.to_thread(_fastembed_run, [q], True))[0], dim)
    elif p == "openai":
        vec = _fit((await _openai_embed([q]))[0], dim)
    else:
        raise EmbeddingError(f"unknown EMBEDDING_PROVIDER '{p}' (use fastembed | openai | hash)")
    _query_cache[key] = vec
    if len(_query_cache) > _QUERY_CACHE_SIZE:
        _query_cache.popitem(last=False)
    return vec
