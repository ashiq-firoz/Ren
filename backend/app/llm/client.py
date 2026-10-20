"""OpenAI-compatible async LLM client (OpenRouter, vLLM, OpenAI, any compatible endpoint).

Features:
- chat completions with native tool calling (or a prompted fallback for models without tool support)
- token streaming with tool-call delta accumulation
- constrained outputs: `choices=[...]` (vLLM guided choice / JSON-schema enum) and `json_schema={...}`
  — used by the STAIR structural retriever so it can only ever return a valid ToC leaf
- embeddings, model listing, retries with backoff
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings

log = logging.getLogger("ren.llm")

_http_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def http_client() -> httpx.AsyncClient:
    """Shared pooled client for the running event loop (clients must not cross loops)."""
    loop = asyncio.get_running_loop()
    c = _http_by_loop.get(loop)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=15.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        _http_by_loop[loop] = c
    return c


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class NoProviderError(LLMError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    extra_content: dict[str, Any] | None = None  # e.g. Gemini thought_signature, echoed back on the next turn

    def to_message(self) -> dict:
        msg: dict[str, Any] = {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or json.dumps(self.arguments)},
        }
        if self.extra_content:
            msg["extra_content"] = self.extra_content
        return msg


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    model: str = ""
    reasoning: str | None = None
    latency_ms: int = 0

    def assistant_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_message() for tc in self.tool_calls]
            if not self.content:
                msg["content"] = None
        return msg

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens", 0)) + int(self.usage.get("completion_tokens", 0))


DeltaFn = Callable[[str], Awaitable[None]]

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)
_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_json_loose(text: str) -> Any:
    """Parse JSON from model output that may include prose or code fences."""
    if text is None:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def snap_to_choice(raw: str, choices: list[str]) -> str | None:
    """Map free text onto one of `choices` (exact → normalized → prefix → token overlap)."""
    if not choices:
        return None
    raw = (raw or "").strip().strip('"').strip("'")
    if raw in choices:
        return raw
    nraw = _norm(raw)
    norm_map = {_norm(c): c for c in choices}
    if nraw in norm_map:
        return norm_map[nraw]
    for c in choices:  # model returned "4.1.1 K-Means" when choice id is "4.1.1"
        nc = _norm(c)
        if nraw.startswith(nc + " ") or nc.startswith(nraw + " "):
            return c
    raw_tokens = set(nraw.split())
    if not raw_tokens:
        return None
    best, best_score = None, 0.0
    for c in choices:
        ct = set(_norm(c).split())
        if not ct:
            continue
        score = len(raw_tokens & ct) / len(raw_tokens | ct)
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.34 else None


_THOUGHT_RE = re.compile(r"<thought>(.*?)(?:</thought>|$)", re.S)


def split_thoughts(text: str) -> tuple[str, str]:
    """Gemma on Google AI Studio inlines its reasoning as <thought>...</thought>; return (content, reasoning)."""
    if not text or "<thought>" not in text:
        return text, ""
    reasoning = "".join(m.group(1) for m in _THOUGHT_RE.finditer(text))
    return _THOUGHT_RE.sub("", text).lstrip(), reasoning


class _ThoughtStream:
    """Incrementally routes streamed text inside <thought>...</thought> to reasoning instead of content."""

    OPEN, CLOSE = "<thought>", "</thought>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.started = False

    def feed(self, piece: str) -> tuple[str, str]:
        self.buf += piece
        content, reasoning = [], []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx == -1:
                # hold back a partial tag at the end ("<tho") until the next chunk decides it
                hold = next((n for n in range(min(len(tag) - 1, len(self.buf)), 0, -1)
                             if tag.startswith(self.buf[-n:])), 0)
                out, self.buf = self.buf[: len(self.buf) - hold], self.buf[len(self.buf) - hold:]
                (reasoning if self.inside else content).append(out)
                break
            (reasoning if self.inside else content).append(self.buf[:idx])
            self.buf = self.buf[idx + len(tag):]
            self.inside = not self.inside
        text = "".join(content)
        if not self.started:  # drop whitespace between </thought> and the answer
            text = text.lstrip()
            self.started = bool(text)
        return text, "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        out, self.buf = self.buf, ""
        return ("", out) if self.inside else (out, "")


def _schema_for_choices(choices: list[str], multi: int = 1) -> dict:
    if multi <= 1:
        return {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": choices}},
            "required": ["choice"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "choices": {"type": "array", "items": {"type": "string", "enum": choices}, "minItems": 1, "maxItems": multi}
        },
        "required": ["choices"],
        "additionalProperties": False,
    }


class LLMClient:
    """One configured endpoint + default model."""

    _guided_style_cache: dict[str, str] = {}

    def __init__(
        self,
        *,
        kind: str,
        base_url: str,
        api_key: str | None,
        model: str,
        supports_tools: bool = True,
        extra: dict[str, Any] | None = None,
        name: str = "",
        provider_id: str | None = None,
    ):
        self.kind = kind
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.supports_tools = supports_tools
        self.extra = extra or {}
        self.name = name or kind
        self.provider_id = provider_id

    # ------------------------------------------------------------------ helpers
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.kind == "openrouter":
            h["HTTP-Referer"] = settings.frontend_url
            h["X-Title"] = "Ren by f69 Labs"
        for k, v in (self.extra.get("headers") or {}).items():
            h[str(k)] = str(v)
        return h

    def _guided_style(self) -> str:
        if self.extra.get("guided_style"):
            return self.extra["guided_style"]
        cached = self._guided_style_cache.get(self.base_url)
        if cached:
            return cached
        return "structured_outputs" if self.kind == "vllm" else "json_schema"

    async def _post(self, path: str, payload: dict, *, retries: int = 4, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await http_client().post(url, headers=self._headers(), json=payload, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = LLMError(f"{self.name}: network error: {e.__class__.__name__}: {e}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429:
                exhausted = _quota_exhausted(resp, resp.text)
                if exhausted:
                    raise LLMError(f"{self.name}: {exhausted}", 429, resp.text[:2000])
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                await asyncio.sleep(_retry_wait(resp, delay))
                delay = min(delay * 2.5, 20.0)
                last_exc = LLMError(f"{self.name}: HTTP {resp.status_code}", resp.status_code, resp.text[:2000])
                continue
            if resp.status_code >= 400:
                raise LLMError(
                    f"{self.name}: HTTP {resp.status_code}: {_error_text(resp.text)}", resp.status_code, resp.text[:4000]
                )
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise LLMError(f"{self.name}: invalid JSON response") from e
            if isinstance(data, dict) and data.get("error") and not data.get("choices") and not data.get("data"):
                err = data["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                if code in (429, 502, 503) and attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.5, 20.0)
                    continue
                raise LLMError(f"{self.name}: {msg}", code if isinstance(code, int) else None, json.dumps(data)[:4000])
            return data
        raise last_exc or LLMError(f"{self.name}: request failed")

    def _build_payload(
        self,
        messages: list[dict],
        *,
        model: str | None,
        tools: list[dict] | None,
        tool_choice: Any,
        temperature: float | None,
        max_tokens: int | None,
        stop: list[str] | None,
        stream: bool,
        response_format: dict | None,
    ) -> dict:
        payload: dict[str, Any] = {"model": model or self.model, "messages": messages}
        fallbacks = [m for m in (self.extra.get("fallback_models") or []) if m and m != payload["model"]]
        if self.kind == "openrouter" and fallbacks:
            payload["models"] = [payload["model"], *fallbacks][:3]  # OpenRouter model fallback routing
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            if self.kind == "google":  # thinking tokens count against the cap; tiny caps truncate mid-thought
                max_tokens = max(max_tokens, 2048)
            payload["max_tokens"] = max_tokens
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        for k, v in (self.extra.get("body") or {}).items():
            payload.setdefault(k, v)
        return payload

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        response_format: dict | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        t0 = time.perf_counter()
        msgs, tools_native, prompted = self._prepare_tools(messages, tools)
        payload = self._build_payload(
            msgs, model=model, tools=tools_native, tool_choice=tool_choice if tools_native else None,
            temperature=temperature, max_tokens=max_tokens, stop=stop, stream=False, response_format=response_format,
        )
        data = await self._post("/chat/completions", payload, timeout=timeout)
        result = self._parse_completion(data)
        if prompted:
            self._extract_prompted_tool_calls(result)
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result

    async def stream(
        self,
        messages: list[dict],
        *,
        on_delta: DeltaFn,
        model: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop_event: asyncio.Event | None = None,
        on_reasoning: DeltaFn | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """Stream a completion, forwarding content deltas (and, if given, reasoning deltas); returns the result."""
        t0 = time.perf_counter()
        msgs, tools_native, prompted = self._prepare_tools(messages, tools)
        payload = self._build_payload(
            msgs, model=model, tools=tools_native, tool_choice=tool_choice if tools_native else None,
            temperature=temperature, max_tokens=max_tokens, stop=None, stream=True, response_format=None,
        )
        self._apply_reasoning(payload, reasoning_effort)
        result = ChatResult(model=payload["model"])
        tc_acc: dict[int, dict[str, Any]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        emitted_any = False
        # When tool calls are prompted (in-text), suppress streaming of the <tool_call> block.
        prompted_buffer = ""
        thoughts = _ThoughtStream() if self.kind == "google" else None

        attempt = 0
        while True:
            attempt += 1
            try:
                async with http_client().stream(
                    "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        if resp.status_code == 429:
                            exhausted = _quota_exhausted(resp, body)
                            if exhausted:
                                raise LLMError(f"{self.name}: {exhausted}", 429, body[:2000])
                        if resp.status_code in (429, 502, 503) and attempt < 4 and not emitted_any:
                            await asyncio.sleep(_retry_wait(resp, min(2.0 * 2.5 ** (attempt - 1), 20.0)))
                            continue
                        raise LLMError(
                            f"{self.name}: HTTP {resp.status_code}: {_error_text(body)}", resp.status_code, body[:4000]
                        )
                    async for line in resp.aiter_lines():
                        if stop_event is not None and stop_event.is_set():
                            result.finish_reason = "stopped"
                            break
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            err = chunk["error"]
                            raise LLMError(f"{self.name}: {err.get('message') if isinstance(err, dict) else err}")
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            result.usage = {
                                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                                "completion_tokens": int(u.get("completion_tokens") or 0),
                            }
                        if chunk.get("model"):
                            result.model = chunk["model"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if choice.get("finish_reason"):
                                result.finish_reason = choice["finish_reason"]
                            rc = delta.get("reasoning") or delta.get("reasoning_content")
                            if rc:
                                reasoning_parts.append(rc)
                                if on_reasoning is not None:
                                    await on_reasoning(rc)
                            piece = delta.get("content")
                            if piece and thoughts is not None:
                                piece, rc = thoughts.feed(piece)
                                if rc:
                                    reasoning_parts.append(rc)
                                    if on_reasoning is not None:
                                        await on_reasoning(rc)
                            if piece:
                                content_parts.append(piece)
                                if prompted:
                                    prompted_buffer += piece
                                    if "<tool_call>" in prompted_buffer:
                                        continue
                                    # hold back a partial "<tool_c" prefix
                                    safe_upto = prompted_buffer.rfind("<")
                                    if safe_upto == -1:
                                        out, prompted_buffer = prompted_buffer, ""
                                    else:
                                        out, prompted_buffer = prompted_buffer[:safe_upto], prompted_buffer[safe_upto:]
                                    if out:
                                        emitted_any = True
                                        await on_delta(out)
                                else:
                                    emitted_any = True
                                    await on_delta(piece)
                            for tcd in delta.get("tool_calls") or []:
                                idx = tcd.get("index", 0)
                                acc = tc_acc.setdefault(idx, {"id": None, "name": "", "args": ""})
                                if tcd.get("id"):
                                    acc["id"] = tcd["id"]
                                if tcd.get("extra_content"):
                                    acc["extra_content"] = tcd["extra_content"]
                                fn = tcd.get("function") or {}
                                if fn.get("name"):
                                    acc["name"] += fn["name"]
                                if fn.get("arguments"):
                                    acc["args"] += fn["arguments"]
                break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 3 and not emitted_any:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                raise LLMError(f"{self.name}: network error during stream: {e}") from e

        if thoughts is not None:
            tail, rc = thoughts.flush()
            if rc:
                reasoning_parts.append(rc)
            if tail:
                content_parts.append(tail)
                await on_delta(tail)
        if prompted and prompted_buffer and "<tool_call>" not in prompted_buffer:
            await on_delta(prompted_buffer)
        result.content = "".join(content_parts)
        result.reasoning = "".join(reasoning_parts) or None
        for idx in sorted(tc_acc):
            acc = tc_acc[idx]
            if not acc["name"]:
                continue
            result.tool_calls.append(
                ToolCall(
                    id=acc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    name=acc["name"],
                    arguments=_parse_args(acc["args"]),
                    raw_arguments=acc["args"] or "{}",
                    extra_content=acc.get("extra_content"),
                )
            )
        if prompted:
            self._extract_prompted_tool_calls(result)
        if not result.usage.get("completion_tokens"):
            result.usage = {
                "prompt_tokens": _estimate_tokens(json.dumps(msgs)),
                "completion_tokens": _estimate_tokens(result.content),
            }
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _apply_reasoning(self, payload: dict, effort: str | None) -> None:
        """OpenRouter's unified reasoning control (ignored by models without reasoning). Provider default:
        extra.reasoning_effort = none | low | medium | high."""
        effort = effort or self.extra.get("reasoning_effort")
        if self.kind != "openrouter" or not effort:
            return
        payload["reasoning"] = {"enabled": False} if effort == "none" else {"effort": effort}

    # ------------------------------------------------------------------ constrained outputs
    async def choose(
        self,
        messages: list[dict],
        choices: list[str],
        *,
        model: str | None = None,
        multi: int = 1,
        temperature: float = 0.0,
        max_tokens: int = 300,
    ) -> tuple[list[str], str]:
        """Constrained selection from `choices`. Returns (valid_choices, method).

        Uses the strongest constraint the endpoint supports (vLLM guided decoding / JSON-schema enum),
        and *always* validates + snaps the output onto the allowed set, so the result can never be a
        hallucinated identifier.
        """
        if not choices:
            return [], "none"
        style = self._guided_style()
        schema = _schema_for_choices(choices, multi)
        attempts = [style] + [s for s in ("guided_choice", "json_schema", "prompt") if s != style]
        if self.kind not in ("vllm", "custom"):
            attempts = [s for s in attempts if s not in ("structured_outputs", "guided_choice")]
        last_err: Exception | None = None
        for st in attempts:
            try:
                raw = await self._choose_with_style(st, messages, choices, schema, model, multi, temperature, max_tokens)
            except LLMError as e:
                last_err = e
                if e.status in (400, 404, 422):
                    continue
                raise
            picked = self._parse_choice_output(raw, choices, multi)
            if picked:
                if st in ("structured_outputs", "guided_choice") and not self.extra.get("guided_style"):
                    self._guided_style_cache[self.base_url] = st
                return picked, st
        if last_err:
            log.warning("choose(): all strategies failed: %s", last_err)
        return [], "failed"

    async def _choose_with_style(self, style, messages, choices, schema, model, multi, temperature, max_tokens) -> str:
        msgs = list(messages)
        payload = self._build_payload(
            msgs, model=model, tools=None, tool_choice=None, temperature=temperature,
            max_tokens=max_tokens, stop=None, stream=False, response_format=None,
        )
        if style == "structured_outputs":
            payload["structured_outputs"] = {"choice": choices} if multi <= 1 else {"json": schema}
        elif style == "guided_choice":
            if multi <= 1:
                payload["guided_choice"] = choices
            else:
                payload["guided_json"] = schema
        elif style == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "selection", "strict": True, "schema": schema},
            }
        else:  # prompt
            hint = (
                'Respond with ONLY a JSON object: {"choice": "<one of the allowed ids>"}'
                if multi <= 1
                else f'Respond with ONLY a JSON object: {{"choices": ["<id>", ...]}} (at most {multi}).'
            )
            msgs = msgs + [{"role": "user", "content": hint}]
            payload["messages"] = msgs
        data = await self._post("/chat/completions", payload, retries=2)
        res = self._parse_completion(data)
        return res.content or ""

    @staticmethod
    def _parse_choice_output(raw: str, choices: list[str], multi: int) -> list[str]:
        raw = (raw or "").strip()
        values: list[str] = []
        parsed = parse_json_loose(raw)
        if isinstance(parsed, dict):
            v = parsed.get("choices", parsed.get("choice"))
            if isinstance(v, list):
                values = [str(x) for x in v]
            elif v is not None:
                values = [str(v)]
        elif isinstance(parsed, list):
            values = [str(x) for x in parsed]
        elif isinstance(parsed, str):
            values = [parsed]
        if not values and raw:
            values = [ln.strip("-• \t") for ln in raw.splitlines() if ln.strip()] if multi > 1 else [raw]
        out: list[str] = []
        for v in values:
            snapped = snap_to_choice(v, choices)
            if snapped and snapped not in out:
                out.append(snapped)
            if len(out) >= max(1, multi):
                break
        return out

    async def json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1500,
        name: str = "output",
    ) -> Any:
        """Structured JSON output with schema enforcement where supported, loose parsing otherwise."""
        style = self._guided_style()
        attempts = []
        if self.kind in ("vllm", "custom"):
            attempts.append("structured_outputs" if style == "structured_outputs" else "guided_json")
        attempts += ["json_schema", "prompt"]
        last: Exception | None = None
        for st in attempts:
            payload = self._build_payload(
                list(messages), model=model, tools=None, tool_choice=None, temperature=temperature,
                max_tokens=max_tokens, stop=None, stream=False, response_format=None,
            )
            if st == "structured_outputs":
                payload["structured_outputs"] = {"json": schema}
            elif st == "guided_json":
                payload["guided_json"] = schema
            elif st == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "strict": False, "schema": schema}}
            else:
                payload["messages"] = list(messages) + [{
                    "role": "user",
                    "content": "Respond with ONLY valid JSON matching this schema (no prose, no code fences):\n"
                    + json.dumps(schema),
                }]
            try:
                data = await self._post("/chat/completions", payload, retries=2)
            except LLMError as e:
                last = e
                if e.status in (400, 404, 422):
                    continue
                raise
            parsed = parse_json_loose(self._parse_completion(data).content)
            if parsed is not None:
                return parsed
        if last:
            raise last
        return None

    # ------------------------------------------------------------------ embeddings / models
    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        data = await self._post("/embeddings", {"model": model or self.model, "input": texts})
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in items]

    async def list_models(self) -> list[dict]:
        try:
            resp = await http_client().get(f"{self.base_url}/models", headers=self._headers(), timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{self.name}: could not list models: {e}") from e
        out = []
        for m in data.get("data", []):
            mid = m.get("id") or ""
            if self.kind == "google":  # ids come as "models/<id>" and include non-chat models
                mid = mid.removeprefix("models/")
                if _GOOGLE_NON_CHAT.search(mid):
                    continue
            out.append({
                "id": mid,
                "name": m.get("name") or mid,
                "context_length": m.get("context_length") or m.get("max_model_len"),
            })
        return out

    # ------------------------------------------------------------------ parsing
    def _parse_completion(self, data: dict) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name}: empty completion", body=json.dumps(data)[:2000])
        ch = choices[0]
        msg = ch.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content, thought = split_thoughts(content)
        result = ChatResult(
            content=content,
            finish_reason=ch.get("finish_reason"),
            model=data.get("model") or self.model,
            reasoning=msg.get("reasoning") or msg.get("reasoning_content") or thought or None,
        )
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            if isinstance(raw, dict):
                raw = json.dumps(raw)
            result.tool_calls.append(
                ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""),
                         arguments=_parse_args(raw), raw_arguments=raw, extra_content=tc.get("extra_content"))
            )
        u = data.get("usage") or {}
        result.usage = {
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or _estimate_tokens(content)),
        }
        return result

    # ------------------------------------------------------------------ prompted tool calling fallback
    def _prepare_tools(self, messages: list[dict], tools: list[dict] | None) -> tuple[list[dict], list[dict] | None, bool]:
        if not tools:
            return messages, None, False
        if self.supports_tools:
            return messages, tools, False
        catalog = "\n".join(
            f"- {t['function']['name']}: {t['function'].get('description', '')}\n  parameters: "
            f"{json.dumps(t['function'].get('parameters', {}))}"
            for t in tools
        )
        instr = (
            "\n\n# Tools\nYou can call tools. To call one, output exactly:\n"
            '<tool_call>{"name": "<tool name>", "arguments": {...}}</tool_call>\n'
            "You may output several tool_call blocks. After tool results arrive (as messages starting with "
            "'TOOL RESULT'), continue. When you are done, answer normally without any tool_call block.\n"
            f"Available tools:\n{catalog}"
        )
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = (msgs[0].get("content") or "") + instr
        else:
            msgs.insert(0, {"role": "system", "content": instr.strip()})
        converted: list[dict] = []
        for m in msgs:  # convert native tool messages into plain text for non-tool models
            if m.get("role") == "tool":
                converted.append({"role": "user", "content": f"TOOL RESULT ({m.get('name', '')}, id={m.get('tool_call_id')}):\n{m.get('content')}"})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                calls = "".join(
                    f'<tool_call>{{"name": "{tc["function"]["name"]}", "arguments": {tc["function"]["arguments"]}}}</tool_call>'
                    for tc in m["tool_calls"]
                )
                converted.append({"role": "assistant", "content": (m.get("content") or "") + calls})
            else:
                converted.append(m)
        return converted, None, True

    @staticmethod
    def _extract_prompted_tool_calls(result: ChatResult) -> None:
        if not result.content or "<tool_call>" not in result.content:
            return
        for m in _TOOL_TAG_RE.finditer(result.content):
            obj = parse_json_loose(m.group(1))
            if isinstance(obj, dict) and obj.get("name"):
                args = obj.get("arguments") or {}
                result.tool_calls.append(
                    ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=obj["name"],
                             arguments=args if isinstance(args, dict) else {}, raw_arguments=json.dumps(args))
                )
        result.content = _TOOL_TAG_RE.sub("", result.content).strip()


_GOOGLE_NON_CHAT = re.compile(
    r"embedding|tts|image|banana|veo|lyria|aqa|live|audio|transcribe|robotics|computer-use|deep-research|translate"
)


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        v = parse_json_loose(raw)
        return v if isinstance(v, dict) else {}


def _quota_exhausted(resp: httpx.Response, body: str) -> str | None:
    """Daily / long-window caps (e.g. OpenRouter free-models-per-day) — retrying cannot help, so fail fast."""
    text = (body or "").lower()
    if "per-day" in text or "per day" in text or "quota" in text or "insufficient credits" in text:
        reset = resp.headers.get("x-ratelimit-reset")
        when = ""
        if reset and reset.isdigit():
            import datetime as _dt

            when = " It resets at " + _dt.datetime.fromtimestamp(int(reset) / 1000, tz=_dt.timezone.utc).strftime(
                "%H:%M UTC") + "."
        return f"daily request limit reached ({_error_text(body)}).{when} Add credits or switch provider in Settings → Models."
    return None


def _retry_wait(resp: httpx.Response, default: float) -> float:
    """Honour Retry-After / X-RateLimit-Reset (capped), else the given backoff."""
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return max(0.5, min(float(ra), 30.0))
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        wait = int(reset) / 1000 - time.time()
        if 0 < wait <= 30:
            return wait
    return default


def _estimate_tokens(text: str | None) -> int:
    return max(0, len(text or "") // 4)


def _error_text(body: str) -> str:
    try:
        d = json.loads(body)
        if isinstance(d, list) and d and isinstance(d[0], dict):  # Google: [{"error": {...}}]
            d = d[0]
        err = d.get("error", d) if isinstance(d, dict) else d
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)[:500]
        return str(err)[:500]
    except Exception:  # noqa: BLE001
        return body[:500]
