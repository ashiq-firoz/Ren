"""Benchmark scoring: retrieval maths, classification, CRAG outcomes, refusal/yes-no detection, DynaBench rule
parsing and the BFCL AST checker."""
import pytest

from bench.align import parse_rules
from bench.metrics import (
    best_threshold,
    binary_metrics,
    crag_scores,
    is_abstention,
    normalize_answer,
    retrieval_metrics,
    roc_auc,
    yes_no,
)
from bench.tools_bfcl import check, to_tools, value_ok


def test_retrieval_metrics():
    m = retrieval_metrics(["x", "a", "y", "b"], {"a", "b"}, ks=(1, 3, 5))
    assert m["recall@1"] == 0 and m["recall@3"] == 0.5 and m["recall@5"] == 1.0
    assert m["hit@1"] == 0 and m["hit@3"] == 1 and m["all@3"] == 0 and m["all@5"] == 1
    assert m["mrr"] == 0.5
    assert m["ap@5"] == pytest.approx((1 / 2 + 2 / 4) / 2)
    perfect = retrieval_metrics(["a", "b"], {"a", "b"}, ks=(2,))
    assert perfect["ndcg@2"] == pytest.approx(1.0) and perfect["mrr"] == 1.0
    assert retrieval_metrics(["a"], set()) == {}


def test_classification_metrics():
    m = binary_metrics([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 1)
    assert m["precision"] == pytest.approx(2 / 3) and m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3) and m["false_positive_rate"] == 0.5
    assert roc_auc([1, 1, 0, 0], [0.9, 0.8, 0.3, 0.1]) == 1.0
    assert roc_auc([1, 0], [0.5, 0.5]) == 0.5
    assert roc_auc([1, 1], [0.2, 0.3]) is None
    t, best = best_threshold([1, 1, 0, 0], [0.9, 0.6, 0.55, 0.1])
    assert best["f1"] == 1.0 and 0.55 < t <= 0.6


def test_crag_scores():
    s = crag_scores(["correct", "correct", "incorrect", "missing"])
    assert s == {"n": 4, "accuracy": 0.5, "hallucination": 0.25, "missing": 0.25, "truthfulness": 0.25}
    assert crag_scores([]) == {"n": 0}


def test_answer_helpers():
    assert normalize_answer("The Sam Bankman-Fried.") == "sam bankmanfried"
    for text in ("The knowledge base does not cover this question.",
                 "I couldn't find information about this in the knowledge base.",
                 "Insufficient information.",
                 "The provided sources don't mention who won.",
                 "There is no information about the merger in the sources."):
        assert is_abstention(text), text
    for text in ("Sam Bankman-Fried was accused of fraud [1][2].", "Yes — both reported revenue growth [1]."):
        assert not is_abstention(text), text
    assert yes_no("Yes, both articles report it [1].") == "yes"
    assert yes_no("No. The Verge reported a decline.") == "no"
    assert yes_no("Both reported higher revenue.") == "yes"
    assert yes_no("Sam Altman left OpenAI.") is None


def test_parse_dynabench_rules():
    policy = ('1. Never use emojis. 2. If the user mentions steel grade 316, include the word "marine" in your '
              'response. 3. Use v2. syntax only when asked. 4. Quote prices with 2.5% tax.')
    rules = parse_rules(policy)
    assert len(rules) == 4
    assert rules[0] == "Never use emojis."
    assert rules[2] == "Use v2. syntax only when asked."
    assert parse_rules("Always be polite.") == ["Always be polite."]


def test_bfcl_checker():
    fn = [{"name": "math.factorial", "description": "n!", "parameters": {
        "type": "dict", "properties": {"number": {"type": "integer"}, "mode": {"type": "string"},
                                       "ratio": {"type": "float"}, "pair": {"type": "tuple", "items": {"type": "integer"}}},
        "required": ["number"]}}]
    tools, names = to_tools(fn)
    params = tools[0]["function"]["parameters"]
    assert tools[0]["function"]["name"] == "math_factorial" and names["math_factorial"] == "math.factorial"
    assert params["type"] == "object" and params["properties"]["ratio"]["type"] == "number"
    assert params["properties"]["pair"]["type"] == "array"

    gt = [{"math.factorial": {"number": [5], "mode": ["exact", ""], "ratio": [0.5, ""]}}]
    assert check("simple", [("math.factorial", {"number": 5})], gt)[0]
    assert check("simple", [("math.factorial", {"number": 5, "mode": "EXACT"})], gt)[0]  # case-insensitive
    assert check("simple", [("math.factorial", {"number": 5.0})], gt)[0]  # int/float
    assert not check("simple", [("math.factorial", {"number": 6})], gt)[0]
    assert not check("simple", [("math.factorial", {"number": 5, "extra": 1})], gt)[0]  # hallucinated param
    assert not check("simple", [("math.factorial", {})], gt)[0]  # missing required
    assert not check("simple", [], gt)[0]

    par = [{"f": {"x": [1]}}, {"f": {"x": [2]}}]
    assert check("parallel", [("f", {"x": 2}), ("f", {"x": 1})], par)[0]  # order-independent
    assert not check("parallel", [("f", {"x": 2}), ("f", {"x": 2})], par)[0]
    assert check("irrelevance", [], [])[0] and not check("irrelevance", [("f", {})], [])[0]
    assert value_ok("New York, NY", ["new york ny"]) and value_ok([1, 2], [[1, 2]]) and not value_ok([2, 1], [[1, 2]])


def test_offline_fact_coverage():
    from bench.offline import _words, coverage, fact_in_context

    ctx = _words("[1] OpenAI. The board fired Sam Altman on Friday,\nciting a lack of candor. [2] Markets fell.")
    shingles = {tuple(ctx[i:i + 5]) for i in range(len(ctx) - 4)}
    text = " ".join(ctx)
    assert fact_in_context("The board fired Sam Altman on Friday, citing a lack of candor.", text, shingles)
    assert not fact_in_context("Microsoft hired Sam Altman to lead a new research team.", text, shingles)
    assert not fact_in_context("", text, shingles)
    rows = [{"facts": 2, "facts_in_context": 2}, {"facts": 3, "facts_in_context": 1}, {"facts": 2, "facts_in_context": 0},
            {"facts": 4, "facts_in_context": 4}]
    c = coverage(rows, "facts_in_context", "facts")
    assert c == {"n": 4, "full": 0.5, "partial": 0.25, "none": 0.25}
