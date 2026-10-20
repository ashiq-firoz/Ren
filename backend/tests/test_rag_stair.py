"""STAIR: ToC rendering, constrained leaf selection (single / multi-doc / beam), never-invalid ids."""
import json

import httpx

from app.llm.client import LLMClient
from app.llm.mock import MockLLM
from app.rag import stair
from app.rag.parsers import Block, ParsedDocument
from app.rag.structure import build_structure
from app.rag.textutil import count_tokens


async def _toc(doc_id: str, title: str, outline: list[tuple[int, str, str | None]]) -> stair.DocToc:
    blocks = []
    for level, heading, text in outline:
        blocks.append(Block("heading", heading, level))
        if text:
            blocks.append(Block("text", text))
    s = await build_structure(ParsedDocument(title=title, blocks=blocks, format="markdown"))
    return stair.toc_from_nodes(doc_id, s.title, s.nodes())


HANDBOOK = [
    (1, "Leave Policy", None),
    (2, "Annual Leave", "Employees receive 25 days of paid annual leave per calendar year."),
    (2, "Parental Leave", "Primary caregivers receive sixteen weeks of fully paid parental leave."),
    (1, "Expenses", None),
    (2, "Travel Expenses", "Economy flights are reimbursed for trips under six hours."),
    (2, "Meal Allowance", "The daily meal allowance while travelling is sixty euros."),
]
SECURITY = [
    (1, "Passwords", "Passwords need fourteen characters and a password manager."),
    (1, "Devices", "Laptops must use full disk encryption; report lost devices within an hour."),
    (1, "Visitors", "Visitors sign in at reception and wear a badge."),
]


async def test_render_toc_marks_leaves_and_indents():
    toc = await _toc("d1", "Handbook", HANDBOOK)
    assert stair.render_toc(toc) == (
        "1 Leave Policy\n  • 1.1 Annual Leave\n  • 1.2 Parental Leave\n"
        "2 Expenses\n  • 2.1 Travel Expenses\n  • 2.2 Meal Allowance")
    toc.items[1].summary = "Twenty-five days per year."
    assert "• 1.1 Annual Leave — Twenty-five days per year." in stair.render_toc(toc)
    assert "Twenty-five" not in stair.render_toc(toc, summaries=False)


async def test_single_document_selection_with_mock():
    toc = await _toc("d1", "Handbook", HANDBOOK)
    out = await stair.select_leaves(MockLLM(), "How many weeks of parental leave do primary caregivers get?",
                                    [toc], k=2)
    assert out.method == "single:mock" and out.llm_calls == 1 and out.error is None
    assert out.selections[0].path == "1.2" and out.selections[0].title == "Parental Leave"
    assert out.selections[0].breadcrumb == "Handbook › Leave Policy › Parental Leave"
    assert out.selections[0].confidence > (out.selections[1].confidence if len(out.selections) > 1 else 0)
    valid = {it.path for it in toc.leaves}
    assert all(s.path in valid for s in out.selections)
    assert out.candidates_considered == len(toc.leaves)


async def test_multi_document_mode_maps_ids_back():
    hb = await _toc("doc-handbook", "Handbook", HANDBOOK)
    sec = await _toc("doc-security", "Security Policy", SECURITY)
    text, idmap = stair.render_multi([hb, sec], summaries=True)
    assert "[D2] Security Policy" in text and "• D2:2 Devices" in text and "D1:1.2" in idmap
    out = await stair.select_leaves(MockLLM(), "laptop disk encryption for devices", [hb, sec], k=2)
    assert out.method.startswith("multi_doc")
    top = out.selections[0]
    assert (top.document_id, top.path, top.title) == ("doc-security", "2", "Devices")
    assert out.documents_considered == 2


