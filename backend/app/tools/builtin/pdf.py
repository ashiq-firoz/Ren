"""PDF group: create PDFs from markdown, read text by page, extract tables (pdfplumber), merge, split, inspect
metadata and watermark (fpdf2-generated stamp merged with pypdf)."""
from __future__ import annotations

import asyncio
import io
import math
from pathlib import Path
from typing import Any, Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _office as O

pdf = ToolGroup(
    "pdf", "PDF",
    "Create polished PDFs from markdown, read PDF text by page, extract tables, merge and split files, inspect "
    "metadata and add watermarks.",
    prefix="pdf", icon="file-type", category="productivity",
)

PDF_EXT = (".pdf",)


@pdf.tool(risk=Risk.MEDIUM, scope="write", timeout=120)
async def create(ctx: ToolContext, markdown: str, title: str | None = None, path: str | None = None,
                 subtitle: str | None = None, author: str | None = None, title_page: bool = False,
                 page_size: Literal["a4", "letter"] = "a4", overwrite: bool = False) -> ToolResult:
    """Create a clean, paginated PDF from markdown (headings, lists, tables, code, quotes, links, workspace images,
    full Unicode), with page numbers and an optional cover page.

    Args:
        markdown: Document body in markdown.
        title: Document title (top of page 1, or the cover page).
        path: Output path, e.g. "output/brief.pdf" (default: output/<title>.pdf).
        subtitle: Optional subtitle.
        author: Author shown on the cover and in PDF metadata.
        title_page: Put the title on a separate cover page.
        page_size: Paper size.
        overwrite: Replace an existing file instead of saving under a new name.
    """
    target = O.ws_output(ctx, path, default_name=title or "document", ext=".pdf", overwrite=overwrite)
    resolver = O.workspace_image_resolver(ctx)
    await asyncio.to_thread(O.markdown_to_pdf, markdown or "", target, title=title, subtitle=subtitle, author=author,
                            title_page=title_page, page_size=page_size, image_resolver=resolver)
    pages = len(O.pdf_reader(target).pages)
    return await O.produced(ctx, target, {"pages": pages}, display=f"Created {target.name} ({pages} pages)")


@pdf.tool(risk=Risk.LOW, scope="read", timeout=120)
async def read(ctx: ToolContext, path: str, pages: str | None = None, max_chars: int = 20000) -> dict:
    """Extract text from a PDF, page by page.

    Args:
        path: Workspace path of the PDF.
        pages: Page ranges like "1-3,7" or "5-" (default: all).
        max_chars: Maximum characters to return.
    """
    p = O.ws_input(ctx, path, PDF_EXT)
    n, items = await asyncio.to_thread(O.pdf_pages_text, p, pages)
    text = "\n\n".join(f"--- Page {i} ---\n{t}" for i, t in items)
    body, truncated = O.clip(text, max(1000, int(max_chars or 20000)))
    out: dict[str, Any] = {"path": O.vpath(ctx, p), "page_count": n, "pages_read": len(items),
                           "truncated": truncated, "text": body}
    if items and sum(len(t) for _, t in items) < 20 * len(items):
        out["note"] = "Little or no text layer (scanned document?). Use vision_ocr to read it."
    return out


def _tables(p: Path, pages: str | None, max_tables: int) -> tuple[list[dict], int, bool]:
    import pdfplumber

    tables: list[dict] = []
    with pdfplumber.open(str(p)) as doc:
        n = len(doc.pages)
        idx = O.parse_page_ranges(pages, n)
        capped = False
        if pages is None and len(idx) > 50:
            idx, capped = idx[:50], True
        for i in idx:
            try:
                found = doc.pages[i].extract_tables()
            except Exception:  # noqa: BLE001
                continue
            for k, t in enumerate(found):
                rows = [[" ".join(str(c or "").split()) for c in row] for row in t if row and any(c for c in row)]
                if not rows:
                    continue
                tables.append({"page": i + 1, "index": k, "header": rows[0], "rows": rows[1:], "n_rows": len(rows) - 1,
                               "n_cols": max(len(r) for r in rows)})
                if len(tables) >= max_tables:
                    return tables, n, True
    return tables, n, capped


