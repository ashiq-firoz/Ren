# Ren — Frontend Design Spec

**Product:** Ren by f69 Labs — an agentic swarm orchestrator. Users talk to **Ren** (the orchestrator agent) in chat;
Ren delegates work to a swarm of specialist agents (research, data, finance, writing, engineering, ops...) that run in the
background while the user keeps chatting. Built for business users inside organizations (also works for one person).

Audience: operators, managers, analysts, executives. Tone: calm, precise, trustworthy, premium. Not "AI toy"; think
"Linear × Notion × a private bank". Dense where it helps (tables, task timelines), spacious where it matters (chat).

## Theme — f69 Labs beige (default) & dark

Palette from the f69 Labs brand sheet (`F69-labs-logo.png`): Deep Green `#0F2D24`, Green `#1F5A45`, Mint `#CFE7D9`,
Beige `#D9CBB8`, Sand `#E8DCC8`, Ivory `#F8F6F1`. Tokens are CSS variables on `<html data-theme>` (mapped to Tailwind v4
utilities in `globals.css`), so dark mode is a token swap. **Beige is the default for everyone**; dark is opt-in via the
sun/moon toggle (sidebar footer, auth and onboarding headers) or Settings → Appearance (Beige / Dark / System).
The choice is stored in `localStorage["ren_theme"]` and applied before first paint.

| token | beige (default) | dark | use |
|---|---|---|---|
| `--bg` | `#EFE7DA` | `#0B1612` | app background (beige / deep-green night) |
| `--surface` | `#F8F6F1` | `#10201A` | cards, panels, inputs, chat composer (ivory) |
| `--surface-2` | `#E7DDCC` | `#152922` | hover, secondary fills, sidebar active |
| `--surface-3` | `#DDD0BB` | `#1B332A` | pressed / selected |
| `--border` | `#DCCFBB` | `#1F382F` | hairlines |
| `--border-strong` | `#C5B396` | `#2E4B3F` | inputs, dividers |
| `--text` | `#0F2D24` | `#F1EBDF` | primary text (deep green / ivory) |
| `--text-muted` | `#56665D` | `#B2AB9D` | secondary text |
| `--text-subtle` | `#7F8A82` | `#75857C` | placeholders, timestamps |
| `--primary` / `--primary-fg` | `#0F2D24` / `#F8F6F1` | `#E8DCC8` / `#0F2D24` | primary buttons (deep green; sand in dark) |
| `--accent` / `--accent-fg` | `#A8875A` / `#0F2D24` | `#C9AD7C` / `#0F2D24` | bronze: "running" activity, medium risk, progress, charts |
| `--focus` | `#1F5A45` | `#8FCBAA` | focus rings, focused inputs, active nav marker (brand green) |
| `--highlight` | `#CFE7D9` | mint @ 22% | text selection (mint) |
| `--brand` / `--brand-fg` | `#0F2D24` / `#E8DCC8` | `#15392D` / `#E8DCC8` | brand tiles: Ren's avatar, auth panel, swarm hub |
| `--mark` | `#0F2D24` | `#E8DCC8` | the F69 mark on the page background |
| `--success` | `#2A6B4F` | `#6FBF96` | |
| `--warning` | `#A0661A` | `#D9A04F` | |
| `--danger` | `#A3392B` | `#E07A6B` | |
| `--info` | `#2D5B87` | `#7FA9D6` | |

Never hard-code hex colours in components — use the tokens (`bg-brand text-brand-fg`, `text-mark`, `bg-accent text-accent-fg`, …).

Status colors are muted (desaturated) to sit in the palette; use tinted backgrounds (`color-mix(in srgb, var(--success) 12%, transparent)`).

- Typography: **Inter** (UI, via `next/font/google`), **Instrument Serif** for the wordmark, page titles (h1) and empty-state headlines.
  Code: **JetBrains Mono**. Base 14px UI, 15px chat body, line-height 1.6 in chat.
- Radius: 10px cards, 8px inputs/buttons, 999px pills. Shadows: almost none; use borders. One soft shadow for popovers/dialogs.
- Motion: 150–200ms ease-out; subtle fades/slides; a gentle pulse for "running" status dots. Respect `prefers-reduced-motion`.
- Icons: `lucide-react`, 16px in UI, stroke 1.75.
- Brand: the f69 Labs **F69 mark** (`LogoMark` in `components/brand/logo.tsx`, an inline SVG traced from `F69-labs-icon.png`,
  2.15 : 1, fills `currentColor` — size it by width) followed by the wordmark "Ren" (Instrument Serif, ~26px) and small caps
  "by f69 Labs" (Inter 10–11px, letter-spacing .08em, muted). Favicon `app/icon.svg` / `app/apple-icon.png`: sand mark on deep green.
  Tagline (auth panel only): "f69 Labs · Intelligence for All".
