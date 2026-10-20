"""Text utilities shared by the RAG pipeline.

- token counting (tiktoken `cl100k_base` when its encoding file is already cached locally — never downloaded
  in-process — otherwise a fast, slightly conservative estimate)
- paragraph / sentence segmentation that keeps fenced code blocks and markdown tables intact
- the lexical analyzer used by the in-process BM25 index and the hashing embedder (lowercase, stopwords,
  light English stemming)
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass

# --------------------------------------------------------------------------- token counting
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

_ENCODER = None
_ENCODER_CHECKED = False


def _tiktoken_encoder():
    """Return the cl100k encoder only if its BPE file is already cached (no network access)."""
    global _ENCODER, _ENCODER_CHECKED
    if _ENCODER_CHECKED:
        return _ENCODER
    _ENCODER_CHECKED = True
    if os.environ.get("RAG_TOKENIZER", "").lower() == "estimate":
        return None
    try:
        import tiktoken  # noqa: F401

        cache_dir = (os.environ.get("TIKTOKEN_CACHE_DIR") or os.environ.get("DATA_GYM_CACHE_DIR")
                     or os.path.join(tempfile.gettempdir(), "data-gym-cache"))
        blob = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
        if os.path.exists(os.path.join(cache_dir, hashlib.sha1(blob.encode()).hexdigest())):
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        _ENCODER = None
    return _ENCODER


def estimate_tokens(text: str) -> int:
    """Fast token estimate calibrated to cl100k (errs slightly high, which is safe for budgets)."""
    if not text:
        return 0
    words = len(_WORD_RE.findall(text))
    punct = len(_PUNCT_RE.findall(text))
    return max(1, int(math.ceil(max(len(text) / 4.0, words * 1.3 + punct * 0.5))))


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _tiktoken_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # noqa: BLE001
            pass
    return estimate_tokens(text)


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Cut `text` to roughly `max_tokens`, preferring a sentence/word boundary."""
    if count_tokens(text) <= max_tokens:
        return text
    # proportional first cut, then trim to a boundary
    ratio = max_tokens / max(1, count_tokens(text))
    cut = text[: max(1, int(len(text) * ratio))]
    while cut and count_tokens(cut) > max_tokens:
        cut = cut[: int(len(cut) * 0.9)]
    m = max(cut.rfind(". "), cut.rfind("\n"))
    if m > len(cut) * 0.6:
        cut = cut[: m + 1]
    else:
        sp = cut.rfind(" ")
        if sp > len(cut) * 0.8:
            cut = cut[:sp]
    return cut.rstrip() + " …"


def truncate_words(text: str, n: int) -> str:
    words = (text or "").split()
    if len(words) <= n:
        return " ".join(words)
    return " ".join(words[:n]) + "…"


