"""Office-document tools: documents (docx), slides (pptx), pdf, artifacts — and the shared _office helpers."""
from __future__ import annotations

import pytest

from app.tools.base import ToolError, ToolResult


def _c(res):
    return res.content if isinstance(res, ToolResult) else res


def _abs(ctx, vpath):
    return ctx.workspace.resolve(vpath, must_exist=True)


REPORT_MD = """Intro with **bold**, *italic*, ~~old~~, `code` and a [link](https://example.com).

## Highlights

- Revenue up **12%** year over year
- Churn down to 3.1%
  - Enterprise: 1.2%
  - SMB: 4.4%

1. First step
2. Second step

> A quote worth keeping.

| Region | Revenue | Growth |
|---|--:|---:|
| North | 1,234,000 | 12% |
| South | 987,500 | -3% |

```python
def hello():
    return "world"
```

## Partners

Our partner **Acme** Corp signed in Q3. Acme Corp renewed too.
"""


# ----------------------------------------------------------------------------------------- documents
async def test_docx_create_read_replace_roundtrip(tool_ctx):
    from docx import Document

    from app.tools.builtin import documents as D

    res = await D.create(tool_ctx, title="Quarterly Review", markdown=REPORT_MD, subtitle="FY26 Q3",
                         author="Finance", path="output/tests/quarterly-review.docx", overwrite=True)
    assert isinstance(res, ToolResult) and res.artifacts, "deliverable must be registered as an artifact"
    info = _c(res)
    assert info["path"] == "output/tests/quarterly-review.docx"
    assert info["tables"] == 1 and info["headings"] >= 2

    doc = Document(str(_abs(tool_ctx, info["path"])))
    styles = [p.style.name for p in doc.paragraphs]
    assert "Title" in styles and "Heading 2" in styles and "List Bullet" in styles and "List Number" in styles
    assert "Code Block" in styles
    assert doc.tables[0].cell(1, 0).text == "North"

    read = await D.read(tool_ctx, path=info["path"])
    md = read["markdown"]
    assert md.startswith("# Quarterly Review")
    assert "## Highlights" in md and "- Revenue up **12%** year over year" in md
    assert "   - Enterprise: 1.2%" in md
    assert "1. First step" in md and "2. Second step" in md
    assert "| North | 1,234,000 | 12% |" in md
    assert "```" in md and 'return "world"' in md
    assert "[link](https://example.com)" in md and "~~old~~" in md

    # "**Acme** Corp" spans two runs with different formatting → must still be replaced
    rep = _c(await D.replace(tool_ctx, path=info["path"], find="Acme Corp", replace="Globex"))
    assert rep["replacements"] == 2
    md2 = (await D.read(tool_ctx, path=info["path"]))["markdown"]
    assert "Acme Corp" not in md2 and "Globex" in md2
    none = _c(await D.replace(tool_ctx, path=info["path"], find="Nonexistent Ltd", replace="x"))
    assert none["replacements"] == 0
    rx = _c(await D.replace(tool_ctx, path=info["path"], find=r"(\d+)%", replace=r"\1 percent", regex=True))
    assert rx["replacements"] >= 3
    assert "12 percent" in (await D.read(tool_ctx, path=info["path"]))["markdown"]


