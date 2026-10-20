"""Audio group: transcribe / translate recordings and generate speech through the workspace's OpenAI-compatible
provider (`POST {base_url}/audio/transcriptions|translations|speech`), plus meeting-style summaries.

Providers without audio endpoints (e.g. OpenRouter) get a clear error; an admin can add an OpenAI (or other
OpenAI-compatible, e.g. a local Whisper server) provider — flag it with `extra.audio = true` to prefer it."""
from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from typing import Any, Literal

import httpx

from app.tools import net
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _office as O

audio = ToolGroup(
    "audio", "Audio",
    "Transcribe and translate recordings (meetings, calls, interviews, voice notes), summarize them into decisions "
    "and action items, and generate spoken audio (text-to-speech) — via the workspace's OpenAI-compatible provider.",
    prefix="audio", icon="audio-lines", category="core",
)

AUDIO_EXTS = (".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".oga", ".flac", ".aac", ".opus")
MAX_UPLOAD = 25 * 1024 * 1024
TTS_CHUNK = 4000


def _supports_audio(client: Any) -> bool:
    base = (getattr(client, "base_url", "") or "").lower()
    if getattr(client, "kind", "") == "mock" or base.startswith("mock://"):
        return False
    if getattr(client, "kind", "") == "openrouter" or "openrouter.ai" in base:
        return False
    if (getattr(client, "extra", None) or {}).get("audio") is False:
        return False
    return bool(base)


async def audio_endpoint(ctx: ToolContext) -> tuple[str, str | None, str]:
    """(base_url, api_key, provider name) of a provider exposing OpenAI-style /audio endpoints."""
    override = ctx.extra.get("audio_provider")
    if isinstance(override, dict) and override.get("base_url"):
        return str(override["base_url"]).rstrip("/"), override.get("api_key"), str(override.get("name", "custom"))
    from app.llm.client import NoProviderError

    try:
        default = await ctx.llm()
    except NoProviderError as e:
        raise ToolError(str(e)) from e
    flagged, openai_like = [], []
    try:
        from sqlalchemy import select

        from app.llm.providers import client_from_provider
        from app.models import Provider

        async with ctx.session() as db:
            rows = (await db.execute(select(Provider).where(Provider.org_id == ctx.org_id))).scalars().all()
        for p in rows:
            if (p.extra or {}).get("audio"):
                flagged.append(client_from_provider(p))
            elif p.kind == "openai" and p.id != getattr(default, "provider_id", None):
                openai_like.append(client_from_provider(p))
    except Exception:  # noqa: BLE001
        pass
    for c in flagged + [default] + openai_like:
        if _supports_audio(c):
            return c.base_url.rstrip("/"), c.api_key, c.name
    name = getattr(default, "name", "the current provider")
    raise ToolError(
        f"The model provider '{name}' has no speech-to-text / text-to-speech endpoints"
        + (" (OpenRouter doesn't offer /audio APIs)" if "openrouter" in (default.base_url or "").lower() else "")
        + ". Ask an admin to add an OpenAI provider (or another OpenAI-compatible server with /audio endpoints, e.g. "
          "a local Whisper server) in Settings → Models; set extra.audio=true on it to use it for audio.")


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        return str(err.get("message") if isinstance(err, dict) else err)[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300]


async def _post_file(ctx: ToolContext, endpoint: str, path: Path, data: dict[str, Any]) -> Any:
    base, key, name = await audio_endpoint(ctx)
    size = path.stat().st_size
    if size > MAX_UPLOAD:
        raise ToolError(f"'{path.name}' is {O.human_size(size)}; the audio API limit is 25 MB. Split the recording or "
                        "compress it (e.g. 64 kbps mono MP3) and try again.")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    files = {"file": (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")}
    form = {k: str(v) for k, v in data.items() if v is not None}
    try:
        resp = await net.http().post(f"{base}/audio/{endpoint}", data=form, files=files, headers=headers, timeout=600)
    except httpx.TimeoutException as e:
        raise ToolError(f"{name} timed out processing the audio.") from e
    except httpx.HTTPError as e:
        raise ToolError(f"Could not reach {name}: {e.__class__.__name__}: {e}") from e
    if resp.status_code in (404, 405, 501):
        raise ToolError(f"{name} doesn't support /audio/{endpoint} (HTTP {resp.status_code}). Use a provider with "
                        "OpenAI-compatible audio endpoints.")
    if resp.status_code >= 400:
        raise ToolError(f"{name} returned HTTP {resp.status_code}: {_error_text(resp)}")
    if "json" in resp.headers.get("content-type", ""):
        return resp.json()
    return {"text": resp.text}


def _fmt_ts(sec: float) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _shape(data: Any, timestamps: bool, max_chars: int) -> dict:
    text = (data.get("text") if isinstance(data, dict) else str(data)) or ""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        if data.get("language"):
            out["language"] = data["language"]
        if data.get("duration"):
            out["duration"] = _fmt_ts(float(data["duration"]))
        if timestamps and data.get("segments"):
            lines = [f"[{_fmt_ts(s.get('start', 0))}] {str(s.get('text', '')).strip()}" for s in data["segments"]]
            text = "\n".join(lines)
            out["segments"] = len(data["segments"])
    body, truncated = O.clip(text.strip(), max(1000, int(max_chars or 40000)))
    out.update({"chars": len(text), "truncated": truncated, "text": body})
    return out


async def _transcript(ctx: ToolContext, p: Path, *, model: str, language: str | None, prompt: str | None,
                      timestamps: bool) -> Any:
    fmt = "verbose_json" if timestamps else "json"
    return await _post_file(ctx, "transcriptions", p, {"model": model, "language": language, "prompt": prompt,
                                                       "response_format": fmt})


async def _maybe_save(ctx: ToolContext, save_as: str | None, text: str, stem: str) -> tuple[Path | None, dict]:
    if not save_as:
        return None, {}
    low = str(save_as).lower()
    ext = ".docx" if low.endswith(".docx") else ".md" if low.endswith(".md") else ".txt"
    target = O.ws_output(ctx, save_as, default_name=f"{stem}-transcript", ext=ext, overwrite=True)
    if ext == ".docx":
        await asyncio.to_thread(O.markdown_to_docx, text, target, title=f"Transcript — {stem}")
    else:
        target.write_text(text, encoding="utf-8")
    return target, {"saved": O.vpath(ctx, target)}


@audio.tool(risk=Risk.LOW, scope="read", timeout=900,
            risk_fn=lambda a: Risk.MEDIUM if a.get("save_as") else Risk.LOW)
async def transcribe(ctx: ToolContext, path: str, model: str = "whisper-1", language: str | None = None,
                     prompt: str | None = None, timestamps: bool = False, save_as: str | None = None,
                     max_chars: int = 40000) -> dict | ToolResult:
    """Transcribe speech in an audio/video file (mp3, mp4, m4a, wav, webm, ogg, flac; max 25 MB) to text.

    Args:
        path: Workspace path of the recording.
        model: Transcription model, e.g. "whisper-1", "gpt-4o-transcribe" or your server's model id.
        language: ISO-639-1 code of the spoken language (e.g. "en", "de") to improve accuracy.
        prompt: Optional context/vocabulary hint (names, jargon).
        timestamps: Include [mm:ss] segment timestamps (needs a model supporting verbose_json, e.g. whisper-1).
        save_as: Optionally save the transcript (.txt, .md or .docx).
        max_chars: Maximum characters returned to you.
    """
    p = O.ws_input(ctx, path, AUDIO_EXTS)
    data = await _transcript(ctx, p, model=model, language=language, prompt=prompt, timestamps=timestamps)
    out = {"path": O.vpath(ctx, p), "model": model, **_shape(data, timestamps, max_chars)}
    target, info = await _maybe_save(ctx, save_as, _shape(data, timestamps, 10 ** 9)["text"], p.stem)
    if target is not None:
        return await O.produced(ctx, target, {**out, **info}, display=f"Transcribed {p.name}")
    return out


@audio.tool(risk=Risk.LOW, scope="read", timeout=900)
async def translate(ctx: ToolContext, path: str, model: str = "whisper-1", prompt: str | None = None,
                    max_chars: int = 40000) -> dict:
    """Translate speech in any supported language into English text (audio → English transcript).

    Args:
        path: Workspace path of the recording (max 25 MB).
        model: Translation-capable model, e.g. "whisper-1".
        prompt: Optional context/vocabulary hint (in English).
        max_chars: Maximum characters returned.
    """
    p = O.ws_input(ctx, path, AUDIO_EXTS)
    data = await _post_file(ctx, "translations", p, {"model": model, "prompt": prompt, "response_format": "json"})
    return {"path": O.vpath(ctx, p), "model": model, "target_language": "en", **_shape(data, False, max_chars)}


_SUMMARY_PROMPT = """You are an executive assistant writing up a recording transcript.{focus}
Write markdown with these sections (omit a section only if truly empty):
## Summary — 3-5 sentences on what happened and why it matters.
## Key points — bullets with facts, numbers and names exactly as said.
## Decisions — bullets.
## Action items — bullets formatted "**Owner** — task (due date if mentioned)"; use "Unassigned" if no owner.
## Open questions — bullets.
Never invent owners, dates or numbers that are not in the transcript."""


@audio.tool(risk=Risk.LOW, scope="read", timeout=900,
            risk_fn=lambda a: Risk.MEDIUM if a.get("save_as") else Risk.LOW)
async def summarize(ctx: ToolContext, path: str, model: str = "whisper-1", language: str | None = None,
                    focus: str | None = None, include_transcript: bool = False,
                    save_as: str | None = None) -> dict | ToolResult:
    """Transcribe a recording and summarize it: summary, key points, decisions, action items (owner + due date)
    and open questions.

    Args:
        path: Workspace path of the recording (max 25 MB).
        model: Transcription model, e.g. "whisper-1".
        language: ISO-639-1 code of the spoken language.
        focus: Optional angle, e.g. "customer objections" or "budget decisions".
        include_transcript: Also return the (truncated) transcript.
        save_as: Optionally save the notes (summary + transcript) as .md or .docx.
    """
    from app.llm.client import LLMError

    p = O.ws_input(ctx, path, AUDIO_EXTS)
    data = await _transcript(ctx, p, model=model, language=language, prompt=None, timestamps=False)
    transcript = (data.get("text") if isinstance(data, dict) else str(data)) or ""
    if not transcript.strip():
        raise ToolError("No speech was recognised in the recording.")
    llm = await ctx.llm()
    system = _SUMMARY_PROMPT.format(focus=f" Focus especially on: {focus}." if focus else "")
    chunk = 60000
    try:
        if len(transcript) <= chunk:
            res = await llm.chat([{"role": "system", "content": system},
                                  {"role": "user",
                                   "content": f"Transcript of {p.name}:\n\"\"\"\n{transcript}\n\"\"\""}],
                                 temperature=0.2, max_tokens=1800)
            notes = res.content.strip()
        else:
            partials = []
            pieces = [transcript[i:i + chunk] for i in range(0, len(transcript), chunk)][:8]
            for i, piece in enumerate(pieces, 1):
                r = await llm.chat([{"role": "system", "content": system},
                                    {"role": "user", "content": f"Part {i}/{len(pieces)} of the transcript:\n{piece}"}],
                                   temperature=0.2, max_tokens=1200)
                partials.append(r.content.strip())
            res = await llm.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": "Merge these partial notes into one set of notes:\n\n"
                                                              + "\n\n".join(partials)}],
                                 temperature=0.2, max_tokens=1800)
            notes = res.content.strip()
    except LLMError as e:
        raise ToolError(f"Summarization failed: {e}") from e
    out: dict[str, Any] = {"path": O.vpath(ctx, p), "transcript_chars": len(transcript), "notes": notes}
    if isinstance(data, dict) and data.get("language"):
        out["language"] = data["language"]
    if include_transcript:
        out["transcript"], out["transcript_truncated"] = O.clip(transcript, 15000)
    if save_as:
        doc = f"# Notes — {p.stem}\n\n{notes}\n\n## Transcript\n\n{transcript}"
        target, info = await _maybe_save(ctx, save_as, doc, p.stem)
        return await O.produced(ctx, target, {**out, **info}, display=f"Summarized {p.name}")
    return out