@pdf.tool(risk=Risk.LOW, scope="read", timeout=180,
          risk_fn=lambda a: Risk.MEDIUM if a.get("save_as") else Risk.LOW)
async def extract_tables(ctx: ToolContext, path: str, pages: str | None = None, max_tables: int = 20,
                         max_rows: int = 50, save_as: str | None = None) -> dict | ToolResult:
    """Extract tables from a text-based PDF (financial statements, invoices, reports). Optionally save them to an
    Excel workbook (one sheet per table) or CSV (first table).

    Args:
        path: Workspace path of the PDF.
        pages: Page ranges like "2-4" (default: all, first 50 pages).
        max_tables: Maximum number of tables to extract.
        max_rows: Maximum rows returned per table (saved files contain all rows).
        save_as: Optional output path (.xlsx or .csv).
    """
    p = O.ws_input(ctx, path, PDF_EXT)
    tables, n, capped = await asyncio.to_thread(_tables, p, pages, max(1, min(int(max_tables or 20), 200)))
    limit = max(1, int(max_rows or 50))
    out: dict[str, Any] = {"path": O.vpath(ctx, p), "page_count": n, "tables_found": len(tables),
                           "tables": [{**t, "rows": t["rows"][:limit], "truncated": t["n_rows"] > limit}
                                      for t in tables]}
    if capped:
        out["note"] = "Stopped early (page or table limit); pass `pages` to target specific pages."
    if not tables:
        out["hint"] = "No ruled tables detected. Try pdf_read (text) or vision_extract_table on a page image."
        return out
    if save_as:
        ext = ".csv" if str(save_as).lower().endswith(".csv") else ".xlsx"
        target = O.ws_output(ctx, save_as, default_name=f"{p.stem}-tables", ext=ext)

        def save() -> None:
            if ext == ".csv":
                t = tables[0]
                O.write_csv(target, t["header"], [[O.coerce_scalar(c) for c in r] for r in t["rows"]])
            else:
                O.write_xlsx(target, {f"p{t['page']} t{t['index'] + 1}": (t["header"],
                                                                          [[O.coerce_scalar(c) for c in r]
                                                                           for r in t["rows"]]) for t in tables})

        await asyncio.to_thread(save)
        out["tables"] = [{k: v for k, v in t.items() if k != "rows"} for t in tables]
        return await O.produced(ctx, target, out, display=f"Saved {len(tables)} table(s) to {target.name}")
    return out


@pdf.tool(risk=Risk.MEDIUM, scope="write", timeout=180)
async def merge(ctx: ToolContext, paths: list[str], output: str | None = None,
                ranges: list[str] | None = None) -> ToolResult:
    """Merge several PDFs into one, in the given order (optionally only some pages of each).

    Args:
        paths: Workspace paths of the PDFs to combine, in order.
        output: Output path (default: output/merged.pdf).
        ranges: Optional page ranges per input, aligned with `paths`, e.g. ["1-2", "all", "3-"].
    """
    if not paths or len(paths) < 2:
        raise ToolError("Provide at least two PDFs to merge.")
    files = [O.ws_input(ctx, x, PDF_EXT) for x in paths]
    if ranges and len(ranges) != len(files):
        raise ToolError("`ranges` must have one entry per input file.")
    target = O.ws_output(ctx, output, default_name="merged", ext=".pdf", overwrite=bool(output))

    def run() -> int:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for i, f in enumerate(files):
            reader = O.pdf_reader(f)
            idx = O.parse_page_ranges(ranges[i] if ranges else None, len(reader.pages))
            for k in idx:
                writer.add_page(reader.pages[k])
        writer.add_metadata({"/Producer": "Ren by f69 Labs"})
        with open(target, "wb") as fh:
            writer.write(fh)
        return len(writer.pages)

    pages = await asyncio.to_thread(run)
    return await O.produced(ctx, target, {"pages": pages, "inputs": [O.vpath(ctx, f) for f in files]},
                            display=f"Merged {len(files)} PDFs ({pages} pages)")