- Agent avatars: rounded-square with the agent's `color` as background tint and its lucide `icon` in the agent color; orchestrator "Ren" uses the F69 mark on a `bg-brand` tile.

## Layout

- **Sidebar** (240px, collapsible to 64px icons; on mobile a drawer): logo + workspace switcher (dropdown of orgs with role & mode, "Create workspace");
  nav groups:
  - *Work*: Overview, Chat, Tasks, Schedules, Outputs, Approvals (badge = pending count)
  - *Swarm*: Agents, Skills, Knowledge, Connectors
  - *Governance*: Policies, Memory, Activity
  - *Workspace*: Team (hidden in personal mode), Settings
  Bottom: live indicator + beige/dark toggle, user menu (name, email, theme choice, sign out).
- **Top bar** per page: title (serif) + description + page actions on the right; notification bell (dropdown list, mark all read).
- Content max width ~1200px for management pages; chat is full-height.
- Fully responsive: at phone width the sidebar becomes a drawer, the chat's right panel becomes a sheet/tab, tables scroll horizontally inside their card.

## Pages

1. **Auth**: `/login`, `/register` (name, email, password, workspace name, mode toggle: *Business* — "A team of agents for your organization" / *Personal* — "Your private agent swarm"),
   `/invite/[token]` (accept invite: shows org + role; login-or-create). Split layout: left brand panel (deep green `bg-brand`, F69 mark, ivory serif quote
   "A world of agents, *working for you.*" with the emphasis in mint), right form on beige with a theme toggle top-right.
2. **Onboarding** `/onboarding` (shown after register when no provider exists): step 1 connect a model provider (cards: OpenRouter — API key + model;
   vLLM — base URL + model + optional key; OpenAI-compatible); "Test connection" → step 2 meet your swarm (agent cards) → step 3 go to chat.
   Allow "Skip for now".
3. **Overview** `/overview`: KPI tiles (Active tasks, Completed today, Pending approvals, Alignment pass rate, Documents, Tokens today),
   "Live swarm" strip (agents with active task counts + pulsing dots), Recent tasks table, Activity feed, 14-day usage chart (tokens bars + tasks line), agent utilization bars.
4. **Chat** `/chat` and `/chat/[id]` — the heart of the product:
   - Left column (260px): "New chat" button, search, pinned + recent conversations (title, preview, time, small dot if active tasks). Collapsible.
   - Center: messages. User messages: right-aligned bubble (`--surface-2` bg). Assistant (Ren) messages: left, full-width prose with avatar + name
     "Ren", markdown (GFM tables, code blocks with copy), citation chips `[1]` rendered as small superscript pills; hover/click shows a popover
     with document title, breadcrumb (`HR › Leave Policy › Parental Leave`), section path, snippet. Under the message: a "Sources" row and an
     alignment badge (✓ Policy check passed · 34 ms · Laya | ⚠ Warning | ⛔ Revised to comply) — subtle, small.
     Tool-call chips while Ren works ("Searching knowledge…", "Delegating to Research Analyst").
     **Event messages** render as compact cards: task created (agent avatar, title, status pill, progress bar, "View"), task completed (✓, duration,
     "View result"), task failed, approval requested (risk badge, tool, preview, **Approve / Reject** buttons inline), approval resolved.
     Streaming: append `message.delta` text live; blinking caret while streaming.
   - Composer (bottom, sticky): autosizing textarea, Enter = send, Shift+Enter = newline; attach button (uploads via attachments endpoint, chips
     above input); `@` opens agent mention picker; a Stop button appears while Ren is responding. **The composer is never disabled** — the
     user can send messages while Ren is replying or while tasks run. If Ren is busy, show a small "Ren will reply after the current response"
     hint and mark the message "queued".
     Status line above composer: "Ren is thinking…", "Ren is delegating…", plus "3 tasks running" link that opens the swarm panel.
   - Right panel (340px, toggleable): **Swarm activity** for this conversation — active tasks (avatar, title, status, progress, current step,
     pause/cancel), pending approvals with approve/reject, recently completed tasks with result preview. Live via stream.
   - Empty state (new chat): serif headline "What should the swarm work on?", 4–6 suggestion cards relevant to business users
     (e.g. "Research our top 3 competitors and draft a brief", "Summarize the uploaded policy and list obligations", "Analyze this spreadsheet and chart revenue by region",
     "Prepare me for tomorrow's board meeting", "Draft a customer email about the pricing change", "Find the answer in our knowledge base: …").
