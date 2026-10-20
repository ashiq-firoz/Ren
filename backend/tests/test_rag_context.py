"""Context builder: grouping by leaf, adjacent-chunk merge, small-leaf expansion, [n] numbering, citations, budget."""
from app.rag.context_builder import build_context, used_citations
from app.rag.textutil import count_tokens


def _r(cid, sec, path, ordinal, content, score, title="Leaf", doc="d1", doc_title="Handbook"):
    return {"chunk_id": cid, "document_id": doc, "document_title": doc_title, "section_id": sec,
            "section_path": path, "section_title": title, "breadcrumb": f"{doc_title} › {title}",
            "content": content, "score": score, "ordinal": ordinal}


def test_grouping_merging_numbering_and_citations():
    a1 = "Alpha one. Alpha two. Alpha three shared sentence."
    a2 = "Alpha three shared sentence. Alpha four. Alpha five."
    results = [
        _r("c2", "sA", "1.1", 2, a2, 0.9, "Parental Leave"),
        _r("c9", "sB", "2.1", 9, "Bravo text about travel.", 0.8, "Travel"),
        _r("c1", "sA", "1.1", 1, a1, 0.7, "Parental Leave"),
        _r("c5", "sC", "3", 5, "Charlie chunk.", 0.5, "Visitors", doc="d2", doc_title="Security"),
    ]
    built = build_context(results, leaf_texts={"sC": ("Charlie FULL leaf text with every detail.", 12)})
    assert [g.n for g in built.groups] == [1, 2, 3]
    assert built.chunk_to_source == {"c2": 1, "c1": 1, "c9": 2, "c5": 3}
    # adjacent chunks merged in document order with the overlap removed
    assert built.groups[0].text == ("Alpha one. Alpha two. Alpha three shared sentence. Alpha four. Alpha five.")
    assert built.groups[2].text == "Charlie FULL leaf text with every detail."  # small leaf expanded
    assert built.text.startswith("[1] Handbook › Parental Leave (§1.1)\nAlpha one.")
    assert "\n\n[2] Handbook › Travel (§2.1)\nBravo text about travel." in built.text
    assert "[3] Security › Visitors (§3)" in built.text
    c1 = built.citations[0]
    assert set(c1) == {"id", "document_id", "document_title", "section_id", "section_path", "section_title",
                       "breadcrumb", "snippet", "score"}
    assert (c1["id"], c1["section_id"], c1["section_path"], c1["score"]) == (1, "sA", "1.1", 0.9)
    assert c1["snippet"].startswith("Alpha three shared sentence.")
    assert built.citations[2]["document_id"] == "d2"


def test_non_adjacent_chunks_and_pages():
    r1 = _r("x1", "s", "4", 1, "First part.", 0.9)
    r2 = _r("x2", "s", "4", 7, "Much later part.", 0.8)
    r1.update(page_start=3, page_end=3)
    r2.update(page_start=5, page_end=6)
    built = build_context([r1, r2])
    assert built.groups[0].text == "First part.\n[…]\nMuch later part."
    assert built.text.startswith("[1] Handbook › Leaf (§4, pp. 3–6)")


def test_token_budget_is_respected():
    long = " ".join(f"Sentence {i} of a very long section." for i in range(400))
    results = [_r(f"c{i}", f"s{i}", str(i), i, long, 1.0 - i * 0.1) for i in range(5)]
    built = build_context(results, budget_tokens=600)
    assert count_tokens(built.text) <= 650
    assert built.groups and len(built.citations) == len(built.groups) <= 2


def test_used_citations_drops_unknown_markers():
    text, used = used_citations("Leave is 16 weeks [2]. Travel is economy [1][7]. See [2, 3] and `arr[0]`.", {1, 2, 3})
    assert used == [2, 1, 3]
    assert "[7]" not in text and "[1]." in text and "`arr[0]`" in text
    assert "[2][3]" in text
