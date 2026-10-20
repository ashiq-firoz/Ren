"""Structure builder: every text in exactly one leaf, overview leaves, paths/breadcrumbs, splitting, augmentation."""
import json
import re

from app.llm.mock import MockLLM
from app.rag.parsers import Block, ParsedDocument
from app.rag.structure import MAX_LEAF_TOKENS, OVERVIEW_TITLE, build_structure
from app.rag.textutil import count_tokens

LONG_INTRO = ("This chapter describes every type of leave that employees can take, how leave accrues over the "
              "year, and who approves requests. It applies to full-time and part-time staff alike.")


def _doc(blocks, title="Handbook", fmt="markdown"):
    return ParsedDocument(title=title, blocks=blocks, format=fmt)


def _paragraphs(n, topic):
    return [f"{topic} paragraph {i}: " + " ".join(f"{topic.lower()}word{i}x{j}" for j in range(45)) + "."
            for i in range(n)]


async def test_every_text_in_exactly_one_leaf_and_overviews():
    blocks = [
        Block("text", "Version 3 of the handbook, approved by the board in March and effective immediately."
                      " It replaces all earlier versions."),
        Block("heading", "Leave", 1),
        Block("text", LONG_INTRO),
        Block("heading", "Annual Leave", 2),
        Block("text", "Employees receive 25 days of paid annual leave per year."),
        Block("heading", "Sick Leave", 2),
        Block("text", "Sick leave requires a doctor's note after three days."),
        Block("heading", "Benefits", 1),
        Block("text", "Short intro."),  # tiny intro → merged into the next leaf
        Block("heading", "Pension", 2),
        Block("text", "The company matches pension contributions up to 5 percent."),
        Block("heading", "Empty Section", 2),  # no text → dropped
        Block("heading", "Health", 2),
        Block("text", "Private health insurance covers the employee and dependants."),
    ]
    s = await build_structure(_doc(blocks))
    leaves = s.leaves()
    for b in blocks:
        if b.kind == "text":
            holders = [lf for lf in leaves if b.text in lf.content]
            assert len(holders) == 1, (b.text, [h.path for h in holders])
    for n in s.nodes():
        if not n.is_leaf:
            assert n.content == ""
    by_path = {n.path: n for n in s.nodes()}
    assert by_path["0"].title == OVERVIEW_TITLE and "Version 3" in by_path["0"].content
    assert by_path["1.0"].title == OVERVIEW_TITLE and by_path["1.0"].content.startswith("This chapter")
    assert by_path["1.1"].title == "Annual Leave" and by_path["1.2"].title == "Sick Leave"
    assert "2.0" not in by_path  # tiny intro merged into the first leaf of "Benefits"
    assert by_path["2.1"].title == "Pension" and by_path["2.1"].content.startswith("Short intro.")
    assert by_path["2.2"].title == "Health"  # "Empty Section" was dropped
    assert all(t not in [n.title for n in s.nodes()] for t in ("Empty Section",))


async def test_paths_breadcrumbs_ordinals_and_label_stripping():
    blocks = [
        Block("heading", "1. Introduction", 1), Block("text", "Intro text for everyone."),
        Block("heading", "2. Scope", 1),
        Block("heading", "2.1 Employees", 2), Block("text", "Applies to employees."),
        Block("heading", "2.2 Contractors", 2), Block("text", "Applies to contractors."),
        Block("heading", "Appendix 7", 1), Block("text", "Forms."),
    ]
    s = await build_structure(_doc(blocks, title="Policy"))
    rows = [(n.path, n.title, n.depth, n.is_leaf) for n in s.nodes()]
    assert rows == [("1", "Introduction", 1, True), ("2", "Scope", 1, False), ("2.1", "Employees", 2, True),
                    ("2.2", "Contractors", 2, True), ("3", "Appendix 7", 1, True)]
    by_path = {n.path: n for n in s.nodes()}
    assert by_path["2.2"].breadcrumb == "Policy › Scope › Contractors"
    assert by_path["2.2"].parent_id == by_path["2"].id and by_path["2"].parent_id is None
    assert [n.ordinal for n in s.nodes()] == [1, 2, 3, 4, 5]
    assert by_path["2"].token_count == by_path["2.1"].token_count + by_path["2.2"].token_count


async def test_single_wrapper_heading_becomes_title():
    blocks = [Block("heading", "Acme Security Handbook", 1), Block("text", LONG_INTRO),
              Block("heading", "Passwords", 2), Block("text", "Use 14 characters."),
              Block("heading", "Devices", 2), Block("text", "Encrypt laptops.")]
    doc = _doc(blocks, title="acme sec", fmt="markdown")
    doc.meta["title_is_default"] = True
    s = await build_structure(doc)
    assert s.title == "Acme Security Handbook"
    assert [(n.path, n.title) for n in s.nodes()] == [("0", OVERVIEW_TITLE), ("1", "Passwords"), ("2", "Devices")]
    assert s.leaves()[1].breadcrumb == "Acme Security Handbook › Passwords"


