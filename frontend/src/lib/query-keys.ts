import type { ApprovalStatus, ArtifactSource, FileKind, TaskStatus } from "./types";

export interface ConversationListParams {
  archived?: boolean;
  q?: string;
}

export interface TaskListParams {
  status?: TaskStatus | "";
  agent_id?: string;
  conversation_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface DocumentListParams {
  collection_id?: string;
  q?: string;
  status?: string;
}

export type ApprovalFilter = ApprovalStatus | "all";

export interface ArtifactListParams {
  q?: string;
  kind?: FileKind | "";
  source?: ArtifactSource | "all" | "";
  task_id?: string;
  conversation_id?: string;
  limit?: number;
  offset?: number;
}

/** active = scheduled + paused; history = done / cancelled / failed. */
export type ScheduleFilter = "active" | "scheduled" | "paused" | "history" | "all";

export interface SchedulePreviewParams {
  cron?: string | null;
  run_at?: string | null;
  timezone?: string | null;
  count?: number;
}

/** What a file viewer opens: a registered artifact, or any workspace path. */
export type FileSource = { artifactId: string } | { path: string };

/**
 * Central registry of TanStack Query keys. The SSE stream dispatcher writes to
 * these same keys, so every list/detail view updates live.
 */
export const qk = {
  me: ["me"] as const,
  org: ["org"] as const,
  orgs: ["orgs"] as const,
  members: ["members"] as const,
  invites: ["invites"] as const,
  dashboard: ["dashboard"] as const,
  providers: ["providers"] as const,
  providerModels: (id: string) => ["providers", id, "models"] as const,

  agents: ["agents"] as const,
  agent: (id: string) => ["agents", id] as const,

  /** Prefix for every conversation list query. */
  conversationLists: ["conversations"] as const,
  conversations: (params: ConversationListParams = {}) =>
    ["conversations", { archived: params.archived ?? false, q: params.q ?? "" }] as const,
  /** `Conversation & { messages }` */
  conversation: (id: string) => ["conversation", id] as const,
  /** `{ status, detail, queued }` from `conversation.status` events */
  conversationStatus: (id: string) => ["conversation-status", id] as const,
  conversationTasks: (id: string) => ["conversation-tasks", id] as const,

  tasks: ["tasks"] as const,
  taskLists: ["tasks", "list"] as const,
  taskList: (params: TaskListParams) => ["tasks", "list", params] as const,
  task: (id: string) => ["tasks", "detail", id] as const,

  approvalLists: ["approvals"] as const,
  approvals: (status: ApprovalFilter) => ["approvals", status] as const,

  collections: ["collections"] as const,
  documentLists: ["documents", "list"] as const,
  documents: (params: DocumentListParams) => ["documents", "list", params] as const,
  document: (id: string) => ["documents", "detail", id] as const,
  section: (docId: string, sectionId: string) => ["documents", "section", docId, sectionId] as const,
  knowledgeStats: ["knowledge", "stats"] as const,

  policies: ["policies"] as const,
  alignmentChecks: (params: Record<string, unknown>) => ["alignment", "checks", params] as const,
  alignmentStats: ["alignment", "stats"] as const,
  alignmentStatus: ["alignment", "status"] as const,

  toolCatalog: ["tools", "catalog"] as const,
  connectorKinds: ["connectors", "kinds"] as const,
  connectors: ["connectors", "list"] as const,
  mcpServers: ["mcp", "servers"] as const,
  mcpPresets: ["mcp", "presets"] as const,

  skills: ["skills"] as const,
  memories: (params: Record<string, unknown>) => ["memories", params] as const,
  memoryLists: ["memories"] as const,
  audit: (params: Record<string, unknown>) => ["audit", params] as const,
  notifications: ["notifications"] as const,
  systemSettings: ["settings", "system"] as const,
  /** Prefix for every artifact list query (invalidated when agents produce files). */
  artifactLists: ["artifacts"] as const,
  artifacts: (params: ArtifactListParams) => ["artifacts", params] as const,
  filePreview: (source: FileSource) =>
    "artifactId" in source ? (["file-preview", "artifact", source.artifactId] as const) : (["file-preview", "path", source.path] as const),
  workspaceFiles: (path: string) => ["files", path] as const,

  /** Prefix for schedule lists and details (not previews). */
  schedules: ["schedules"] as const,
  scheduleLists: ["schedules", "list"] as const,
  scheduleList: (status: ScheduleFilter) => ["schedules", "list", status] as const,
  /** `{ job, runs }` */
  schedule: (id: string) => ["schedules", "detail", id] as const,
  schedulePreview: (params: SchedulePreviewParams) => ["schedule-preview", params] as const,
};
