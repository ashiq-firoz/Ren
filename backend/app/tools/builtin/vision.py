"""Vision group: understand images, screenshots, scans and PDF pages with a vision-capable model (describe, OCR,
table extraction, comparison, object / UI-element detection) and crop images locally with Pillow.

Images are sent as OpenAI-style content parts ({"type": "image_url", "image_url": {"url": "data:..."}}).
PDF pages are rendered with pypdfium2 when available; OCR on PDFs uses the embedded text layer first."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import re
from pathlib import Path
from typing import Any, Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _office as O

vision = ToolGroup(
    "vision", "Vision",
    "See and read images: describe photos, screenshots and charts, OCR scans and PDFs, extract tables from images, "
    "compare two images, detect objects or UI elements with positions, and crop images. Requires a vision-capable "
    "model for everything except crop.",
    prefix="vision", icon="eye", category="core",
)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
VISION_EXTS = IMAGE_EXTS + (".pdf",)


def pdfium_available() -> bool:
    return importlib.util.find_spec("pypdfium2") is not None


def load_image(path: Path, page: int = 1):
    """PIL image of an image file, or of one PDF page (rendered with pypdfium2)."""
    from PIL import Image, ImageOps

    if path.suffix.lower() == ".pdf":
        if not pdfium_available():
            raise ToolError("Rendering PDF pages needs pypdfium2 (`pip install pypdfium2`). Use pdf_read for text.")
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        try:
            n = len(doc)
            if page < 1 or page > n:
                raise ToolError(f"Page {page} is out of range (1–{n}).")
            return doc[page - 1].render(scale=2.0).to_pil().convert("RGB")
        finally:
            doc.close()
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not open '{path.name}' as an image: {e}") from e
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001
        pass
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    return im


def data_url(im, max_side: int = 1600) -> str:
    from PIL import Image

    img = im.copy()
    img.thumbnail((max_side, max_side))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    mime = "png"
    if buf.tell() > 3_500_000:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        mime = "jpeg"
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


async def ask_vision(ctx: ToolContext, images: list[str], prompt: str, *, system: str | None = None,
                     model: str | None = None, max_tokens: int = 1500, schema: dict | None = None) -> Any:
    """Send images + prompt to the workspace model. Raises a clear ToolError when the model can't see images."""
    from app.llm.client import LLMError, NoProviderError

    try:
        llm = await ctx.llm(purpose="vision")
    except NoProviderError as e:
        raise ToolError(str(e)) from e
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    try:
        if schema is not None:
            data = await llm.json(messages, schema, model=model, max_tokens=max_tokens, name="vision")
            if data is None:
                raise ToolError("The model did not return valid JSON for this image.")
            return data
        res = await llm.chat(messages, model=model, temperature=0.1, max_tokens=max_tokens)
        return (res.content or "").strip()
    except LLMError as e:
        msg = str(e)
        if e.status in (400, 404, 415, 422) or re.search(r"image|vision|multimodal|modalit|content part|image_url",
                                                          msg, re.I):
            raise ToolError(
                "The configured model can't read images (provider said: " + msg[:200] + "). A vision-capable model "
                "is needed — pick one in Settings → Models (e.g. gpt-4.1-mini, claude-sonnet, gemini-2.5-flash) or "
                "pass `model` to this tool.") from e
        raise ToolError(f"Vision request failed: {msg[:300]}") from e


async def describe_image(ctx: ToolContext, path: Path, prompt: str, *, model: str | None = None,
                         page: int = 1) -> str:
    im = await asyncio.to_thread(load_image, path, page)
    url = await asyncio.to_thread(data_url, im)
    return await ask_vision(ctx, [url], prompt, model=model)


def _input(ctx: ToolContext, path: str, exts: tuple[str, ...] = VISION_EXTS) -> Path:
    return O.ws_input(ctx, path, exts)


