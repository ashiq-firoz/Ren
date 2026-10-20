"""STAIR — STructure-Aware Information Retriever (IBM, arXiv 2609.03874).

The document's Table of Contents is the retrieval map. The retriever LLM receives the query plus the complete ToC
and *generates the identifier of the leaf section* that contains the answer; generation is constrained to the set
of valid leaf ids (`LLMClient.choose`: vLLM guided decoding / JSON-schema enum, always validated and snapped onto
the allowed set), so a hallucinated target is impossible.

Modes (chosen automatically per query):
  * single     — one document whose ToC fits the token budget: ids are bare leaf paths ("4.1.1"), the exact format
                 used by the fine-tuning dataset (`app.rag.questions.export_training_data`);
  * multi_doc  — several candidate documents whose ToCs fit together: ids are "D2:4.1.1";
  * per_doc    — candidate ToCs don't fit together (or a fine-tuned single-document retriever is configured):
                 one constrained call per document, concurrently, merged by rank and document routing order;
  * beam       — a single ToC is too large for the budget: hierarchical beam descent — choose among the top-level
                 nodes (with child-title hints), keep the best `beam_width`, descend, and finish with a constrained
                 choice over the leaves of a subtree once it fits.
Leaves carry a confidence (position-based here; the search service blends in cross-retriever agreement).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.rag.cache import LRU
from app.rag.textutil import count_tokens, truncate_words

log = logging.getLogger("ren.rag.stair")

DEFAULT_BUDGET_TOKENS = 6000
DEFAULT_BEAM_WIDTH = 3
MAX_BEAM_CALLS = 10
MAX_PER_DOC_CONCURRENCY = 4

STAIR_SYSTEM = (
    "You are STAIR, a structure-aware retriever. You receive a user query and the table of contents of one or "
    "more documents. Each line shows a section id followed by the section title (and sometimes a short summary); "
    "indentation shows nesting. Leaf sections are marked with a bullet (•): they hold the document text and are "
    "the only valid answers. Choose the leaf section whose text most likely contains the answer to the query, "
    "reasoning from the titles, the hierarchy and any summaries. Prefer the most specific leaf. When asked for "
    "several, order them from most to least likely. Reply with section ids exactly as written in the table of "
    "contents."
)
BEAM_SYSTEM = (
    "You are STAIR, a structure-aware retriever navigating a large table of contents level by level. You see the "
    "user query and the sections at the current level. Sections marked + contain subsections that will be shown "
    "next; sections marked • are leaves that hold the document text. Choose the sections most likely to lead to "
    "the answer, best first, and reply with their ids exactly as written."
)


@dataclass
class TocItem:
    id: str
    parent_id: str | None
    path: str
    title: str
    depth: int
    is_leaf: bool
    summary: str | None = None
    token_count: int = 0
    breadcrumb: str = ""
    ordinal: int = 0


@dataclass
class DocToc:
    document_id: str
    title: str
    items: list[TocItem]                                   # pre-order (document order)
    examples: list[tuple[str, str]] = field(default_factory=list)   # (synthetic question, leaf path)

    def __post_init__(self) -> None:
        self.by_id = {it.id: it for it in self.items}
        self.by_path = {it.path: it for it in self.items}
        self.pos = {it.id: i for i, it in enumerate(self.items)}
        self.children: dict[str | None, list[TocItem]] = {}
        for it in self.items:
            self.children.setdefault(it.parent_id, []).append(it)
        self.leaves = [it for it in self.items if it.is_leaf]

    def subtree(self, node: TocItem | None) -> list[TocItem]:
        if node is None:
            return list(self.items)
        start = self.pos[node.id] + 1
        out = []
        for it in self.items[start:]:
            if it.depth <= node.depth:
                break
            out.append(it)
        return out


@dataclass
class Selection:
    document_id: str
    document_title: str
    section_id: str
    path: str
    title: str
    breadcrumb: str
    confidence: float
    rank: int
    doc_rank: int = 0

    def to_api(self) -> dict:
        return {"document_id": self.document_id, "document_title": self.document_title,
                "section_id": self.section_id, "path": self.path, "title": self.title,
                "breadcrumb": self.breadcrumb, "confidence": round(self.confidence, 3)}


@dataclass
class StairOutcome:
    selections: list[Selection] = field(default_factory=list)
    method: str = "none"
    latency_ms: int = 0
    candidates_considered: int = 0
    documents_considered: int = 0
    llm_calls: int = 0
    invalid_outputs: int = 0
    error: str | None = None


# --------------------------------------------------------------------------- ToC construction / loading
def toc_from_nodes(document_id: str, title: str, nodes, examples: list[tuple[str, str]] | None = None) -> DocToc:
    """Build a `DocToc` from `app.rag.structure.TocNode`s (pre-order, root excluded)."""
    items = [TocItem(id=n.id, parent_id=n.parent_id, path=n.path, title=n.title, depth=n.depth, is_leaf=n.is_leaf,
                     summary=n.summary, token_count=n.token_count, breadcrumb=n.breadcrumb, ordinal=n.ordinal)
             for n in nodes]
    return DocToc(document_id=document_id, title=title, items=items, examples=list(examples or []))


_toc_cache = LRU(maxsize=256)


def _spread(items: list, n: int) -> list:
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


async def load_toc(db, document_id: str, *, title: str | None = None, version: str | None = None) -> DocToc | None:
    """Load (and cache by document version) the ToC of a document, with a few synthetic-question examples."""
    import sqlalchemy as sa

    from app.models import Document, Section, SectionQuestion

    if title is None or version is None:
        doc = await db.get(Document, document_id)
        if doc is None:
            return None
        title, version = doc.title, str(doc.updated_at)
    key = (document_id, version)
    cached = _toc_cache.get(key)
    if cached is not None:
        return cached
    rows = (await db.execute(
        sa.select(Section.id, Section.parent_id, Section.path, Section.title, Section.level, Section.is_leaf,
                  Section.summary, Section.token_count, Section.breadcrumb, Section.ordinal)
        .where(Section.document_id == document_id).order_by(Section.ordinal)
    )).all()
    items = [TocItem(id=r[0], parent_id=r[1], path=r[2], title=r[3], depth=int(r[4] or 1), is_leaf=bool(r[5]),
                     summary=r[6], token_count=int(r[7] or 0), breadcrumb=r[8] or "", ordinal=int(r[9] or 0))
             for r in rows]
    qrows = (await db.execute(
        sa.select(SectionQuestion.section_id, SectionQuestion.question)
        .where(SectionQuestion.document_id == document_id).limit(4000)
    )).all()
    first_q: dict[str, str] = {}
    for sid, q in qrows:
        first_q.setdefault(sid, q)
    path_of = {it.id: it.path for it in items if it.is_leaf}
    ex_items = [(first_q[sid], path_of[sid]) for sid in [it.id for it in items if it.is_leaf] if sid in first_q]
    toc = DocToc(document_id=document_id, title=title or "", items=items, examples=_spread(ex_items, 6))
    _toc_cache.set(key, toc)
    return toc


# --------------------------------------------------------------------------- rendering
def item_line(it: TocItem, *, prefix: str = "", base_depth: int = 1, summaries: bool = True) -> str:
    indent = "  " * max(0, it.depth - base_depth)
    bullet = "• " if it.is_leaf else ""
    line = f"{indent}{bullet}{prefix}{it.path} {it.title}"
    if summaries and it.summary:
        line += f" — {truncate_words(it.summary, 28)}"
    return line


def render_toc(toc: DocToc, *, prefix: str = "", summaries: bool = True, items: list[TocItem] | None = None,
               base_depth: int = 1) -> str:
    """Compact ToC: one `<path> <title>` line per node, indented by depth, leaves marked with •."""
    seq = toc.items if items is None else items
    return "\n".join(item_line(it, prefix=prefix, base_depth=base_depth, summaries=summaries) for it in seq)


def render_budgeted(toc: DocToc, budget: int) -> str | None:
    text = render_toc(toc, summaries=True)
    if count_tokens(text) <= budget:
        return text
    if any(it.summary for it in toc.items):
        text = render_toc(toc, summaries=False)
        if count_tokens(text) <= budget:
            return text
    return None


def render_multi(docs: list[DocToc], *, summaries: bool) -> tuple[str, dict[str, tuple[DocToc, TocItem]]]:
    blocks, idmap = [], {}
    for i, d in enumerate(docs, start=1):
        tag = f"D{i}"
        lines = [f"[{tag}] {d.title}"]
        for it in d.items:
            lines.append(item_line(it, prefix=f"{tag}:", summaries=summaries))
            if it.is_leaf:
                idmap[f"{tag}:{it.path}"] = (d, it)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), idmap


def render_for_training(toc: DocToc, target: TocItem, budget: int = DEFAULT_BUDGET_TOKENS) -> tuple[str, str]:
    """ToC text + answer id for a fine-tuning example. Uses the full ToC when it fits (the inference format of
    `single` mode); otherwise the final view of a beam descent: the subtree of the target's top-level ancestor."""
    full = render_budgeted(toc, budget)
    if full is not None:
        return full, target.path
    anc = target
    while anc.parent_id and anc.parent_id in toc.by_id:
        anc = toc.by_id[anc.parent_id]
    sub = toc.subtree(anc) if not anc.is_leaf else [anc]
    text = render_toc(toc, items=sub, base_depth=sub[0].depth if sub else 1, summaries=False)
    return text, target.path


