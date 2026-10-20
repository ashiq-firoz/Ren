"""Document parsers → a structured AST (`ParsedDocument`).

Every parser returns the document as an ordered list of blocks — headings (with a level) and content blocks
(paragraphs, lists, markdown tables, code) — each with an optional 1-based page number, plus the document title,
page count and the native ToC (PDF bookmarks) when one exists. `app.rag.structure` turns the blocks into the
Table-of-Contents tree used by STAIR.

Supported: PDF (outline → native ToC, else font/numbering heuristics; optional docling), DOCX, Markdown, HTML,
TXT, XLSX/CSV, PPTX. Parsers are synchronous (run them in a thread) and raise `ParseError` with a user-facing
message for bad or unsupported files.
"""
from __future__ import annotations

import datetime as _dt
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.textutil import clean_inline, clean_text


class ParseError(Exception):
    """Expected parsing failure; the message is shown to the user on the failed document."""


@dataclass
class Block:
    kind: str                 # heading | text | table | code
    text: str
    level: int = 0            # heading level (1 = top) — only for headings
    page: int | None = None   # 1-based page / slide number


@dataclass
class TocEntry:
    """An entry of an embedded outline (e.g. PDF bookmarks)."""

    level: int
    title: str
    page: int | None  # 1-based


@dataclass
class ParsedDocument:
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    page_count: int | None = None
    native_toc: list[TocEntry] | None = None
    format: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def headings(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "heading"]

    @property
    def structure_hint(self) -> str:
        if self.native_toc and self.headings:
            return "native"
        return "headings" if self.headings else "flat"

    @property
    def text_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks if b.kind != "heading")


# --------------------------------------------------------------------------- formats
EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx", ".docm": "docx",
    ".md": "markdown", ".markdown": "markdown", ".mdown": "markdown", ".mkd": "markdown",
    ".txt": "text", ".text": "text", ".rst": "text", ".log": "text",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".csv": "csv", ".tsv": "csv",
    ".pptx": "pptx",
}
LEGACY = {".doc": "Word 97-2003 (.doc)", ".xls": "Excel 97-2003 (.xls)", ".ppt": "PowerPoint 97-2003 (.ppt)",
          ".rtf": "Rich Text (.rtf)", ".odt": "OpenDocument text (.odt)", ".pages": "Apple Pages"}
MIME_FORMATS: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/markdown": "markdown", "text/x-markdown": "markdown",
    "text/plain": "text",
    "text/html": "html", "application/xhtml+xml": "html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv", "text/tab-separated-values": "csv",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
FORMAT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "markdown": "text/markdown", "text": "text/plain", "html": "text/html",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
SUPPORTED_EXTENSIONS = sorted(EXTENSIONS)


