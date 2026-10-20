/* eslint-disable @typescript-eslint/no-explicit-any */
// Types mirror docs/API.md (Ren API Contract v1). Keep in sync with the contract.

export type Role = "owner" | "admin" | "member" | "viewer";
export type WorkspaceMode = "business" | "personal";

export interface User {
  id: string;
  email: string;
  name: string;
  created_at: string;
  /** IANA zone remembered from the browser's `X-Timezone` header (null until first seen). */
  timezone: string | null;
}

export interface OrgSummary {
  id: string;
  name: string;
  slug: string;
  mode: WorkspaceMode;
  role: Role;
}

export interface AuthResponse {
  access_token: string;
  token_type: "bearer";
  user: User;
  orgs: OrgSummary[];
}

export interface OrgSettings {
  default_provider_id: string | null;
  orchestrator_model: string | null;
  autonomy_default: Autonomy;
  max_concurrent_tasks: number;
  alignment: {
    enabled: boolean;
    mode: "post" | "pre";
    block_threshold: number;
    warn_threshold: number;
    judge_fallback: boolean;
    /** triage (default): Laya screens fast, the judge confirms suspicious cases; authoritative: Laya's scores decide. */
    laya_mode: "triage" | "authoritative";
    /** triage: Laya score (0..1) at/above which the judge reviews (default 0.5). */
    escalate_threshold: number;
  };
  retrieval: {
    stair_enabled: boolean;
    generate_questions: boolean;
    structure_augmentation: boolean;
    rerank: boolean;
    top_k: number;
  };
  web_search: {
    provider: "duckduckgo" | "tavily" | "brave" | "searxng";
    has_key: boolean;
    base_url?: string | null;
  };
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  mode: WorkspaceMode;
  created_at: string;
  settings: OrgSettings;
  member_count: number;
}

export interface Member {
  user_id: string;
  email: string;
  name: string;
  role: Role;
  title: string | null;
  joined_at: string;
  last_active_at: string | null;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  token: string;
  link: string;
  created_at: string;
  expires_at: string;
  accepted: boolean;
  invited_by_name: string | null;
}

export type ProviderKind = "openrouter" | "google" | "vllm" | "openai" | "custom" | "mock";

export interface Provider {
  id: string;
  name: string;
  kind: ProviderKind;
  base_url: string;
  default_model: string;
  has_key: boolean;
  is_default: boolean;
  supports_tools: boolean;
  extra: Record<string, any>;
  created_at: string;
}

export type Autonomy = "supervised" | "balanced" | "autonomous";

export interface AgentStats {
  active_tasks: number;
  tasks_total: number;
  tasks_completed: number;
  tasks_failed: number;
  success_rate: number;
  avg_duration_s: number;
  tokens_total: number;
  alignment_pass_rate: number;
}

export interface Agent {
  id: string;
  name: string;
  slug: string;
  role: string;
  description: string;
  system_prompt: string;
  provider_id: string | null;
  model: string | null;
  temperature: number;
  tool_groups: string[];
  policy_ids: string[];
  collection_ids: string[];
  autonomy: Autonomy;
  max_steps: number;
  color: string;
  icon: string;
  is_orchestrator: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  stats?: AgentStats;
}

export type ConversationStatus = "idle" | "thinking" | "responding" | "using_tool";

export interface Conversation {
  id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  updated_at: string;
  last_message_preview: string | null;
  active_tasks: number;
  status: ConversationStatus;
}

export type MessageRole = "user" | "assistant" | "event" | "system";
export type MessageStatus = "streaming" | "complete" | "error" | "queued";

export interface ToolCall {
  id: string;
  name: string;
  display: string;
  args: any;
  status: "running" | "done" | "error";
  result_preview?: string;
}

export interface MessageMetadata {
  citations?: Citation[];
  tool_calls?: ToolCall[];
  alignment?: AlignmentSummary;
  event?: ChatEvent;
  attachments?: Attachment[];
  mention_agent_id?: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  agent_id: string | null;
  task_id: string | null;
  status: MessageStatus;
  metadata: MessageMetadata;
  created_at: string;
  user_id: string | null;
}

