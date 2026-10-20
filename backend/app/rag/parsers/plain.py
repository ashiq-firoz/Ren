"""Plain-text parser with cheap heading heuristics.

Recognized heading styles (a scheme is only accepted when it yields ≥ 2 headings):
  - numbered:   "1. Introduction", "1.2 Scope", "4.1.1 Parental leave"   (level = numbering depth)
  - keywords:   "Part II", "Chapter 3: Benefits", "Section 4", "Article 5", "Appendix A"
  - underlined: "Title" followed by "=====" (level 1) or "-----" (level 2)
  - ALL-CAPS lines: "LEAVE POLICY"
  - markdown "#" headings → delegated to the markdown parser
Paragraphs are split on blank lines and hard-wrapped lines are unwrapped.
"""
from __future__ import annotations

import re

from app.rag.parsers import Block, ParsedDocument

NUM_RE = re.compile(r"^(?P<num>(?:\d{1,3}\.){0,5}\d{1,3})(?P<dot>[.)])?\s+(?P<title>\S.*)$")
KEYWORD_RE = re.compile(
    r"^(?P<kw>part|chapter|section|article|appendix|annex|schedule|exhibit)\s+"
    r"(?P<num>\d{1,3}(?:\.\d{1,3})*|[ivxlcdm]{1,7}|[a-z])\b\s*[.:\-–—]?\s*(?P<title>.*)$",
    re.I,
)
UNDERLINE_RE = re.compile(r"^\s*(?P<ch>=|-|~)(?P=ch){2,}\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+•▪◦–]|\(?[a-z0-9]{1,3}[.)])\s+", re.I)
KEYWORD_LEVEL = {"part": 1, "chapter": 2, "section": 3, "article": 3, "appendix": 1, "annex": 1, "schedule": 1,
                 "exhibit": 1}
MAX_HEADING_CHARS = 120


def _letters(s: str) -> str:
    return "".join(ch for ch in s if ch.isalpha())


def is_all_caps_heading(line: str) -> bool:
    s = line.strip()
    letters = _letters(s)
    if len(letters) < 4 or len(s) > 90 or len(s.split()) > 12:
        return False
    if s.endswith((".", ",", ";")):
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters) >= 0.9


def classify_heading(line: str, *, prev_blank: bool, next_line: str | None) -> tuple[str, int, str] | None:
    """Return (scheme, level, title) when `line` looks like a heading, else None."""
    s = line.strip()
    if not s or len(s) > MAX_HEADING_CHARS or not re.search(r"[A-Za-z]", s):
        return None
    nxt = (next_line or "").strip()
    m = KEYWORD_RE.match(s)
    if m:
        rest = m.group("title").strip()
        words = rest.split()
        sentence_like = (rest.endswith(".") and len(words) > 6) or (rest[:1].islower() and len(words) > 3)
        if (len(words) <= 12 and not rest.endswith((",", ";")) and not sentence_like
                and (prev_blank or len(words) <= 6)):
            return "keyword", KEYWORD_LEVEL[m.group("kw").lower()], s
    m = NUM_RE.match(s)
    if m:
        title = m.group("title").strip()
        depth = m.group("num").count(".") + 1
        words = title.split()
        if not words or len(words) > 14 or title.endswith((".", ",", ";", "?")) and len(words) > 3:
            return None
        if not (title[0].isupper() or title[0].isdigit()):
            return None
        if depth == 1:
            # "1. Buy milk" lists: consecutive numbered lines without blank lines are list items
            if not prev_blank or (nxt and NUM_RE.match(nxt) and not nxt.endswith(":")):
                return None
            if len(words) > 10:
                return None
        if re.fullmatch(r"[\d.\s%$€£,]+", title):  # "1. 2.5 3.0" — numbers, not a heading
            return None
        return "numbered", depth, s
    if prev_blank and is_all_caps_heading(s):
        return "caps", 1, s
    return None