async def test_oversized_leaf_is_split_into_parts_without_losing_text():
    paras = _paragraphs(40, "Retention")
    blocks = [Block("heading", "Records", 1), Block("text", "Short records intro."),
              Block("heading", "Retention Schedule", 2)] + [Block("text", p) for p in paras]
    s = await build_structure(_doc(blocks))
    sched = next(n for n in s.nodes() if n.title == "Retention Schedule")
    assert not sched.is_leaf and len(sched.children) >= 3
    for i, part in enumerate(sched.children, start=1):
        assert part.title == f"Retention Schedule (part {i})" and part.is_leaf
        assert part.path == f"{sched.path}.{i}"
        assert part.token_count <= MAX_LEAF_TOKENS
    joined = "\n\n".join(p.content for p in sched.children)
    for p in paras:
        assert joined.count(p) == 1


async def test_flat_document_without_llm_is_segmented():
    paras = _paragraphs(30, "Minutes")
    s = await build_structure(_doc([Block("text", p) for p in paras], title="Board minutes", fmt="text"),
                              llm=None, augment=True)
    assert s.structure_source == "flat"
    leaves = s.leaves()
    assert len(leaves) >= 3
    for lf in leaves[:-1]:
        assert 400 <= count_tokens(lf.content) <= 950
        assert re.match(r"Part \d+", lf.title)
    joined = "\n\n".join(lf.content for lf in leaves)
    assert all(joined.count(p) == 1 for p in paras)


async def test_augmentation_with_mock_llm_falls_back_gracefully():
    # MockLLM returns no titles for the batched schema → heuristic titles, still well-formed
    paras = _paragraphs(30, "Notes")
    s = await build_structure(_doc([Block("text", p) for p in paras], title="Notes", fmt="text"),
                              llm=MockLLM(), augment=True)
    assert s.structure_source == "flat"
    assert all(lf.title for lf in s.leaves())
    joined = "\n\n".join(lf.content for lf in s.leaves())
    assert all(joined.count(p) == 1 for p in paras)


class TitlingLLM(MockLLM):
    """MockLLM that answers the augmentation prompts properly (titles per excerpt id, chapter grouping)."""

    async def json(self, messages, schema, **kw):
        text = messages[-1]["content"]
        props = schema.get("properties", {})
        if "headings" in props:
            ids = [int(x) for x in re.findall(r"<excerpt id=(\d+)>", text)]
            return {"headings": [{"id": i, "title": f"Topic number {i}"} for i in ids]}
        if "chapters" in props:
            n = len(re.findall(r"(?m)^\d+\. ", text))
            half = n // 2
            return {"chapters": [{"title": "First half", "first": 1, "last": half},
                                 {"title": "Second half", "first": half + 1, "last": n}]}
        return await super().json(messages, schema, **kw)


async def test_augmentation_titles_and_chapters():
    paras = _paragraphs(70, "Report")
    s = await build_structure(_doc([Block("text", p) for p in paras], title="Annual report", fmt="text"),
                              llm=TitlingLLM(), augment=True)
    assert s.structure_source == "augmented"
    tops = s.root.children
    assert [t.title for t in tops] == ["First half", "Second half"]
    assert all(not t.is_leaf for t in tops)
    leaves = s.leaves()
    assert len(leaves) >= 8
    assert leaves[0].title == "Topic number 1" and leaves[0].path == "1.1"
    assert leaves[-1].breadcrumb.startswith("Annual report › Second half › Topic number")
    joined = "\n\n".join(lf.content for lf in leaves)
    assert all(joined.count(p) == 1 for p in paras)


async def test_leaf_summaries_batched():
    class SummLLM(MockLLM):
        async def json(self, messages, schema, **kw):
            if "summaries" in schema.get("properties", {}):
                ids = [int(x) for x in re.findall(r"<leaf id=(\d+)", messages[-1]["content"])]
                return {"summaries": [{"id": i, "summary": f"Summary {i}"} for i in ids]}
            return await super().json(messages, schema, **kw)

    blocks = []
    for i in range(10):
        blocks += [Block("heading", f"Topic {i}", 1), Block("text", f"Details about topic {i}. " * 5)]
    s = await build_structure(_doc(blocks), llm=SummLLM(), summaries=True)
    assert [lf.summary for lf in s.leaves()] == [f"Summary {i}" for i in range(1, 11)]
    assert json.dumps([lf.summary for lf in s.leaves()])


def test_document_preamble_becomes_overview_leaf_and_dotted_numbers_are_dropped():
    """Regression: a short document-level preamble gets its own '0 Overview' leaf (not merged into the first
    section), and source outline numbers are replaced by computed paths even when the source skips numbers."""
    import asyncio

    from app.rag.parsers.md import parse_markdown
    from app.rag.structure import build_structure

    md = ("# Handbook\n\nApplies to all full-time employees.\n\n## 1. Leave\n\n### 1.1 Annual Leave\nTwenty days.\n\n"
          "### 1.3 Parental Leave\nSixteen weeks.\n\n## 2. Remote Work\nThree days a week.\n")
    res = asyncio.run(build_structure(parse_markdown(md), title=None, llm=None, augment=False))
    nodes = {n.path: n for n in res.nodes()}
    assert nodes["0"].title == "Overview" and nodes["0"].is_leaf and "full-time" in nodes["0"].content
    assert "full-time" not in nodes["1.1"].content
    assert nodes["1.2"].title == "Parental Leave"