export interface Citation {
  id: number;
  document_id: string;
  document_title: string;
  section_id: string | null;
  section_path: string | null;
  section_title: string | null;
  breadcrumb: string | null;
  snippet: string;
  score: number | null;
}

export type ChatEventKind =
  | "task_created"
  | "task_completed"
  | "task_failed"
  | "task_cancelled"
  | "task_paused"
  | "approval_requested"
  | "approval_resolved"
  | "skill_started"
  | "info";

export interface ChatEvent {
  kind: ChatEventKind;
  task_id?: string;
  approval_id?: string;
  agent_id?: string;
  agent_name?: string;
  title?: string;
  status?: string;
  summary?: string;
  risk?: Risk;
  tool?: string;
  decision?: "approved" | "rejected" | "expired";
}

export interface Attachment {
  id: string;
  name: string;
  path: string;
  size: number;
  mime: string;
  document_id: string | null;
}

export type AlignmentVerdict = "pass" | "warn" | "block" | "pending" | "skipped";

export interface PolicyViolation {
  policy_id: string;
  policy_name: string;
  rule: string | null;
  score: number;
  explanation?: string | null;
}

export interface AlignmentSummary {
  verdict: AlignmentVerdict;
  checker: "rules" | "laya" | "judge" | "laya+judge" | "none";
  latency_ms: number | null;
  violations: PolicyViolation[];
  revised?: boolean;
}

export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type Priority = "low" | "normal" | "high" | "urgent";

/** Derived by the backend from the file extension. */
export type FileKind =
  | "document"
  | "spreadsheet"
  | "presentation"
  | "pdf"
  | "image"
  | "data"
  | "code"
  | "web"
  | "text"
  | "audio"
  | "video"
  | "archive"
  | "other";

/** generated = written by an agent task; uploaded = chat attachment. */
export type ArtifactSource = "generated" | "uploaded";

export interface Artifact {
  id: string;
  name: string;
  path: string;
  mime: string;
  size: number;
  task_id: string | null;
  conversation_id: string | null;
  created_at: string;
  kind: FileKind;
  source: ArtifactSource;
  task_title: string | null;
  agent_id: string | null;
  agent_name: string | null;
  schedule_id: string | null;
}

export type FilePreviewRender = "markdown" | "code" | "table" | "html" | "image" | "pdf" | "audio" | "video" | "none";

/** `GET /files/preview?path=` and `GET /artifacts/{id}/preview`. */
export interface FilePreview {
  name: string;
  path: string;
  mime: string;
  size: number;
  modified_at: string | null;
  kind: FileKind;
  render: FilePreviewRender;
  /** Markdown (docx/pptx/xlsx are converted to markdown), code/text, or HTML source. */
  content: string | null;
  /** render = "code": e.g. "python", "json", "text". */
  language: string | null;
  /** render = "table" (csv/tsv). */
  table: { columns: string[]; rows: string[][]; total_rows: number } | null;
  /** content/table cut to the preview limit (download for the full file). */
  truncated: boolean;
  /** "docx" | "pptx" | "xlsx" when shown as markdown. */
  converted_from: string | null;
  /** API route for the raw bytes, e.g. `{ path: "/artifacts/ID/download", query: {} }`. */
  download: { path: string; query: Record<string, string> };
  artifact_id: string | null;
  task_id: string | null;
  task_title: string | null;
  agent_name: string | null;
  created_at: string | null;
}

export interface Task {
  id: string;
  title: string;
  instructions: string;
  status: TaskStatus;
  priority: Priority;
  agent_id: string;
  agent_name: string;
  agent_color: string;
  conversation_id: string | null;
  parent_id: string | null;
  skill_id: string | null;
  progress: number;
  current_step: string | null;
  result: string | null;
  error: string | null;
  steps: number;
  tokens: number;
  created_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  children_count: number;
  artifacts: Artifact[];
  /** Set when the task was started by a schedule. */
  schedule_id: string | null;
}

export type TaskEventKind =
  | "status"
  | "thought"
  | "tool_call"
  | "tool_result"
  | "message"
  | "progress"
  | "inbox"
  | "policy"
  | "delegation"
  | "error"
  | "artifact";

export interface TaskEvent {
  id: string;
  task_id: string;
  kind: TaskEventKind;
  content: Record<string, any>;
  created_at: string;
}

