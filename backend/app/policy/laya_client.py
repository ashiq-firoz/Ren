"""Client for Laya (convaiinnovations/laya) — a non-autoregressive "System 1" decision model that answers typed
questions about a state in one forward pass (~33 ms GPU) with calibrated probabilities. Used here as the fast
policy-compliance checker.

Modes:
  http  – a `laya-serve` server (`POST /v1/systemone`, `/v1/systemone/batch`; Jev-compatible wire format)
  local – in-process `laya.Router` (requires `pip install laya`, loads torch + weights)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

log = logging.getLogger("ren.laya")

MAX_BATCH_STATES = 64


class LayaUnavailable(Exception):
    pass


class LayaClient:
    def __init__(self) -> None:
        self._router = None
        self._router_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._last_status: dict[str, Any] | None = None
        self._last_status_at = 0.0

    @property
    def mode(self) -> str:
        if settings.laya_url:
            return "http"
        if settings.laya_local:
            return "local"
        return "none"

    @property
    def configured(self) -> bool:
        return self.mode != "none"

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http_loop = loop
            headers = {"Content-Type": "application/json"}
            if settings.laya_api_key:
                headers["Authorization"] = f"Bearer {settings.laya_api_key}"
            self._http = httpx.AsyncClient(base_url=settings.laya_url.rstrip("/"), headers=headers,
                                           timeout=httpx.Timeout(settings.laya_timeout_seconds, connect=5.0))
        return self._http

    async def _local_router(self):
        async with self._router_lock:
            if self._router is None:
                def load():
                    import os

                    os.environ.setdefault("USE_TF", "0")
                    from laya import Router  # type: ignore

                    return Router(preload=False)

                self._router = await asyncio.to_thread(load)
        return self._router

    def _model_field(self) -> dict:
        m = settings.laya_model
        return {} if not m or m == "auto" else {"model": m}

    async def predict_batch(self, states: list[Any], questions: dict[str, dict], *, max_len: int | None = None) -> list[dict]:
        """Answer `questions` for each state. Returns one `answers` dict per state."""
        if not states:
            return []
        if self.mode == "http":
            out: list[dict] = []
            for i in range(0, len(states), MAX_BATCH_STATES):
                chunk = states[i:i + MAX_BATCH_STATES]
                body: dict[str, Any] = {"questions": questions, **self._model_field()}
                if max_len:
                    body["max_len"] = max_len
                try:
                    if len(chunk) == 1:
                        r = await self._client().post("/v1/systemone", json={**body, "state": chunk[0]})
                        r.raise_for_status()
                        out.append(r.json().get("answers", {}))
                    else:
                        r = await self._client().post("/v1/systemone/batch", json={**body, "states": chunk})
                        r.raise_for_status()
                        out.extend(res.get("answers", {}) for res in r.json().get("results", []))
                except httpx.HTTPError as e:
                    raise LayaUnavailable(f"Laya request failed: {e}") from e
            return out
        if self.mode == "local":
            router = await self._local_router()

            def run():
                res = []
                for st in states:
                    kwargs = {}
                    if settings.laya_model and settings.laya_model != "auto":
                        kwargs["model"] = settings.laya_model
                    if max_len:
                        kwargs["max_len"] = max_len
                    res.append(router.predict(st, questions, **kwargs).get("answers", {}))
                return res

            try:
                return await asyncio.to_thread(run)
            except Exception as e:  # noqa: BLE001
                raise LayaUnavailable(f"Local Laya failed: {e}") from e
        raise LayaUnavailable("Laya is not configured (set LAYA_URL or LAYA_LOCAL=1)")

    async def status(self, force: bool = False) -> dict:
        if not force and self._last_status and time.time() - self._last_status_at < 30:
            return self._last_status
        st: dict[str, Any] = {"configured": self.configured, "reachable": False, "url": settings.laya_url,
                              "mode": self.mode, "latency_ms": None, "error": None}
        if self.mode == "http":
            t0 = time.perf_counter()
            try:
                r = await self._client().post("/v1/systemone", json={
                    "state": {"text": "ping"},
                    "questions": {"q": {"type": "choice", "instructions": "Is this a greeting?",
                                        "criteria": {"A": "yes", "B": "no"}}},
                    **self._model_field(),
                })
                r.raise_for_status()
                st["reachable"] = True
                st["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001
                st["error"] = str(e)[:300]
        elif self.mode == "local":
            import importlib.util

            st["reachable"] = importlib.util.find_spec("laya") is not None
            if not st["reachable"]:
                st["error"] = "Python package 'laya' is not installed"
        self._last_status, self._last_status_at = st, time.time()
        return st


laya = LayaClient()


def violation_probability(answer: dict, violating_key: str = "B") -> float:
    """Extract P(violation) from a Laya `choice` answer whose option `violating_key` means 'violates'."""
    if not isinstance(answer, dict):
        return 0.0
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and violating_key in probs:
        try:
            return float(probs[violating_key])
        except (TypeError, ValueError):
            pass
    if isinstance(probs, list):  # [{"label": "B", "probability": .8}] variants
        for p in probs:
            if isinstance(p, dict) and p.get("label") == violating_key:
                return float(p.get("probability") or p.get("p") or 0.0)
    choice = answer.get("choice")
    conf = answer.get("answer_confidence", answer.get("confidence"))
    try:
        conf = float(conf) if conf is not None else 0.75
    except (TypeError, ValueError):
        conf = 0.75
    if choice == violating_key:
        return conf
    if choice is not None:
        return 1.0 - conf
    return 0.0