# ----------------------------------------------------------------------------------------- tools
@vision.tool(risk=Risk.LOW, scope="read", timeout=180)
async def describe(ctx: ToolContext, path: str, question: str | None = None, page: int = 1,
                   detail: Literal["brief", "detailed"] = "detailed", model: str | None = None) -> dict:
    """Describe an image, screenshot, chart, diagram or PDF page — or answer a specific question about it.

    Args:
        path: Workspace path of the image (PNG/JPG/WebP/GIF/BMP/TIFF) or PDF.
        question: Optional question, e.g. "What is the Q3 value in this chart?".
        page: PDF page number to look at.
        detail: Brief (2-3 sentences) or detailed description.
        model: Optional vision-capable model id to use instead of the workspace default.
    """
    p = _input(ctx, path)
    if question:
        prompt = f"Look carefully at the image and answer precisely. If something is not visible, say so.\n\n{question}"
    elif detail == "brief":
        prompt = "Describe this image in 2-3 sentences: what it shows and the most important details."
    else:
        prompt = ("Describe this image for someone who can't see it: the type of image, its main content, any text, "
                  "numbers, labels, people/objects, layout and notable details. For charts give axes, series and key "
                  "values; for UI screenshots list the main sections and controls. Use short markdown sections.")
    answer = await describe_image(ctx, p, prompt, model=model, page=page)
    return {"path": O.vpath(ctx, p), **({"page": page} if p.suffix.lower() == ".pdf" else {}), "description": answer}


_OCR_PROMPT = ("Transcribe ALL text in this image exactly, preserving reading order. Use markdown: headings for "
               "titles, '-' for bullets and pipe tables for tabular data. Do not summarise, translate or add "
               "commentary. Mark unreadable words as [illegible].")


@vision.tool(risk=Risk.LOW, scope="read", timeout=300)
async def ocr(ctx: ToolContext, path: str, pages: str | None = None, model: str | None = None,
              max_chars: int = 30000) -> dict:
    """Extract text from an image or scanned document (OCR via the vision model). For PDFs, pages with an embedded
    text layer are read directly; only image-only pages are sent to the model (needs pypdfium2).

    Args:
        path: Workspace path of the image or PDF.
        pages: PDF page ranges like "1-3" (default: all, up to 20 pages via the model).
        model: Optional vision-capable model id.
        max_chars: Maximum characters to return.
    """
    p = _input(ctx, path)
    parts: list[str] = []
    methods = {"text_layer": 0, "vision": 0, "skipped": 0}
    if p.suffix.lower() == ".pdf":
        n, items = await asyncio.to_thread(O.pdf_pages_text, p, pages)
        vision_budget = 20
        for page_no, text in items:
            if len(text.strip()) >= 40:
                parts.append(f"--- Page {page_no} ---\n{text}")
                methods["text_layer"] += 1
            elif pdfium_available() and vision_budget > 0:
                vision_budget -= 1
                im = await asyncio.to_thread(load_image, p, page_no)
                url = await asyncio.to_thread(data_url, im, 2000)
                await ctx.progress(f"OCR page {page_no}", None)
                parts.append(f"--- Page {page_no} (OCR) ---\n" + await ask_vision(ctx, [url], _OCR_PROMPT, model=model,
                                                                                 max_tokens=4000))
                methods["vision"] += 1
            else:
                parts.append(f"--- Page {page_no} ---\n[no text layer; install pypdfium2 to OCR image-only pages]"
                             if not pdfium_available() else f"--- Page {page_no} ---\n[skipped: OCR page limit]")
                methods["skipped"] += 1
        text = "\n\n".join(parts)
        out: dict[str, Any] = {"path": O.vpath(ctx, p), "page_count": n, "methods": methods}
    else:
        im = await asyncio.to_thread(load_image, p)
        url = await asyncio.to_thread(data_url, im, 2000)
        text = await ask_vision(ctx, [url], _OCR_PROMPT, model=model, max_tokens=4000)
        out = {"path": O.vpath(ctx, p)}
    body, truncated = O.clip(text, max(1000, int(max_chars or 30000)))
    out.update({"chars": len(text), "truncated": truncated, "text": body})
    return out


_TABLE_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "null"]}}}},
        "required": ["columns", "rows"]}}},
    "required": ["tables"],
}


@vision.tool(risk=Risk.LOW, scope="read", timeout=240,
             risk_fn=lambda a: Risk.MEDIUM if a.get("save_as") else Risk.LOW)