5. **Tasks** `/tasks`: filter bar (status tabs: All, Running, Waiting approval, Completed, Failed; agent select; search), table (title, agent, status
   pill, progress, started, duration, tokens). `/tasks/[id]`: header (title, agent, status, progress, timings, tokens, actions: Message agent,
   Pause/Resume, Cancel, Retry), tabs: **Timeline** (vertical timeline of TaskEvents with icons; tool calls expandable showing args JSON and result
   preview; policy events; inbox messages; delegation links to child tasks), **Result** (markdown), **Artifacts** (files with download),
   **Sub-tasks**, **Instructions**. Live updates via stream. A "message the agent" input at the bottom of the timeline.
5a. **Schedules** `/schedules`: tabs Active (scheduled + paused) / History. Each schedule: title, agent, the server's human
   description ("Every day at 09:00 (Asia/Kolkata)") + time-zone chip, next run in the viewer's local time (and the job's
   zone when different), last run (task status pill → task), run count, Paused badge. Actions: Run now, Pause/Resume,
   Edit, Delete. Click → drawer with upcoming runs, instructions and run history (each run's outputs openable).
   "New schedule" / Edit dialog: title, agent, instructions, friendly frequency builder (Once / Hourly / Daily / Weekdays /
   Weekly / Monthly / Custom cron) + time zone (default: browser), with a live server-validated preview of the next runs.
5b. **Outputs** `/outputs`: everything agents produced — tabs Generated / Uploads / All files (workspace folder browser),
   search + file-type filter, rows with type icon, name, path, size, time, agent + task link; View / Download / Copy path.
   **File viewer** (right drawer from anywhere, or full page `/outputs/view?artifact=…|path=…`): markdown (also Word,
   PowerPoint and Excel converted for preview), CSV tables, code, sandboxed HTML, images, PDFs, audio/video.
   Workspace paths in any rendered markdown (e.g. `output/report.md` in Ren's replies) become links that open the viewer.
6. **Approvals** `/approvals`: tabs Pending / History. Pending: cards with risk badge (low/medium/high/critical colors), agent, task link,
   tool display name, reason, **preview** (markdown — e.g. an email rendered as To/Subject/Body), expandable raw args (editable JSON → `modified_args`),
   comment box, Approve (primary) / Reject (danger outline).
7. **Agents** `/agents`: two views — Grid (cards: avatar, name, role, description, tools count, autonomy pill, active tasks, success rate) and
   **Swarm map** (SVG: Ren in the center, agents in a ring, live edges pulsing when a task is running for that agent).
   `/agents/new` and `/agents/[id]`: editor with sections/tabs: Profile (name, role, description, color, icon), Instructions (system prompt textarea, mono),
   Model (provider select, model input with suggestions from `/api/providers/{id}/models`, temperature slider, max steps), Tools (tool groups as
   toggle cards with risk summary; connector status), Knowledge (collections multi-select), Policies (multi-select of agent-scoped + note that org policies always apply),
   Autonomy (3 radio cards with explanation: Supervised / Balanced / Autonomous). Stats panel on the right for existing agents.
8. **Knowledge** `/knowledge`: left list of collections (+ new collection), main: upload dropzone (drag & drop multiple files: pdf, docx, md, txt, html, xlsx, csv, pptx),
   documents table (title, status pill with progress bar while processing — statuses parsing → structuring → embedding → questions → ready, sections/leaves, chunks,
   structure source badge: Native ToC / Headings / AI-augmented / Flat, uploaded by, date, actions: reindex, delete, download).
   `/knowledge/[docId]`: **structure explorer** — left: ToC tree (collapsible, leaf nodes marked, token counts), right: selected section (breadcrumb,
   content, summary, generated questions "STAIR training questions"). Header shows doc stats.
   `/knowledge/search`: **Retrieval playground** — query input, mode segmented control (Hybrid / Semantic / Lexical / Structure), rerank toggle, top-k;
   shows "STAIR selected sections" (breadcrumb pills with confidence), result cards with per-retriever rank badges (BM25 #3, Vector #1, STAIR ✓, Q #2),
   fused score and rerank score, stage timings bar; "Generate answer" button → grounded answer with citations + alignment badge; collapsible "Context sent to the model".
   Also an "Export STAIR training data" button (downloads NDJSON).
9. **Skills** `/skills`: category filter chips, cards grid (icon, name, description, preferred agent, Run). Run → dialog with the skill's inputs form →
   POST run → toast with link to the conversation/task. Create/edit skill dialog or page (name, description, category, icon, agent, tool groups, inputs builder, instructions markdown).
10. **Connectors** `/connectors`: tabs **Built-in tools** (tool groups grid: icon, name, description, tool count, risk mix, enabled toggle, availability /
    "Needs connection" badge; click → drawer listing each tool with risk & scope), **Integrations** (connector kinds grid: Gmail, Google Calendar,
    Google Drive, Slack, Microsoft 365, GitHub, Jira, Linear, Notion, HubSpot, Salesforce, IMAP/SMTP email, PostgreSQL/MySQL, AWS...; "Connect" opens
    a form generated from `ConnectorKind.fields`; connected ones show status + Test + Edit + Remove), **MCP servers** (list with status/tool count,
    "Add server" form for stdio (command + args + env) or HTTP (url + headers), presets gallery with one-click prefill, Refresh, toggle, remove;
    plus a card explaining Ren's own MCP endpoint URL `/mcp`).
11. **Policies** `/policies`: header stats (pass rate, checks, blocked, p50 latency of Laya), Laya status pill (reachable/unreachable),
    policy list (name, category, severity pill, scope, rules count, enabled toggle). Editor drawer: name, description, category, severity, scope (+ agents),
    applies to, **rules list** (add/remove short atomic rules — each becomes one Laya question), regex patterns. **Test console**: paste a response (+ optional user request) →
    verdict with per-rule violation probability bars. **Alignment log** table (time, agent, kind, verdict, checker, latency, excerpt) with filter by verdict.
12. **Memory** `/memory`: search, kind filter chips (Preference, Semantic, Episodic, Project, Org), scope filter (Mine / Workspace), table/cards with edit/delete, "Add memory".
13. **Activity** `/activity` (admin): audit log table (time, actor user/agent, action, target, risk pill, details expandable), filters.
14. **Team** `/team` (business mode, admin): members table (avatar initials, name, email, title, role select, joined, last active, remove), invite dialog (email + role) →
    shows copyable invite link; pending invites list with revoke.
15. **Settings** `/settings`: tabs — Workspace (name, mode, default autonomy, max concurrent tasks), Models (providers list: add OpenRouter/vLLM/OpenAI/custom,
    test, set default, delete; orchestrator model override), Retrieval (STAIR on/off, question generation, structure augmentation, rerank, top-k; read-only
    embedding/reranker info from `/api/settings/system`), Alignment (enabled, mode post/pre, thresholds sliders, judge fallback; Laya status), Web search
    (provider + API key), Profile (name, change password), Appearance (light/dark/system).

## Components (hand-written, no shadcn CLI)

Button (primary/secondary/ghost/danger/outline; sizes sm/md; loading), IconButton, Input, Textarea (autosize), Select (native styled),
Switch, Checkbox, Slider, SegmentedControl, Badge/Pill (status variants), Card, Dialog (modal), Drawer (side sheet), Tabs, DropdownMenu,
Popover, Tooltip, Table, Skeleton, EmptyState (serif headline + muted text + action), Toast (bottom-right stack), Avatar (initials),
AgentAvatar, StatusDot (pulsing when running), ProgressBar, Markdown (react-markdown + remark-gfm + code highlight optional; supports citation chips
by turning `[n]` into chips when `citations` are provided), JsonView (collapsible pretty JSON), FileDropzone, KeyValue list, Timeline.

## Behavior

- Auth token in `localStorage["ren_token"]`, active workspace in `localStorage["ren_org"]`. `401` → clear and go to `/login`.
- Data fetching with TanStack Query; the SSE stream (single connection per tab, auto-reconnect with backoff) updates the cache:
  messages (by conversation), tasks (lists + detail), approvals (list + count badge), documents, notifications, conversation status.
- Optimistic UI for sending messages (insert the user message immediately; reconcile with the server message by id).
- Toasts for errors (show `detail`).
- Keyboard: `Ctrl/Cmd+K` command palette (navigate pages, new chat, search conversations) — nice to have.
- Accessibility: focus rings (`--accent`), aria labels on icon buttons, sufficient contrast in both themes.