export interface TaskDetail extends Task {
  events: TaskEvent[];
  children: Task[];
  approvals: Approval[];
}

export type Risk = "low" | "medium" | "high" | "critical";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export interface Approval {
  id: string;
  task_id: string | null;
  task_title: string | null;
  agent_id: string | null;
  agent_name: string | null;
  tool: string;
  tool_display: string;
  args: Record<string, any>;
  risk: Risk;
  reason: string;
  preview: string;
  status: ApprovalStatus;
  requested_for_user_id: string | null;
  conversation_id: string | null;
  created_at: string;
  decided_by_name: string | null;
  decided_at: string | null;
  comment: string | null;
  expires_at: string | null;
}

export interface Collection {
  id: string;
  name: string;
  description: string;
  access: "org" | "restricted";
  member_ids: string[];
  document_count: number;
  created_at: string;
}

export type DocumentStatus =
  | "uploaded"
  | "parsing"
  | "structuring"
  | "embedding"
  | "questions"
  | "ready"
  | "failed";

export type StructureSource = "native" | "headings" | "augmented" | "flat";

export interface KDocument {
  id: string;
  collection_id: string;
  collection_name: string;
  title: string;
  filename: string;
  mime: string;
  size: number;
  status: DocumentStatus;
  progress: number;
  error: string | null;
  page_count: number | null;
  section_count: number;
  leaf_count: number;
  chunk_count: number;
  question_count: number;
  structure_source: StructureSource | null;
  uploaded_by_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface TocNode {
  id: string;
  path: string;
  title: string;
  level: number;
  is_leaf: boolean;
  token_count: number;
  summary: string | null;
  children: TocNode[];
}

export interface KDocumentDetail extends KDocument {
  toc: TocNode[];
}

export interface SectionDetail {
  id: string;
  document_id: string;
  path: string;
  title: string;
  level: number;
  is_leaf: boolean;
  breadcrumb: string;
  content: string;
  summary: string | null;
  token_count: number;
  questions: string[];
  children: { id: string; path: string; title: string; is_leaf: boolean }[];
  page_start: number | null;
  page_end: number | null;
}

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  document_title: string;
  section_id: string | null;
  section_path: string | null;
  section_title: string | null;
  breadcrumb: string | null;
  content: string;
  score: number;
  rerank_score: number | null;
  sources: {
    bm25?: number | null;
    vector?: number | null;
    stair?: number | null;
    questions?: number | null;
  };
}

export interface StairSelection {
  document_id: string;
  document_title: string;
  section_id: string;
  path: string;
  title: string;
  breadcrumb: string;
  confidence: number | null;
}

export interface SearchResponse {
  query: string;
  results: SearchResult[];
  stair: {
    enabled: boolean;
    selected: StairSelection[];
    method: string;
    latency_ms: number;
    candidates_considered: number;
  };
  timings: Record<string, number>;
  context: string;
}

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  alignment: AlignmentSummary;
  retrieval: SearchResponse;
}

export type SearchMode = "hybrid" | "semantic" | "lexical" | "structure";