async def extract_table(ctx: ToolContext, path: str, page: int = 1, instructions: str | None = None,
                        save_as: str | None = None, model: str | None = None) -> dict | ToolResult:
    """Extract tables from an image, screenshot or PDF page into rows (e.g. a photographed invoice or a chart's data
    table). Optionally save them to .xlsx (one sheet per table) or .csv.

    Args:
        path: Workspace path of the image or PDF.
        page: PDF page number.
        instructions: Extra guidance, e.g. "only the line items table; amounts as numbers".
        save_as: Optional output path (.xlsx or .csv).
        model: Optional vision-capable model id.
    """
    p = _input(ctx, path)
    im = await asyncio.to_thread(load_image, p, page)
    url = await asyncio.to_thread(data_url, im, 2000)
    prompt = ("Extract every table in the image. Copy cell text exactly; use numbers for numeric cells (no thousands "
              "separators or currency symbols); use null for empty cells. Keep the column order.")
    if instructions:
        prompt += f"\nAdditional instructions: {instructions}"
    data = await ask_vision(ctx, [url], prompt, model=model, max_tokens=4000, schema=_TABLE_SCHEMA)
    tables = [t for t in (data.get("tables") or []) if isinstance(t, dict)] if isinstance(data, dict) else []
    out: dict[str, Any] = {"path": O.vpath(ctx, p), "tables_found": len(tables), "tables": tables}
    if save_as and tables:
        ext = ".csv" if str(save_as).lower().endswith(".csv") else ".xlsx"
        target = O.ws_output(ctx, save_as, default_name=f"{p.stem}-tables", ext=ext)

        def save() -> None:
            if ext == ".csv":
                O.write_csv(target, tables[0].get("columns") or None, tables[0].get("rows") or [])
            else:
                O.write_xlsx(target, {(t.get("title") or f"Table {i + 1}")[:31]: (t.get("columns") or None,
                                                                              t.get("rows") or [])
                                      for i, t in enumerate(tables)})

        await asyncio.to_thread(save)
        return await O.produced(ctx, target, out, display=f"Extracted {len(tables)} table(s)")
    if not tables:
        out["hint"] = "No tables recognised. Try vision_ocr, or crop to the table first with vision_crop."
    return out


@vision.tool(risk=Risk.LOW, scope="read", timeout=240)
async def compare(ctx: ToolContext, path_a: str, path_b: str, focus: str | None = None, model: str | None = None,
                  page_a: int = 1, page_b: int = 1) -> dict:
    """Compare two images (e.g. design vs. implementation screenshots, two versions of a chart or document page)
    and report the differences.

    Args:
        path_a: First image or PDF (the reference / "before").
        path_b: Second image or PDF (the "after").
        focus: What to pay attention to, e.g. "layout and copy changes" or "numbers in the table".
        model: Optional vision-capable model id.
        page_a: PDF page for path_a.
        page_b: PDF page for path_b.
    """
    a, b = _input(ctx, path_a), _input(ctx, path_b)
    ia, ib = await asyncio.gather(asyncio.to_thread(load_image, a, page_a), asyncio.to_thread(load_image, b, page_b))
    ua, ub = await asyncio.gather(asyncio.to_thread(data_url, ia, 1400), asyncio.to_thread(data_url, ib, 1400))
    prompt = ("The first image is A (before/reference), the second is B (after/candidate). Compare them carefully. "
              "Report: 1) overall verdict (identical / minor differences / major differences), 2) a bulleted list of "
              "concrete differences (location, what changed from A to B), 3) anything present in only one image.")
    if focus:
        prompt += f"\nFocus on: {focus}"
    answer = await ask_vision(ctx, [ua, ub], prompt, model=model, max_tokens=1800)
    same_size = ia.size == ib.size
    pixel = None
    if same_size:
        def diff_ratio() -> float:
            from PIL import ImageChops

            d = ImageChops.difference(ia.convert("RGB"), ib.convert("RGB")).convert("L")
            hist = d.histogram()
            changed = sum(hist[16:])
            return round(100 * changed / max(1, ia.size[0] * ia.size[1]), 2)

        pixel = await asyncio.to_thread(diff_ratio)
    out: dict[str, Any] = {"a": O.vpath(ctx, a), "b": O.vpath(ctx, b), "comparison": answer,
                           "sizes": {"a": list(ia.size), "b": list(ib.size)}}
    if pixel is not None:
        out["pixels_changed_pct"] = pixel
    return out


_DETECT_SCHEMA = {
    "type": "object",
    "properties": {"objects": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "description": {"type": "string"},
        "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
        "required": ["label", "box"]}}},
    "required": ["objects"],
}


