# Ren benchmark suite

Benchmarks **Ren as a system**, not just a model. Every benchmark item goes through Ren's real pipelines, wired to the
model you serve. Scoring is done by `bench` itself.

| Suite | Benchmark | What it measures in Ren |
|---|---|---|
| `rag` *(required)* | **MultiHop-RAG**: 609 news articles, 2,556 multi-hop questions with gold evidence, 301 of them unanswerable | Ingestion (STAIR structure, synthetic questions, embeddings) → hybrid retrieval (BM25 + vectors + questions + STAIR) + cross-encoder rerank → grounded cited answers. Retrieval Recall@k / MRR / nDCG; **hallucination** with CRAG scoring (correct / hallucinated / declined, truthfulness = correct − hallucinated), unanswerable-question refusal rate, claim-level faithfulness; BM25 vs vector vs hybrid ablation |
| `align` *(required)* | **DynaBench** (DynaGuard, ICLR 2026): 543 dialogues with user-defined policies<br>**BeaverTails** 30k_test: 600 balanced safe/unsafe responses | The policy guard: **Laya** (fast zero-shot screen) and the **LLM judge**, scored as precision, recall, F1 and false-positive rate for: judge-only, Laya-only (plus AUC and best threshold), and Ren's default **triage** (Laya screens, judge confirms). Also latency and the share of judge calls triage avoids. A sample is re-run through the composed `check_text` pipeline as a cross-check. |
| `bfcl` *(model-dependent)* | **BFCL v3** non-live AST: simple, multiple, parallel, parallel_multiple, irrelevance (1,240 items) | Native tool calling through Ren's LLM client: the path every agent action takes |

Results land in `bench_runs/runs/<run-id>/`: `report.md`, `summary.json` and per-item `*.jsonl` files. Runs are
**resumable**: rerun with the same `--run-id` and finished items are skipped. Each suite works through its items in a
seeded random order, so a run cut short by `--minutes` is still a fair sample.

## Model: Qwen3.6-35B-A3B on one H200

35B MoE with 3B active. Use the **Q8 Ollama tag `qwen3.6:35b-a3b-mtp-q8_0`** (39 GB, near-lossless) — the default
`qwen3.6:35b` tag is 4-bit. It has a 256k context and strong native tool calling (τ³-bench 67.2, SWE-bench Verified
73.4%), and leaves most of the H200's 141 GB free for parallel requests. Larger models that fit one H200 only fit
quantised, leave little room for parallel requests, and take longer to download. To benchmark another model, pass
`--model`; the suite is model-agnostic.

## Runbook for the GPU hour (Ollama)

Run the harness **on the H200 machine**. Ren's reranker and embeddings are CPU-heavy, and a laptop CPU becomes the
bottleneck. Everything below runs on the H200 box; ports stay on localhost.