def detect_format(filename: str | None, mime: str | None = None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    if mime:
        base = mime.split(";")[0].strip().lower()
        if base in MIME_FORMATS:
            return MIME_FORMATS[base]
    return None


def guess_mime(filename: str, mime: str | None = None) -> str:
    fmt = detect_format(filename, mime)
    if fmt:
        return FORMAT_MIME[fmt]
    if mime and mime != "application/octet-stream":
        return mime
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def unsupported_message(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in LEGACY:
        return (f"{LEGACY[ext]} files are not supported. Save the file in a modern format "
                f"(PDF, DOCX, XLSX, PPTX, Markdown or plain text) and upload it again.")
    return (f"Unsupported file type '{ext or 'unknown'}'. Supported: PDF, DOCX, Markdown, TXT, HTML, XLSX, CSV, PPTX.")


# --------------------------------------------------------------------------- helpers shared by parsers
def decode_bytes(data: bytes) -> str:
    """Decode text files: BOM-aware UTF-8/UTF-16, then cp1252, then latin-1 (never fails)."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M") if (value.hour or value.minute) else value.strftime("%Y-%m-%d")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def _md_cell(text: str, max_chars: int) -> str:
    t = clean_inline(str(text)).replace("|", "\\|")
    if len(t) > max_chars:
        t = t[: max_chars - 1] + "…"
    return t


def table_to_markdown(rows: list[list], *, max_rows: int = 2000, max_cols: int = 40, max_cell: int = 300,
                      header: bool = True) -> str:
    """Rows (first row = header) → GitHub markdown table. Trailing empty columns are dropped."""
    rows = [[format_cell(c) for c in r] for r in rows if any(format_cell(c).strip() for c in r)]
    if not rows:
        return ""
    width = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if r[i].strip():
                width = max(width, i + 1)
                break
    width = max(1, min(width, max_cols))
    norm = [(r + [""] * width)[:width] for r in rows]
    if not header:
        norm = [[f"Column {i + 1}" for i in range(width)]] + norm
    head = "| " + " | ".join(_md_cell(c, max_cell) or f"Column {i + 1}" for i, c in enumerate(norm[0])) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(_md_cell(c, max_cell) for c in r) + " |" for r in norm[1: max_rows + 1]]
    out = [head, sep, *body]
    if len(norm) - 1 > max_rows:
        out.append(f"\n(… {len(norm) - 1 - max_rows} more rows truncated)")
    return "\n".join(out)


_NUM_LABEL_RE = re.compile(r"^\s*((?:\d+\.)*\d+)\.?\s+")


def numbering_depth(title: str) -> int | None:
    """'4.1.2 Foo' → 3, '3. Foo' → 1, 'Foo' → None."""
    m = _NUM_LABEL_RE.match(title or "")
    if not m:
        return None
    return m.group(1).count(".") + 1


def finalize(doc: ParsedDocument) -> ParsedDocument:
    """Common cleanup: normalize text, drop empty blocks, bound heading length."""
    out: list[Block] = []
    for b in doc.blocks:
        if b.kind == "heading":
            t = clean_inline(b.text)[:300]
            if t and re.search(r"\w", t):
                out.append(Block("heading", t, max(1, min(int(b.level or 1), 9)), b.page))
        else:
            t = clean_text(b.text)
            if t:
                out.append(Block(b.kind, t, 0, b.page))
    doc.blocks = out
    doc.title = clean_inline(doc.title or "")[:300]
    return doc


def parse_file(path: str | Path, filename: str | None = None, mime: str | None = None) -> ParsedDocument:
    """Parse a stored file into a `ParsedDocument`. Raises `ParseError` with a user-facing message."""
    path = Path(path)
    filename = filename or path.name
    fmt = detect_format(filename, mime)
    if fmt is None:
        raise ParseError(unsupported_message(filename))
    if not path.exists():
        raise ParseError("The stored file is missing; upload the document again.")
    if path.stat().st_size == 0:
        raise ParseError("The file is empty.")
    try:
        if fmt == "pdf":
            from app.rag.parsers.pdf import parse_pdf
            doc = parse_pdf(path)
        elif fmt == "docx":
            from app.rag.parsers.word import parse_docx
            doc = parse_docx(path)
        elif fmt == "markdown":
            from app.rag.parsers.md import parse_markdown
            doc = parse_markdown(decode_bytes(path.read_bytes()))
        elif fmt == "html":
            from app.rag.parsers.web import parse_html
            doc = parse_html(path.read_bytes())
        elif fmt == "text":
            from app.rag.parsers.plain import parse_text
            doc = parse_text(decode_bytes(path.read_bytes()))
        elif fmt == "xlsx":
            from app.rag.parsers.sheets import parse_xlsx
            doc = parse_xlsx(path)
        elif fmt == "csv":
            from app.rag.parsers.sheets import parse_csv
            doc = parse_csv(path, delimiter="\t" if path.suffix.lower() == ".tsv" else None)
        elif fmt == "pptx":
            from app.rag.parsers.slides import parse_pptx
            doc = parse_pptx(path)
        else:  # pragma: no cover
            raise ParseError(unsupported_message(filename))
    except ParseError:
        raise
    except MemoryError as e:
        raise ParseError("The document is too large to process.") from e
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"Could not read the {fmt.upper()} file — it may be corrupted or not a valid "
                         f"{fmt.upper()} ({type(e).__name__}: {str(e)[:200]}).") from e
    doc.format = doc.format or fmt
    doc = finalize(doc)
    if not doc.title:
        doc.title = default_title(filename)
        doc.meta["title_is_default"] = True
    if doc.text_chars < 1 and not doc.headings:
        if fmt == "pdf":
            raise ParseError("No extractable text found — the PDF looks like scanned images (OCR is not enabled).")
        raise ParseError("No text content found in the document.")
    return doc


def default_title(filename: str) -> str:
    stem = Path(filename or "document").stem
    t = re.sub(r"[_]+", " ", stem).strip()
    return t[:300] or "Untitled document"
