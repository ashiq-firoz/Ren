# Ren benchmark: projected results (simulated)

> **This is a projection, not a measured run.** No model could be served for the benchmark: vLLM wasn't working on the L4, and the OpenRouter key is on the free tier. What *could* run without a model was **measured** on this machine with Ren's own pipeline: retrieval, and whether each question's evidence reaches the answer context. Everything that needs the model (answers, hallucination, the policy judge, tool calls) is **projected** from published results for the same model family, with every assumption listed below. Replace this file with `python -m bench run` results once a model is served.

Base model: **Qwen3.6-35B-A3B**, the suite's default (Q8 on one H200). **Qwen3.5-9B**, the one-L4 option, is shown alongside. Projected numbers are a mid estimate with a low–high range.

## Headline

| Benchmark | Metric | Qwen3.6-35B-A3B | Qwen3.5-9B | Basis |
|---|---|---|---|---|
| MultiHop-RAG retrieval | Recall@5 · MRR (document level) | 61.5% · 0.82 | same (no model involved) | **Measured**, LLM-free index: a floor |
| MultiHop-RAG retrieval | All evidence sentences in the answer context | 17.3% | same | **Measured** |
| MultiHop-RAG answers | Accuracy, all 2,556 questions (CRAG) | **52%** (43%–60%) | **48%** (38%–56%) | Projected from measured coverage |
| MultiHop-RAG answers | Hallucination rate (confident wrong answer) | **17%** (11%–25%) | **22%** (15%–30%) | Projected |
| MultiHop-RAG answers | Truthfulness (correct − hallucinated) | **+0.35** (+0.18 to +0.49) | **+0.26** (+0.08 to +0.41) | Projected |
| MultiHop-RAG null queries | Correctly declines (no answer exists) | **80%** (65%–90%) | **72%** (55%–85%) | Projected |
| DynaBench | Policy-judge F1 | **72** (66–77) | **64** (58–70) | Projected (DynaGuard paper) |
| BeaverTails | Safety-judge F1 | **80** (76–85) | **74** (69–80) | Projected (DynaGuard, WildGuard papers) |
| BFCL v3 non-live | AST accuracy (mean of 4 categories) | **93%** (88–95) | **92%** (86–94) | Projected (BFCL leaderboard) |
| BFCL v3 non-live | Irrelevance detection | **85%** (78–89) | **84%** (76–88) | Projected |

Laya alone and Ren's default triage mode (Laya screens, judge confirms) are **not** projected; see section 2.

## 1 · RAG: MultiHop-RAG (retrieval and hallucination)

MultiHop-RAG: 609 news articles and 2,556 questions. Each question needs 2–4 articles; 301 of them have no answer in the corpus.

### Measured: retrieval

- **Index:** 609/609 articles indexed (0 failed), built without an LLM: heading structure, chunks and bge-small embeddings, but no synthetic questions, LLM summaries or STAIR leaf selection. With a model, Ren adds those signals, so these numbers are a **floor**.
- **Search:** Ren's answer path, `rag.service.search` with hybrid BM25 + vectors → MiniLM cross-encoder rerank → top 8 chunks; reranked on 100% of queries.
- **Sample:** 1,053 answerable questions (inference 366, comparison 416, temporal 271), a seeded random sample of the 2,255 answerable ones. Errors: 0.

Document level: the distinct articles behind the 8 retrieved chunks, scored against the gold evidence articles.

| Questions | n | Recall@1 | Recall@3 | Recall@5 | Hit@5 | All evidence@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|---|---|---|
| All answerable | 1053 | 30.0% | 54.6% | 61.5% | 95.3% | 28.4% | 0.816 | 0.618 |
| Inference | 366 | 23.3% | 47.0% | 55.0% | 97.3% | 13.4% | 0.805 | 0.566 |
| Comparison | 416 | 34.5% | 59.1% | 65.7% | 93.5% | 39.4% | 0.818 | 0.649 |
| Temporal | 271 | 32.2% | 57.8% | 63.8% | 95.6% | 31.7% | 0.828 | 0.640 |