def stair_messages(query: str, toc_text: str, *, examples: list[tuple[str, str]] | None = None, k: int = 1,
                   finetuned: bool = False, multi_doc: bool = False) -> list[dict]:
    """The STAIR prompt. With `finetuned=True` it is exactly the training format:
    system = STAIR_SYSTEM, user = "QUERY: …\\n\\nTABLE OF CONTENTS:\\n…" (assistant = leaf path)."""
    user = f"QUERY: {query.strip()}\n\nTABLE OF CONTENTS:\n{toc_text}"
    if not finetuned:
        if examples:
            user += "\n\nEXAMPLE QUESTIONS AND THE LEAF THAT ANSWERS THEM:\n" + "\n".join(
                f'- "{q}" → {lid}' for q, lid in examples)
        ask = (f"Return the ids of the {k} leaf sections most likely to contain the answer, best first."
               if k > 1 else "Return the id of the leaf section most likely to contain the answer.")
        if multi_doc:
            ask += " Each id is the document tag, a colon and the section path, exactly as listed."
        user += "\n\n" + ask
    return [{"role": "system", "content": STAIR_SYSTEM}, {"role": "user", "content": user}]


def training_messages(query: str, toc_text: str, leaf_path: str) -> list[dict]:
    return stair_messages(query, toc_text, finetuned=True) + [{"role": "assistant", "content": leaf_path}]