async def test_docx_edit_tools(tool_ctx):
    from docx import Document
    from PIL import Image

    from app.tools.builtin import documents as D

    base = _c(await D.create(tool_ctx, title="Vendor Memo", markdown="## Background\n\nThe vendor is reliable.\n\n"
                                                                     "## Terms\n\nNet 30 payment terms apply.",
                             path="output/tests/memo.docx", overwrite=True))["path"]
    img = tool_ctx.workspace.resolve("output/tests/chart.png", for_write=True)
    Image.new("RGB", (600, 300), (184, 155, 106)).save(img)

    await D.append(tool_ctx, path=base, markdown="## Next steps\n\n- Sign contract\n- Kick-off call")
    await D.insert_table(tool_ctx, path=base, rows=[["Item", "Cost"], ["Licences", 1200.5], ["Support", 300]],
                         after="Net 30", caption="Table 1 — Costs")
    await D.insert_image(tool_ctx, path=base, image="output/tests/chart.png", caption="Figure 1", width_inches=4)
    com = _c(await D.add_comment(tool_ctx, path=base, anchor="reliable", comment="Source?", author="Reviewer"))
    assert com["type"] in ("comment", "inline-note")

    doc = Document(str(_abs(tool_ctx, base)))
    texts = [p.text for p in doc.paragraphs]
    assert "Next steps" in texts and "Sign contract" in texts
    assert len(doc.tables) == 1 and doc.tables[0].cell(1, 0).text == "Licences"
    # table was moved right after the "Net 30" paragraph (caption first)
    body = list(doc.element.body)
    net30 = next(i for i, el in enumerate(body) if "Net 30" in "".join(el.itertext()))
    assert body[net30 + 1].tag.endswith("p") and "Table 1" in "".join(body[net30 + 1].itertext())
    assert body[net30 + 2].tag.endswith("tbl")
    assert len(doc.inline_shapes) == 1
    if com["type"] == "comment":
        assert len(list(doc.comments)) == 1
        assert "Source?" in list(doc.comments)[0].text

    # compare two versions
    v2_md = "## Background\n\nThe vendor is very reliable.\n\n## Terms\n\nNet 45 payment terms apply.\n\nNew clause."
    v2 = _c(await D.create(tool_ctx, title="Vendor Memo", markdown=v2_md, path="output/tests/memo-v2.docx",
                           overwrite=True))["path"]
    v1 = _c(await D.create(tool_ctx, title="Vendor Memo", markdown="## Background\n\nThe vendor is reliable.\n\n"
                                                                   "## Terms\n\nNet 30 payment terms apply.",
                           path="output/tests/memo-v1.docx", overwrite=True))["path"]
    cmp = await D.compare(tool_ctx, path_a=v1, path_b=v2)
    assert cmp["changed"] == 2 and cmp["added"] == 1 and cmp["removed"] == 0
    assert "~~Net 30~~ **Net 45**" in cmp["report"] or "~~30~~ **45**" in cmp["report"]
    saved = await D.compare(tool_ctx, path_a=v1, path_b=v2, save_as="output/tests/memo-diff.md")
    assert _c(saved)["path"].endswith(".md")

    # export + restyle
    pdf = _c(await D.export_pdf(tool_ctx, path=base, engine="builtin"))
    assert pdf["pages"] >= 1 and pdf["engine"] == "builtin"
    from pypdf import PdfReader

    text = "".join(pg.extract_text() for pg in PdfReader(str(_abs(tool_ctx, pdf["path"]))).pages)
    assert "Vendor Memo" in text and "Licences" in text
    await D.apply_style(tool_ctx, path=base, heading_font="Georgia", heading_color="#1F3864", body_size=11)
    doc = Document(str(_abs(tool_ctx, base)))
    assert doc.styles["Heading 1"].font.name == "Georgia"
    assert str(doc.styles["Heading 2"].font.color.rgb) == "1F3864"
    with pytest.raises(ToolError):
        await D.apply_style(tool_ctx, path=base)
    with pytest.raises(ToolError):
        await D.add_comment(tool_ctx, path=base, anchor="not in the doc", comment="x")