@pdf.tool(risk=Risk.MEDIUM, scope="write", timeout=180)
async def split(ctx: ToolContext, path: str, ranges: list[str] | None = None, every: int | None = None,
                output_dir: str | None = None) -> ToolResult:
    """Split a PDF into several files — by page ranges (one file per range) or every N pages (default: one file
    per page).

    Args:
        path: Workspace path of the PDF.
        ranges: Page ranges, one output file each, e.g. ["1-3", "4-10", "11-"].
        every: Split into chunks of N pages instead.
        output_dir: Folder for the parts (default: output/<name>-parts/).
    """
    src = O.ws_input(ctx, path, PDF_EXT)
    O.require_write(ctx)
    folder = (output_dir or f"output/{O.slugify(src.stem)}-parts").strip().rstrip("/")
    out_dir = O.ws_path(ctx, folder + "/x.pdf", for_write=True).parent

    def run() -> list[Path]:
        from pypdf import PdfWriter

        reader = O.pdf_reader(src)
        n = len(reader.pages)
        if ranges:
            groups = [(r, O.parse_page_ranges(r, n)) for r in ranges]
        else:
            step = max(1, int(every or 1))
            groups = [(f"{a + 1}-{min(a + step, n)}" if step > 1 else str(a + 1), list(range(a, min(a + step, n))))
                      for a in range(0, n, step)]
        if len(groups) > 500:
            raise ToolError(f"That would create {len(groups)} files; use larger `every` or explicit ranges.")
        made = []
        for label, idx in groups:
            w = PdfWriter()
            for k in idx:
                w.add_page(reader.pages[k])
            target = O.unique_path(out_dir / f"{O.slugify(src.stem)}-p{label.replace(',', '_')}.pdf")
            with open(target, "wb") as fh:
                w.write(fh)
            made.append(target)
        return made

    made = await asyncio.to_thread(run)
    arts = []
    for f in made[:20]:
        arts.append(await O.publish(ctx, f))
    return ToolResult(content={"files": [O.vpath(ctx, f) for f in made], "count": len(made),
                               "folder": O.vpath(ctx, out_dir)},
                      artifacts=arts, display=f"Split {src.name} into {len(made)} files")


def _page_label(w_pt: float, h_pt: float) -> str:
    sizes = {"A4": (595.3, 841.9), "Letter": (612, 792), "Legal": (612, 1008), "A3": (841.9, 1190.6),
             "A5": (419.5, 595.3)}
    a, b = sorted((w_pt, h_pt))
    for name, (x, y) in sizes.items():
        if abs(a - x) < 4 and abs(b - y) < 4:
            return name + (" landscape" if w_pt > h_pt else "")
    return f"{w_pt / 72:.2f} × {h_pt / 72:.2f} in"


@pdf.tool(risk=Risk.LOW, scope="read", timeout=60)
async def info(ctx: ToolContext, path: str) -> dict:
    """PDF facts: page count, page size, metadata (title, author, dates), encryption, whether it has a text layer,
    form fields, bookmarks and attachments.

    Args:
        path: Workspace path of the PDF.
    """
    p = O.ws_input(ctx, path, PDF_EXT)

    def run() -> dict:
        from pypdf import PdfReader

        raw = PdfReader(str(p))
        encrypted = raw.is_encrypted
        r = O.pdf_reader(p)
        n = len(r.pages)
        first = r.pages[0] if n else None
        size = _page_label(float(first.mediabox.width), float(first.mediabox.height)) if first is not None else None
        uniform = True
        if n > 1 and first is not None:
            fw, fh = float(first.mediabox.width), float(first.mediabox.height)
            uniform = all(abs(float(pg.mediabox.width) - fw) < 1 and abs(float(pg.mediabox.height) - fh) < 1
                          for pg in r.pages[: min(n, 200)])
        sample = "".join((r.pages[i].extract_text() or "") for i in range(min(n, 3)))
        meta = r.metadata or {}
        md = {}
        for key, label in (("/Title", "title"), ("/Author", "author"), ("/Subject", "subject"),
                           ("/Creator", "creator"), ("/Producer", "producer"), ("/CreationDate", "created"),
                           ("/ModDate", "modified")):
            v = meta.get(key) if hasattr(meta, "get") else None
            if v:
                md[label] = str(v)[:200]
        try:
            fields = r.get_fields() or {}
        except Exception:  # noqa: BLE001
            fields = {}
        try:
            outline = len(r.outline or [])
        except Exception:  # noqa: BLE001
            outline = 0
        try:
            attachments = list((r.attachments or {}).keys())
        except Exception:  # noqa: BLE001
            attachments = []
        return {"path": O.vpath(ctx, p), "page_count": n, "page_size": size, "uniform_page_size": uniform,
                "encrypted": encrypted, "has_text_layer": len(sample.strip()) > 20, "metadata": md,
                "form_fields": len(fields), "bookmarks": outline, "attachments": attachments[:20],
                "file_size": O.human_size(p.stat().st_size)}

    return await asyncio.to_thread(run)