def _chunks(text: str, size: int = TTS_CHUNK) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > size:
            out.append(sent[:size])
            sent = sent[size:]
        if len(cur) + len(sent) + 1 > size:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


@audio.tool(risk=Risk.MEDIUM, scope="write", timeout=600)
async def speak(ctx: ToolContext, text: str, voice: str = "alloy", model: str = "tts-1",
                format: Literal["mp3", "opus", "aac", "flac", "wav"] = "mp3", speed: float = 1.0,
                output: str | None = None) -> ToolResult:
    """Generate natural speech (text-to-speech) and save it as an audio file in output/ — e.g. a narrated summary
    or a voice message draft. Long texts are synthesized in parts and joined (mp3/aac/opus).

    Args:
        text: What to say.
        voice: Voice name, e.g. alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer (provider-specific).
        model: TTS model, e.g. "tts-1", "tts-1-hd", "gpt-4o-mini-tts".
        format: Audio format.
        speed: 0.25–4.0.
        output: Output path (default: output/speech-<words>.<format>).
    """
    if not (text or "").strip():
        raise ToolError("`text` is empty.")
    parts = _chunks(text)
    if len(parts) > 1 and format in ("wav", "flac"):
        raise ToolError(f"Text is too long for a single {format} file ({len(text)} chars); use mp3.")
    if len(parts) > 25:
        raise ToolError("Text is too long (max ~100,000 characters).")
    base, key, name = await audio_endpoint(ctx)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    audio_bytes = bytearray()
    for i, part in enumerate(parts, 1):
        if len(parts) > 1:
            await ctx.progress(f"Synthesizing part {i} of {len(parts)}", int(100 * i / len(parts)))
        payload = {"model": model, "input": part, "voice": voice, "response_format": format,
                   "speed": max(0.25, min(float(speed or 1.0), 4.0))}
        try:
            resp = await net.http().post(f"{base}/audio/speech", json=payload, headers=headers, timeout=300)
        except httpx.HTTPError as e:
            raise ToolError(f"Could not reach {name}: {e.__class__.__name__}: {e}") from e
        if resp.status_code in (404, 405, 501):
            raise ToolError(f"{name} doesn't support /audio/speech (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            raise ToolError(f"{name} returned HTTP {resp.status_code}: {_error_text(resp)}")
        if "json" in resp.headers.get("content-type", ""):
            raise ToolError(f"{name} returned JSON instead of audio: {_error_text(resp)}")
        audio_bytes.extend(resp.content)
    words = "-".join(re.findall(r"[A-Za-z0-9]+", text)[:5]) or "speech"
    target = O.ws_output(ctx, output, default_name=f"speech-{words}", ext="." + format, overwrite=bool(output))
    target.write_bytes(bytes(audio_bytes))
    return await O.produced(ctx, target, {"voice": voice, "model": model, "chars": len(text), "parts": len(parts)},
                            display=f"Generated speech → {target.name}")