# --------------------------------------------------------------------------- selection
def _position_confidence(rank: int) -> float:
    return round(1.0 / (1.0 + 0.6 * rank), 4)


class _Run:
    def __init__(self, llm, query: str, k: int, budget: int, beam_width: int, finetuned: bool, meter,
                 out: StairOutcome) -> None:
        self.llm, self.query, self.k, self.budget = llm, query, k, budget
        self.beam_width, self.finetuned, self.meter, self.out = beam_width, finetuned, meter, out
        self.styles: list[str] = []
        self.modes: list[str] = []

    async def choose(self, messages: list[dict], choices: list[str], multi: int) -> list[str]:
        if not choices:
            return []
        multi = max(1, min(multi, len(choices)))
        self.out.llm_calls += 1
        self.out.candidates_considered += len(choices)
        picked, style = await self.llm.choose(messages, choices, multi=multi, max_tokens=max(200, 24 * multi + 60))
        self.styles.append(style)
        allowed = set(choices)
        valid: list[str] = []
        for p in picked or []:
            if p in allowed:
                if p not in valid:
                    valid.append(p)
            else:  # cannot happen with LLMClient.choose (snap-to-valid); counted for transparency
                self.out.invalid_outputs += 1
        if self.meter is not None:
            self.meter.add_estimate(getattr(self.llm, "model", "unknown"), messages, valid)
        return valid[:multi]