export interface Policy {
  id: string;
  name: string;
  description: string;
  category: string;
  rules: string[];
  patterns: string[];
  severity: "block" | "warn";
  scope: "org" | "agent";
  applies_to: "responses" | "outbound" | "all";
  agent_ids: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AlignmentCheck {
  id: string;
  agent_id: string | null;
  agent_name: string | null;
  task_id: string | null;
  message_id: string | null;
  kind: "response" | "outbound" | "test";
  verdict: AlignmentVerdict;
  checker: string;
  latency_ms: number;
  violations: PolicyViolation[];
  excerpt: string;
  created_at: string;
}

export interface ToolInfo {
  name: string;
  display_name: string;
  description: string;
  risk: Risk;
  scope: "read" | "write" | "admin";
}

export interface ToolGroup {
  id: string;
  name: string;
  description: string;
  icon: string;
  category: string;
  connector_kind: string | null;
  connected: boolean;
  enabled: boolean;
  available: boolean;
  unavailable_reason: string | null;
  tools: ToolInfo[];
}

export interface ConnectorField {
  key: string;
  label: string;
  type: "text" | "password" | "url" | "select" | "textarea" | "number";
  required: boolean;
  secret: boolean;
  placeholder?: string;
  help?: string;
  options?: string[];
}

export interface ConnectorKind {
  kind: string;
  name: string;
  description: string;
  icon: string;
  category: string;
  fields: ConnectorField[];
  tool_groups: string[];
  docs_url?: string;
}

export interface Connector {
  id: string;
  kind: string;
  name: string;
  status: "connected" | "error" | "unknown";
  config: Record<string, any>;
  error: string | null;
  created_at: string;
}

export interface McpServer {
  id: string;
  name: string;
  transport: "stdio" | "http" | "sse";
  command: string | null;
  args: string[];
  url: string | null;
  enabled: boolean;
  status: "connected" | "error" | "disconnected";
  error: string | null;
  tools: { name: string; description: string }[];
  env_keys: string[];
  header_keys: string[];
  created_at: string;
}

export interface McpPreset {
  id: string;
  name: string;
  description: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  url?: string;
  env_fields: ConnectorField[];
  docs_url?: string;
}

export interface SkillInput {
  key: string;
  label: string;
  type: "text" | "textarea" | "number" | "date" | "select";
  required: boolean;
  placeholder?: string;
  options?: string[];
}

export interface Skill {
  id: string;
  name: string;
  slug: string;
  description: string;
  category: string;
  icon: string;
  instructions: string;
  agent_id: string | null;
  tool_groups: string[];
  inputs: SkillInput[];
  builtin: boolean;
  created_at: string;
}

export type MemoryKind = "episodic" | "semantic" | "preference" | "project" | "org";

export interface Memory {
  id: string;
  content: string;
  kind: MemoryKind;
  scope: "user" | "org";
  agent_id: string | null;
  importance: number;
  source: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEntry {
  id: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  user_id: string | null;
  user_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  task_id: string | null;
  risk: Risk | null;
  details: Record<string, any>;
  created_at: string;
}

export interface Notification {
  id: string;
  kind: string;
  title: string;
  body: string;
  link: string | null;
  read: boolean;
  created_at: string;
}

export type ScheduleStatus = "scheduled" | "paused" | "done" | "cancelled" | "failed";

export interface ScheduledJob {
  id: string;
  /** task = runs an agent; notification = a reminder (no agent). */
  kind: "task" | "notification";
  title: string;
  status: ScheduleStatus;
  /** Recurring: 5-field cron, interpreted in `timezone`; null = one-off. */
  cron: string | null;
  /** IANA zone, e.g. "Asia/Kolkata" ("UTC" for older jobs). */
  timezone: string;
  /** Next run (UTC ISO); for paused jobs the next run it would have had. */
  run_at: string;
  /** Human summary, e.g. "Every day at 11:00 (UTC)", "Once · Tue 6 Oct, 09:00 (Asia/Kolkata)". */
  description: string;
  last_run_at: string | null;
  run_count: number;
  created_at: string;
  user_id: string | null;
  /** kind = "task" */
  agent_id: string | null;
  agent_name: string | null;
  /** Task instructions, or the reminder text for notifications. */
  instructions: string | null;
  conversation_id: string | null;
  last_task: {
    id: string;
    title: string;
    status: TaskStatus;
    created_at: string;
    finished_at: string | null;
  } | null;
  payload: Record<string, any>;
}

/** One task started by a schedule (`id` is the task id). */
export interface ScheduleRun {
  id: string;
  title: string;
  status: TaskStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  artifacts: Artifact[];
}

export interface WorkspaceFile {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified_at: string;
  mime: string | null;
}

/* ------------------------------------------------------------------ */
/* Endpoint response shapes                                            */
/* ------------------------------------------------------------------ */

/** Every list endpoint returns this envelope. */
export interface ListResponse<T> {
  items: T[];
  total: number;
}

export interface HealthResponse {
  status: "ok";
  version: string;
  db: "postgresql" | "sqlite";
}

export interface MeResponse {
  user: User;
  orgs: OrgSummary[];
}

export interface InviteInfo {
  email: string;
  org_name: string;
  role: Role;
  inviter_name: string;
  user_exists: boolean;
}

export type ConversationDetail = Conversation & { messages: Message[] };

export interface DashboardStats {
  active_tasks: number;
  completed_today: number;
  failed_today: number;
  pending_approvals: number;
  documents: number;
  agents: number;
  alignment_pass_rate: number | null;
  tokens_today: number;
  conversations: number;
}

export interface DashboardResponse {
  stats: DashboardStats;
  recent_tasks: Task[];
  recent_activity: AuditEntry[];
  usage_series: { date: string; tokens: number; tasks: number }[];
  agent_utilization: {
    agent_id: string;
    name: string;
    color: string;
    tasks: number;
    tokens: number;
    active: number;
  }[];
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms: number;
  model: string;
  sample: string | null;
  error: string | null;
}

export interface ProviderModel {
  id: string;
  name: string;
  context_length: number | null;
}

export interface SkillRunResponse {
  task: Task;
  conversation_id: string;
}

/** `GET /schedules/{id}` — latest 25 runs, newest first. */
export interface ScheduleDetail {
  job: ScheduledJob;
  runs: ScheduleRun[];
}

/** `GET /schedules/preview` — `next` are upcoming run times (UTC ISO). */
export interface SchedulePreview {
  valid: boolean;
  error: string | null;
  description: string;
  next: string[];
}

/** `POST /schedules/{id}/run` */
export interface ScheduleRunNowResponse {
  task: Task;
}

export interface KnowledgeStats {
  documents: number;
  sections: number;
  leaves: number;
  chunks: number;
  questions: number;
  embedding_model: string;
  embedding_dim: number;
  reranker: string | null;
}

export interface PolicyTestRule {
  policy_id: string;
  policy_name: string;
  rule: string;
  violation_probability: number;
  checker: string;
}

export type PolicyTestResponse = AlignmentSummary & { per_rule: PolicyTestRule[] };

export interface AlignmentStats {
  total: number;
  passed: number;
  warned: number;
  blocked: number;
  pass_rate: number | null;
  p50_latency_ms: number | null;
  by_policy: { policy_id: string; policy_name: string; violations: number }[];
  by_agent: { agent_id: string; agent_name: string; checks: number; pass_rate: number }[];
  series: { date: string; checks: number; violations: number }[];
}

export interface AlignmentStatus {
  laya: {
    configured: boolean;
    reachable: boolean;
    url: string | null;
    mode: "http" | "local" | "none";
    latency_ms: number | null;
    error: string | null;
  };
  judge: { enabled: boolean };
}

export interface SystemSettings {
  version: string;
  db: "postgresql" | "sqlite";
  embedding: { provider: string; model: string; dim: number };
  reranker: { provider: string; model: string | null };
  sandbox: "local" | "remote";
  browser: boolean;
  laya: { mode: string; url: string | null; reachable: boolean };
  mcp_endpoint: string;
}

export interface OkResponse {
  ok: boolean;
}

export interface ConnectorTestResult {
  ok: boolean;
  message: string;
}

/* ------------------------------------------------------------------ */
/* Real-time stream events                                             */
/* ------------------------------------------------------------------ */

export interface ConversationStatusEvent {
  conversation_id: string;
  status: ConversationStatus;
  detail: string | null;
  queued: number;
}

export type StreamEvent =
  | { type: "message.created"; data: { message: Message } }
  | { type: "message.delta"; data: { conversation_id: string; message_id: string; delta: string } }
  | { type: "message.completed"; data: { message: Message } }
  | { type: "message.updated"; data: { message: Message } }
  | { type: "conversation.created"; data: { conversation: Conversation } }
  | { type: "conversation.updated"; data: { conversation: Conversation } }
  | { type: "conversation.status"; data: ConversationStatusEvent }
  | { type: "task.created"; data: { task: Task } }
  | { type: "task.updated"; data: { task: Task } }
  | { type: "task.event"; data: { task_id: string; event: TaskEvent } }
  | { type: "approval.requested"; data: { approval: Approval } }
  | { type: "approval.resolved"; data: { approval: Approval } }
  | { type: "document.updated"; data: { document: KDocument } }
  | { type: "notification.created"; data: { notification: Notification } }
  | { type: "ping"; data: Record<string, never> }
  /** Sent once on connect by newer backends; carries no data. */
  | { type: "ready"; data: Record<string, never> };

export type StreamEventType = StreamEvent["type"];
