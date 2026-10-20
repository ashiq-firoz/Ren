# Backend conventions (for everyone writing backend code)

## Environment
- Windows 11. Bash tool = Git Bash (POSIX syntax); PowerShell also available. Prefer absolute paths.
- Backend root: `E:\Agent World\backend`. Python venv: `E:\Agent World\backend\.venv` (Python 3.12).
  - Run Python: `"/e/Agent World/backend/.venv/Scripts/python.exe"` (from Git Bash) — run tests from the backend dir:
    `cd "/e/Agent World/backend" && .venv/Scripts/python.exe -m pytest tests/test_<area>*.py -q`
  - Add a dependency: `cd "/e/Agent World/backend" && uv pip install --python .venv/Scripts/python.exe <pkg>` **and** add it to
    `pyproject.toml` (`dependencies`, or `[project.optional-dependencies]` if heavy/optional). Never downgrade existing packages.
  - Installed (latest): fastapi, sqlalchemy 2.1 (async), aiosqlite, asyncpg, pgvector, pydantic 2, httpx, numpy, pandas 3,
    fastembed, tiktoken, pypdf, pdfplumber, python-docx, openpyxl, python-pptx, beautifulsoup4, markdown-it-py, fpdf2,
    matplotlib, pillow, duckdb, croniter, psycopg (v3), pymysql, ddgs, mcp (v2 SDK), pytest, pytest-asyncio.
- Several engineers work concurrently in this repo. **Only create/modify the files you own.** If you truly need a change in a
  shared/core file, make the smallest additive change and list it in your final report.
- Never start long-running servers in the foreground; never leave background processes running when you finish.
- Tests must not need internet access or real credentials: mock HTTP (`httpx.MockTransport` / monkeypatch). Optional live tests
  must be skipped unless an env var is set.

## Shared core (read before coding; owned by the lead — don't rewrite)
- `app/core/config.py` (`settings`), `app/core/db.py` (`session_scope()`, `Base`, `Vector`, `JSONType`, `new_id`, `utcnow`, `iso`, `IS_POSTGRES`),
  `app/core/security.py` (`encrypt_secret`, `decrypt_secret`, `encrypt_json`, `decrypt_json`), `app/core/events.py` (`bus.emit(org_id, type, data, user_id=..., admin_visible=...)`),
  `app/core/deps.py` (`Ctx`, `get_ctx`, `require_admin`, `require_member`, `get_current_user`, `get_db`).
- `app/models/*` — all ORM models (Organization, User, Membership, Provider, Agent, Policy, Skill, Conversation, Message, Task, TaskEvent,
  Approval, Artifact, Collection, Document, Section, Chunk, SectionQuestion, Memory, Connector, McpServer, ToolGroupSetting, AuditLog,
  Notification, AlignmentCheck, UsageRecord, ScheduledJob).
- `app/llm/client.py` — `LLMClient` with `chat()`, `stream()`, `choose(messages, choices, multi=1)` (constrained selection, never returns
  an invalid choice), `json(messages, schema)` (structured output), `embed()`. `app/llm/providers.py` — `get_llm(db, org_id, purpose=...)`.
  `app/llm/mock.py` — `MockLLM` (offline; used by tests via the mock provider created in `tests/conftest.py`).
- `app/services/serialize.py` — ORM → API dict shapes from `docs/API.md` (use these for responses and events).
- `app/services/{audit,usage,notifications,artifacts,workspace}.py`.
- `app/tools/base.py` — tool framework (read its docstring). `app/tools/connectors.py` — integration kinds registry. `app/tools/net.py` —
  `http()` shared httpx client and `await assert_public_url(url)` (SSRF guard — call it before fetching any agent-supplied URL).
- `tests/conftest.py` — isolated temp DB/data dir; fixtures `db_ready`, `org_ctx` (org + owner + mock LLM provider), `tool_ctx` (a `ToolContext`).

## Tool authoring rules
- One module per tool group in `app/tools/builtin/<name>.py` (auto-imported at startup; modules starting with `_` are helpers and are
  not auto-imported). Create the group with `ToolGroup(id, name, description, prefix=..., icon=<lucide icon name>, category=...)`.
  Categories: `core`, `productivity`, `engineering`, `data`, `business`, `infrastructure`, `knowledge`, `agents`.
- Tool name = `<prefix>_<function name>` (must match `^[a-zA-Z0-9_-]{1,64}$`). Write a clear first docstring paragraph (the model reads it)
  and an `Args:` section describing every parameter.
- **Risk** (drives human approval; the executor enforces it — tools must NOT ask for approval themselves):
  - `LOW` read-only, no side effects (list/read/search/get)
  - `MEDIUM` changes data inside Ren's workspace or creates drafts / internal, reversible changes (write file, create draft, comment on an issue)
  - `HIGH` external side effects people will see or that run code on a host: send email/message, post to Slack, execute shell, push git,
    share a file, submit a web form, create calendar invites with attendees
  - `CRITICAL` destructive/irreversible: delete remote data, merge to protected branches, deploy, infrastructure changes, payments
  - Use `risk_fn=lambda args: ...` when risk depends on args or config (e.g. code execution is `MEDIUM` when `settings.sandbox_url` is set,
    `HIGH` when running on the host).
- **Scope**: `read` (no side effects), `write` (modifies data / sends), `admin` (infrastructure or org-wide destructive). Viewers only get
  `read` tools; the executor enforces this.
- Outbound content (email bodies, chat messages, PR descriptions...) must be declared with `content_fields=("subject", "body")` so the
  policy guard can check it before it leaves the system. Provide `preview=lambda args: "<markdown>"` for HIGH/CRITICAL tools so approvers
  see a readable summary (e.g. an email rendered as To / Subject / Body).
- Return compact, model-friendly results (dicts/lists or concise text). Truncate large outputs and say so. Raise `ToolError("...")` for
  expected failures with an actionable message (e.g. "Slack is not connected — ask an admin to connect it in Connectors").
- Files: only through `ctx.workspace` (`resolve(path, must_exist=..., for_write=...)`, `virtual(abs_path)`, `list()`), virtual paths like
  `output/report.docx`, `shared/finance/q3.xlsx`. Register produced deliverables with `await ctx.add_artifact("output/report.docx")`.
  Viewer role must not write; deleting under `shared/` requires `ctx.is_admin`.
- Credentials: `cfg = await ctx.connector("github")` (merged config + decrypted secrets; raises a friendly ToolError if not connected).
  Never return secrets in tool output. Org-level settings/secrets: `async with ctx.session() as db: org = await db.get(Organization, ctx.org_id)`;
  `org.settings` (dict), `decrypt_json(org.secrets_enc)` (e.g. `web_search_api_key`).
- LLM inside tools (summaries, vision, extraction): `llm = await ctx.llm()`; then `await llm.chat([...])` or `await llm.json([...], schema)`.
- Long operations: `await ctx.progress("Fetched 120 of 400 rows", 30)`.
- Blocking libraries (sync SDKs, subprocess.run, pandas on big files): wrap in `await asyncio.to_thread(...)`. On Windows, prefer
  `subprocess.run` in a thread over `asyncio.create_subprocess_exec`.
- Register each integration kind your tools consume with `register_connector_kind(ConnectorKindSpec(...))` including a cheap `test` coroutine.