def _stamp(text: str, w: float, h: float, *, opacity: float, angle: float, size: float | None, color: str) -> bytes:
    cls = O._pdf_class()
    s = cls(unit="pt", format=(w, h))
    s.skip_pages = {1}
    s.set_auto_page_break(False)
    s.set_margins(0, 0, 0)
    O.register_fonts(s, mono=False, styles=("B",))
    s.add_page()
    t = s.fix(text)
    s.set_font(s.sans, "B", 100)
    w100 = max(1.0, s.get_string_width(t))
    diag = math.hypot(w, h) if angle % 180 else w
    fs = size or max(14.0, min(160.0, 100 * 0.62 * diag / w100))
    s.set_font(s.sans, "B", fs)
    s.set_text_color(*O.rgb(color))
    tw = s.get_string_width(t)
    with s.local_context(fill_opacity=opacity):
        with s.rotation(angle, x=w / 2, y=h / 2):
            s.text(w / 2 - tw / 2, h / 2 + fs * 0.35, t)
    return bytes(s.output())


@pdf.tool(risk=Risk.MEDIUM, scope="write", timeout=180)
async def watermark(ctx: ToolContext, path: str, text: str, output: str | None = None, opacity: float = 0.15,
                    angle: float = 45, font_size: float | None = None, color: str = "#5E6E65",
                    pages: str | None = None) -> ToolResult:
    """Stamp a text watermark (e.g. "CONFIDENTIAL", "DRAFT") diagonally across PDF pages.

    Args:
        path: Workspace path of the PDF.
        text: Watermark text.
        output: Output path (default: output/<name>-watermarked.pdf).
        opacity: 0.05 (faint) – 1.0 (solid).
        angle: Rotation in degrees.
        font_size: Font size in points (default: scaled to the page).
        color: Hex colour of the text.
        pages: Page ranges to stamp (default: all).
    """
    if not (text or "").strip():
        raise ToolError("Watermark text is required.")
    src = O.ws_input(ctx, path, PDF_EXT)
    target = O.ws_output(ctx, output, default_name=f"{src.stem}-watermarked", ext=".pdf", overwrite=bool(output))
    op = max(0.02, min(float(opacity), 1.0))
    try:
        O.rgb(color)
    except (ValueError, IndexError) as e:
        raise ToolError(f"Invalid colour '{color}'. Use hex like #5E6E65.") from e

    def run() -> int:
        from pypdf import PdfReader, PdfWriter, Transformation

        reader = O.pdf_reader(src)
        idx = set(O.parse_page_ranges(pages, len(reader.pages)))
        writer = PdfWriter(clone_from=reader)
        stamps: dict[tuple[int, int], Any] = {}
        count = 0
        for i, page in enumerate(writer.pages):
            if i not in idx:
                continue
            mb = page.mediabox
            w, h = float(mb.width), float(mb.height)
            key = (round(w), round(h))
            if key not in stamps:
                stamps[key] = PdfReader(io.BytesIO(_stamp(text.strip(), w, h, opacity=op, angle=angle,
                                                          size=font_size, color=color))).pages[0]
            page.merge_transformed_page(stamps[key], Transformation().translate(float(mb.left), float(mb.bottom)),
                                        over=True)
            count += 1
        with open(target, "wb") as fh:
            writer.write(fh)
        return count

    count = await asyncio.to_thread(run)
    return await O.produced(ctx, target, {"pages_stamped": count, "text": text.strip()},
                            display=f"Watermarked {count} page(s)")
