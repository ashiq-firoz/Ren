"""Table-of-Contents builder: `ParsedDocument` → section tree for STAIR.

Invariants of the produced tree:
  * every piece of text lives in **exactly one leaf** (non-leaf nodes carry no text);
  * intro text of a non-leaf becomes an "Overview" leaf child with path `<parent>.0` ("0" at the top level);
    tiny intros are merged into the next leaf in reading order instead;
  * oversized leaves are split at paragraph boundaries into "<title> (part n)" leaf children;
  * empty headings are dropped; a single top-level wrapper heading (e.g. a markdown H1 title) is collapsed into
    the document title;
  * hierarchical paths ("4.1.1"), breadcrumbs ("Doc › Chapter › Section"), document-order ordinals, token counts
    and page ranges are assigned; a heading's own numbering label is stripped when it equals the computed path.

Flat documents (no headings) are segmented at paragraph boundaries into ≈600–900-token segments. With the org
setting `retrieval.structure_augmentation`, an LLM writes a short title for every segment (batched) and groups the
segments into chapters (`structure_source = "augmented"`); otherwise segments get heuristic titles ("flat").
Optionally the LLM writes one-line leaf summaries (batched) that enrich the ToC shown to the STAIR retriever.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.core.db import new_id
from app.rag.llmutil import UsageMeter, safe_json
from app.rag.parsers import ParsedDocument
from app.rag.textutil import count_tokens, join_units, split_paragraphs, split_sentences, text_units, truncate_tokens

log = logging.getLogger("ren.rag.structure")

MAX_LEAF_TOKENS = 1600
PART_TARGET_TOKENS = 1000
MIN_FRAGMENT_TOKENS = 25
SEGMENT_MIN_TOKENS = 600
SEGMENT_TARGET_TOKENS = 750
SEGMENT_MAX_TOKENS = 900
OVERVIEW_TITLE = "Overview"
BREADCRUMB_SEP = " › "
TITLE_BATCH = 10
SUMMARY_BATCH = 8
LLM_CONCURRENCY = 3


@dataclass
class Part:
    text: str
    page: int | None = None


@dataclass(eq=False)
class TocNode:
    title: str
    level: int = 0                                   # raw heading level while building
    parts: list[Part] = field(default_factory=list)  # direct text (only leaves keep text after normalization)
    children: list[TocNode] = field(default_factory=list)
    kind: str = "section"                            # root | section | overview | part | segment | chapter
    page_start: int | None = None
    page_end: int | None = None
    summary: str | None = None
    id: str = field(default_factory=new_id)
    parent_id: str | None = None
    path: str = ""
    depth: int = 0                                   # 1 = top level
    breadcrumb: str = ""
    token_count: int = 0
    ordinal: int = 0

    @property
    def content(self) -> str:
        return "\n\n".join(p.text for p in self.parts if p.text.strip())

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def walk(self) -> Iterator[TocNode]:
        """Pre-order traversal (self first)."""
        yield self
        for c in self.children:
            yield from c.walk()

    def leaves(self) -> list[TocNode]:
        return [n for n in self.walk() if n.is_leaf and n.kind != "root"]


@dataclass
class StructureResult:
    root: TocNode
    title: str
    structure_source: str

    def nodes(self) -> list[TocNode]:
        return [n for n in self.root.walk() if n is not self.root]

    def leaves(self) -> list[TocNode]:
        return [n for n in self.nodes() if n.is_leaf]


# --------------------------------------------------------------------------- tree construction
def _pages(parts: list[Part]) -> tuple[int | None, int | None]:
    ps = [p.page for p in parts if p.page is not None]
    return (min(ps), max(ps)) if ps else (None, None)


def tree_from_blocks(doc: ParsedDocument) -> tuple[TocNode, str | None]:
    """Stack-based tree from heading levels. Returns (root, title_candidate)."""
    root = TocNode(title=doc.title or "", level=0, kind="root")
    stack: list[TocNode] = [root]
    title_candidate: str | None = None
    for i, b in enumerate(doc.blocks):
        if b.kind == "heading":
            lvl = max(1, b.level)
            while len(stack) > 1 and stack[-1].level >= lvl:
                stack.pop()
            node = TocNode(title=b.text.strip(), level=lvl, page_start=b.page, page_end=b.page)
            stack[-1].children.append(node)
            stack.append(node)
        else:
            stack[-1].parts.append(Part(b.text, b.page))
    # A leading heading with no text before the next heading of the same/higher level is the document title
    # (e.g. "# Handbook" followed by "# Chapter 1") — drop it as a section and offer it as the title.
    if root.children and not root.parts:
        first = root.children[0]
        if not first.parts and not first.children and len(root.children) > 1:
            title_candidate = first.title
            root.children.pop(0)
    return root, title_candidate


def _single_wrapper(root: TocNode) -> TocNode | None:
    """The only top-level node when it is a non-leaf and the root has no meaningful own text (e.g. an H1 title)."""
    if len(root.children) != 1:
        return None
    only = root.children[0]
    if only.is_leaf or count_tokens(root.content) >= MIN_FRAGMENT_TOKENS:
        return None
    return only


def _collapse(root: TocNode, wrapper: TocNode) -> None:
    root.parts = root.parts + wrapper.parts
    root.children = wrapper.children


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _same_title(a: str, b: str) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


def _first_leaf(node: TocNode) -> TocNode:
    while node.children:
        node = node.children[0]
    return node


def _pack_paragraphs(parts: list[Part], target: int, hard_max: int) -> list[list[Part]]:
    """Greedy, balanced packing of paragraphs into groups ≈ target tokens (≤ hard_max where possible).

    Paragraphs larger than `hard_max` are broken into sentences / rows / lines; pieces of the same paragraph that
    land in the same group are re-joined with their original separators, so no text is altered or lost.
    """
    units = []  # Unit objects (text, tokens, sep, page)
    for p in parts:
        for para in split_paragraphs(p.text):
            units.extend(text_units(para, max(50, hard_max // 2), p.page, first_sep="\n\n"))
    total = sum(u.tokens for u in units)
    if total == 0:
        return []
    n_groups = max(1, math.ceil(total / target))
    goal = total / n_groups
    raw_groups: list[list] = []
    cur: list = []
    cur_tok = 0
    for u in units:
        if cur and (cur_tok + u.tokens > hard_max or (cur_tok >= goal * 0.9 and cur_tok + u.tokens > goal * 1.15)):
            raw_groups.append(cur)
            cur, cur_tok = [], 0
        cur.append(u)
        cur_tok += u.tokens
    if cur:
        if (raw_groups and cur_tok < MIN_FRAGMENT_TOKENS * 2
                and sum(x.tokens for x in raw_groups[-1]) + cur_tok <= hard_max * 1.1):
            raw_groups[-1].extend(cur)  # don't leave a tiny tail fragment
        else:
            raw_groups.append(cur)
    groups: list[list[Part]] = []
    for g in raw_groups:
        out: list[Part] = []
        if g and g[0].kind == "row" and g[0].header:  # group starts mid-table: repeat the table header
            out.append(Part(g[0].header, g[0].page))
        for u in g:
            if not out or u.sep == "\n\n":
                out.append(Part(u.text, u.page))
            else:
                out[-1] = Part(out[-1].text + u.sep + u.text, out[-1].page if out[-1].page is not None else u.page)
        groups.append(out)
    return groups


def _split_leaf(leaf: TocNode) -> None:
    groups = _pack_paragraphs(leaf.parts, PART_TARGET_TOKENS, MAX_LEAF_TOKENS)
    if len(groups) <= 1:
        return
    kids = []
    for i, g in enumerate(groups, start=1):
        ps, pe = _pages(g)
        kids.append(TocNode(title=f"{leaf.title} (part {i})", kind="part", parts=g, page_start=ps, page_end=pe))
    leaf.parts = []
    leaf.children = kids


def _normalize(node: TocNode) -> None:
    for c in node.children:
        _normalize(c)
    # drop empty leaves (headings with no text and no subsections)
    node.children = [c for c in node.children if c.children or c.content.strip()]
    # intro text of a non-leaf → Overview leaf. Tiny intros of inner sections join the next leaf in reading order,
    # but a document-level preamble always gets its own leaf: it carries scope (who/what it applies to, version,
    # effective date) and must not be attributed to whichever section happens to come first.
    if node.children and node.content.strip():
        intro = list(node.parts)
        node.parts = []
        tiny = count_tokens("\n\n".join(p.text for p in intro)) < MIN_FRAGMENT_TOKENS
        if tiny and node.kind != "root":
            target = _first_leaf(node.children[0])
            target.parts = intro + target.parts
        else:
            ps, pe = _pages(intro)
            node.children.insert(0, TocNode(title=OVERVIEW_TITLE, kind="overview", parts=intro,
                                            page_start=ps, page_end=pe))
    # oversized leaves → "(part n)" children
    for c in node.children:
        if c.is_leaf and count_tokens(c.content) > MAX_LEAF_TOKENS:
            _split_leaf(c)


_NUM_PREFIX_RE = re.compile(r"^\s*((?:\d+\.)*\d+)\.?\s*[:.)\-–—]?\s+(\S.*)$")


def clean_title(title: str, path: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    m = _NUM_PREFIX_RE.match(t)
    # Drop the source's own numbering: always for dotted outline numbers ("4.1.1 K-Means" — the computed path
    # replaces them, even when the source skips numbers), and for a bare integer only when it equals the path, so
    # titles like "2026 Roadmap" keep their number.
    if m and (m.group(1).rstrip(".") == path or "." in m.group(1)):
        t = m.group(2).strip()
    return t[:300] or "Untitled section"


def finalize_tree(root: TocNode, doc_title: str) -> None:
    """Assign ids/paths/depths/breadcrumbs/ordinals/token counts/page ranges (pre-order)."""
    root.title = doc_title
    counter = [0]

    def assign(node: TocNode, prefix: str, depth: int, crumbs: list[str]) -> None:
        idx = 1
        for child in node.children:
            num = "0" if child.kind == "overview" else str(idx)
            if child.kind != "overview":
                idx += 1
            child.path = num if not prefix else f"{prefix}.{num}"
            child.depth = depth
            child.title = clean_title(child.title, child.path)
            child.breadcrumb = BREADCRUMB_SEP.join(crumbs + [child.title])
            child.parent_id = None if node is root else node.id
            counter[0] += 1
            child.ordinal = counter[0]
            assign(child, child.path, depth + 1, crumbs + [child.title])

    assign(root, "", 1, [doc_title] if doc_title else [])

    def totals(node: TocNode) -> tuple[int, int | None, int | None]:
        if node.is_leaf and node is not root:
            node.token_count = count_tokens(node.content)
            node.page_start, node.page_end = _pages(node.parts) if node.parts else (node.page_start, node.page_end)
            return node.token_count, node.page_start, node.page_end
        tok, ps, pe = 0, None, None
        for c in node.children:
            t, a, b = totals(c)
            tok += t
            if a is not None:
                ps = a if ps is None else min(ps, a)
            if b is not None:
                pe = b if pe is None else max(pe, b)
        node.token_count = tok
        node.page_start, node.page_end = ps, pe
        return tok, ps, pe

    totals(root)


# --------------------------------------------------------------------------- flat docs & augmentation
def _heuristic_title(text: str, idx: int) -> str:
    first = split_sentences(text[:600])
    s = first[0] if first else text[:120]
    s = re.sub(r"[#*_`>|]+", " ", s)
    words = s.split()
    snippet = " ".join(words[:9]) + ("…" if len(words) > 9 else "")
    return f"Part {idx}: {snippet}" if snippet else f"Part {idx}"


_TITLES_SCHEMA = {
    "type": "object",
    "properties": {
        "headings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "title": {"type": "string"}},
                "required": ["id", "title"],
            },
        }
    },
    "required": ["headings"],
}
_TITLES_SYSTEM = (
    "You write table-of-contents headings for document excerpts. For each excerpt, write a specific heading of "
    "three to eight words naming what that excerpt is about, the way a section title in a handbook, policy or "
    "report would. Use the document's own terminology. No numbering, no quotes, no trailing punctuation."
)
_CHAPTERS_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "first": {"type": "integer"}, "last": {"type": "integer"}},
                "required": ["title", "first", "last"],
            },
        }
    },
    "required": ["chapters"],
}
_CHAPTERS_SYSTEM = (
    "You organize a document's consecutive segments into chapters for its table of contents. Group neighbouring "
    "segments that share a topic. Chapters must cover every segment exactly once, in order, without gaps or "
    "overlaps; give each chapter a short descriptive title (two to six words)."
)


def _sanitize_title(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'“”‘’").strip()
    t = re.sub(r"^(?:\d+[.)]\s*|#+\s*|[-*•]\s*)", "", t).rstrip(".:;,")
    return t[:100]


async def _title_segments(llm, doc_title: str, segments: list[str], meter: UsageMeter | None) -> list[str | None]:
    titles: list[str | None] = [None] * len(segments)
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def run(start: int) -> None:
        batch = segments[start: start + TITLE_BATCH]
        excerpts = "\n\n".join(
            f"<excerpt id={start + i + 1}>\n{truncate_tokens(seg, 170)}\n</excerpt>" for i, seg in enumerate(batch))
        messages = [
            {"role": "system", "content": _TITLES_SYSTEM},
            {"role": "user", "content": f"DOCUMENT: {doc_title}\n\n{excerpts}\n\nReturn one heading per excerpt id."},
        ]
        async with sem:
            out = await safe_json(llm, messages, _TITLES_SCHEMA, meter=meter, max_tokens=60 * len(batch) + 100,
                                  name="segment_headings")
        items = out.get("headings") if isinstance(out, dict) else None
        if not isinstance(items, list):
            return
        for pos, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("id")) - 1
            except (TypeError, ValueError):
                idx = start + pos
            if start <= idx < start + len(batch):
                t = _sanitize_title(it.get("title", ""))
                if t:
                    titles[idx] = t

    await asyncio.gather(*(run(s) for s in range(0, len(segments), TITLE_BATCH)))
    return titles


def _validate_chapters(raw, n: int) -> list[tuple[str, int, int]] | None:
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    out = []
    for c in raw:
        if not isinstance(c, dict):
            return None
        try:
            a, b = int(c.get("first")), int(c.get("last"))
        except (TypeError, ValueError):
            return None
        t = _sanitize_title(c.get("title", ""))
        if not t or a > b:
            return None
        out.append((t, a, b))
    out.sort(key=lambda x: x[1])
    if out[0][1] != 1 or out[-1][2] != n:
        return None
    for prev, cur in zip(out, out[1:]):
        if cur[1] != prev[2] + 1:
            return None
    if len(out) >= n:  # one chapter per segment adds nothing
        return None
    return out


async def _group_segments(llm, doc_title: str, titles: list[str], meter: UsageMeter | None):
    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    max_ch = max(2, min(12, len(titles) // 2))
    messages = [
        {"role": "system", "content": _CHAPTERS_SYSTEM},
        {"role": "user", "content": (f"DOCUMENT: {doc_title}\n\nSEGMENTS:\n{listing}\n\n"
                                     f"Group them into between 2 and {max_ch} chapters (first/last are segment ids).")},
    ]
    out = await safe_json(llm, messages, _CHAPTERS_SCHEMA, meter=meter, max_tokens=800, name="chapters")
    return _validate_chapters(out.get("chapters") if isinstance(out, dict) else None, len(titles))


async def _segment_flat(root: TocNode, doc_title: str, llm, augment: bool, meter: UsageMeter | None) -> str:
    """Turn a heading-less document into titled segments (optionally grouped into chapters)."""
    groups = _pack_paragraphs(root.parts, SEGMENT_TARGET_TOKENS, SEGMENT_MAX_TOKENS)
    root.parts = []
    if not groups:
        return "flat"
    texts = ["\n\n".join(p.text for p in g) for g in groups]
    if len(groups) == 1:
        ps, pe = _pages(groups[0])
        root.children = [TocNode(title=doc_title or _heuristic_title(texts[0], 1), kind="segment", parts=groups[0],
                                 page_start=ps, page_end=pe)]
        return "flat"
    llm_titles: list[str | None] = [None] * len(groups)
    if augment and llm is not None:
        llm_titles = await _title_segments(llm, doc_title, texts, meter)
    n_llm = sum(1 for t in llm_titles if t)
    nodes = []
    for i, (g, t) in enumerate(zip(groups, llm_titles), start=1):
        ps, pe = _pages(g)
        nodes.append(TocNode(title=t or _heuristic_title(texts[i - 1], i), kind="segment", parts=g,
                             page_start=ps, page_end=pe))
    augmented = augment and llm is not None and n_llm >= max(1, math.ceil(len(groups) / 2))
    chapters = None
    if augmented and len(nodes) >= 8:
        chapters = await _group_segments(llm, doc_title, [n.title for n in nodes], meter)
    if chapters:
        root.children = [TocNode(title=t, kind="chapter", children=nodes[a - 1:b]) for t, a, b in chapters]
    else:
        root.children = nodes
    return "augmented" if augmented else "flat"


# --------------------------------------------------------------------------- summaries
_SUMMARIES_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "summary": {"type": "string"}},
                "required": ["id", "summary"],
            },
        }
    },
    "required": ["summaries"],
}
_SUMMARIES_SYSTEM = (
    "You write one-line summaries for the leaf sections of a document's table of contents. For each section, "
    "write a single factual sentence (at most twenty-five words) stating what it covers: key facts, numbers, rules "
    "or definitions a reader would look it up for. No preamble."
)


async def summarize_leaves(llm, doc_title: str, leaves: list[TocNode], meter: UsageMeter | None = None) -> int:
    """Batched one-line leaf summaries (enriches the ToC rendered for STAIR). Returns how many were written."""
    if llm is None or not leaves:
        return 0
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    done = [0]

    async def run(start: int) -> None:
        batch = leaves[start: start + SUMMARY_BATCH]
        body = "\n\n".join(
            f"<leaf id={start + i + 1} path=\"{n.path}\" title=\"{n.title}\">\n{truncate_tokens(n.content, 260)}\n</leaf>"
            for i, n in enumerate(batch))
        messages = [{"role": "system", "content": _SUMMARIES_SYSTEM},
                    {"role": "user", "content": f"DOCUMENT: {doc_title}\n\n{body}\n\nReturn one summary per leaf id."}]
        async with sem:
            out = await safe_json(llm, messages, _SUMMARIES_SCHEMA, meter=meter, max_tokens=70 * len(batch) + 100,
                                  name="leaf_summaries")
        items = out.get("summaries") if isinstance(out, dict) else None
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("id")) - 1
            except (TypeError, ValueError):
                continue
            s = re.sub(r"\s+", " ", str(it.get("summary") or "")).strip()
            if start <= idx < start + len(batch) and s:
                leaves[idx].summary = s[:300]
                done[0] += 1

    await asyncio.gather(*(run(s) for s in range(0, len(leaves), SUMMARY_BATCH)))
    return done[0]


_DOC_SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def outline_text(root: TocNode, max_depth: int = 2, max_lines: int = 60) -> str:
    lines = []
    for n in root.walk():
        if n is root or n.depth > max_depth:
            continue
        lines.append("  " * (n.depth - 1) + f"{n.path} {n.title}")
        if len(lines) >= max_lines:
            lines.append("…")
            break
    return "\n".join(lines)


def heuristic_summary(root: TocNode) -> str:
    leaves = root.leaves()
    first = next((lf.content for lf in leaves if count_tokens(lf.content) >= 20), leaves[0].content if leaves else "")
    return truncate_tokens(re.sub(r"\s+", " ", first), 80)


async def document_summary(llm, doc_title: str, root: TocNode, meter: UsageMeter | None = None) -> str:
    """2–3 sentence document summary (used for document routing); heuristic fallback without an LLM."""
    if llm is not None:
        leaves = root.leaves()
        opening = truncate_tokens("\n\n".join(lf.content for lf in leaves[:3]), 700)
        messages = [
            {"role": "system", "content": ("Summarize documents for a retrieval index. In two or three sentences, "
                                           "say what the document is, who it is for and the main topics it covers.")},
            {"role": "user", "content": (f"DOCUMENT: {doc_title}\n\nCONTENTS:\n{outline_text(root)}\n\n"
                                         f"OPENING TEXT:\n{opening}")},
        ]
        out = await safe_json(llm, messages, _DOC_SUMMARY_SCHEMA, meter=meter, max_tokens=250, name="document_summary")
        s = out.get("summary") if isinstance(out, dict) else None
        if isinstance(s, str) and len(s.strip()) >= 20:
            return re.sub(r"\s+", " ", s).strip()[:1200]
    return heuristic_summary(root)


# --------------------------------------------------------------------------- entry point
async def build_structure(doc: ParsedDocument, *, title: str | None = None, llm=None, augment: bool = False,
                          summaries: bool = False, meter: UsageMeter | None = None) -> StructureResult:
    """Build the ToC tree. `title` overrides the parsed title (e.g. a user-provided document name)."""
    root, title_candidate = tree_from_blocks(doc)
    source = doc.structure_hint
    parsed_title = "" if doc.meta.get("title_is_default") else (doc.title or "")
    fallback_title = doc.title if doc.meta.get("title_is_default") else ""
    explicit = (title or parsed_title or "").strip()
    wrapper = _single_wrapper(root)
    wrapper_title = None
    if wrapper is not None and (not explicit or _same_title(explicit, wrapper.title)):
        _collapse(root, wrapper)  # the lone top-level heading is the document title, not a section
        wrapper_title = wrapper.title
    doc_title = (explicit or wrapper_title or title_candidate or fallback_title or "Untitled document").strip()
    if not root.children:
        source = await _segment_flat(root, doc_title, llm, augment, meter)
    _normalize(root)
    if not root.children and root.content.strip():  # safety net: never lose text
        root.children = [TocNode(title=doc_title, kind="segment", parts=root.parts)]
        root.parts = []
    finalize_tree(root, doc_title)
    if summaries and llm is not None:
        await summarize_leaves(llm, doc_title, root.leaves(), meter)
    return StructureResult(root=root, title=doc_title, structure_source=source)
