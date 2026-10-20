"""Rank fusion: weighted Reciprocal Rank Fusion (Cormack et al., k = 60) + STAIR scoped boost."""
from __future__ import annotations

from collections import defaultdict

RRF_K = 60
DEFAULT_WEIGHTS: dict[str, float] = {"bm25": 1.0, "vector": 1.0, "questions": 0.7, "stair": 1.0}


def rrf(rankings: dict[str, list[str]], *, k: int = RRF_K, weights: dict[str, float] | None = None
        ) -> list[tuple[str, float]]:
    """Fuse ranked id lists: score(d) = Σ_s w_s / (k + rank_s(d)) (1-based ranks). Best first; ties keep the order
    of first appearance (stable)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    scores: dict[str, float] = defaultdict(float)
    first_seen: dict[str, int] = {}
    n = 0
    for source, ids in rankings.items():
        ws = w.get(source, 1.0)
        seen: set[str] = set()
        for rank, id_ in enumerate(ids, start=1):
            if id_ in seen:  # a source contributes once per id (its best rank)
                continue
            seen.add(id_)
            scores[id_] += ws / (k + rank)
            if id_ not in first_seen:
                first_seen[id_] = n
                n += 1
    return sorted(scores.items(), key=lambda x: (-x[1], first_seen[x[0]]))


def source_ranks(rankings: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """id → {source: best 1-based rank}."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for source, ids in rankings.items():
        for rank, id_ in enumerate(ids, start=1):
            out[id_].setdefault(source, rank)
    return out


def max_score(rankings: dict[str, list[str]], *, k: int = RRF_K, weights: dict[str, float] | None = None) -> float:
    """Score of an id ranked first by every non-empty source (used to scale fused scores to ~[0, 1])."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    return sum(w.get(s, 1.0) for s, ids in rankings.items() if ids) / (k + 1) or 1.0


def scoped_boost(fused: list[tuple[str, float]], chunk_section: dict[str, str | None],
                 section_confidence: dict[str, float], *, strength: float = 0.3) -> list[tuple[str, float]]:
    """Multiply the fused score of chunks inside STAIR-selected leaves by (1 + strength · confidence).

    Kept moderate on purpose: STAIR already contributes its own RRF term, so a confident, corroborated leaf rises
    to the top while an uncorroborated pick cannot by itself override BM25 + vector consensus."""
    if not section_confidence:
        return fused
    out = []
    for cid, s in fused:
        conf = section_confidence.get(chunk_section.get(cid) or "")
        out.append((cid, s * (1.0 + strength * conf) if conf is not None else s))
    order = {cid: i for i, (cid, _) in enumerate(fused)}
    return sorted(out, key=lambda x: (-x[1], order[x[0]]))
