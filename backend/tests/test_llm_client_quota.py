"""Rate-limit handling: transient 429s retry; daily quota exhaustion fails fast with a clear message."""
import asyncio
import json

import httpx
import pytest

from app.llm import client as llm_client
from app.llm.client import LLMClient, LLMError


def _client_with(transport):
    llm_client._http_by_loop.clear()

    def factory():
        loop = asyncio.get_running_loop()
        c = llm_client._http_by_loop.get(loop)
        if c is None:
            c = httpx.AsyncClient(transport=transport)
            llm_client._http_by_loop[loop] = c
        return c
    return factory


def test_daily_quota_fails_fast(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"}})

    monkeypatch.setattr(llm_client, "http_client", _client_with(httpx.MockTransport(handler)))
    c = LLMClient(kind="openrouter", base_url="https://openrouter.test/api/v1", api_key="k", model="m")
    with pytest.raises(LLMError) as ei:
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert "daily request limit" in str(ei.value) and calls["n"] == 1


def test_transient_429_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "temporarily rate-limited upstream"}})
        body = json.loads(request.content)
        assert body["models"] == ["m", "fallback/x"]  # OpenRouter fallback routing
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})

    monkeypatch.setattr(llm_client, "http_client", _client_with(httpx.MockTransport(handler)))
    c = LLMClient(kind="openrouter", base_url="https://openrouter.test/api/v1", api_key="k", model="m",
                  extra={"fallback_models": ["fallback/x"]})
    res = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert res.content == "ok" and calls["n"] == 2
