# Laya — Ren's policy-alignment checker

[Laya](https://huggingface.co/convaiinnovations/laya) (Apache-2.0, Convai Innovations) is a non-autoregressive
"System 1" decision model: given a *state* (the agent's response) and typed questions, it returns calibrated
probabilities in a single forward pass (~33 ms on a T4 GPU, ~0.2–0.6 s on CPU). It never generates text.

## How Ren uses it
Every policy is a list of short atomic rules. For each rule Ren asks Laya two neutrally-keyed `choice` questions
("compliant vs. violation" phrasings, averaged), over ~300-token segments of the response, in one batched call.

- **Triage mode (default)** — Laya screens: if every rule scores below `escalate_threshold` (0.5) the response passes
  immediately with no LLM call; suspicious responses go to the LLM judge, which confirms and attributes violations.
  This matches Laya's documented behaviour: strong at separating clearly-fine from clearly-problematic text zero-shot,
  weaker at fine-grained, per-rule attribution.
- **Authoritative mode** — Laya's probabilities decide directly (block ≥ `block_threshold`, warn ≥ `warn_threshold`).
  Use this after fine-tuning Laya on your own policies: download *Policies → Export Laya training data*
  (`GET /api/alignment/training-data`, judged checks as labelled examples) and follow Laya's fine-tuning notebook.
- Deterministic regex rules (e.g. `@secret:api_key`, `@pii:credit_card`) run first and always apply.

## Run it
**Docker** (used by `docker-compose.yml`): `docker compose up -d laya` — CPU by default; GPU via `docker-compose.gpu.yml`.

**Local (no Docker):**
```bash
cd services/laya
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe "laya[serve]" --torch-backend=cpu   # or --torch-backend=auto for CUDA
LAYA_HOST=127.0.0.1 LAYA_PORT=8001 LAYA_PRELOAD=1 LAYA_MODELS=english .venv/Scripts/laya-serve
```
Then set `LAYA_URL=http://127.0.0.1:8001` in `backend/.env`. The first start downloads ~800 MB of weights.

Check it: `curl http://127.0.0.1:8001/health` · In Ren: *Policies* page shows Laya status and p50 latency.
