# Ren — by f69 Labs

**A world of agents, working for you.** Ren is a deployable agentic-swarm orchestrator for organizations (or a single
person). You talk to **Ren**, the orchestrator, in chat. Ren delegates work to a swarm of specialist agents —
research, data, finance, writing, operations, engineering, sales, legal, knowledge — who run in the background with real
tools, while you keep chatting. Ren tracks everything in flight and reports back proactively.

```
            ┌────────────────────────── Next.js app (f69 beige · dark toggle) ───────────────────────────┐
            │ Chat · Tasks · Approvals · Agents · Skills · Knowledge · Connectors · Policies · Team · …   │
            └──────────────┬───────────────────────── REST + SSE ──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────── FastAPI / Uvicorn ──────────────────────────────────────────┐
│  Orchestrator hub ── one inbox per conversation; never blocks the user; live task board; proactive reports         │
│       │ delegate_task / message_task / cancel / search_knowledge / remember / run_skill / schedule                 │
│  Task manager ── concurrent worker agents (checkpointed, resumable, pause/cancel/retry, inbox, sub-delegation)      │
│       │                                                                                                            │
│  Tool executor ── validate → role scope → policy pre-check (outbound) → risk-based approval → run → audit          │
│       │                                                                                                            │
│  ┌────┴───────────────┬───────────────────┬──────────────────────┬─────────────────────┬────────────────────────┐ │
│  │ ~250 built-in tools │ external MCP      │ STAIR hybrid RAG     │ Policy guard        │ Memory / skills /       │ │
│  │ (files, shell, code,│ servers (stdio /  │ ToC-aware retrieval, │ rules → Laya (fast) │ scheduler / approvals / │ │
│  │ browser, docs, data,│ HTTP / SSE)       │ BM25 + vectors +     │ → LLM judge (only   │ audit / notifications   │ │
│  │ email, calendar, …) │ + Ren's own /mcp  │ rerank + citations   │ when uncertain)     │                         │ │
│  └─────────────────────┴───────────────────┴──────────────────────┴─────────────────────┴────────────────────────┘ │
└──────────────┬──────────────────────────┬───────────────────────────┬──────────────────────────────────────────────┘
               │                          │                           │
     PostgreSQL + pgvector          Sandbox service              Laya service
   (relational + vectors + FTS)   (isolated shell/code)     (calibrated policy checks)
```

## Highlights

| | |
|---|---|
| **Always-responsive orchestration** | Messages are never blocked. Ren answers quick things directly and delegates real work to specialists running in parallel. Every turn sees a live task board, pending approvals and memories; finished/failed work is reported proactively, batched with whatever you just asked. Change requirements mid-flight (`message_task`), pause, cancel, retry. |
| **Swarm of specialists** | 10 business agents (5 in personal mode) with tuned personas, tool grants, autonomy levels and per-agent models/providers. Agents delegate to each other (depth-limited) and wait for results. |
| **STAIR hybrid RAG** | Implements IBM's *STAIR* (arXiv 2609.03874): documents become a Table-of-Contents tree; an LLM selects the **leaf section** that answers the query with **constrained decoding** over valid leaf ids (vLLM guided choice / JSON-schema enum + snap-to-valid ⇒ no hallucinated targets). Fused (RRF) with BM25, vectors and synthetic-question matching, then cross-encoder reranking and a citation-aware context builder. Exports STAIR fine-tuning data. |
| **Cowork-class tools (MCP)** | Filesystem, shell, code, git, browser & computer-use, web search, Word/Excel/PowerPoint/PDF, analytics (DuckDB, charts), databases, email, calendar, drive, Slack/Teams, GitHub, Jira/Linear/Asana, Notion/Confluence, HubSpot/Salesforce, AWS/Azure/GCP, docker/k8s, vision, audio, notifications, memory, skills, approvals, scheduling. Plus any **external MCP server**, and Ren **exposes its own governed toolbox as an MCP server** at `/mcp`. |
| **Governance built in** | Roles (owner/admin/member/viewer), risk-based human approvals (supervised/balanced/autonomous), policy checks on every answer and on outbound content *before* it leaves (emails, messages), full audit trail, credential broker (agents never see secrets), sandboxed execution, SSRF guard. |
| **Alignment with Laya** | `convaiinnovations/laya` — a non-generative "System 1" decision model (~33 ms GPU) — scores every atomic policy rule with calibrated probabilities in one forward pass. Uncertain cases escalate to an LLM judge. Judged checks export as Laya fine-tuning data. |
| **Business UX** | Overview dashboard, live swarm panel, approvals queue, retrieval playground, structure explorer, skills library (25 playbooks), team management, activity log. |

## Quick start (local development)

Requirements: Python 3.11+, Node 20+ (24 recommended). Optional: Docker for Postgres/Laya.

```bash
# 1) Backend
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows (Git Bash: source .venv/Scripts/activate)
pip install -e ".[dev]"
cp .env.example .env                                  # add OPENROUTER_API_KEY or VLLM_BASE_URL (+ VLLM_MODEL)
python run.py --port 8000                             # http://localhost:8000/docs

# 2) Frontend
cd ../frontend
npm install
cp .env.example .env.local                            # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev                                           # http://localhost:3000
```

Open http://localhost:3000, create a workspace (**Business** or **Personal**), connect a model provider (OpenRouter key,
or a vLLM base URL), and start chatting. SQLite is used by default — no database setup needed.

