"""Metrics: retrieval ranking (Recall/Hit/MRR/nDCG/AP@k), binary classification (P/R/F1, ROC-AUC, best threshold),
CRAG-style answer scoring and answer normalisation helpers."""
from __future__ import annotations

import math
import re
import string
from collections.abc import Iterable, Sequence

# ----------------------------------------------------------------------------------------- answers
_ARTICLES = re.compile(r"\b(a|an|the)\b", re.I)
_PUNCT = str.maketrans("", "", string.punctuation + "‘’“”–—")


def normalize_answer(text: str) -> str:
    """SQuAD-style: lowercase, drop punctuation and articles, squeeze whitespace."""
    t = (text or "").lower().translate(_PUNCT)
    return " ".join(_ARTICLES.sub(" ", t).split())


_ABSTAIN = re.compile(
    r"(insufficient information|not enough information|(does|do|did) not (cover|contain|mention|include|provide|say|"
    r"specify|state|address)|doesn'?t (cover|contain|mention|include|provide|say|specify|state|address)|"
    r"(no|not any) (information|mention|details?|data|evidence|record) (about|on|regarding|of|in|that)|"
    r"(could ?n[o']?t|can ?not|can'?t|cannot) (find|determine|answer|confirm|identify|verify|locate)|"
    r"unable to (find|determine|answer|confirm|"
    r"identify|verify)|cannot be (determined|answered|confirmed|found)|there is no (information|mention|evidence|"
    r"indication)|the (knowledge base|sources?|provided (sources|context|documents?)) (do(es)?n'?t|do(es)? not)|"
    r"not (found|covered|available) in the (knowledge base|sources?|provided))", re.I)


def is_abstention(text: str) -> bool:
    """The answer declines for lack of information (Ren's grounded answers say the knowledge base doesn't cover it)."""
    head = (text or "").strip()[:600]
    return bool(_ABSTAIN.search(head))


_YES = re.compile(r"^\W*(yes|yeah|yep|correct|true|indeed|both (do|did|are|were|have|report|reported))\b", re.I)
_NO = re.compile(r"^\W*(no|nope|incorrect|false|neither|not)\b", re.I)


def yes_no(text: str) -> str | None:
    """Polarity of a yes/no answer from its first words; None when it doesn't open with one."""
    t = (text or "").strip()
    if _YES.search(t):
        return "yes"
    if _NO.search(t):
        return "no"
    return None


# ----------------------------------------------------------------------------------------- retrieval
def dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def retrieval_metrics(ranked: Sequence[str], gold: set[str], ks: Sequence[int] = (1, 3, 5, 10)) -> dict:
    """Document-level metrics for one query. `ranked` = distinct doc ids in rank order."""
    out: dict[str, float] = {}
    if not gold:
        return out
    hits = [1 if d in gold else 0 for d in ranked]
    for k in ks:
        top = hits[:k]
        out[f"recall@{k}"] = sum(top) / len(gold)
        out[f"hit@{k}"] = float(any(top))
        out[f"all@{k}"] = float(sum(top) >= len(gold))
        dcg = sum(h / math.log2(i + 2) for i, h in enumerate(top))
        idcg = sum(1 / math.log2(i + 2) for i in range(min(k, len(gold))))
        out[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    first = next((i for i, h in enumerate(hits) if h), None)
    out["mrr"] = 1.0 / (first + 1) if first is not None else 0.0
    found, precisions = 0, []
    for i, h in enumerate(hits[: max(ks)]):
        if h:
            found += 1
            precisions.append(found / (i + 1))
    out[f"ap@{max(ks)}"] = sum(precisions) / min(len(gold), max(ks)) if precisions else 0.0
    return out


def average_dicts(rows: Iterable[dict]) -> dict:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sums[k] = sums.get(k, 0.0) + v
                counts[k] = counts.get(k, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


# ----------------------------------------------------------------------------------------- classification
def binary_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict:
    tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if not t and p)
    fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and not p)
    tn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if not t and not p)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n = len(y_true)
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1,
            "accuracy": (tp + tn) / n if n else 0.0, "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0}


def roc_auc(y_true: Sequence[int], scores: Sequence[float]) -> float | None:
    """Probability a random positive outscores a random negative (ties count half)."""
    pos = [s for t, s in zip(y_true, scores, strict=True) if t]
    neg = [s for t, s in zip(y_true, scores, strict=True) if not t]
    if not pos or not neg:
        return None
    ranked = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):  # average ranks over ties
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        rank_sum += avg * sum(1 for k in range(i, j + 1) if ranked[k][1])
        i = j + 1
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def best_threshold(y_true: Sequence[int], scores: Sequence[float]) -> tuple[float, dict]:
    """Threshold on `scores` with the best F1 (an oracle reference point, not a deployable setting)."""
    best_t, best = 0.5, binary_metrics(y_true, [int(s >= 0.5) for s in scores])
    for t in sorted(set(round(s, 3) for s in scores)):
        m = binary_metrics(y_true, [int(s >= t) for s in scores])
        if m["f1"] > best["f1"]:
            best_t, best = t, m
    return best_t, best


# ----------------------------------------------------------------------------------------- CRAG-style scoring
def crag_scores(outcomes: Sequence[str]) -> dict:
    """outcome per question: correct | incorrect (hallucinated) | missing (abstained). Truthfulness = acc − halluc."""
    n = len(outcomes)
    if not n:
        return {"n": 0}
    c = sum(1 for o in outcomes if o == "correct")
    i = sum(1 for o in outcomes if o == "incorrect")
    m = sum(1 for o in outcomes if o == "missing")
    return {"n": n, "accuracy": c / n, "hallucination": i / n, "missing": m / n, "truthfulness": (c - i) / n}
