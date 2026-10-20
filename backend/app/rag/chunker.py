"""Token-aware chunking inside ToC leaves.

Chunks never cross leaf boundaries (so every chunk belongs to exactly one STAIR target). Inside a leaf, text is
split at paragraph → sentence → word boundaries into ~350-token chunks with ~15% (sentence-granular) overlap;
markdown tables are split by rows with the header repeated; code/list lines stay intact. Each chunk's
`search_text` is `breadcrumb + "\\n" + content` — a contextual chunk header that helps both BM25 and vectors.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.rag.textutil import Unit, count_tokens, join_units, split_paragraphs, text_units

TARGET_TOKENS = 350
OVERLAP_RATIO = 0.15
MAX_TOKENS = 500


@dataclass
class ChunkDraft:
    section_id: str | None
    ordinal: int
    content: str
    search_text: str
    token_count: int
    meta: dict = field(default_factory=dict)


def _units(parts: list[tuple[str, int | None]], target: int) -> list[Unit]:
    units: list[Unit] = []
    for text, page in parts:
        for para in split_paragraphs(text):
            units.extend(text_units(para, target, page, first_sep="\n\n", sentences=True))
    return units


def pack_units(units: list[Unit], *, target: int = TARGET_TOKENS, overlap_ratio: float = OVERLAP_RATIO,
               max_tokens: int = MAX_TOKENS) -> list[list[Unit]]:
    """Greedy packing with sentence-level overlap; tables repeat their header on continuation chunks."""
    overlap_budget = int(target * overlap_ratio)
    # tokens of the paragraph each paragraph-starting unit opens (to prefer closing chunks at paragraph ends)
    para_tok = [0] * len(units)
    i = 0
    while i < len(units):
        j = i + 1
        while j < len(units) and units[j].sep != "\n\n":
            j += 1
        para_tok[i] = sum(u.tokens for u in units[i:j])
        i = j
    chunks: list[tuple[list[Unit], int]] = []  # (units, number of leading carried-over units)
    cur: list[Unit] = []
    carried = 0
    cur_tok = 0
    fresh = 0  # tokens of new (non-overlap) content in `cur`
    for idx, u in enumerate(units):
        at_paragraph = u.sep == "\n\n"
        overflow = cur_tok + u.tokens > target
        early = at_paragraph and cur_tok >= 0.6 * target and cur_tok + para_tok[idx] > target
        if cur and fresh > 0 and (overflow or early):
            chunks.append((cur, carried))
            # overlap: trailing prose units of the previous chunk (never table rows / code lines)
            ov: list[Unit] = []
            t = 0
            for x in reversed(cur):
                if x.kind != "text" or t + x.tokens > overlap_budget:
                    break
                ov.insert(0, x)
                t += x.tokens
            cur, cur_tok, fresh, carried = list(ov), t, 0, len(ov)
            if u.kind == "row" and u.header:
                hdr = Unit(u.header, count_tokens(u.header), "\n\n", u.page, None, "row")
                cur.append(hdr)
                cur_tok += hdr.tokens
                carried += 1
        cur.append(u)
        cur_tok += u.tokens
        fresh += u.tokens
    if cur and fresh > 0:
        chunks.append((cur, carried))
    # fold a tiny tail into the previous chunk
    if len(chunks) >= 2:
        tail, tail_carried = chunks[-1]
        new_units = tail[tail_carried:]
        new_tok = sum(x.tokens for x in new_units)
        prev, prev_carried = chunks[-2]
        if new_tok < target * 0.25 and sum(x.tokens for x in prev) + new_tok <= max_tokens:
            # carried-over units (overlap / repeated table header) are already in `prev`
            chunks[-2] = (prev + new_units, prev_carried)
            chunks.pop()
    return [c for c, _ in chunks]


def chunk_text(text: str, *, target: int = TARGET_TOKENS, overlap_ratio: float = OVERLAP_RATIO) -> list[str]:
    units = _units([(text, None)], target)
    out = []
    for grp in pack_units(units, target=target, overlap_ratio=overlap_ratio):
        s = _join(grp)
        if s:
            out.append(s)
    return out


def _join(units: list[Unit]) -> str:
    return join_units(units)


def chunk_leaves(leaves, *, target: int = TARGET_TOKENS, overlap_ratio: float = OVERLAP_RATIO) -> list[ChunkDraft]:
    """Chunk every leaf (`app.rag.structure.TocNode`) in document order."""
    drafts: list[ChunkDraft] = []
    ordinal = 0
    for leaf in leaves:
        parts = [(p.text, p.page) for p in leaf.parts if p.text.strip()]
        if not parts:
            continue
        units = _units(parts, target)
        for grp in pack_units(units, target=target, overlap_ratio=overlap_ratio):
            content = _join(grp)
            if not content.strip():
                continue
            pages = [u.page for u in grp if u.page is not None]
            header = leaf.breadcrumb or leaf.title
            drafts.append(ChunkDraft(
                section_id=leaf.id,
                ordinal=ordinal,
                content=content,
                search_text=f"{header}\n{content}" if header else content,
                token_count=count_tokens(content),
                meta={"path": leaf.path, "page_start": min(pages) if pages else leaf.page_start,
                      "page_end": max(pages) if pages else leaf.page_end},
            ))
            ordinal += 1
    return drafts