@vision.tool(risk=Risk.LOW, scope="read", timeout=240)
async def detect(ctx: ToolContext, path: str, targets: str | None = None, page: int = 1,
                 model: str | None = None, max_objects: int = 40) -> dict:
    """Detect and locate objects or UI elements (buttons, fields, logos, products, signatures…) in an image; returns
    labels with pixel bounding boxes and centre points (usable with computer_* click tools on screenshots).

    Args:
        path: Workspace path of the image or PDF.
        targets: What to look for, e.g. "all buttons and input fields" (default: all distinct objects).
        page: PDF page number.
        model: Optional vision-capable model id.
        max_objects: Maximum objects to return.
    """
    p = _input(ctx, path)
    im = await asyncio.to_thread(load_image, p, page)
    w, h = im.size
    url = await asyncio.to_thread(data_url, im, 1600)
    prompt = (f"Find {targets or 'all distinct objects and UI elements'} in the image. For each, give a short label, "
              "an optional one-line description, and its bounding box as [x_min, y_min, x_max, y_max] in a 0-1000 "
              f"normalized coordinate space (0,0 = top-left). At most {max_objects} items.")
    data = await ask_vision(ctx, [url], prompt, model=model, max_tokens=3000, schema=_DETECT_SCHEMA)
    objs = []
    for o in (data.get("objects") or [])[: max(1, int(max_objects))] if isinstance(data, dict) else []:
        box = o.get("box") or []
        if not isinstance(box, list) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = [float(v) for v in box]
        except (TypeError, ValueError):
            continue
        scale = 1.0 if max(x0, y0, x1, y1) <= 1.0 else 1000.0
        px = [round(x0 / scale * w), round(y0 / scale * h), round(x1 / scale * w), round(y1 / scale * h)]
        objs.append({"label": o.get("label"), "description": o.get("description") or None, "box": px,
                     "center": [round((px[0] + px[2]) / 2), round((px[1] + px[3]) / 2)]})
    return {"path": O.vpath(ctx, p), "image_size": [w, h], "count": len(objs), "objects": objs}


@vision.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def crop(ctx: ToolContext, path: str, box: list[float], output: str | None = None, padding: int = 0,
               page: int = 1, scale: float = 1.0) -> ToolResult:
    """Crop an image (or a PDF page) to a box and save it as a new PNG in output/ — e.g. to zoom into a table
    before OCR. Box values ≤ 1 are treated as fractions of the image size, otherwise as pixels.

    Args:
        path: Workspace path of the image or PDF.
        box: [left, top, right, bottom] in pixels, or fractions (0-1).
        output: Output path (default: output/<name>-crop.png).
        padding: Extra pixels around the box.
        page: PDF page number.
        scale: Resize factor for the result (e.g. 2 to enlarge small text).
    """
    p = _input(ctx, path)
    if not isinstance(box, list) or len(box) != 4:
        raise ToolError("`box` must be [left, top, right, bottom].")
    target = O.ws_output(ctx, output, default_name=f"{p.stem}-crop", ext=".png", overwrite=bool(output))

    def run() -> dict:
        from PIL import Image

        im = load_image(p, page)
        w, h = im.size
        vals = [float(v) for v in box]
        if max(vals) <= 1.0:
            vals = [vals[0] * w, vals[1] * h, vals[2] * w, vals[3] * h]
        x0, y0, x1, y1 = vals
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        pad = max(0, int(padding or 0))
        x0, y0 = max(0, int(x0) - pad), max(0, int(y0) - pad)
        x1, y1 = min(w, int(round(x1)) + pad), min(h, int(round(y1)) + pad)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise ToolError(f"The box is empty or outside the image ({w}×{h}).")
        out = im.crop((x0, y0, x1, y1))
        f = max(0.1, min(float(scale or 1.0), 8.0))
        if abs(f - 1.0) > 1e-3:
            out = out.resize((max(1, int(out.width * f)), max(1, int(out.height * f))), Image.LANCZOS)
        if out.mode not in ("RGB", "RGBA", "L"):
            out = out.convert("RGBA")
        out.save(target, "PNG")
        return {"box": [x0, y0, x1, y1], "size": [out.width, out.height], "source_size": [w, h]}

    info = await asyncio.to_thread(run)
    return await O.produced(ctx, target, info, display=f"Cropped → {target.name}")