async def test_hierarchical_beam_descent_for_large_tocs():
    chapters = {
        "Leave and Time Off Arrangements": ["Annual Vacation Entitlement", "Sickness Absence Reporting",
                                            "Parental Leave Rules", "Unpaid Sabbatical Requests",
                                            "Public Holiday Calendar", "Jury Service Absence", "Bereavement Days",
                                            "Volunteering Time"],
        "Compensation and Benefits Package": ["Pension Plan Contributions", "Private Health Insurance Coverage",
                                              "Company Car Scheme", "Annual Bonus Calculation",
                                              "Stock Option Vesting", "Salary Review Cycle", "Overtime Payments",
                                              "Relocation Support"],
        "Workplace Conduct and Ethics": ["Gifts and Hospitality Rules", "Conflicts of Interest Disclosure",
                                         "Harassment Reporting Procedure", "Social Media Guidelines",
                                         "Political Activity Restrictions", "Dress Code Expectations",
                                         "Alcohol and Drugs", "Whistleblowing Channel"],
        "Information Security Duties": ["Password Requirements", "Laptop Encryption", "Phishing Awareness",
                                        "Clean Desk Rule", "Removable Media", "Incident Reporting",
                                        "Remote Access", "Data Classification"],
    }
    outline = []
    for ch, secs in chapters.items():
        outline.append((1, ch, None))
        outline += [(2, s, f"Details about {s.lower()} for every employee.") for s in secs]
    toc = await _toc("big", "Employee Handbook", outline)
    top_level = "\n".join(stair._beam_line(toc, kid) for kid in toc.children[None])
    budget = count_tokens(top_level) + 10
    assert count_tokens(stair.render_toc(toc)) > budget  # the full ToC does not fit → beam descent
    out = await stair.select_leaves(MockLLM(), "Does private health insurance cover my spouse?", [toc], k=1,
                                    budget_tokens=budget)
    assert out.method.startswith("beam"), out.method
    assert out.llm_calls == 2  # top level (with child-title hints), then the leaves of the chosen chapter
    assert [(s.path, s.title) for s in out.selections] == [("2.2", "Private Health Insurance Coverage")]
    assert out.selections[0].breadcrumb == ("Employee Handbook › Compensation and Benefits Package › "
                                            "Private Health Insurance Coverage")


def _mock_http(monkeypatch, handler):
    import app.llm.client as client_mod

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_mod, "http_client", lambda: client)
    return client


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


async def test_constrained_output_never_returns_invalid_ids(monkeypatch):
    toc = await _toc("d1", "Handbook", HANDBOOK)
    valid = {it.path for it in toc.leaves}
    calls = []

    def hallucinating(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _completion(json.dumps({"choices": ["9.9.9", "banana", "1.2", "2"]}))

    client = _mock_http(monkeypatch, hallucinating)
    llm = LLMClient(kind="openai", base_url="http://llm.test/v1", api_key="k", model="m")
    out = await stair.select_leaves(llm, "parental leave weeks", [toc], k=3)
    paths = [s.path for s in out.selections]
    assert paths[0] == "1.2" and "9.9.9" not in paths
    assert all(p in valid for p in paths) and out.invalid_outputs == 0  # "2" is snapped onto a valid leaf
    schema = calls[0]["response_format"]["json_schema"]["schema"]  # JSON-schema enum over valid leaf ids
    assert set(schema["properties"]["choices"]["items"]["enum"]) == valid
    assert out.method == "single:json_schema"

    def garbage(request: httpx.Request) -> httpx.Response:
        return _completion("I think the answer is section 42.7 about holidays")

    await client.aclose()
    client = _mock_http(monkeypatch, garbage)
    out = await stair.select_leaves(llm, "parental leave weeks", [toc], k=2)
    assert out.selections == []  # nothing valid → nothing selected (never a hallucinated id)
    await client.aclose()


async def test_vllm_guided_decoding_payload(monkeypatch):
    toc = await _toc("d1", "Handbook", HANDBOOK)
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        return _completion("2.2")

    client = _mock_http(monkeypatch, handler)
    LLMClient._guided_style_cache.clear()
    llm = LLMClient(kind="vllm", base_url="http://vllm.test/v1", api_key=None, model="stair-ft")
    out = await stair.select_leaves(llm, "meal allowance per day", [toc], k=1, finetuned=True)
    assert [s.path for s in out.selections] == ["2.2"]
    assert payloads[0]["structured_outputs"] == {"choice": sorted({it.path for it in toc.leaves},
                                                                  key=[it.path for it in toc.leaves].index)}
    # fine-tuned retriever prompt == training format (system + "QUERY: …\n\nTABLE OF CONTENTS:\n…")
    msgs = payloads[0]["messages"]
    assert msgs[0]["content"] == stair.STAIR_SYSTEM
    assert msgs[1]["content"] == f"QUERY: meal allowance per day\n\nTABLE OF CONTENTS:\n{stair.render_toc(toc)}"
    await client.aclose()


def test_training_messages_format():
    msgs = stair.training_messages("How long is parental leave?", "• 1 Leave", "1")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert msgs[1]["content"].startswith("QUERY: How long is parental leave?\n\nTABLE OF CONTENTS:\n")
    assert msgs[2]["content"] == "1"


async def test_no_llm_or_no_documents():
    toc = await _toc("d1", "Handbook", HANDBOOK)
    out = await stair.select_leaves(None, "anything", [toc])
    assert out.method == "unavailable" and out.selections == []
    out = await stair.select_leaves(MockLLM(), "anything", [])
    assert out.method == "no_documents"