def detect_headings(lines: list[str]) -> dict[int, tuple[int, str]]:
    """Map line index → (level, title) for lines detected as headings (consistent scheme, ≥ 2 hits)."""
    found: dict[int, tuple[str, int, str]] = {}
    skip: set[int] = set()
    n = len(lines)
    for i, line in enumerate(lines):
        if i in skip:
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        nxt = lines[i + 1] if i + 1 < n else None
        # underlined headings
        if nxt is not None and line.strip() and UNDERLINE_RE.match(nxt) and len(line.strip()) <= MAX_HEADING_CHARS:
            if len(nxt.strip()) >= max(3, int(len(line.strip()) * 0.5)):
                level = 1 if nxt.strip()[0] == "=" else 2
                found[i] = ("underline", level, line.strip())
                skip.add(i + 1)
                continue
        c = classify_heading(line, prev_blank=prev_blank, next_line=nxt)
        if c:
            found[i] = c
    if len(found) < 2:
        return {}
    schemes = {s for s, _, _ in found.values()}
    # Mixed docs: keyword chapters + numbered subsections → numbered levels nest below the keyword level.
    kw_levels = sorted({lvl for s, lvl, _ in found.values() if s == "keyword"})
    out: dict[int, tuple[int, str]] = {}
    caps_level = 1
    if "numbered" in schemes and "caps" in schemes:
        # ALL-CAPS lines in a numbered document are usually top-level (front/back matter): keep at 1.
        caps_level = 1
    for i, (scheme, level, title) in found.items():
        if scheme == "numbered" and kw_levels:
            base = kw_levels[-1]
            level = base + max(0, level - 1) if level > 1 else base + 1
        elif scheme == "caps":
            level = caps_level
        out[i] = (max(1, min(level, 6)), title)
    # A lone heading style that fires on most lines is noise (e.g. a CAPS-only data file)
    nonblank = sum(1 for ln in lines if ln.strip())
    if nonblank and len(out) > 0.5 * nonblank:
        return {}
    return out


def unwrap_paragraph(lines: list[str]) -> str:
    """Join hard-wrapped lines; keep list items / table rows on their own lines; de-hyphenate."""
    out: list[str] = []
    for raw in lines:
        s = raw.rstrip()
        if not s.strip():
            continue
        if not out:
            out.append(s.strip())
            continue
        prev = out[-1]
        if BULLET_RE.match(s) or s.lstrip().startswith("|") or prev.lstrip().startswith("|") or raw.startswith(("    ", "\t")):
            out.append(s.strip() if not raw.startswith(("    ", "\t")) else s)
        elif prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and s.strip()[:1].islower():
            out[-1] = prev[:-1] + s.strip()
        else:
            out[-1] = prev + " " + s.strip()
    return "\n".join(out)


def paragraphs_from_lines(lines: list[str]) -> list[str]:
    paras: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if ln.strip():
            cur.append(ln)
        elif cur:
            paras.append(unwrap_paragraph(cur))
            cur = []
    if cur:
        paras.append(unwrap_paragraph(cur))
    return [p for p in paras if p.strip()]


def looks_like_markdown(text: str) -> bool:
    return len(re.findall(r"(?m)^#{1,6}\s+\S", text)) >= 2


def parse_text(text: str, *, title: str | None = None) -> ParsedDocument:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    if looks_like_markdown(text):
        from app.rag.parsers.md import parse_markdown

        doc = parse_markdown(text)
        doc.format = "text"
        return doc
    lines = text.split("\n")
    heads = detect_headings(lines)
    blocks: list[Block] = []
    buf: list[str] = []
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        if i in heads:
            for p in paragraphs_from_lines(buf):
                blocks.append(Block("text", p))
            buf = []
            level, htitle = heads[i]
            blocks.append(Block("heading", htitle, level))
            if i + 1 < len(lines) and UNDERLINE_RE.match(lines[i + 1] or ""):
                skip_next = True
            continue
        buf.append(line)
    for p in paragraphs_from_lines(buf):
        blocks.append(Block("text", p))
    return ParsedDocument(title=title or "", blocks=blocks, format="text")