def _selection(d: DocToc, it: TocItem, rank: int, doc_rank: int = 0, discount: float = 1.0) -> Selection:
    return Selection(document_id=d.document_id, document_title=d.title, section_id=it.id, path=it.path,
                     title=it.title, breadcrumb=it.breadcrumb, confidence=_position_confidence(rank) * discount,
                     rank=rank, doc_rank=doc_rank)


async def _single_or_beam(run: _Run, d: DocToc, doc_rank: int = 0, k: int | None = None) -> list[Selection]:
    k = 1 if run.finetuned else (k or run.k)
    text = render_budgeted(d, run.budget)
    if text is None:
        return await _beam(run, d, doc_rank, k)
    run.modes.append("single")
    choices = [it.path for it in d.leaves]
    kk = min(k, len(choices))
    msgs = stair_messages(run.query, text, examples=None if run.finetuned else d.examples[:3], k=kk,
                          finetuned=run.finetuned)
    picked = await run.choose(msgs, choices, kk)
    return [_selection(d, d.by_path[p], r, doc_rank) for r, p in enumerate(picked)]


def _beam_line(d: DocToc, it: TocItem) -> str:
    line = f"{'• ' if it.is_leaf else '+ '}{it.path} {it.title}"
    if it.summary:
        line += f" — {truncate_words(it.summary, 20)}"
    if not it.is_leaf:
        kids = d.children.get(it.id, [])
        if kids:
            names = "; ".join(truncate_words(k.title, 8) for k in kids[:6]) + ("; …" if len(kids) > 6 else "")
            line += f" (contains: {names})"
    return line


async def _beam_step(run: _Run, d: DocToc, node: TocItem | None, k: int) -> tuple[list[TocItem], list[TocItem]]:
    sub = d.subtree(node)
    base = node.depth + 1 if node is not None else 1
    within = (f"\n\n(Subsections of: {node.breadcrumb or node.title})"
              if node is not None and not run.finetuned else "")
    text = render_toc(d, items=sub, base_depth=base, summaries=True)
    if count_tokens(text) > run.budget and any(it.summary for it in sub):
        text = render_toc(d, items=sub, base_depth=base, summaries=False)
    if count_tokens(text) <= run.budget:
        choices = [it.path for it in sub if it.is_leaf]
        kk = min(k, len(choices))
        msgs = stair_messages(run.query, text + within, k=kk, finetuned=run.finetuned)
        picked = await run.choose(msgs, choices, kk)
        return [d.by_path[p] for p in picked], []
    kids = d.children.get(node.id if node is not None else None, [])
    lines = [_beam_line(d, kd) for kd in kids]
    body = "\n".join(lines)
    if count_tokens(body) > run.budget:  # extremely wide level: drop hints, then truncate
        lines = [f"{'• ' if kd.is_leaf else '+ '}{kd.path} {kd.title}" for kd in kids]
        kept, used = [], 0
        for ln in lines:
            t = count_tokens(ln) + 1
            if used + t > run.budget:
                break
            kept.append(ln)
            used += t
        kids = kids[: len(kept)]
        body = "\n".join(kept)
    choices = [kd.path for kd in kids]
    width = min(max(run.beam_width, k), len(choices))
    user = (f"QUERY: {run.query.strip()}\n\nSECTIONS AT THIS LEVEL:\n{body}{within}\n\n"
            f"Return the ids of the {width} sections most likely to lead to the answer, best first.")
    picked = await run.choose([{"role": "system", "content": BEAM_SYSTEM}, {"role": "user", "content": user}],
                              choices, width)
    chosen = [d.by_path[p] for p in picked]
    return [c for c in chosen if c.is_leaf], [c for c in chosen if not c.is_leaf]


async def _beam(run: _Run, d: DocToc, doc_rank: int, k: int) -> list[Selection]:
    run.modes.append("beam")
    frontier: list[TocItem | None] = [None]
    selected: list[TocItem] = []
    levels = 0
    while frontier and len(selected) < k and run.out.llm_calls < MAX_BEAM_CALLS and levels < 12:
        levels += 1
        results = await asyncio.gather(*(_beam_step(run, d, node, k) for node in frontier))
        nxt: list[TocItem] = []
        for leaves, branches in results:
            for it in leaves:
                if it not in selected:
                    selected.append(it)
            for b in branches:
                if b not in nxt:
                    nxt.append(b)
        frontier = nxt[: run.beam_width]
    return [_selection(d, it, r, doc_rank, discount=0.95) for r, it in enumerate(selected[:k])]