**0. Before the clock starts** (on your PC, in cmd or PowerShell from `backend\`): check the harness works offline, then
pack it up (about 6 MB, cached datasets included).
```powershell
.venv\Scripts\python -m bench prepare
.venv\Scripts\python -m bench run --provider mock --workdir bench_runs\dry --run-id dry --skip-preflight --rag-queries 12 --corpus-limit 30 --dynabench 8 --beavertails 8 --bfcl-per-category 3 --faithfulness 4 --ablation 4
tar -czf ..\ren-backend.tgz --exclude=.venv --exclude=./data --exclude=bench_runs --exclude=__pycache__ .
scp ..\ren-backend.tgz <h200>:
```

**1. Ollama: context window and parallelism are server settings.** This matters because Ollama **silently
truncates** prompts longer than its context window (4k by default). The benchmark prompts reach about 10k tokens: STAIR
tables of contents, RAG source context, and DynaBench's long policies. `bench check` fails if a 13k-token prompt comes
back truncated.
```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_NUM_PARALLEL=32 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KEEP_ALIVE=2h ollama serve
# second terminal
ollama pull qwen3.6:35b-a3b-mtp-q8_0
```
If Ollama runs as a systemd service, put those variables in `sudo systemctl edit ollama` (`[Service]` with
`Environment=OLLAMA_CONTEXT_LENGTH=32768`, and so on), then `sudo systemctl restart ollama`. If the model won't load
with 32 parallel slots, use `OLLAMA_NUM_PARALLEL=16`. Thinking is turned off per request (`reasoning_effort: none`);
add `--thinking` to keep it on.

**2. Laya** (another terminal; about 800 MB of weights; uses the GPU automatically):
```bash
python3.12 -m venv laya && . laya/bin/activate && pip install "laya[serve]"
LAYA_HOST=127.0.0.1 LAYA_PORT=8001 LAYA_PRELOAD=1 LAYA_MODELS=english laya-serve
```

**3. Harness** (another terminal; can run while the model downloads):
```bash
mkdir ren && tar -xzf ren-backend.tgz -C ren && cd ren
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e .
pip install fastembed-gpu   # optional: embeddings + reranker on the GPU (falls back to CPU if CUDA libs are missing)
python -m bench prepare
```

**4. Preflight, then run.** `--minutes` is the budget for the whole run; the suites run at the same time.
```bash
export BENCH_PROVIDER=ollama BENCH_MODEL=qwen3.6:35b-a3b-mtp-q8_0 BENCH_LAYA_URL=http://127.0.0.1:8001
python -m bench check          # chat · tool call · constrained choice · JSON · 13k context · Laya
python -m bench run --minutes 40 --run-id h200-qwen36
```
The report is written to `bench_runs/runs/h200-qwen36/report.md`; copy the run folder back with `scp -r`. If the run
stops early, rerun the same command and it resumes. With Ollama, indexing the 609 articles takes roughly 10–15
minutes. Everything else runs alongside it, and whatever the budget doesn't cover is reported as "not run".

**Using vLLM instead** (several times higher throughput for this many parallel requests, so more items fit in the
hour):
```bash
vllm serve Qwen/Qwen3.6-35B-A3B --served-model-name qwen3.6-35b-a3b --max-model-len 65536   --gpu-memory-utilization 0.80 --max-num-seqs 256 --enable-auto-tool-choice --tool-call-parser qwen3_coder   --reasoning-parser qwen3 --host 127.0.0.1 --port 8000
python -m bench run --provider vllm --minutes 40 --run-id h200-qwen36-vllm
```

### Smaller GPU: one L4 (24 GB)

Use **`Qwen/Qwen3.5-9B` with vLLM's on-the-fly FP8**:
- **Capability:** strong tool calling for its size (BFCL-V4 66.1, τ²-bench 79.1).
- **Memory:** about 13 GB on the GPU in text-only mode, so with vLLM capped at 80% there's still room for parallel
  requests and for Laya and the embedding models.
- **FP8:** runs natively on the L4.

Larger models (Qwen3.6-27B or -35B-A3B at 4-bit) barely fit and are about 3× slower to process prompts on the L4's
300 GB/s memory.
```bash
python3.12 -m venv ~/vllm-env && . ~/vllm-env/bin/activate && pip install -U pip "vllm>=0.19"
vllm serve Qwen/Qwen3.5-9B --served-model-name qwen3.5-9b --quantization fp8 --language-model-only \
  --max-model-len 32768 --max-num-seqs 32 --gpu-memory-utilization 0.80 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 \
  --host 127.0.0.1 --port 8000 --api-key "$BENCH_API_KEY"
```
An L4 processes prompts about 20× slower than an H200, so sample. Laya and the harness are set up as in steps 2–3;
the corpus index is built once and reused by later runs.
```bash
export BENCH_PROVIDER=vllm BENCH_MODEL=qwen3.5-9b BENCH_LAYA_URL=http://127.0.0.1:8001
python -m bench check
python -m bench run --minutes 55 --concurrency 16 --rag-queries 300 --faithfulness 100 --ablation 100 --run-id l4-qwen35-9b
```

**Running the harness from your PC instead**, through an SSH tunnel to the Ollama and Laya ports: the laptop reranks
about 2 questions per second, so sample with `--rag-queries 600 --ablation 100`.

## No model available: `python -m bench offline`
Measures the parts of Ren that need no LLM, on the same datasets and item order as `bench run`:
- **rag**: MultiHop-RAG retrieval over an index built without an LLM (heading structure, chunks and bge-small
  embeddings; no synthetic questions, LLM summaries or STAIR), through the answer path's search settings (hybrid
  BM25 + vectors → rerank → top-k). It also measures evidence coverage: how many of each question's gold articles,
  and the exact evidence sentences, reach the answer context.
- **align**: Laya alone on DynaBench and BeaverTails (`--laya-url`). Laya needs a GPU for this: on a laptop CPU one
  example takes minutes.
```bash
python -m bench offline --suites rag --run-id offline-rag --rag-queries 1200 --ablation 200
```
On a laptop CPU this takes about 35 minutes: indexing takes ~8 minutes, and the cross-encoder reranks ~0.75
questions/s. Leave out `--rag-queries` to run all 2,255 answerable questions.
It uses its own work dir (`bench_runs/work-offline`, a workspace with no model provider), so it never mixes with a
model run's index. Answer quality, hallucination, the LLM judge and tool calls still need `bench run`.

## Options worth knowing
- `--thinking`: turn on the model's thinking mode (Qwen's `enable_thinking`). It's off by default: faster, and Ren
  doesn't need it.
- `--suites rag,align,bfcl`: run a subset. `--rag-queries`, `--dynabench`, `--beavertails`, `--bfcl-per-category`
  cap the item counts; `--faithfulness N` and `--ablation N` set the sample sizes of those extras (0 = off).
- `--workdir`: the isolated Ren instance (SQLite DB, uploads, index). It's reused across runs, so later runs skip
  ingestion. Use a fresh one when you change models, so the index is built by the model under test.
- `python -m bench report bench_runs/runs/<id>`: rebuild `report.md` from `summary.json`.

## Caveats
- **Self-grading**: the LLM judge and the answer grader run on the model under test. Grading uses fixed labels
  (constrained decoding), exact entity, yes/no and refusal matches skip the judge, and the report shows how many items
  each path graded.
- **Laya** is zero-shot here (not fine-tuned on DynaBench or BeaverTails). Its best-threshold row is an oracle
  reference, not a deployable setting.
- **DynaBench** labels a whole transcript. Ren checks it in transcript mode: only agent turns are judged, with earlier
  turns as context.
- **BeaverTails** is CC-BY-NC-4.0 (research use). The other datasets are MIT, ODC-BY and Apache-2.0.