**Evidence in the answer context.** This bounds what any model can answer. *Articles*: the gold article is among the sources. *Sentences*: the exact evidence sentence the question was written from is in the text the model would read (MultiHop-RAG's evidence facts are verbatim article sentences).

| Questions | n | All articles | No article | All sentences | Some sentences | No sentence |
|---|---|---|---|---|---|---|
| All answerable | 1053 | 29.9% | 3.9% | 17.3% | 65.7% | 17.0% |
| Inference | 366 | 14.8% | 2.2% | 7.7% | 80.9% | 11.5% |
| Comparison | 416 | 40.6% | 5.5% | 25.0% | 59.6% | 15.4% |
| Temporal | 271 | 33.9% | 3.7% | 18.5% | 54.6% | 26.9% |
| Yes/no answers | 649 | 38.7% | 4.8% | 23.0% | 57.0% | 20.0% |
| Entity answers | 404 | 15.8% | 2.5% | 8.2% | 79.7% | 12.1% |
| Needs 2 articles | 562 | 48.0% | 3.4% | 30.2% | 55.5% | 14.2% |
| Needs 3 articles | 356 | 11.8% | 5.3% | 3.1% | 75.0% | 21.9% |
| Needs 4 articles | 135 | 2.2% | 2.2% | 0.7% | 83.7% | 15.6% |

Answer context: p50 1,725 words.

**Ablation** (top 20 chunks → distinct articles, same index):

| Retriever | n | Recall@1 | Recall@5 | Recall@10 | MRR | nDCG@10 | Mean latency |
|---|---|---|---|---|---|---|---|
| BM25 only | 200 | 32.4% | 72.7% | 81.3% | 0.862 | 0.751 | 0.04 s |
| Vectors only (bge-small) | 200 | 28.6% | 57.0% | 66.1% | 0.774 | 0.611 | 0.05 s |
| Hybrid (RRF), no rerank | 200 | 30.7% | 64.8% | 75.4% | 0.833 | 0.688 | 0.06 s |
| Hybrid + cross-encoder rerank (Ren) | 200 | 29.9% | 64.4% | 75.9% | 0.813 | 0.684 | 3.37 s |

Latency is this laptop's CPU, shared with Docker; the cross-encoder dominates it.

**Finding:** on this index, **BM25 only** beats the hybrid + rerank configuration Ren uses: Recall@5 72.7% vs 64.4%, MRR 0.862 vs 0.813. bge-small vectors are the weakest signal (Recall@5 57.0%), and the MiniLM cross-encoder adds nothing over plain fusion (Recall@5 64.8% without it). These questions name their sources and entities ("according to TechCrunch…"), which favours lexical matching. That leaves room in the retrieval stack, through a stronger embedder or reranker or a BM25-weighted fusion, and more evidence in context raises every answer projection below. With a model, synthetic questions and STAIR join the fusion, so recheck this on the full pipeline.

### Projected: answers and hallucination (CRAG scoring)

Each answer is scored *correct*, *hallucinated* (a confident wrong answer) or *declined*. Truthfulness = correct − hallucinated, so declining scores 0 and a wrong answer scores −1. On the 301 null questions, declining is the only correct answer.

**Method.** Every measured question falls into a class: all, some or none of its evidence sentences in the answer context. With only some, it matters whether the answer is an entity or a yes/no. Each class gets outcome rates for the model (the assumptions below), and each question contributes its class's rates. The measured coverage carries the pipeline-specific part; only the model's behaviour is assumed. Rates come from the 1,053 measured questions; the all-questions row weights them by the full set's 2,255 answerable and 301 null questions.

**Qwen3.6-35B-A3B**

| Questions | Correct | Hallucinated | Declined | Truthfulness |
|---|---|---|---|---|
| All 2,556 questions | **52%** (43%–60%) | **17%** (11%–25%) | **30%** (29%–31%) | **+0.35** (+0.18 to +0.49) |
| Answerable (2,255) | **49%** (41%–56%) | **17%** (11%–24%) | **35%** (32%–36%) | **+0.32** (+0.17 to +0.45) |
| Inference | **58%** (47%–67%) | **15%** (10%–22%) | **28%** (24%–31%) | **+0.43** (+0.25 to +0.57) |
| Comparison | **46%** (39%–53%) | **18%** (12%–25%) | **36%** (35%–36%) | **+0.28** (+0.14 to +0.42) |
| Temporal | **40%** (34%–47%) | **18%** (12%–25%) | **42%** | **+0.23** (+0.09 to +0.36) |
| Null (301, unanswerable) | **80%** (65%–90%) | **20%** (10%–35%) | — | **+0.60** (+0.30 to +0.80) |

**Qwen3.5-9B**

| Questions | Correct | Hallucinated | Declined | Truthfulness |
|---|---|---|---|---|
| All 2,556 questions | **48%** (38%–56%) | **22%** (15%–30%) | **31%** (29%–32%) | **+0.26** (+0.08 to +0.41) |
| Answerable (2,255) | **44%** (36%–52%) | **21%** (15%–28%) | **35%** (33%–36%) | **+0.24** (+0.08 to +0.38) |
| Inference | **52%** (42%–61%) | **19%** (13%–26%) | **29%** (26%–32%) | **+0.34** (+0.16 to +0.48) |
| Comparison | **42%** (35%–50%) | **22%** (16%–29%) | **36%** (35%–36%) | **+0.20** (+0.06 to +0.34) |
| Temporal | **37%** (30%–44%) | **22%** (15%–29%) | **41%** | **+0.15** (+0.01 to +0.28) |
| Null (301, unanswerable) | **72%** (55%–85%) | **28%** (15%–45%) | — | **+0.44** (+0.10 to +0.70) |

Measured class shares (from evidence sentences in context):

| Questions | n | All sentences | Some · entity answer | Some · yes/no answer | No sentence |
|---|---|---|---|---|---|
| All answerable | 1053 | 17.3% | 30.6% | 35.1% | 17.0% |
| Inference | 366 | 7.7% | 80.9% | 0.0% | 11.5% |
| Comparison | 416 | 25.0% | 2.2% | 57.5% | 15.4% |
| Temporal | 271 | 18.5% | 6.3% | 48.3% | 26.9% |

**Assumptions:** outcome rates, given as correct / hallucinated / declined for each class of evidence in context.

| Model | Scenario | All sentences | Some · entity answer | Some · yes/no answer | No sentence | Null: declines |
|---|---|---|---|---|---|---|
| Qwen3.6-35B-A3B | low | 80% / 14% / 6% | 50% / 22% / 28% | 30% / 30% / 40% | 5% / 25% / 70% | 65% |
|  | mid | 87% / 9% / 4% | 62% / 15% / 23% | 38% / 22% / 40% | 8% / 17% / 75% | 80% |
|  | high | 92% / 6% / 2% | 72% / 10% / 18% | 47% / 15% / 38% | 12% / 10% / 78% | 90% |
| Qwen3.5-9B | low | 74% / 19% / 7% | 44% / 26% / 30% | 26% / 34% / 40% | 4% / 29% / 67% | 55% |
|  | mid | 82% / 13% / 5% | 56% / 19% / 25% | 34% / 26% / 40% | 7% / 21% / 72% | 72% |
|  | high | 88% / 9% / 3% | 66% / 13% / 21% | 43% / 19% / 38% | 10% / 14% / 76% | 85% |

What these rest on:

- **All evidence present.** GPT-4 answered 89% of MultiHop-RAG correctly when handed the gold evidence. ChatGPT managed 57%, and Llama-2-70B 32% (MultiHop-RAG paper). Qwen3.6-35B-A3B is a far stronger 2026 model (GPQA 86.0 on its card), but Ren runs it with thinking off and its context also holds distractor chunks, so it is projected at GPT-4's level, not above.
- **Partial or no evidence.** Ren's answer prompt says to state that the knowledge base doesn't cover a question and never to guess, so most gaps become *declined*, not *hallucinated*. That doesn't happen every time: straightforward RAG with GPT-4 Turbo hallucinated on 25–30% of CRAG questions when retrieval was noisy. Comparison and temporal yes/no questions need both sides of the evidence, while entity questions can often be answered from one article.
- **Null questions.** These are written to look answerable from the corpus. The projection assumes modern instruction-tuned models, following Ren's grounding rules, decline 80% (35B) and 72% (9B) of the time.
- Grading in a real run is done by the same model (self-grading, constrained labels); exact entity and yes/no matches skip the judge.

## 2 · Alignment: the policy guard (projected)

Projected for Ren's **LLM judge on every response** (Laya off), the configuration the published numbers support:

| Benchmark | Qwen3.6-35B-A3B | Qwen3.5-9B |
|---|---|---|
| DynaBench (543 dialogues, user-defined policies) — F1 | **72** (66–77) | **64** (58–70) |
| BeaverTails 30k_test (600, balanced) — F1 | **80** (76–85) | **74** (69–80) |

What the projection is anchored on (F1, same test sets):

| Model | DynaBench | BeaverTails | Source |
|---|---|---|---|
| GPT-4o-mini (generalist judge) | 70.1 | 82.6 | DynaGuard paper, Table 3 |
| Qwen3-8B (generalist judge) | 60.7 | 71.1 | DynaGuard paper, Table 3 |
| DynaGuard-8B (fine-tuned guard, no CoT) | 72.5 | 83.6 | DynaGuard paper, Table 3 |
| LlamaGuard3 (safety guard) | 13.1 | 71.3 | DynaGuard paper, Table 3 |
| WildGuard (safety guard) | 20.9 | 83.5 | DynaGuard paper, Table 3 |
| GPT-4 | — | 86.1 | WildGuard paper |

- Qwen3.6-35B-A3B is a far stronger 2026 generalist than GPT-4o-mini (GPQA 86.0 on its model card), so it is
  projected at the fine-tuned DynaGuard-8B level on DynaBench rather than above it. Ren's judge runs with thinking off
  and sees the policy as numbered rules over the whole transcript.
- Qwen3.5-9B is projected a few points above Qwen3-8B, its predecessor.
- BeaverTails is sampled 50/50 safe/unsafe in this bench. The published test set is 57% unsafe, and a balanced set costs
  about 2 F1 points at the same recall and false-positive rate, so the projection subtracts 2.

**Not projected: Laya and triage** (Ren's default: Laya screens, the judge confirms). Laya publishes no zero-shot
results on safety or policy benchmarks. On this laptop's CPU it took 1–5 minutes per example, so the 1,143 examples
would take tens of hours. Triage can only flag what Laya escalates: its recall is at most Laya's recall at the 0.5
threshold, and in exchange it skips the judge for most responses. Both need a GPU run.

## 3 · Tool calling: BFCL v3 non-live AST (projected)

Ren passes tools to the model natively, so the relevant numbers are the BFCL leaderboard's FC (native function
calling) results. The anchor is Qwen3-30B-A3B-Instruct-2507, the predecessor in Qwen3.6-35B-A3B's line, which also
runs without thinking:

| Category (items) | Qwen3-30B-A3B-2507 FC | Qwen3-32B FC | Qwen3-8B FC | **Qwen3.6-35B-A3B (proj.)** | **Qwen3.5-9B (proj.)** |
|---|---|---|---|---|---|
| simple (400) | 94.75 | 94.75 | 95.50 | **95** | **95** |
| multiple (200) | 94.50 | 94.50 | 96.50 | **95** | **94** |
| parallel (200) | 91.50 | 93.50 | 92.00 | **92** | **91** |
| parallel_multiple (200) | 88.50 | 91.50 | 89.00 | **89** | **88** |
| AST summary (mean of 4) | 92.3 | 93.6 | 93.3 | **93** (88–95) | **92** (86–94) |
| irrelevance (240) | 84.58 | 80.00 | 81.67 | **85** (78–89) | **84** (76–88) |

The range allows for Ren's path: the serving engine's tool-call parser (vLLM `qwen3_coder`, Ollama), tool-name
sanitising and the bench's own AST checker all differ from BFCL's handlers. Non-live AST is near the ceiling for this
model family, so the 9B and 35B models land within noise of each other here. Harder agentic suites (BFCL-V4
multi-turn, τ-bench) separate them more.

## Reproduce, then replace with real numbers

Measured part (no model needed). On this laptop it takes about 35 minutes: ~8 minutes of indexing, then ~0.75 questions/s, dominated by the cross-encoder:

```bash
cd backend
python -m bench offline --suites rag --run-id offline-rag --rag-queries 1200 --ablation 200
```

Projection: `backend/bench_runs/runs/offline-rag/projection.py`, which has the assumption tables at the top and writes `projection.json`.

Real results, once a model is served (see `backend/bench/README.md`):

```bash
python -m bench check
python -m bench run --minutes 40 --run-id h200-qwen36
```

## Sources

- MultiHop-RAG (Tang & Yang, 2024): https://arxiv.org/abs/2401.15391
- CRAG: Comprehensive RAG Benchmark (Yang et al., NeurIPS 2024): https://arxiv.org/abs/2406.04744
- DynaGuard / DynaBench (Hoover et al., ICLR 2026), Table 3: https://arxiv.org/abs/2509.02563
- WildGuard (Han et al., 2024), BeaverTails F1 incl. GPT-4: https://arxiv.org/abs/2406.18495
- BFCL leaderboard, non-live categories (`data_non_live.csv`): https://gorilla.cs.berkeley.edu/leaderboard.html
- Qwen3.6-35B-A3B model card: https://huggingface.co/Qwen/Qwen3.6-35B-A3B
- Laya model card (no safety/policy results published): https://huggingface.co/convaiinnovations/laya
