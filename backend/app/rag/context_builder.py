"""Builds the grounded context block (with `[n]` source tags) and the matching `Citation` objects.

Results are grouped by ToC leaf: adjacent chunks are merged (their overlap removed), small leaves are expanded to
their full text, every group becomes one numbered source `[n]` headed by its breadcrumb and section path, and
groups are added in rank order until the token budget is used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.rag.textutil import count_tokens, truncate_tokens

DEFAULT_BUDGET_TOKENS = 3500
EXPAND_LEAF_TOKENS = 450
SNIPPET_CHARS = 280


@dataclass
class SourceGroup:
    n: int
    document_id: str
    document_title: str
    section_id: str | None
    section_path: str | None
    section_title: str | None
    breadcrumb: str | None
    score: float | None
    chunk_ids: list[str] = field(default_factory=list)
    text: str = ""
    snippet: str = ""
    page_start: int | None = None
    page_end: int | None = None


@dataclass
class BuiltContext:
    text: str
    citations: list[dict]
    groups: list[SourceGroup]
    chunk_to_source: dict[str, int]
    tokens: int


def _merge_overlap(a: str, b: str, max_overlap: int = 1200) -> str:
    """Concatenate two adjacent chunks, dropping the longest suffix of `a` that prefixes `b`."""
    a_s, b_s = a.rstrip(), b.lstrip()
    lim = min(len(a_s), len(b_s), max_overlap)
    for size in range(lim, 15, -1):
        if a_s.endswith(b_s[:size]):
            return a_s + b_s[size:]
    return a_s + "\n\n" + b_s


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(",;:") + "…"


def source_header(g: SourceGroup) -> str:
    label = g.breadcrumb or g.document_title or "Source"
    extra = []
    if g.section_path:
        extra.append(f"§{g.section_path}")
    if g.page_start:
        extra.append(f"p. {g.page_start}" if not g.page_end or g.page_end == g.page_start
                     else f"pp. {g.page_start}–{g.page_end}")
    return f"[{g.n}] {label}" + (f" ({', '.join(extra)})" if extra else "")


def build_context(results: list[dict], *, leaf_texts: dict[str, tuple[str, int]] | None = None,
                  budget_tokens: int = DEFAULT_BUDGET_TOKENS, expand_leaf_tokens: int = EXPAND_LEAF_TOKENS
                  ) -> BuiltContext:
    """`results`: ranked dicts with chunk_id, document_id, document_title, section_id, section_path, section_title,
    breadcrumb, content, score, ordinal (+ optional page_start/page_end). `leaf_texts`: section_id → (full leaf
    text, token count) for leaves eligible for expansion."""
    leaf_texts = leaf_texts or {}
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for r in results:
        key = r.get("section_id") or f"chunk:{r['chunk_id']}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(r)

    groups: list[SourceGroup] = []
    pieces: list[str] = []
    chunk_to_source: dict[str, int] = {}
    used = 0
    for key in order:
        items = grouped[key]
        best = items[0]
        sec = best.get("section_id")
        if sec and sec in leaf_texts and leaf_texts[sec][1] <= expand_leaf_tokens and leaf_texts[sec][0].strip():
            text = leaf_texts[sec][0].strip()
        else:
            ordered = sorted(items, key=lambda r: r.get("ordinal") or 0)
            text = ordered[0]["content"].strip()
            for prev, cur in zip(ordered, ordered[1:]):
                if (cur.get("ordinal") or 0) - (prev.get("ordinal") or 0) == 1:
                    text = _merge_overlap(text, cur["content"])
                else:
                    text = text.rstrip() + "\n[…]\n" + cur["content"].strip()
        g = SourceGroup(
            n=len(groups) + 1, document_id=best["document_id"], document_title=best.get("document_title") or "",
            section_id=sec, section_path=best.get("section_path"), section_title=best.get("section_title"),
            breadcrumb=best.get("breadcrumb"), score=best.get("score"), chunk_ids=[r["chunk_id"] for r in items],
            page_start=min((r["page_start"] for r in items if r.get("page_start")), default=None),
            page_end=max((r["page_end"] for r in items if r.get("page_end")), default=None),
        )
        header = source_header(g)
        tokens = count_tokens(header) + count_tokens(text) + 2
        remaining = budget_tokens - used
        if tokens > remaining:
            if groups and remaining < 120:
                break
            text = truncate_tokens(text, max(60, remaining - count_tokens(header) - 4))
            tokens = count_tokens(header) + count_tokens(text) + 2
        g.text = text
        g.snippet = _snippet(best["content"])
        groups.append(g)
        for cid in g.chunk_ids:
            chunk_to_source[cid] = g.n
        pieces.append(f"{header}\n{text}")
        used += tokens
        if used >= budget_tokens:
            break
    citations = [citation_dict(g) for g in groups]
    return BuiltContext(text="\n\n".join(pieces), citations=citations, groups=groups,
                        chunk_to_source=chunk_to_source, tokens=used)


def citation_dict(g: SourceGroup) -> dict:
    return {
        "id": g.n,
        "document_id": g.document_id,
        "document_title": g.document_title,
        "section_id": g.section_id,
        "section_path": g.section_path,
        "section_title": g.section_title,
        "breadcrumb": g.breadcrumb,
        "snippet": g.snippet,
        "score": round(float(g.score), 4) if g.score is not None else None,
    }


_CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*(?:,|–|-)\s*\d{1,3})*)\]")


_CODE_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)


def used_citations(text: str, valid: set[int]) -> tuple[str, list[int]]:
    """Find `[n]`, `[1, 3]`, `[2-4]` markers (outside code); drop references to unknown sources.
    Returns (clean text, source ids in order of first use)."""
    used: list[int] = []
    dropped = [False]

    def repl(m: re.Match) -> str:
        nums: list[int] = []
        for part in re.split(r"\s*,\s*", m.group(1)):
            rng = re.split(r"\s*[–-]\s*", part)
            if len(rng) == 2 and rng[0].isdigit() and rng[1].isdigit():
                a, b = int(rng[0]), int(rng[1])
                nums.extend(range(a, b + 1) if 0 < b - a < 20 else [a, b])
            elif part.strip().isdigit():
                nums.append(int(part))
        keep = [x for x in nums if x in valid]
        if len(keep) != len(nums):
            dropped[0] = True
        for x in keep:
            if x not in used:
                used.append(x)
        return "".join(f"[{x}]" for x in dict.fromkeys(keep))

    parts = _CODE_RE.split(text or "")
    out = []
    for i, part in enumerate(parts):
        out.append(part if i % 2 == 1 else _CITE_RE.sub(repl, part))
    clean = "".join(out)
    if dropped[0]:
        clean = re.sub(r"[ \t]+([.,;:])(?=\s|$)", r"\1", clean)
    return clean, used