# ----------------------------------------------------------------------------------------- slides
async def test_pptx_create_read_and_edit(tool_ctx):
    from lxml import etree
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    from app.tools.builtin import slides as S

    res = await S.create(tool_ctx, title="Board Update", subtitle="October 2026", path="output/tests/board.pptx",
                         overwrite=True, slides=[
                             {"title": "Highlights", "bullets": ["Revenue up **12%**", "Churn down",
                                                                 ["Enterprise 1.2%", "SMB 4.4%"]],
                              "notes": "Open with the win."},
                             {"title": "By region", "table": [["Region", "Revenue"], ["North", 1234000],
                                                              ["South", 987500]]},
                             {"title": "Trend", "chart": {"type": "column", "categories": ["Q1", "Q2", "Q3"],
                                                          "series": {"Revenue": [10, 12, 15]}}},
                             {"title": "Section break"},
                         ])
    info = _c(res)
    assert info["slides"] == 5 and res.artifacts
    path = info["path"]
    prs = Presentation(str(_abs(tool_ctx, path)))
    assert round(prs.slide_width / 914400, 2) == 13.33
    theme = prs.slide_master.part.part_related_by(RT.THEME).blob.decode()
    assert "EFE7DA" in theme and "0F2D24" in theme and "1F5A45" in theme  # f69 beige theme

    md = (await S.read(tool_ctx, path=path))["markdown"]
    assert "## Slide 1: Board Update" in md and "October 2026" in md
    assert "## Slide 2: Highlights" in md and "- Revenue up 12%" in md and "  - Enterprise 1.2%" in md
    assert "> Notes: Open with the win." in md
    assert "| North | 1,234,000 |" in md
    assert "[Chart (column" in md and "Revenue" in md

    await S.add_slide(tool_ctx, path=path, title="Risks", bullets=["Supply chain", "FX"], position=2)
    await S.duplicate_slide_(tool_ctx, path=path, slide=4)  # the chart slide (after insert at 2)
    prs = Presentation(str(_abs(tool_ctx, path)))
    titles = [s.shapes.title.text_frame.text for s in prs.slides]
    assert titles[:6] == ["Board Update", "Risks", "Highlights", "By region", "By region", "Trend"] or \
        titles.count("Trend") == 2
    charts = [sh for s in prs.slides for sh in s.shapes if getattr(sh, "has_chart", False) and sh.has_chart]
    assert len(charts) >= 1

    await S.duplicate_slide_(tool_ctx, path=path, slide=titles.index("Trend") + 1)
    prs = Presentation(str(_abs(tool_ctx, path)))
    charts = [sh.chart for s in prs.slides for sh in s.shapes if getattr(sh, "has_chart", False) and sh.has_chart]
    assert len(charts) >= 2 and all(list(c.plots[0].categories) == ["Q1", "Q2", "Q3"] for c in charts)
    chart_parts = {sh.chart.part.partname for s in prs.slides for sh in s.shapes
                   if getattr(sh, "has_chart", False) and sh.has_chart}
    assert len(chart_parts) == len(charts), "duplicated charts must get their own chart part"

    n = len(prs.slides)
    await S.reorder(tool_ctx, path=path, slide=n, to=2)
    await S.delete_slide(tool_ctx, path=path, slide=2)
    prs = Presentation(str(_abs(tool_ctx, path)))
    assert len(prs.slides) == n - 1
    with pytest.raises(ToolError):
        await S.reorder(tool_ctx, path=path, order=[1, 1, 2])

    await S.add_text(tool_ctx, path=path, slide=2, text="Key takeaway: **growth**", font_size=20, color="#B89B6A")
    await S.add_table(tool_ctx, path=path, slide=3, rows="a,b\n1,2\n3,4")
    await S.add_chart(tool_ctx, path=path, slide=3, chart_type="pie", categories=["A", "B"], series={"Share": [60, 40]})
    from PIL import Image

    img = tool_ctx.workspace.resolve("output/tests/slide-img.png", for_write=True)
    Image.new("RGB", (800, 400), (18, 18, 17)).save(img)
    await S.add_image(tool_ctx, path=path, slide=2, image="output/tests/slide-img.png")
    dark = _c(await S.apply_theme(tool_ctx, path=path, theme="dark", output="output/tests/board-dark.pptx"))
    prs = Presentation(str(_abs(tool_ctx, dark["path"])))
    theme = etree.fromstring(prs.slide_master.part.part_related_by(RT.THEME).blob)
    vals = theme.xpath("//a:clrScheme/a:lt1/a:srgbClr/@val",
                       namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"})
    assert vals == ["0F2D24"]  # f69 dark: deep-green background
    md = (await S.read(tool_ctx, path=dark["path"]))["markdown"]
    assert "Key takeaway" in md and "[Image" in md
    with pytest.raises(ToolError):
        await S.delete_slide(tool_ctx, path=path, slide=99)


# ----------------------------------------------------------------------------------------- pdf
UNICODE_MD = """# Café Report

Naïve façade — “smart quotes” – Ελληνικά – Русский – ✓ done.

- Item one
- Item two

| Name | Value |
|---|---|
| Ä | 1 |
"""


async def test_pdf_create_read_merge_split_info_watermark(tool_ctx):
    from pypdf import PdfReader

    from app.tools.builtin import pdf as P

    a = _c(await P.create(tool_ctx, markdown=UNICODE_MD, title="Unicode Check", path="output/tests/uni.pdf",
                          overwrite=True))
    assert a["pages"] == 1
    read = await P.read(tool_ctx, path=a["path"])
    assert "Café" in read["text"] and "Русский" in read["text"] and "Ελληνικά" in read["text"]
    assert read["page_count"] == 1

    b = _c(await P.create(tool_ctx, markdown="## Cover\n\nSecond file.", title="Second", title_page=True,
                          path="output/tests/second.pdf", overwrite=True, author="Ren"))
    assert b["pages"] == 2  # cover + content

    merged = _c(await P.merge(tool_ctx, paths=[a["path"], b["path"]], output="output/tests/merged.pdf"))
    assert merged["pages"] == 3
    parts = _c(await P.split(tool_ctx, path=merged["path"], ranges=["1", "2-3"]))
    assert parts["count"] == 2
    assert len(PdfReader(str(_abs(tool_ctx, parts["files"][1]))).pages) == 2
    per_page = _c(await P.split(tool_ctx, path=merged["path"], output_dir="output/tests/pages"))
    assert per_page["count"] == 3

    info = await P.info(tool_ctx, path=merged["path"])
    assert info["page_count"] == 3 and info["page_size"].startswith("A4") and info["has_text_layer"]

    wm = _c(await P.watermark(tool_ctx, path=a["path"], text="CONFIDENTIAL", output="output/tests/wm.pdf"))
    assert wm["pages_stamped"] == 1
    text = PdfReader(str(_abs(tool_ctx, wm["path"]))).pages[0].extract_text()
    assert "CONFIDENTIAL" in text and "Café" in text

    with pytest.raises(ToolError):
        await P.merge(tool_ctx, paths=[a["path"]])
    with pytest.raises(ToolError):
        await P.read(tool_ctx, path=a["path"], pages="5-9")


async def test_pdf_extract_tables(tool_ctx):
    from fpdf import FPDF

    from app.tools.builtin import pdf as P

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    with pdf.table() as t:
        for row in (("Invoice", "Amount"), ("INV-1", "100.00"), ("INV-2", "250.50")):
            r = t.row()
            for v in row:
                r.cell(v)
    target = tool_ctx.workspace.resolve("output/tests/table.pdf", for_write=True)
    pdf.output(str(target))
    res = await P.extract_tables(tool_ctx, path="output/tests/table.pdf")
    assert res["tables_found"] == 1
    t = res["tables"][0]
    assert t["header"] == ["Invoice", "Amount"] and t["rows"][1] == ["INV-2", "250.50"]
    saved = _c(await P.extract_tables(tool_ctx, path="output/tests/table.pdf", save_as="output/tests/tables.xlsx"))
    assert saved["path"].endswith(".xlsx")


async def test_markdown_pdf_helpers_unicode_and_fallback():
    from app.tools.builtin import _office as O

    data = O.markdown_to_pdf(UNICODE_MD, title="T", title_page=True)
    assert data[:5] == b"%PDF-"
    assert O.latin1("“Hi” — ✓") == '"Hi" - v'
    blocks = O.parse_markdown(UNICODE_MD)
    kinds = [b.kind for b in blocks]
    assert kinds[:4] == ["heading", "para", "list", "table"]
    html = O.markdown_to_html("# T\n\n<script>alert(1)</script>\n\n**b**", "T")
    assert "<script>" not in html and "&lt;script&gt;" in html and "<strong>b</strong>" in html


# ----------------------------------------------------------------------------------------- artifacts
async def test_artifact_create_every_format_and_validate(tool_ctx):
    from app.tools.builtin import artifacts as A

    md = "# Plan\n\n## Goals\n\n- Grow revenue\n- Cut churn\n\n## Budget\n\n| Item | Cost |\n|---|---|\n| Ads | 100 |"
    rows = [{"region": "North", "revenue": 120.5}, {"region": "South", "revenue": 80}]
    cases = {
        "md": md, "txt": "plain text", "html": md, "json": {"a": 1, "items": [1, 2]}, "csv": rows, "xlsx": rows,
        "docx": md, "pdf": md, "pptx": md,
    }
    for fmt, content in cases.items():
        res = await A.create(tool_ctx, content=content, format=fmt, name=f"output/tests/artifact-{fmt}",
                             title="Plan", overwrite=True)
        info = _c(res)
        assert info["path"].endswith("." + fmt) and res.artifacts, fmt
        v = await A.validate(tool_ctx, path=info["path"])
        assert v["ok"], (fmt, v)
    assert (await A.validate(tool_ctx, path="output/tests/artifact-pptx.pptx"))["stats"]["slides"] >= 2
    assert (await A.validate(tool_ctx, path="output/tests/artifact-csv.csv"))["stats"] == {"rows": 2, "columns": 2}
    html = tool_ctx.workspace.resolve("output/tests/artifact-html.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in html and "#EFE7DA" in html and "<table>" in html

    with pytest.raises(ToolError):
        await A.create(tool_ctx, content="{not json", format="json", name="output/tests/bad")

    conv = _c(await A.export(tool_ctx, path="output/tests/artifact-md.md", to="docx"))
    assert conv["path"].endswith(".docx")
    conv = _c(await A.export(tool_ctx, path="output/tests/artifact-csv.csv", to="xlsx"))
    assert (await A.validate(tool_ctx, path=conv["path"]))["ok"]
    conv = _c(await A.export(tool_ctx, path="output/tests/artifact-xlsx.xlsx", to="json"))
    import json

    recs = json.loads(tool_ctx.workspace.resolve(conv["path"]).read_text(encoding="utf-8"))
    assert recs[0]["region"] == "North"
    conv = _c(await A.export(tool_ctx, path="output/tests/artifact-docx.docx", to="md"))
    assert "## Goals" in tool_ctx.workspace.resolve(conv["path"]).read_text(encoding="utf-8")
    conv = _c(await A.export(tool_ctx, path="output/tests/artifact-pdf.pdf", to="txt"))
    assert "Grow revenue" in tool_ctx.workspace.resolve(conv["path"]).read_text(encoding="utf-8")
    with pytest.raises(ToolError):
        await A.export(tool_ctx, path="output/tests/artifact-json.json", to="pptx")

    listing = await A.list_(tool_ctx, path="output/tests", extension="pptx")
    assert listing["count"] >= 1 and all(f["path"].endswith(".pptx") for f in listing["files"])


async def test_viewer_cannot_write_and_paths_stay_in_sandbox(org_ctx, tool_ctx):
    from app.tools.base import ToolContext
    from app.tools.builtin import artifacts as A
    from app.tools.builtin import documents as D

    viewer = ToolContext(org_id=org_ctx["org_id"], user_id=org_ctx["user_id"], role="viewer")
    with pytest.raises(ToolError, match="read-only"):
        await A.create(viewer, content="x", format="md", name="output/tests/viewer")
    with pytest.raises(ToolError):
        await A.create(tool_ctx, content="x", format="md", name="../../escape")
    with pytest.raises(ToolError):
        await D.read(tool_ctx, path="../../../etc/passwd")
    with pytest.raises(ToolError, match="Not found"):
        await D.read(tool_ctx, path="output/tests/missing.docx")
