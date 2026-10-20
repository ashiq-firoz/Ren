# Ren API Contract (v1)

> **Authoritative contract** between the Next.js frontend and the FastAPI backend.
> Product: **Ren by f69 Labs** — an agentic swarm orchestrator for businesses and individuals.

## Conventions

- Base URL: `${NEXT_PUBLIC_API_URL}` (default `http://localhost:8000`). Every route is prefixed with `/api`.
- Auth: `Authorization: Bearer <access_token>` on every route except `/api/auth/register`, `/api/auth/login`, `/api/auth/invite/{token}`, `/api/auth/accept-invite`, `/api/health`.
- Workspace scoping: org-scoped routes require header `X-Org-Id: <org_id>` (the active workspace). The backend checks membership and role.
- IDs are UUID strings. Timestamps are ISO-8601 UTC strings (`2026-10-05T12:00:00Z`).
- Time zone: the frontend sends `X-Timezone: <IANA name>` (e.g. `Asia/Kolkata`, from
  `Intl.DateTimeFormat().resolvedOptions().timeZone`) on every request. The backend remembers it on the user
  (`User.timezone`) and uses it for schedules created in chat and for "today" in Ren's prompt.
- **Every list endpoint returns** `{ "items": T[], "total": number }`.
- Errors: HTTP status + `{ "detail": string }` (422 validation errors return FastAPI's `{ "detail": [...] }`).
- Roles (ascending): `viewer` < `member` < `admin` < `owner`.
  - viewer: read-only (can chat; agents only get read-scoped tools on their behalf).
  - member: chat, run tasks/skills, upload documents, approve approvals for their own tasks.
  - admin: everything a member can do + manage agents, policies, connectors, MCP servers, providers, members, settings; approve any approval.
  - owner: everything + delete workspace / change owner.

---

## TypeScript types

```ts
type Role = "owner" | "admin" | "member" | "viewer";
type WorkspaceMode = "business" | "personal";

interface User { id: string; email: string; name: string; created_at: string; timezone: string | null; }

interface OrgSummary { id: string; name: string; slug: string; mode: WorkspaceMode; role: Role; }

interface AuthResponse { access_token: string; token_type: "bearer"; user: User; orgs: OrgSummary[]; }

interface Org {
  id: string; name: string; slug: string; mode: WorkspaceMode; created_at: string;
  settings: OrgSettings; member_count: number;
}

interface OrgSettings {
  default_provider_id: string | null;
  orchestrator_model: string | null;          // overrides provider default for the orchestrator
  autonomy_default: Autonomy;
  max_concurrent_tasks: number;               // default 6
  alignment: {
    enabled: boolean;                         // default true
    mode: "post" | "pre";                     // post = stream then check; pre = check before showing
    block_threshold: number;                  // 0..1, violation probability to block (default 0.7)
    warn_threshold: number;                   // 0..1 (default 0.45)
    judge_fallback: boolean;                  // escalate uncertain Laya verdicts to an LLM judge
    laya_mode: "triage" | "authoritative";    // triage (default): Laya screens fast, judge confirms suspicious cases;
                                              // authoritative: Laya's scores decide (use after fine-tuning Laya)
    escalate_threshold: number;               // triage: Laya score (0..1) at/above which the judge reviews (default 0.5)
  };
  retrieval: {
    stair_enabled: boolean;                   // structure-aware retrieval on
    generate_questions: boolean;              // synthetic section questions during ingestion
    structure_augmentation: boolean;          // LLM titles for unstructured docs
    rerank: boolean;
    top_k: number;                            // default 8
  };
  web_search: { provider: "duckduckgo" | "tavily" | "brave" | "searxng"; has_key: boolean; base_url?: string | null };
}

interface Member {
  user_id: string; email: string; name: string; role: Role; title: string | null;
  joined_at: string; last_active_at: string | null;
}

interface Invite {
  id: string; email: string; role: Role; token: string; link: string;   // link = `${FRONTEND_URL}/invite/${token}`
  created_at: string; expires_at: string; accepted: boolean; invited_by_name: string | null;
}

type ProviderKind = "openrouter" | "vllm" | "openai" | "custom" | "mock";
interface Provider {
  id: string; name: string; kind: ProviderKind; base_url: string; default_model: string;
  has_key: boolean; is_default: boolean; supports_tools: boolean;
  extra: Record<string, any>; created_at: string;
}

type Autonomy = "supervised" | "balanced" | "autonomous";
// supervised: approval needed for medium+ risk tools; balanced: high+; autonomous: critical only.

interface Agent {
  id: string; name: string; slug: string; role: string; description: string;
  system_prompt: string; provider_id: string | null; model: string | null; temperature: number;
  tool_groups: string[]; policy_ids: string[]; collection_ids: string[];
  autonomy: Autonomy; max_steps: number; color: string; icon: string;   // icon = lucide icon name (kebab-case)
  is_orchestrator: boolean; enabled: boolean; created_at: string; updated_at: string;
  stats?: AgentStats;
}
interface AgentStats {
  active_tasks: number; tasks_total: number; tasks_completed: number; tasks_failed: number;
  success_rate: number; avg_duration_s: number; tokens_total: number; alignment_pass_rate: number;
}

interface Conversation {
  id: string; title: string; pinned: boolean; archived: boolean; created_at: string; updated_at: string;
  last_message_preview: string | null; active_tasks: number;
  status: ConversationStatus;               // live orchestrator state
}
type ConversationStatus = "idle" | "thinking" | "responding" | "using_tool";

type MessageRole = "user" | "assistant" | "event" | "system";
interface Message {
  id: string; conversation_id: string; role: MessageRole; content: string;   // markdown
  agent_id: string | null; task_id: string | null;
  status: "streaming" | "complete" | "error" | "queued";
  metadata: MessageMetadata; created_at: string; user_id: string | null;
}
interface MessageMetadata {
  citations?: Citation[];
  tool_calls?: { id: string; name: string; display: string; args: any; status: "running" | "done" | "error"; result_preview?: string }[];
  alignment?: AlignmentSummary;
  event?: ChatEvent;                        // only when role === "event"
  attachments?: Attachment[];
  mention_agent_id?: string;
}
interface Citation {
  id: number;                               // the [n] number used in content
  document_id: string; document_title: string; section_id: string | null;
  section_path: string | null;              // "4.1.1"
  section_title: string | null; breadcrumb: string | null;   // "HR > Leave Policy > Parental Leave"
  snippet: string; score: number | null;
}
interface ChatEvent {
  kind: "task_created" | "task_completed" | "task_failed" | "task_cancelled" | "task_paused"
      | "approval_requested" | "approval_resolved" | "skill_started" | "info";
  task_id?: string; approval_id?: string; agent_id?: string; agent_name?: string;
  title?: string; status?: string; summary?: string; risk?: Risk; tool?: string; decision?: "approved" | "rejected" | "expired";
}
interface Attachment { id: string; name: string; path: string; size: number; mime: string; document_id: string | null; }

type AlignmentVerdict = "pass" | "warn" | "block" | "pending" | "skipped";
interface AlignmentSummary {
  verdict: AlignmentVerdict; checker: "rules" | "laya" | "judge" | "laya+judge" | "none";
  latency_ms: number | null; violations: PolicyViolation[]; revised?: boolean;
}
interface PolicyViolation { policy_id: string; policy_name: string; rule: string | null; score: number; explanation?: string | null; }

type TaskStatus = "queued" | "running" | "waiting_approval" | "paused" | "completed" | "failed" | "cancelled";
type Priority = "low" | "normal" | "high" | "urgent";
interface Task {
  id: string; title: string; instructions: string; status: TaskStatus; priority: Priority;
  agent_id: string; agent_name: string; agent_color: string;
  conversation_id: string | null; parent_id: string | null; skill_id: string | null;
  progress: number;                         // 0..100
  current_step: string | null;              // human-readable latest activity
  result: string | null;                    // markdown
  error: string | null; steps: number; tokens: number;
  created_by: string | null; created_at: string; started_at: string | null; finished_at: string | null;
  children_count: number; artifacts: Artifact[];
  schedule_id: string | null;   // set when the task was started by a schedule
}
interface TaskDetail extends Task { events: TaskEvent[]; children: Task[]; approvals: Approval[]; }
type TaskEventKind = "status" | "thought" | "tool_call" | "tool_result" | "message" | "progress"
                   | "inbox" | "policy" | "delegation" | "error" | "artifact";
interface TaskEvent { id: string; task_id: string; kind: TaskEventKind; content: Record<string, any>; created_at: string; }
// content by kind:
//  status:      { status: TaskStatus, note?: string }
//  thought:     { text: string }
//  tool_call:   { call_id, tool: string, display: string, args: any, risk: Risk }
//  tool_result: { call_id, tool: string, ok: boolean, preview: string, duration_ms: number }
//  message:     { text: string }                      // agent's final or interim message
//  progress:    { progress: number, note: string }
//  inbox:       { from: "user" | "orchestrator" | "agent", text: string, sender_name?: string }
//  policy:      { verdict: AlignmentVerdict, violations: PolicyViolation[], action: "passed" | "revised" | "blocked" | "warned" }
//  delegation:  { child_task_id, agent_name, title }
//  error:       { message: string }
//  artifact:    { artifact: Artifact }

type Risk = "low" | "medium" | "high" | "critical";
interface Approval {
  id: string; task_id: string | null; task_title: string | null; agent_id: string | null; agent_name: string | null;
  tool: string; tool_display: string; args: Record<string, any>; risk: Risk;
  reason: string;                           // why approval is needed
  preview: string;                          // human-readable markdown summary of the action (e.g. email preview)
  status: "pending" | "approved" | "rejected" | "expired";
  requested_for_user_id: string | null; conversation_id: string | null;
  created_at: string; decided_by_name: string | null; decided_at: string | null; comment: string | null; expires_at: string | null;
}

type FileKind = "document" | "spreadsheet" | "presentation" | "pdf" | "image" | "data" | "code" | "web" | "text"
  | "audio" | "video" | "archive" | "other";
interface Artifact { id: string; name: string; path: string; mime: string; size: number; task_id: string | null;
  conversation_id: string | null; created_at: string;
  kind: FileKind;                       // derived from the extension
  source: "generated" | "uploaded";     // generated = written by an agent task; uploaded = chat attachment
  task_title: string | null; agent_id: string | null; agent_name: string | null; schedule_id: string | null; }

interface Collection { id: string; name: string; description: string; access: "org" | "restricted"; member_ids: string[]; document_count: number; created_at: string; }
type DocumentStatus = "uploaded" | "parsing" | "structuring" | "embedding" | "questions" | "ready" | "failed";
interface KDocument {
  id: string; collection_id: string; collection_name: string; title: string; filename: string; mime: string; size: number;
  status: DocumentStatus; progress: number; error: string | null;
  page_count: number | null; section_count: number; leaf_count: number; chunk_count: number; question_count: number;
  structure_source: "native" | "headings" | "augmented" | "flat" | null;
  uploaded_by_name: string | null; created_at: string; updated_at: string;
}
interface TocNode {
  id: string; path: string; title: string; level: number; is_leaf: boolean;
  token_count: number; summary: string | null; children: TocNode[];
}
interface KDocumentDetail extends KDocument { toc: TocNode[]; }
interface SectionDetail {
  id: string; document_id: string; path: string; title: string; level: number; is_leaf: boolean;
  breadcrumb: string; content: string; summary: string | null; token_count: number;
  questions: string[]; children: { id: string; path: string; title: string; is_leaf: boolean }[];
  page_start: number | null; page_end: number | null;
}

interface SearchResult {
  chunk_id: string; document_id: string; document_title: string; section_id: string | null;
  section_path: string | null; section_title: string | null; breadcrumb: string | null;
  content: string; score: number; rerank_score: number | null;
  sources: { bm25?: number | null; vector?: number | null; stair?: number | null; questions?: number | null };   // 1-based ranks per retriever
}
interface StairSelection { document_id: string; document_title: string; section_id: string; path: string; title: string; breadcrumb: string; confidence: number | null; }
interface SearchResponse {
  query: string; results: SearchResult[];
  stair: { enabled: boolean; selected: StairSelection[]; method: string; latency_ms: number; candidates_considered: number };
  timings: Record<string, number>;          // ms per stage: bm25, vector, stair, questions, fusion, rerank, total
  context: string;                          // the built context block with [n] source tags
}
interface AnswerResponse { answer: string; citations: Citation[]; alignment: AlignmentSummary; retrieval: SearchResponse; }

interface Policy {
  id: string; name: string; description: string; category: string;   // e.g. "confidentiality", "tone", "compliance", "safety", "custom"
  rules: string[];                         // short atomic rules — each becomes one Laya question
  patterns: string[];                      // optional regexes; a match is an instant violation
  severity: "block" | "warn"; scope: "org" | "agent"; applies_to: "responses" | "outbound" | "all";
  agent_ids: string[];                     // when scope === "agent"
  enabled: boolean; created_at: string; updated_at: string;
}
interface AlignmentCheck {
  id: string; agent_id: string | null; agent_name: string | null; task_id: string | null; message_id: string | null;
  kind: "response" | "outbound" | "test"; verdict: AlignmentVerdict; checker: string; latency_ms: number;
  violations: PolicyViolation[]; excerpt: string; created_at: string;
}

interface ToolInfo { name: string; display_name: string; description: string; risk: Risk; scope: "read" | "write" | "admin"; }
interface ToolGroup {
  id: string; name: string; description: string; icon: string; category: string;   // "core" | "productivity" | "engineering" | "data" | "business" | "infrastructure" | "knowledge" | "agents" | "mcp"
  connector_kind: string | null; connected: boolean; enabled: boolean; available: boolean;   // available=false if a dependency (e.g. playwright) is missing
  unavailable_reason: string | null; tools: ToolInfo[];
}
interface ConnectorField { key: string; label: string; type: "text" | "password" | "url" | "select" | "textarea" | "number"; required: boolean; secret: boolean; placeholder?: string; help?: string; options?: string[]; }
interface ConnectorKind { kind: string; name: string; description: string; icon: string; category: string; fields: ConnectorField[]; tool_groups: string[]; docs_url?: string; }
interface Connector { id: string; kind: string; name: string; status: "connected" | "error" | "unknown"; config: Record<string, any>; error: string | null; created_at: string; }

interface McpServer {
  id: string; name: string; transport: "stdio" | "http" | "sse"; command: string | null; args: string[]; url: string | null;
  enabled: boolean; status: "connected" | "error" | "disconnected"; error: string | null;
  tools: { name: string; description: string }[]; env_keys: string[]; header_keys: string[]; created_at: string;
}
interface McpPreset { id: string; name: string; description: string; transport: "stdio" | "http"; command?: string; args?: string[]; url?: string; env_fields: ConnectorField[]; docs_url?: string; }

interface Skill {
  id: string; name: string; slug: string; description: string; category: string; icon: string;
  instructions: string;                    // markdown playbook
  agent_id: string | null;                 // preferred agent
  tool_groups: string[]; inputs: SkillInput[]; builtin: boolean; created_at: string;
}
interface SkillInput { key: string; label: string; type: "text" | "textarea" | "number" | "date" | "select"; required: boolean; placeholder?: string; options?: string[]; }

interface Memory {
  id: string; content: string; kind: "episodic" | "semantic" | "preference" | "project" | "org";
  scope: "user" | "org"; agent_id: string | null; importance: number; source: string | null;
  created_at: string; updated_at: string;
}

interface AuditEntry {
  id: string; action: string; target_type: string | null; target_id: string | null;
  user_id: string | null; user_name: string | null; agent_id: string | null; agent_name: string | null; task_id: string | null;
  risk: Risk | null; details: Record<string, any>; created_at: string;
}

interface Notification { id: string; kind: string; title: string; body: string; link: string | null; read: boolean; created_at: string; }

interface WorkspaceFile { name: string; path: string; is_dir: boolean; size: number; modified_at: string; mime: string | null; }
```

---

## Endpoints

### Health
- `GET /api/health` → `{ status: "ok", version: string, db: "postgresql" | "sqlite" }`

### Auth
- `POST /api/auth/register` `{ email, password, name, org_name?: string, mode: WorkspaceMode }` → `AuthResponse`
  (creates the user and a workspace they own, seeded with default agents, policies, skills; `org_name` defaults to "<name>'s Workspace")
- `POST /api/auth/login` `{ email, password }` → `AuthResponse`
- `GET /api/auth/me` → `{ user: User, orgs: OrgSummary[] }`
- `PATCH /api/auth/me` `{ name?: string, current_password?: string, new_password?: string }` → `User`
- `GET /api/auth/invite/{token}` → `{ email, org_name, role, inviter_name, user_exists: boolean }` (public)
- `POST /api/auth/accept-invite` `{ token, name?: string, password?: string }` → `AuthResponse`
  (if a user with the invite email exists, `password` must be that user's password; otherwise `name`+`password` create the account)

### Workspaces (orgs)
- `GET /api/orgs` → list of `OrgSummary`
- `POST /api/orgs` `{ name, mode }` → `OrgSummary` (no X-Org-Id needed)
- `GET /api/orgs/current` → `Org`
- `PATCH /api/orgs/current` `{ name?, mode?, settings?: Partial<OrgSettings> & { web_search_api_key?: string } }` → `Org` (admin)
- `DELETE /api/orgs/current` (owner) → `{ ok: true }`
- `GET /api/orgs/current/members` → list of `Member`
- `PATCH /api/orgs/current/members/{user_id}` `{ role?, title? }` → `Member` (admin; cannot demote the last owner)
- `DELETE /api/orgs/current/members/{user_id}` (admin) → `{ ok: true }`
- `GET /api/orgs/current/invites` → list of `Invite` (admin)
- `POST /api/orgs/current/invites` `{ email, role }` → `Invite` (admin)
- `DELETE /api/orgs/current/invites/{id}` (admin) → `{ ok: true }`

### Dashboard
- `GET /api/dashboard` →
```ts
{
  stats: { active_tasks: number; completed_today: number; failed_today: number; pending_approvals: number;
           documents: number; agents: number; alignment_pass_rate: number | null; tokens_today: number; conversations: number };
  recent_tasks: Task[];                                    // 8 most recent
  recent_activity: AuditEntry[];                           // 12 most recent
  usage_series: { date: string; tokens: number; tasks: number }[];   // last 14 days, oldest first
  agent_utilization: { agent_id: string; name: string; color: string; tasks: number; tokens: number; active: number }[];
}
```

### LLM providers (admin for writes)
- `GET /api/providers` → list of `Provider`
- `POST /api/providers` `{ name, kind, base_url?, api_key?, default_model, is_default?, supports_tools?, extra? }` → `Provider`
  (base_url defaults: openrouter → `https://openrouter.ai/api/v1`, openai → `https://api.openai.com/v1`, vllm → required)
- `PATCH /api/providers/{id}` (same fields, all optional; `api_key: ""` clears the key) → `Provider`
- `DELETE /api/providers/{id}` → `{ ok: true }`
- `POST /api/providers/{id}/test` → `{ ok: boolean; latency_ms: number; model: string; sample: string | null; error: string | null }`
- `GET /api/providers/{id}/models` → list of `{ id: string; name: string; context_length: number | null }`

### Agents
- `GET /api/agents` → list of `Agent` (with `stats`)
- `POST /api/agents` `Partial<Agent>` (name, role required) → `Agent` (admin)
- `GET /api/agents/{id}` → `Agent` (with `stats`)
- `PATCH /api/agents/{id}` `Partial<Agent>` → `Agent` (admin)
- `DELETE /api/agents/{id}` → `{ ok: true }` (admin; orchestrator cannot be deleted)

### Conversations & chat
- `GET /api/conversations?archived=false&q=` → list of `Conversation` (current user's, newest first, pinned first)
- `POST /api/conversations` `{ title?: string }` → `Conversation`
- `GET /api/conversations/{id}` → `Conversation & { messages: Message[] }` (last 200 messages, oldest first)
- `GET /api/conversations/{id}/messages?before=<message_id>&limit=50` → list of `Message` (oldest first)
- `PATCH /api/conversations/{id}` `{ title?, pinned?, archived? }` → `Conversation`
- `DELETE /api/conversations/{id}` → `{ ok: true }`
- `POST /api/conversations/{id}/messages` `{ content: string; attachment_ids?: string[]; mention_agent_id?: string }`
  → **202** `Message` (the stored user message). **Never blocks**: the orchestrator reply arrives over the stream.
  The user may post at any time, including while the orchestrator is replying or tasks are running.
- `POST /api/conversations/{id}/stop` → `{ ok: true }` (stop the orchestrator's current generation)
- `POST /api/conversations/{id}/attachments` (multipart: `file`) → `Attachment` (saved to the user's workspace and indexed into the "Chat Uploads" collection)
- `GET /api/conversations/{id}/tasks` → list of `Task`

### Tasks
- `GET /api/tasks?status=&agent_id=&conversation_id=&q=&limit=50&offset=0` → `{ items: Task[], total }` (members see their own; admins see all)
- `POST /api/tasks` `{ agent_id, title, instructions, conversation_id?, priority? }` → `Task` (direct assignment)
- `GET /api/tasks/{id}` → `TaskDetail`
- `GET /api/tasks/{id}/events?after=<event_id>` → list of `TaskEvent`
- `POST /api/tasks/{id}/cancel` | `/pause` | `/resume` | `/retry` → `Task`
- `POST /api/tasks/{id}/message` `{ content }` → `TaskEvent` (delivered to the running agent's inbox before its next step)

### Approvals
- `GET /api/approvals?status=pending|approved|rejected|expired|all` → list of `Approval`
- `POST /api/approvals/{id}/approve` `{ comment?: string; modified_args?: Record<string, any> }` → `Approval`
- `POST /api/approvals/{id}/reject` `{ comment?: string }` → `Approval`

### Knowledge (STAIR hybrid RAG)
- `GET /api/collections` → list of `Collection`
- `POST /api/collections` `{ name, description?, access?, member_ids? }` → `Collection` (admin)
- `PATCH /api/collections/{id}` / `DELETE /api/collections/{id}` (admin)
- `GET /api/documents?collection_id=&q=&status=` → list of `KDocument`
- `POST /api/documents` (multipart: `file`, `collection_id`) → `KDocument` (status `uploaded`; ingestion continues in background, progress via `document.updated` events)
- `GET /api/documents/{id}` → `KDocumentDetail`
- `DELETE /api/documents/{id}` → `{ ok: true }`
- `POST /api/documents/{id}/reindex` → `KDocument`
- `GET /api/documents/{id}/download` → original file bytes
- `GET /api/documents/{id}/sections/{section_id}` → `SectionDetail`
- `POST /api/knowledge/search` `{ query, collection_ids?, document_ids?, mode?: "hybrid" | "semantic" | "lexical" | "structure", top_k?, rerank?: boolean }` → `SearchResponse`
- `POST /api/knowledge/answer` `{ query, collection_ids?, document_ids? }` → `AnswerResponse`
- `GET /api/knowledge/training-data?collection_id=` → `application/x-ndjson` download (STAIR fine-tuning pairs: query + ToC → leaf)
- `GET /api/knowledge/stats` → `{ documents, sections, leaves, chunks, questions, embedding_model, embedding_dim, reranker }`

### Policies & alignment (admin for writes)
- `GET /api/policies` → list of `Policy`
- `POST /api/policies` `Partial<Policy>` (name required) → `Policy`
- `PATCH /api/policies/{id}` / `DELETE /api/policies/{id}`
- `POST /api/policies/test` `{ text: string; user_request?: string; agent_id?: string; policy_ids?: string[]; kind?: "response" | "outbound" }` → `AlignmentSummary & { per_rule: { policy_id, policy_name, rule, violation_probability: number, checker: string }[] }`
- `GET /api/alignment/checks?verdict=&agent_id=&limit=50&offset=0` → `{ items: AlignmentCheck[], total }`
- `GET /api/alignment/stats` → `{ total, passed, warned, blocked, pass_rate: number | null, p50_latency_ms: number | null, by_policy: { policy_id, policy_name, violations }[], by_agent: { agent_id, agent_name, checks, pass_rate }[], series: { date, checks, violations }[] }`
- `GET /api/alignment/status` → `{ laya: { configured: boolean; reachable: boolean; url: string | null; mode: "http" | "local" | "none"; latency_ms: number | null; error: string | null }, judge: { enabled: boolean } }`

### Tools, connectors, MCP (admin for writes)
- `GET /api/tools/catalog` → list of `ToolGroup` (built-in groups + one group per connected MCP server, id `mcp:<server_id>`)
- `PATCH /api/tools/groups/{group_id}` `{ enabled: boolean }` → `ToolGroup`
- `GET /api/connectors/kinds` → list of `ConnectorKind`
- `GET /api/connectors` → list of `Connector` (secrets never returned; secret fields appear as `"••••"` when set)
- `POST /api/connectors` `{ kind, name, config }` → `Connector`
- `PATCH /api/connectors/{id}` `{ name?, config? }` → `Connector` (omitted secret fields keep their value)
- `DELETE /api/connectors/{id}` → `{ ok: true }`
- `POST /api/connectors/{id}/test` → `{ ok: boolean; message: string }`
- `GET /api/mcp/servers` → list of `McpServer`
- `GET /api/mcp/presets` → list of `McpPreset`
- `POST /api/mcp/servers` `{ name, transport, command?, args?, url?, env?: Record<string,string>, headers?: Record<string,string> }` → `McpServer`
- `PATCH /api/mcp/servers/{id}` `{ name?, enabled?, command?, args?, url?, env?, headers? }` → `McpServer`
- `DELETE /api/mcp/servers/{id}` → `{ ok: true }`
- `POST /api/mcp/servers/{id}/refresh` → `McpServer` (reconnect and re-list tools)
- Ren also **exposes its own tools as an MCP server** at `/mcp` (Streamable HTTP, bearer-token auth).

### Skills
- `GET /api/skills` → list of `Skill`
- `POST /api/skills` `Partial<Skill>` → `Skill` (admin) ; `PATCH /api/skills/{id}` ; `DELETE /api/skills/{id}` (built-ins cannot be deleted, only edited)
- `POST /api/skills/{id}/run` `{ inputs: Record<string, any>; conversation_id?: string }` → `{ task: Task; conversation_id: string }`

### Memory
- `GET /api/memories?q=&kind=&scope=` → list of `Memory`
- `POST /api/memories` `{ content, kind, scope }` → `Memory`
- `PATCH /api/memories/{id}` `{ content?, kind?, importance? }` → `Memory`
- `DELETE /api/memories/{id}` → `{ ok: true }`

### Files & artifacts (outputs)
- `GET /api/files?path=` → list of `WorkspaceFile` (the caller's workspace; top-level folders: `workspace`, `projects`, `downloads`, `shared`, `output`, `uploads`)
- `GET /api/files/download?path=` → file bytes. Paths are workspace-relative (`output/report.md`). If the path belongs to a
  registered artifact the caller may see (admins: any in the workspace), it resolves in the artifact owner's workspace.
- `GET /api/files/preview?path=` → `FilePreview` (same path resolution as download)
- `POST /api/files/upload` (multipart: `file`, `path`) → `WorkspaceFile`
- `DELETE /api/files?path=` → `{ ok: true }`
- `GET /api/artifacts?q=&kind=&source=&task_id=&conversation_id=&limit=50&offset=0` → `{ items: Artifact[], total }`
  (newest first; members see their own, admins the whole workspace). `q` matches name/path, `kind` is a `FileKind`,
  `source` = `generated` | `uploaded` | `all` (default `all`).
- `GET /api/artifacts/{id}/download` → file bytes
- `GET /api/artifacts/{id}/preview` → `FilePreview`

```ts
interface FilePreview {
  name: string; path: string; mime: string; size: number; modified_at: string | null; kind: FileKind;
  // How the UI should render it:
  render: "markdown" | "code" | "table" | "html" | "image" | "pdf" | "audio" | "video" | "none";
  content: string | null;      // markdown (also docx/pptx/xlsx converted to markdown), code/text, or HTML source
  language: string | null;     // render = "code": e.g. "python", "json", "text"
  table: { columns: string[]; rows: string[][]; total_rows: number } | null;   // render = "table" (csv/tsv)
  truncated: boolean;          // content/table cut to the preview limit (download for the full file)
  converted_from: string | null;   // "docx" | "pptx" | "xlsx" when shown as markdown
  download: { path: string; query: Record<string, string> };  // API route for the raw bytes, e.g. {path: "/artifacts/ID/download", query: {}}
  artifact_id: string | null; task_id: string | null; task_title: string | null; agent_name: string | null;
  created_at: string | null;
}
```
Raw bytes (images, PDFs, media) need the bearer header: fetch `download` with auth and display via an object URL.
HTML previews must be shown in a sandboxed iframe (`srcdoc`, no `allow-scripts`).

### Audit & notifications
- `GET /api/audit?action=&user_id=&agent_id=&risk=&limit=50&offset=0` → `{ items: AuditEntry[], total }` (admin)
- `GET /api/notifications?unread=false` → list of `Notification`
- `POST /api/notifications/{id}/read` → `{ ok: true }` ; `POST /api/notifications/read-all` → `{ ok: true }`

### Settings
- `GET /api/settings/system` → `{ version: string; db: "postgresql" | "sqlite"; embedding: { provider: string; model: string; dim: number }; reranker: { provider: string; model: string | null }; sandbox: "local" | "remote"; browser: boolean; laya: { mode, url, reachable }; mcp_endpoint: string }`

---

## Real-time stream (Server-Sent Events)

`GET /api/stream` with headers `Authorization` and `X-Org-Id`. Use `fetch()` + `ReadableStream` (not `EventSource`, which cannot send headers).
Each event is `data: <json>\n\n` where json is `{ "type": string, "data": object }`. A `{"type":"ping"}` heartbeat arrives every 15 s.
On reconnect, refetch the active views (events are not replayed).

| type | data |
|---|---|
| `message.created` | `{ message: Message }` |
| `message.delta` | `{ conversation_id, message_id, delta: string }` — append to content |
| `message.completed` | `{ message: Message }` — final content + metadata |
| `message.updated` | `{ message: Message }` — e.g. alignment verdict attached, event card status changed |
| `conversation.created` / `conversation.updated` | `{ conversation: Conversation }` |
| `conversation.status` | `{ conversation_id, status: ConversationStatus, detail: string \| null, queued: number }` |
| `task.created` / `task.updated` | `{ task: Task }` |
| `task.event` | `{ task_id, event: TaskEvent }` |
| `approval.requested` / `approval.resolved` | `{ approval: Approval }` |
| `document.updated` | `{ document: KDocument }` |
| `notification.created` | `{ notification: Notification }` |
| `ping` | `{}` |

### Chat lifecycle (what the UI should expect)
1. User posts → `202 Message` (status `complete`, or `queued` if Ren is mid-reply) and `message.created` for the same message.
2. `conversation.status` → `thinking`.
3. `message.created` (assistant, `status: "streaming"`, empty content) → many `message.delta` → `message.completed`.
4. When Ren delegates work: `message.created` (role `event`, `event.kind = "task_created"`) + `task.created`; the task then streams `task.updated` / `task.event`.
5. When a task needs approval: `approval.requested` + an event message (`approval_requested`) with inline approve/reject.
6. When a task finishes: `task.updated` (completed) + event message (`task_completed`) + Ren posts a proactive assistant summary.
7. Alignment: assistant messages carry `metadata.alignment.verdict = "pending"` then `message.updated` with the final verdict.
   If blocked under `post` mode, the content is replaced by the revised, compliant reply and `alignment.revised = true`.
8. `conversation.status` → `idle` (with `queued: 0`).

### Schedules
```ts
type ScheduleStatus = "scheduled" | "paused" | "done" | "cancelled" | "failed";
interface ScheduledJob {
  id: string; kind: "task" | "notification"; title: string; status: ScheduleStatus;
  cron: string | null;          // recurring: 5-field cron, interpreted in `timezone`; null = one-off
  timezone: string;             // IANA zone, e.g. "Asia/Kolkata" ("UTC" for older jobs)
  run_at: string;               // next run (UTC ISO); for paused jobs the next run it would have had
  description: string;          // human summary, e.g. "Every day at 11:00 (UTC)", "Once · Tue 6 Oct, 09:00 (Asia/Kolkata)"
  last_run_at: string | null; run_count: number; created_at: string; user_id: string | null;
  agent_id: string | null; agent_name: string | null;   // kind = "task"
  instructions: string | null;  // task instructions, or the reminder text for notifications
  conversation_id: string | null;
  last_task: { id: string; title: string; status: TaskStatus; created_at: string; finished_at: string | null } | null;
  payload: Record<string, any>;
}
interface ScheduleRun { id: string; title: string; status: TaskStatus; created_at: string; started_at: string | null;
  finished_at: string | null; error: string | null; artifacts: Artifact[]; }
```
- `GET /api/schedules?status=active|paused|history|all` → `{ items: ScheduledJob[], total }` (default `active` = scheduled +
  paused; `history` = done/cancelled/failed). Members see their own; admins the whole workspace. Sorted by next run.
- `GET /api/schedules/preview?cron=&run_at=&timezone=&count=5` → `{ valid: boolean; error: string | null; description: string; next: string[] }`
  (`next` = upcoming run times, UTC ISO). Use it to validate and preview a schedule while editing.
- `POST /api/schedules` (member) `{ title, agent_id, instructions, cron?, run_at?, timezone? }` → `ScheduledJob` (kind `task`;
  give `cron` for recurring or `run_at` for one-off; a `run_at` without offset is read in `timezone`; default timezone = the user's)
- `GET /api/schedules/{id}` → `{ job: ScheduledJob, runs: ScheduleRun[] }` (latest 25 runs, newest first)
- `PATCH /api/schedules/{id}` (member: own; admin: any) `{ title?, instructions?, agent_id?, cron?, run_at?, timezone? }` → `ScheduledJob`
  (`cron: null` turns it into a one-off and requires `run_at`; changing cron/timezone recomputes the next run)
- `POST /api/schedules/{id}/pause` → `ScheduledJob` ; `POST /api/schedules/{id}/resume` → `ScheduledJob` (recurring jobs resume at their next occurrence)
- `POST /api/schedules/{id}/run` → `{ task: Task }` — run once now (doesn't change the schedule; counts as a run)
- `DELETE /api/schedules/{id}` → `{ ok: true }` (status `cancelled`; it moves to history)
- Created by Ren (`schedule_task`, which takes a `timezone`), agents (`schedule_task` tool) or reminders (`notify_schedule`).
  Tasks started by a schedule carry `schedule_id`.