async def _multi(run: _Run, docs: list[DocToc], text: str, idmap: dict[str, tuple[DocToc, TocItem]]
                 ) -> list[Selection]:
    run.modes.append("multi_doc")
    choices = list(idmap)
    kk = min(run.k, len(choices))
    examples: list[tuple[str, str]] = []
    for i, d in enumerate(docs, start=1):
        if d.examples and len(examples) < 3:
            q, p = d.examples[0]
            examples.append((q, f"D{i}:{p}"))
    msgs = stair_messages(run.query, text, examples=examples, k=kk, multi_doc=True)
    picked = await run.choose(msgs, choices, kk)
    order = {d.document_id: i for i, d in enumerate(docs)}
    out = []
    for r, pid in enumerate(picked):
        d, it = idmap[pid]
        out.append(_selection(d, it, r, order[d.document_id]))
    return out


async def _per_doc(run: _Run, docs: list[DocToc]) -> list[Selection]:
    run.modes.append("per_doc")
    sem = asyncio.Semaphore(MAX_PER_DOC_CONCURRENCY)
    per_doc_k = 1 if run.finetuned else max(1, min(run.k, 2))

    async def one(i: int, d: DocToc) -> list[Selection]:
        async with sem:
            try:
                return await _single_or_beam(run, d, doc_rank=i, k=per_doc_k)
            except Exception as e:  # noqa: BLE001
                log.warning("STAIR selection failed for document %s: %s", d.document_id, e)
                return []

    results = await asyncio.gather(*(one(i, d) for i, d in enumerate(docs)))
    merged = [s for sels in results for s in sels]
    merged.sort(key=lambda s: (s.rank, s.doc_rank))  # interleave: best leaf of each document first
    for r, s in enumerate(merged):
        s.confidence = round(s.confidence * (1.0 / (1.0 + 0.15 * s.doc_rank)), 4)
        s.rank = r
    return merged


async def select_leaves(llm, query: str, docs: list[DocToc], *, k: int = 3, budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                        beam_width: int = DEFAULT_BEAM_WIDTH, finetuned: bool = False, meter=None) -> StairOutcome:
    """Constrained leaf selection over the candidate documents' ToCs (documents ordered by routing score)."""
    t0 = time.perf_counter()
    out = StairOutcome(documents_considered=len(docs))
    docs = [d for d in docs if d.leaves]
    if llm is None:
        out.method = "unavailable"
        out.error = "No LLM provider is configured for structure-aware retrieval."
        return out
    if not docs or not (query or "").strip():
        out.method = "no_documents"
        return out
    run = _Run(llm, query, max(1, k), max(1, int(budget_tokens)), max(1, beam_width), finetuned, meter, out)
    sels: list[Selection] = []
    try:
        if finetuned and len(docs) > 1:
            sels = await _per_doc(run, docs)
        elif len(docs) == 1:
            sels = await _single_or_beam(run, docs[0])
        else:
            text, idmap = render_multi(docs, summaries=True)
            if count_tokens(text) > run.budget and any(it.summary for d in docs for it in d.items):
                text, idmap = render_multi(docs, summaries=False)
            if count_tokens(text) <= run.budget:
                sels = await _multi(run, docs, text, idmap)
            else:
                sels = await _per_doc(run, docs)
    except Exception as e:  # noqa: BLE001
        log.warning("STAIR selection failed: %s", e)
        out.error = f"{type(e).__name__}: {str(e)[:280]}"
    out.selections = sels[: max(k, 1)]
    modes = list(dict.fromkeys(run.modes)) or ["none"]
    mode = "per_doc" if "per_doc" in modes else modes[0]
    if mode == "per_doc" and "beam" in modes:
        mode = "per_doc+beam"
    styles = [s for s in dict.fromkeys(run.styles) if s]
    out.method = f"{mode}:{'/'.join(styles)}" if styles else mode
    out.latency_ms = int((time.perf_counter() - t0) * 1000)
    return out