def clean_inline(text: str) -> str:
    """Collapse whitespace inside a single line of text."""
    return re.sub(r"\s+", " ", (text or "")).strip()


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Normalize newlines, drop control characters, trim trailing spaces and collapse 3+ blank lines."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("﻿", "")
    t = _CTRL_RE.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# --------------------------------------------------------------------------- segmentation
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+•▪◦–]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, never inside a fenced code block; markdown tables stay one paragraph."""
    if not text:
        return []
    paras: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue
        if not in_fence and not line.strip():
            if cur:
                paras.append("\n".join(cur).strip("\n"))
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append("\n".join(cur).strip("\n"))
    return [p for p in paras if p.strip()]


def is_table(paragraph: str) -> bool:
    lines = [ln for ln in paragraph.split("\n") if ln.strip()]
    return len(lines) >= 2 and all(_TABLE_LINE_RE.match(ln) for ln in lines[:3])


def is_code(paragraph: str) -> bool:
    return bool(_FENCE_RE.match(paragraph.split("\n", 1)[0]))


def is_list(paragraph: str) -> bool:
    lines = [ln for ln in paragraph.split("\n") if ln.strip()]
    return len(lines) >= 2 and sum(1 for ln in lines if _LIST_RE.match(ln)) >= max(2, len(lines) // 2)


def table_header(paragraph: str) -> str:
    """Header line(s) of a markdown table (header row + separator row)."""
    lines = paragraph.split("\n")
    if len(lines) >= 2 and _TABLE_SEP_RE.match(lines[1]):
        return "\n".join(lines[:2])
    return lines[0] if lines else ""


_ABBREV = {
    "e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "no", "fig", "figs", "eq", "al",
    "inc", "ltd", "co", "corp", "dept", "approx", "est", "art", "sec", "ch", "vol", "pp", "p", "u.s", "u.k", "jan",
    "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "cf", "ref", "para",
}
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    """Rule-based sentence splitter that respects common abbreviations and decimals."""
    text = (text or "").strip()
    if not text:
        return []
    pieces: list[str] = []
    start = 0
    for m in _SENT_SPLIT_RE.finditer(text):
        end = m.start()
        candidate = text[start:end].rstrip()
        last = candidate.rsplit(None, 1)[-1] if candidate.split() else ""
        token = last.rstrip(".!?…\"'”’)]").lower()
        if token in _ABBREV or (len(token) == 1 and token.isalpha()):
            continue  # "Dr. Smith", "J. Doe" — not a boundary
        pieces.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    # newline-separated lines inside a paragraph (e.g. lists) are natural boundaries too
    out: list[str] = []
    for p in pieces:
        if "\n" in p:
            out.extend(s.strip() for s in p.split("\n") if s.strip())
        else:
            out.append(p)
    return out


@dataclass
class Unit:
    """Smallest packable piece of text (a sentence, table row, code line or word window)."""

    text: str
    tokens: int
    sep: str = " "          # separator placed before this unit when it isn't first in a chunk
    page: int | None = None
    header: str | None = None   # markdown table header to repeat when a chunk starts mid-table
    kind: str = "text"      # text | row | code | line


def _word_windows(text: str, max_tokens: int, page: int | None, sep: str) -> list[Unit]:
    words = text.split()
    out: list[Unit] = []
    cur: list[str] = []
    for w in words:
        cur.append(w)
        if estimate_tokens(" ".join(cur)) >= max_tokens:
            out.append(Unit(" ".join(cur), count_tokens(" ".join(cur)), sep if not out else " ", page))
            cur = []
    if cur:
        out.append(Unit(" ".join(cur), count_tokens(" ".join(cur)), sep if not out else " ", page))
    return out


def text_units(paragraph: str, max_tokens: int, page: int | None = None, first_sep: str = "\n\n",
               sentences: bool = False) -> list[Unit]:
    """Break one paragraph into units no larger than `max_tokens` (sentences, rows, lines, word windows).

    With `sentences=True`, prose paragraphs are always split into sentence units (so chunk overlap can be
    sentence-granular); tables, code and lists that fit stay whole.
    """
    tok = count_tokens(paragraph)
    structured = is_table(paragraph) or is_code(paragraph) or is_list(paragraph)
    if tok <= max_tokens and (not sentences or structured):
        return [Unit(paragraph, tok, first_sep, page)]
    if is_table(paragraph):
        header = table_header(paragraph)
        hdr_lines = header.count("\n") + 1
        rows = paragraph.split("\n")[hdr_lines:]
        units = [Unit(header, count_tokens(header), first_sep, page, None, "row")]
        for r in rows:
            if not r.strip():
                continue
            rt = count_tokens(r)
            if rt > max_tokens:  # absurdly wide row: window it
                units.extend(_word_windows(r, max_tokens, page, "\n"))
            else:
                units.append(Unit(r, rt, "\n", page, header, "row"))
        return units
    if is_code(paragraph) or is_list(paragraph):
        units = []
        for i, line in enumerate(paragraph.split("\n")):
            if not line.strip():
                continue
            lt = count_tokens(line)
            kind = "code" if is_code(paragraph) else "line"
            if lt > max_tokens:
                units.extend(_word_windows(line, max_tokens, page, first_sep if not units else "\n"))
            else:
                units.append(Unit(line, lt, first_sep if not units else "\n", page, None, kind))
        return units
    units = []
    for s in split_sentences(paragraph):
        st = count_tokens(s)
        if st > max_tokens:
            units.extend(_word_windows(s, max_tokens, page, first_sep if not units else " "))
        else:
            units.append(Unit(s, st, first_sep if not units else " ", page))
    return units


def join_units(units: Iterable[Unit]) -> str:
    out = ""
    for u in units:
        out = u.text if not out else out + u.sep + u.text
    return out.strip()


# --------------------------------------------------------------------------- lexical analysis
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers
herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on once
only or other our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yours yourself yourselves shall may might must per via etc
""".split())

_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)


def stem(word: str) -> str:
    """Tiny, deterministic English stemmer (plural/-ed/-ing/-ly folding). Same mapping for docs and queries."""
    w = word
    if len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("'s"):
        w = w[:-2]
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        w = w[:-1]
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "aeiouls":
            w = w[:-1]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "aeiouls":
            w = w[:-1]
    if w.endswith("ly") and len(w) > 5:
        w = w[:-2]
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def tokenize(text: str) -> list[str]:
    """Analyzer for BM25 / hashing embeddings: lowercase → word tokens → drop stopwords → stem."""
    out = []
    for raw in _TOKEN_RE.findall((text or "").lower()):
        raw = raw.replace("'", "")
        if raw in STOPWORDS or (len(raw) < 2 and not raw.isdigit()):
            continue
        out.append(stem(raw))
    return out


def query_terms(text: str, limit: int = 32) -> list[str]:
    """Distinct, sanitized raw query words (no stemming): Unicode letters/digits only, so no tsquery operator
    (& | ! : * ( ) ') can reach Postgres."""
    seen: list[str] = []
    for raw in re.findall(r"[^\W_]+", text or "", re.UNICODE):
        w = raw.lower()
        if w in STOPWORDS or len(w) < 2 or w in seen:
            continue
        seen.append(w)
        if len(seen) >= limit:
            break
    return seen
