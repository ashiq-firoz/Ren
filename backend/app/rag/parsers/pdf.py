"""PDF parser (pypdf + pdfplumber; no AGPL dependencies).

1. **Native ToC** — the PDF outline (bookmarks) is the most reliable structure. Each outline entry is located on
   its destination page (fuzzy, line-anchored title match) and the text between consecutive entries becomes that
   section's content, keeping page numbers.
2. Otherwise **docling** (optional, only if installed) for layout-aware conversion.
3. Otherwise **heading inference** from font size relative to the body text, boldness and numbering ("4.1.2 …");
   running headers/footers and page numbers are removed. Lines come from pdfplumber (precise layout) for files up
   to `PLUMBER_MAX_PAGES` pages, and from a fast pypdf text-visitor pass (font name/size per text run) for larger
   files. If no font signal is usable, plain-text heading heuristics (numbering, ALL-CAPS …) are applied.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.rag.parsers import Block, ParseError, ParsedDocument, TocEntry, numbering_depth
from app.rag.parsers.plain import BULLET_RE, detect_headings, unwrap_paragraph
from app.rag.textutil import clean_inline

log = logging.getLogger("ren.rag.parsers")

PLUMBER_MAX_PAGES = int(os.environ.get("RAG_PDF_LAYOUT_MAX_PAGES", "120"))
DOCLING_MAX_PAGES = 300
_PAGE_NUM_RE = re.compile(r"^(?:page\s*)?[-–—(\[]?\s*\d{1,4}\s*[-–—)\]]?(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_SENT_END = (".", "!", "?", ":", "”", '"', "»")


# --------------------------------------------------------------------------- entry point
def parse_pdf(path: Path) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"Could not open the PDF ({type(e).__name__}); the file may be corrupted.") from e
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            raise ParseError("The PDF is password-protected. Remove the password and upload it again.")
    try:
        n_pages = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"Could not read the PDF page tree ({type(e).__name__}).") from e
    if n_pages == 0:
        raise ParseError("The PDF has no pages.")
    title = _meta_title(reader)

    outline = _flatten_outline(reader)
    if len(outline) >= 2:
        texts = _strip_running_lines(_page_texts(reader))
        blocks = _blocks_from_outline(outline, texts)
        if any(b.kind != "heading" for b in blocks):
            return ParsedDocument(title=title, blocks=blocks, page_count=n_pages, native_toc=outline, format="pdf",
                                  meta={"parser": "pypdf+outline", "outline_entries": len(outline)})

    doc = _try_docling(path, n_pages)
    if doc is not None:
        doc.page_count = n_pages
        doc.title = doc.title or title
        return doc

    blocks: list[Block] = []
    parser = ""
    if n_pages <= PLUMBER_MAX_PAGES:
        try:
            lines, n = _layout_lines(path)
            blocks, parser = _blocks_from_lines(lines, n), "pdfplumber"
        except Exception:  # noqa: BLE001
            log.warning("pdfplumber layout pass failed; trying the pypdf layout pass", exc_info=True)
            blocks = []
    if not blocks:
        try:
            lines, n = _layout_lines_pypdf(reader)
            blocks, parser = _blocks_from_lines(lines, n), "pypdf-layout"
        except Exception:  # noqa: BLE001
            log.warning("pypdf layout pass failed; using plain text", exc_info=True)
            blocks = []
    if not any(b.kind == "heading" for b in blocks):
        text_blocks = _blocks_from_text(_strip_running_lines(_page_texts(reader)))
        if any(b.kind == "heading" for b in text_blocks) or not blocks:
            blocks, parser = text_blocks, "pypdf"
    return ParsedDocument(title=title, blocks=blocks, page_count=n_pages, format="pdf", meta={"parser": parser})


# --------------------------------------------------------------------------- metadata / outline
def _meta_title(reader) -> str:
    try:
        t = (reader.metadata.title if reader.metadata else None) or ""
    except Exception:  # noqa: BLE001
        return ""
    t = clean_inline(str(t))
    t = re.sub(r"^microsoft (?:word|powerpoint|excel)\s*-\s*", "", t, flags=re.I)
    t = re.sub(r"\.(?:docx?|pptx?|xlsx?|pdf)$", "", t, flags=re.I)
    if t.lower() in ("untitled", "untitled document", "document", "title", "slide 1") or len(t) < 3:
        return ""
    return t


def _flatten_outline(reader) -> list[TocEntry]:
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001
        return []
    out: list[TocEntry] = []

    def walk(items, level: int) -> None:
        for it in items:
            if len(out) > 5000:
                return
            if isinstance(it, list):
                walk(it, level + 1)
                continue
            try:
                title = clean_inline(str(getattr(it, "title", "") or ""))
            except Exception:  # noqa: BLE001
                continue
            if not title:
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:  # noqa: BLE001
                page = None
            out.append(TocEntry(level=min(level, 9), title=title[:300],
                                page=(page + 1) if isinstance(page, int) and page >= 0 else None))

    try:
        walk(outline or [], 1)
    except Exception:  # noqa: BLE001
        log.warning("could not read the PDF outline", exc_info=True)
    return [e for e in out if e.page is not None]


# --------------------------------------------------------------------------- text helpers
def _clean_page_text(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(lines)


def _page_texts(reader) -> list[str]:
    texts = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        texts.append(_clean_page_text(t))
    return texts


def _norm_running(line: str) -> str:
    return re.sub(r"\d+", "#", line.lower()).strip()


def _strip_running_lines(texts: list[str]) -> list[str]:
    """Remove running headers/footers (repeated first/last lines) and bare page numbers."""
    n = len(texts)
    pages = [[ln for ln in t.split("\n")] for t in texts]
    counts: Counter = Counter()
    for lines in pages:
        nonblank = [ln for ln in lines if ln.strip()]
        edge = set(_norm_running(x) for x in nonblank[:2] + nonblank[-2:])
        counts.update(edge)
    threshold = max(3, int(0.4 * n))
    running = {k for k, c in counts.items() if c >= threshold and len(k) <= 120} if n >= 3 else set()
    out = []
    for lines in pages:
        idx = [i for i, ln in enumerate(lines) if ln.strip()]
        edges = set(idx[:2] + idx[-2:])
        keep = []
        for i, ln in enumerate(lines):
            if i in edges and (_norm_running(ln) in running or _PAGE_NUM_RE.match(ln.strip() or "x")):
                continue
            keep.append(ln)
        out.append("\n".join(keep))
    return out


def _paragraphs(lines: list[str]) -> list[str]:
    """Group PDF text lines into paragraphs: blank lines, bullets and short sentence-final lines end paragraphs."""
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    if not nonblank:
        return []
    lengths = sorted(len(x) for x in nonblank)
    typical = lengths[int(len(lengths) * 0.75)] if lengths else 80
    paras: list[str] = []
    cur: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            if cur:
                paras.append(unwrap_paragraph(cur))
                cur = []
            continue
        if cur and BULLET_RE.match(s):
            paras.append(unwrap_paragraph(cur))
            cur = []
        cur.append(s)
        if s.endswith(_SENT_END) and len(s) < 0.7 * typical:
            paras.append(unwrap_paragraph(cur))
            cur = []
    if cur:
        paras.append(unwrap_paragraph(cur))
    return [p for p in paras if p.strip()]


def _emit_span(blocks: list[Block], texts: list[str], offsets: list[int], a: int, b: int) -> None:
    """Emit paragraphs for full-text span [a, b) with page numbers (span may cross pages)."""
    if b <= a:
        return
    for p in range(len(texts)):
        ps, pe = offsets[p], offsets[p] + len(texts[p])
        lo, hi = max(a, ps), min(b, pe)
        if lo >= hi:
            continue
        seg = texts[p][lo - ps: hi - ps]
        for para in _paragraphs(seg.split("\n")):
            if (blocks and blocks[-1].kind == "text" and blocks[-1].page == p and para[:1].islower()
                    and not blocks[-1].text.endswith(_SENT_END)):
                blocks[-1] = Block("text", blocks[-1].text + " " + para, 0, blocks[-1].page)  # page-wrapped sentence
            else:
                blocks.append(Block("text", para, 0, p + 1))


def _find_title(lower: str, title: str, a: int, b: int) -> tuple[int, int] | None:
    window = lower[a:b]
    if not window:
        return None
    t = clean_inline(title).lower()
    cands = [t]
    stripped = re.sub(r"^\s*(?:(?:\d+\.)*\d+|[ivxlcdm]+|[a-z])[.)]?\s+", "", t)
    if stripped and stripped != t and len(stripped) >= 3:
        cands.append(stripped)
    for c in cands:
        words = re.findall(r"\w+", c)[:10]
        if not words:
            continue
        pat = r"[\W_]*".join(re.escape(w) for w in words)
        m = re.search(r"(?m)^[\W_]*(?:\S+\s+)?" + pat, window)  # line-anchored (allows a leading number)
        if m is None:
            m = re.search(pat, window)
        if m:
            return a + m.start(), a + m.end()
    return None


def _blocks_from_outline(outline: list[TocEntry], texts: list[str]) -> list[Block]:
    offsets: list[int] = []
    pos = 0
    for t in texts:
        offsets.append(pos)
        pos += len(t) + 1
    full = "\n".join(texts)
    lower = full.lower()
    n = len(texts)
    spans: list[tuple[TocEntry, int, int]] = []
    last = 0
    for e in outline:
        if e.page is None or not (1 <= e.page <= n):
            continue
        start_search = max(offsets[e.page - 1], last)
        end_search = offsets[e.page + 1] if e.page + 1 < n else len(full)
        found = _find_title(lower, e.title, start_search, end_search) if start_search < end_search else None
        if found is None:
            s = start_search
            span = (e, s, s)
        else:
            span = (e, found[0], found[1])
        spans.append(span)
        last = span[2]
    blocks: list[Block] = []
    first_start = spans[0][1] if spans else len(full)
    _emit_span(blocks, texts, offsets, 0, first_start)
    for i, (e, s, title_end) in enumerate(spans):
        blocks.append(Block("heading", e.title, e.level, e.page))
        nxt = spans[i + 1][1] if i + 1 < len(spans) else len(full)
        _emit_span(blocks, texts, offsets, title_end, max(title_end, nxt))
    return blocks


def _blocks_from_text(texts: list[str]) -> list[Block]:
    """Fast path: pypdf text + plain-text heading heuristics, with page numbers."""
    lines: list[str] = []
    pages: list[int] = []
    for p, t in enumerate(texts, start=1):
        for ln in t.split("\n"):
            lines.append(ln)
            pages.append(p)
        lines.append("")
        pages.append(p)
    heads = detect_headings(lines)
    blocks: list[Block] = []
    buf: list[str] = []
    buf_page = 1

    def flush() -> None:
        nonlocal buf
        for para in _paragraphs(buf):
            blocks.append(Block("text", para, 0, buf_page))
        buf = []

    for i, ln in enumerate(lines):
        if i in heads:
            flush()
            level, htitle = heads[i]
            blocks.append(Block("heading", htitle, level, pages[i]))
            continue
        if not buf:
            buf_page = pages[i]
        if pages[i] != buf_page and not ln.strip():
            flush()
            continue
        buf.append(ln)
    flush()
    return blocks


# --------------------------------------------------------------------------- layout heuristics (pdfplumber)
@dataclass
class _Line:
    text: str
    size: float
    bold: bool
    page: int
    top: float
    bottom: float
    rel_top: float


def _is_bold(fontname: str) -> bool:
    f = (fontname or "").lower()
    return any(k in f for k in ("bold", "black", "heavy", "semibold", "demi")) or f.endswith((",b", "-b", ".b"))


def _layout_lines(path: Path) -> tuple[list[_Line], int]:
    import pdfplumber

    out: list[_Line] = []
    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, start=1):
            try:
                raw = page.extract_text_lines(return_chars=True, strip=True)
            except Exception:  # noqa: BLE001
                raw = []
            height = float(page.height or 792) or 792.0
            for ln in raw:
                chars = [c for c in ln.get("chars", []) if str(c.get("text", "")).strip()]
                text = clean_inline(str(ln.get("text", "")))
                if not chars or not text:
                    continue
                sizes = sorted(float(c.get("size") or 0.0) for c in chars)
                size = round(sizes[len(sizes) // 2], 1)
                bold = sum(1 for c in chars if _is_bold(str(c.get("fontname", "")))) >= 0.6 * len(chars)
                top = float(ln.get("top", 0.0))
                out.append(_Line(text, size, bold, pno, top, float(ln.get("bottom", top + size)), top / height))
            close = getattr(page, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return out, n


def _layout_lines_pypdf(reader) -> tuple[list[_Line], int]:
    """Fast layout pass: pypdf's text visitor reports font name + size + text matrix for every text run."""
    out: list[_Line] = []
    n = len(reader.pages)
    for pno, page in enumerate(reader.pages, start=1):
        try:
            height = float(page.mediabox.height) or 792.0
        except Exception:  # noqa: BLE001
            height = 792.0
        buf: list[str] = []
        sizes: list[float] = []
        bolds: list[bool] = []
        state: dict = {"y": None, "last_top": None}

        def flush() -> None:
            text = clean_inline("".join(buf))
            if text and sizes:
                ss = sorted(sizes)
                size = round(ss[len(ss) // 2], 1)
                bold = sum(bolds) >= 0.6 * len(bolds)
                if state["y"] is not None and 0 < state["y"] <= height * 1.05:
                    top = max(0.0, height - state["y"] - size)
                else:  # position unknown → continue below the previous line
                    top = (state["last_top"] or 0.0) + size * 1.2
                out.append(_Line(text, size, bold, pno, top, top + size, min(1.0, top / height)))
                state["last_top"] = top
            buf.clear()
            sizes.clear()
            bolds.clear()
            state["y"] = None

        def visitor(text, cm, tm, font_dict, font_size) -> None:
            if not text:
                return
            try:
                scale = abs(float(tm[3]) or float(tm[0]) or 1.0) * abs(float(cm[3]) or float(cm[0]) or 1.0)
                eff = float(font_size or 0.0) * scale
                y = float(tm[5]) * (float(cm[3]) or 1.0) + float(cm[5])
            except (TypeError, ValueError, IndexError):
                eff, y = 0.0, None
            fname = str(font_dict.get("/BaseFont", "")) if isinstance(font_dict, dict) else ""
            for i, piece in enumerate(text.split("\n")):
                if i > 0:
                    flush()
                if not piece:
                    continue
                buf.append(piece)
                visible = len(piece.strip())
                if visible:
                    sizes.extend([eff] * visible)
                    bolds.extend([_is_bold(fname)] * visible)
                    if state["y"] is None and y:
                        state["y"] = y

        try:
            page.extract_text(visitor_text=visitor)
        except Exception:  # noqa: BLE001
            pass
        flush()
    return out, n


def _blocks_from_lines(lines: list[_Line], n_pages: int) -> list[Block]:
    """Heading inference (font size / bold / numbering) + paragraph grouping over layout lines."""
    if not lines:
        return []
    if not any(ln.size > 0 for ln in lines):
        return []
    # 1) running headers/footers + page numbers in the top/bottom bands
    edge_pages: dict[str, set[int]] = defaultdict(set)
    for ln in lines:
        if ln.rel_top < 0.09 or ln.rel_top > 0.91:
            edge_pages[_norm_running(ln.text)].add(ln.page)
    threshold = max(3, int(0.35 * n_pages))
    running = {k for k, pages in edge_pages.items() if len(pages) >= threshold} if n_pages >= 3 else set()
    lines = [ln for ln in lines
             if not ((ln.rel_top < 0.09 or ln.rel_top > 0.91)
                     and (_norm_running(ln.text) in running or _PAGE_NUM_RE.match(ln.text)))]
    if not lines:
        return []
    # 2) body font size = the size carrying most characters
    weights: Counter = Counter()
    for ln in lines:
        weights[round(ln.size * 2) / 2] += len(ln.text)
    body = weights.most_common(1)[0][0] or 10.0

    def candidate(ln: _Line, allow_bold: bool) -> bool:
        t = ln.text
        words = t.split()
        if len(t) > 150 or not words or len(words) > 18 or not re.search(r"[A-Za-z]", t):
            return False
        if t.endswith((".", ",", ";")) and not numbering_depth(t):
            return False
        if re.fullmatch(r"[\d\W]+", t):
            return False
        big = ln.size >= body * 1.12
        if big:
            return True
        if allow_bold and ln.bold and ln.size >= body * 0.97 and len(t) <= 90:
            return True
        return False

    heads_idx = [i for i, ln in enumerate(lines) if candidate(ln, True)]
    if len(heads_idx) > 0.3 * len(lines):  # bold everywhere → only size-based headings
        heads_idx = [i for i, ln in enumerate(lines) if candidate(ln, False)]
    if len(heads_idx) > 0.3 * len(lines) or len(heads_idx) < 2:
        heads_idx = []
    head_set = set(heads_idx)

    # 3) merge multi-line headings (same page, same style, adjacent)
    merged_heads: dict[int, str] = {}
    skip: set[int] = set()
    for i in heads_idx:
        if i in skip:
            continue
        text = lines[i].text
        j = i
        while (j + 1 in head_set and lines[j + 1].page == lines[i].page
               and abs(lines[j + 1].size - lines[i].size) < 0.6 and lines[j + 1].bold == lines[i].bold
               and lines[j + 1].top - lines[j].bottom < 1.2 * max(1.0, lines[j].bottom - lines[j].top)
               and not numbering_depth(lines[j + 1].text) and len(text) < 140):
            j += 1
            text += " " + lines[j].text
            skip.add(j)
        merged_heads[i] = text

    # 4) levels: font-size rank, numbering depth wins when present
    sizes = sorted({round(lines[i].size * 2) / 2 for i in merged_heads if lines[i].size >= body * 1.12},
                   reverse=True)
    rank = {s: k + 1 for k, s in enumerate(sizes[:4])}
    blocks: list[Block] = []
    para: list[str] = []
    para_page = lines[0].page
    prev: _Line | None = None

    def flush() -> None:
        nonlocal para
        if para:
            text = unwrap_paragraph(para)
            if text.strip():
                blocks.append(Block("text", text, 0, para_page))
        para = []

    for i, ln in enumerate(lines):
        if i in skip:
            continue
        if i in merged_heads:
            flush()
            s = round(ln.size * 2) / 2
            level = rank.get(s, len(rank) + 1)
            depth = numbering_depth(merged_heads[i])
            if depth:
                level = depth
            blocks.append(Block("heading", merged_heads[i], min(level, 6), ln.page))
            prev = ln
            continue
        new_para = prev is None or not para
        if prev is not None and para:
            if ln.page != prev.page:
                new_para = para[-1].rstrip().endswith(_SENT_END)
            else:
                gap = ln.top - prev.bottom
                lh = max(1.0, prev.bottom - prev.top)
                new_para = gap > 0.75 * lh
            if BULLET_RE.match(ln.text):
                new_para = True
        if new_para:
            flush()
            para_page = ln.page
        para.append(ln.text)
        prev = ln
    flush()
    return blocks


# --------------------------------------------------------------------------- optional docling
def _try_docling(path: Path, n_pages: int) -> ParsedDocument | None:
    if os.environ.get("RAG_DOCLING", "1") == "0" or n_pages > DOCLING_MAX_PAGES:
        return None
    if importlib.util.find_spec("docling") is None:
        return None
    try:
        from docling.document_converter import DocumentConverter  # type: ignore

        from app.rag.parsers.md import parse_markdown

        result = DocumentConverter().convert(str(path))
        doc = parse_markdown(result.document.export_to_markdown())
        if not doc.blocks:
            return None
        doc.format = "pdf"
        doc.meta["parser"] = "docling"
        return doc
    except Exception:  # noqa: BLE001
        log.warning("docling conversion failed; using built-in PDF heuristics", exc_info=True)
        return None
