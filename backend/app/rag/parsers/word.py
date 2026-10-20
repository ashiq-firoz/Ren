"""DOCX parser (python-docx): Heading 1–9 / outline levels (+ automatic heading numbering), lists, tables → markdown.
Documents that don't use heading styles fall back to formatting heuristics (bold / large short paragraphs,
numbered titles)."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from app.rag.parsers import Block, ParsedDocument, numbering_depth, table_to_markdown
from app.rag.textutil import clean_inline

_HEADING_STYLE_RE = re.compile(r"^(?:heading|überschrift|titre|título|titolo|kop)\s*(\d)$", re.I)


def _outline_level(pPr) -> int | None:
    if pPr is None:
        return None
    from docx.oxml.ns import qn

    el = pPr.find(qn("w:outlineLvl"))
    if el is None:
        return None
    try:
        v = int(el.get(qn("w:val")))
    except (TypeError, ValueError):
        return None
    return v + 1 if 0 <= v <= 8 else None


def _has_numbering(pPr) -> bool:
    if pPr is None:
        return False
    from docx.oxml.ns import qn

    return pPr.find(qn("w:numPr")) is not None


def _style_chain(style):
    seen = 0
    while style is not None and seen < 10:
        yield style
        style = getattr(style, "base_style", None)
        seen += 1


def _heading_level(par) -> int | str | None:
    """Heading level for a paragraph: int, 'title', 'toc' or None."""
    style = par.style
    for st in _style_chain(style):
        name = (st.name or "").strip()
        low = name.lower()
        if low == "title":
            return "title"
        if low.startswith("toc") or low.startswith("table of contents"):
            return "toc"
        m = _HEADING_STYLE_RE.match(name)
        if m:
            return int(m.group(1))
        lvl = _outline_level(getattr(st.element, "pPr", None))
        if lvl:
            return lvl
    return _outline_level(par._p.pPr)


def _style_numbered(par) -> bool:
    if _has_numbering(par._p.pPr):
        return True
    for st in _style_chain(par.style):
        if _has_numbering(getattr(st.element, "pPr", None)):
            return True
    return False


def _para_size(par) -> float | None:
    sizes = [r.font.size.pt for r in par.runs if r.font is not None and r.font.size is not None and r.text.strip()]
    if sizes:
        return max(sizes)
    for st in _style_chain(par.style):
        f = getattr(st, "font", None)
        if f is not None and f.size is not None:
            return f.size.pt
    return None


def _all_bold(par) -> bool:
    runs = [r for r in par.runs if r.text.strip()]
    if not runs:
        return False
    for r in runs:
        b = r.bold
        if b is None:
            for st in _style_chain(par.style):
                f = getattr(st, "font", None)
                if f is not None and f.bold is not None:
                    b = f.bold
                    break
        if not b:
            return False
    return True


def _iter_block_items(doc):
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def walk(parent_el):
        for child in parent_el.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, doc)
            elif child.tag == qn("w:tbl"):
                yield Table(child, doc)
            elif child.tag == qn("w:sdt"):  # content controls (cover pages, ToC fields …)
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    yield from walk(content)

    yield from walk(doc.element.body)


def _table_rows(table) -> list[list[str]]:
    rows = []
    for row in table.rows:
        try:
            cells = [clean_inline(c.text) for c in row.cells]
        except Exception:  # noqa: BLE001  (malformed grid spans)
            continue
        rows.append(cells)
    return rows


def parse_docx(path: Path) -> ParsedDocument:
    import docx

    d = docx.Document(str(path))
    title = (d.core_properties.title or "").strip()
    items: list = list(_iter_block_items(d))  # Paragraph | Table in body order

    # ---- pass 1: does the document use heading styles?
    styled = 0
    for kind_obj in items:
        if kind_obj.__class__.__name__ == "Paragraph" and kind_obj.text.strip():
            lvl = _heading_level(kind_obj)
            if isinstance(lvl, int):
                styled += 1
    use_styles = styled >= 1

    # ---- heuristics for unstyled documents
    body_size = None
    if not use_styles:
        sizes = Counter()
        for p in items:
            if p.__class__.__name__ == "Paragraph" and p.text.strip():
                s = _para_size(p)
                if s:
                    sizes[round(s * 2) / 2] += len(p.text)
        body_size = sizes.most_common(1)[0][0] if sizes else 11.0

    def heuristic_level(p) -> int | None:
        text = p.text.strip()
        words = text.split()
        if not words or len(text) > 110 or len(words) > 14 or text.endswith((".", ",", ";")):
            return None
        size = _para_size(p) or body_size or 11.0
        depth = numbering_depth(text)
        if size >= (body_size or 11.0) * 1.35:
            return 1
        if size >= (body_size or 11.0) * 1.15:
            return 2 if depth is None else depth
        if _all_bold(p):
            return depth or 3
        return None

    blocks: list[Block] = []
    counters = [0] * 10
    first_title_used = False
    for obj in items:
        cls = obj.__class__.__name__
        if cls == "Table":
            md = table_to_markdown(_table_rows(obj))
            if md:
                blocks.append(Block("table", md))
            continue
        p = obj
        text = p.text.strip()
        if not text:
            continue
        lvl = _heading_level(p) if use_styles else heuristic_level(p)
        if lvl == "toc":
            continue  # entries of an auto-generated table of contents
        if lvl == "title":
            if not title:
                title = text
            if not first_title_used:
                first_title_used = True
                continue
            lvl = 1
        if isinstance(lvl, int):
            label = ""
            if use_styles and _style_numbered(p) and numbering_depth(text) is None:
                counters[lvl - 1] += 1
                for j in range(lvl, len(counters)):
                    counters[j] = 0
                label = ".".join(str(max(1, c)) for c in counters[:lvl]) + " "
            blocks.append(Block("heading", label + text, lvl))
            continue
        if _style_numbered(p) or (p.style is not None and "list" in (p.style.name or "").lower()):
            blocks.append(Block("text", f"- {text}"))
        else:
            blocks.append(Block("text", text))

    # Merge consecutive list items into one paragraph block so they chunk together.
    merged: list[Block] = []
    for b in blocks:
        if (b.kind == "text" and b.text.startswith("- ") and merged and merged[-1].kind == "text"
                and merged[-1].text.split("\n")[-1].startswith("- ")):
            merged[-1] = Block("text", merged[-1].text + "\n" + b.text)
        else:
            merged.append(b)
    return ParsedDocument(title=title, blocks=merged, format="docx")