> **OpenRouter free models** (e.g. `google/gemma-4-26b-a4b-it:free`): set `OPENROUTER_FALLBACK_MODELS` (comma list) so
> requests fall through to another model when the free pool is rate-limited upstream — Ren also backs off on 429s.
> Free-tier accounts have small daily request caps, and an agent swarm makes several calls per request.
>
> **vLLM tips:** serve a tool-calling model with `--enable-auto-tool-choice --tool-call-parser <parser>` (e.g. `hermes`
> for Qwen). STAIR uses vLLM's guided decoding automatically. Models without native tool calling still work: untick
> "supports tools" on the provider and Ren falls back to prompted tool calls.

## Production deployment (Docker Compose)

```bash
cp .env.example .env        # set SECRET_KEY, SANDBOX_TOKEN and a provider key
docker compose up -d --build
# GPU for Laya: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Open **http://localhost** — by default (`DOMAIN=:80`) Caddy serves plain HTTP on port 80 for any host, so localhost,
127.0.0.1 or a LAN IP all work with no certificate warnings. On a server, set `DOMAIN=ren.example.com` and
`PUBLIC_URL=https://ren.example.com` (DNS pointed at the server) and Caddy obtains TLS certificates automatically.
The frontend calls the API on its own origin, so changing the address needs no frontend rebuild. If port 80 is
taken (e.g. by Apache or IIS on the host or inside WSL), `http://localhost` will hit that server instead — stop it.

Services: `db` (PostgreSQL 17 + pgvector), `redis`, `laya` (alignment checker), `sandbox` (isolated execution),
`backend` (FastAPI/Uvicorn + agent runtime), `frontend` (Next.js standalone), `caddy` (reverse proxy, automatic HTTPS on
a real domain; `/api` and `/mcp` proxied with streaming enabled). Run a single backend replica — it hosts the agent runtime and resumes interrupted
tasks (and their pending approvals) after restarts.

**Why PostgreSQL + pgvector?** One store for relational data (orgs, tasks, approvals, audit), vector search (HNSW over
chunk/question/memory embeddings) and full-text search (a generated `tsvector` column with GIN index for the BM25-style
lexical retriever) — transactional consistency across all of it and no extra infrastructure. SQLite remains available
for zero-ops single-user installs (numpy vectors + in-process BM25).

## How it works

### Orchestration (`backend/app/agents/`)
- `orchestrator.py` — per-conversation inbox + serialized short turns. Posting returns `202` immediately. Turns stream
  tokens over SSE; tool calls (delegation, knowledge search, memory, skills, scheduling) are capped per turn. Task
  completions enqueue a trigger, so Ren reports proactively and coalesces bursts.
- `task_manager.py` / `runtime.py` — worker agents run as asyncio tasks with per-org concurrency limits, checkpoint
  their message history after every step, honour pause/cancel between steps, read their inbox (messages that arrive
  mid-task), ask the user questions (`ask_user`), and policy-check their final answer (with automatic revision).
- `approvals.py` — durable human-in-the-loop: risky calls suspend until approved/rejected in chat or the queue.

### Knowledge (`backend/app/rag/`)
Upload → parse (PDF outline/heading inference, DOCX styles, Markdown, HTML, TXT numbering, XLSX/CSV, PPTX) → ToC tree
(every passage lives in exactly one leaf; LLM *structure augmentation* titles unstructured docs) → contextual chunks →
embeddings → synthetic questions per leaf. Query time: BM25 + vectors + question matches + **STAIR leaf selection**
→ RRF → scoped boost → rerank → context with `[n]` citations (breadcrumb + section path). See the Retrieval Playground.

### Policy & alignment (`backend/app/policy/`)
Policies are lists of short atomic rules (plus optional regex/macros like `@pii:credit_card`, `@secret:api_key`).
Each rule becomes one Laya `choice` question; long texts are segmented. Verdict thresholds (`block`/`warn`) are
configurable per workspace; `post` mode streams then revises, `pre` mode checks before showing.

### Tools & MCP (`backend/app/tools/`)
`base.py` derives JSON schemas from Python signatures; `executor.py` enforces governance; `builtin/*` hold the tool
groups; `mcp_client.py` connects external MCP servers (presets: Playwright, GitHub, Notion, Linear, Atlassian, Sentry,
Stripe, AWS, Azure, Laya…); `mcp_server.py` serves Ren's tools at `/mcp` (Streamable HTTP; `Authorization: Bearer
<token>` + `X-Org-Id`).

## Configuration
All backend settings are environment variables — see [`backend/.env.example`](backend/.env.example) (local) and
[`.env.example`](.env.example) (compose). Workspace-level settings (default provider, orchestrator model, autonomy,
retrieval and alignment thresholds, web search) live in **Settings** in the app.

## API
Contract: [`docs/API.md`](docs/API.md) · interactive docs at `/docs` · real-time events at `GET /api/stream` (SSE).

## Tests
```bash
cd backend && .venv/Scripts/python.exe -m pytest -q      # offline: mock LLM + hashing embeddings
cd frontend && npm run build
```

## Security model
- **Identity**: bcrypt passwords, signed JWTs, login throttling, workspace membership on every request.
- **Authorization**: viewer → read-only tools; member → write tools; admin → admin-scope tools and governance.
- **Human approval** by risk × agent autonomy; outbound content is policy-checked before execution.
- **Secrets**: provider keys and connector credentials encrypted at rest (Fernet); never returned to the UI or agents.
- **Isolation**: per-user workspaces with path-escape protection; shell/code in a separate sandbox container;
  web tools block private-network targets by default.
- **Audit**: every medium+ risk action, approval, policy change and admin operation is logged.

---
© f69 Labs. Ren is a product of f69 Labs.
