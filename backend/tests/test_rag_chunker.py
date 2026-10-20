"""Chunker: token-aware chunks inside leaves, overlap, table header repetition, contextual search_text."""
from app.rag.chunker import MAX_TOKENS, chunk_leaves, chunk_text
from app.rag.parsers import Block, ParsedDocument
from app.rag.structure import build_structure
from app.rag.textutil import count_tokens, split_sentences


def _sentences(n):
    return " ".join(f"Sentence number {i} explains rule {i} of the travel policy in some detail." for i in range(n))


def test_chunk_sizes_and_overlap():
    text = "\n\n".join(_sentences(12) for _ in range(6))
    chunks = chunk_text(text, target=350)
    assert len(chunks) >= 3
    for c in chunks:
        assert count_tokens(c) <= MAX_TOKENS
    for a, b in zip(chunks, chunks[1:]):
        last_sentence = split_sentences(a)[-1]
        assert b.startswith(last_sentence) or last_sentence in b[:400], "consecutive chunks should overlap"
    # nothing lost: every sentence appears somewhere
    joined = " ".join(chunks)
    for s in split_sentences(text):
        assert s in joined


def test_small_text_single_chunk():
    assert chunk_text("Just one short paragraph.") == ["Just one short paragraph."]


def test_table_rows_repeat_header():
    header = "| Region | Office | Phone |\n| --- | --- | --- |"
    rows = "\n".join(f"| Region {i} | Office building number {i} downtown | +1 555 0100 {i:03d} |" for i in range(80))
    chunks = chunk_text(header + "\n" + rows, target=200)
    assert len(chunks) >= 3
    for c in chunks:
        assert c.startswith("| Region | Office | Phone |\n| --- | --- | --- |")
    assert sum(c.count("| Region ") for c in chunks) >= 80


async def test_chunk_leaves_search_text_and_ordinals():
    blocks = [Block("heading", "Travel", 1),
              Block("heading", "Flights", 2), Block("text", _sentences(40)),
              Block("heading", "Hotels", 2), Block("text", "Hotels must be booked through the portal.")]
    s = await build_structure(ParsedDocument(title="Policy", blocks=blocks, format="markdown"))
    leaves = s.leaves()
    drafts = chunk_leaves(leaves)
    assert [d.ordinal for d in drafts] == list(range(len(drafts)))
    flights = next(lf for lf in leaves if lf.title == "Flights")
    fl_chunks = [d for d in drafts if d.section_id == flights.id]
    assert len(fl_chunks) >= 2
    for d in drafts:
        leaf = next(lf for lf in leaves if lf.id == d.section_id)
        assert d.search_text == f"{leaf.breadcrumb}\n{d.content}"
        assert d.meta["path"] == leaf.path
        assert d.content in leaf.content or all(s in leaf.content for s in split_sentences(d.content))
    assert drafts[-1].search_text.startswith("Policy › Travel › Hotels\n")
