"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, type QueryParams } from "@/lib/api";
import type { LiveConversationStatus } from "@/lib/cache";
import {
  qk,
  type ApprovalFilter,
  type ArtifactListParams,
  type ConversationListParams,
  type DocumentListParams,
  type FileSource,
  type ScheduleFilter,
  type SchedulePreviewParams,
  type TaskListParams,
} from "@/lib/query-keys";
import type {
  Agent,
  AlignmentCheck,
  AlignmentStats,
  AlignmentStatus,
  Approval,
  Artifact,
  AuditEntry,
  Collection,
  Connector,
  ConnectorKind,
  Conversation,
  ConversationDetail,
  DashboardResponse,
  FilePreview,
  Invite,
  KDocument,
  KDocumentDetail,
  KnowledgeStats,
  ListResponse,
  McpPreset,
  McpServer,
  Member,
  Memory,
  Notification,
  Org,
  Policy,
  Provider,
  ProviderModel,
  ScheduledJob,
  ScheduleDetail,
  SchedulePreview,
  SectionDetail,
  Skill,
  SystemSettings,
  Task,
  TaskDetail,
  ToolGroup,
  WorkspaceFile,
} from "@/lib/types";

/** List endpoints return `{ items, total }`; tolerate a bare array too. */
export async function getList<T>(path: string, query?: QueryParams): Promise<ListResponse<T>> {
  const data = await api.get<ListResponse<T> | T[]>(path, query);
  if (Array.isArray(data)) return { items: data, total: data.length };
  if (data && Array.isArray((data as ListResponse<T>).items)) return data as ListResponse<T>;
  return { items: [], total: 0 };
}

/* ---------------- Workspace / auth ---------------- */

export function useOrg() {
  return useQuery({ queryKey: qk.org, queryFn: () => api.get<Org>("/orgs/current") });
}

export function useMembers(enabled = true) {
  return useQuery({ queryKey: qk.members, queryFn: () => getList<Member>("/orgs/current/members"), enabled });
}

export function useInvites(enabled = true) {
  return useQuery({ queryKey: qk.invites, queryFn: () => getList<Invite>("/orgs/current/invites"), enabled });
}

export function useProviders(enabled = true) {
  return useQuery({ queryKey: qk.providers, queryFn: () => getList<Provider>("/providers"), enabled });
}

export function useProviderModels(providerId: string | null | undefined) {
  return useQuery({
    queryKey: qk.providerModels(providerId ?? ""),
    queryFn: () => getList<ProviderModel>(`/providers/${providerId}/models`),
    enabled: !!providerId,
    staleTime: 10 * 60_000,
  });
}

export function useSystemSettings() {
  return useQuery({
    queryKey: qk.systemSettings,
    queryFn: () => api.get<SystemSettings>("/settings/system"),
    staleTime: 5 * 60_000,
  });
}

export function useDashboard() {
  return useQuery({ queryKey: qk.dashboard, queryFn: () => api.get<DashboardResponse>("/dashboard") });
}

/* ---------------- Agents ---------------- */

export function useAgents() {
  return useQuery({ queryKey: qk.agents, queryFn: () => getList<Agent>("/agents") });
}

export function useAgentsMap() {
  const { data } = useAgents();
  return useMemo(() => new Map((data?.items ?? []).map((a) => [a.id, a])), [data]);
}

export function useAgent(id: string | null | undefined) {
  const { data: list } = useAgents();
  return useQuery({
    queryKey: qk.agent(id ?? ""),
    queryFn: () => api.get<Agent>(`/agents/${id}`),
    enabled: !!id,
    placeholderData: () => list?.items.find((a) => a.id === id),
  });
}

/* ---------------- Conversations ---------------- */

export function useConversations(params: ConversationListParams = {}) {
  const key = qk.conversations(params);
  return useQuery({
    queryKey: key,
    queryFn: () => getList<Conversation>("/conversations", { archived: key[1].archived, q: key[1].q }),
    placeholderData: (prev) => prev,
  });
}

export function useConversation(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.conversation(id ?? ""),
    queryFn: () => api.get<ConversationDetail>(`/conversations/${id}`),
    enabled: !!id,
    staleTime: 60_000,
  });
}

export function useConversationTasks(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.conversationTasks(id ?? ""),
    queryFn: () => getList<Task>(`/conversations/${id}/tasks`),
    enabled: !!id,
  });
}

/** Live `{ status, detail, queued }` written by `conversation.status` stream events. */
export function useConversationLiveStatus(id: string | null | undefined): LiveConversationStatus | null {
  const { data } = useQuery<LiveConversationStatus | null>({
    queryKey: qk.conversationStatus(id ?? ""),
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
  });
  return data ?? null;
}

/* ---------------- Tasks ---------------- */

export function useTasks(params: TaskListParams, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.taskList(params),
    queryFn: () =>
      getList<Task>("/tasks", {
        status: params.status || undefined,
        agent_id: params.agent_id,
        conversation_id: params.conversation_id,
        q: params.q,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      }),
    placeholderData: (prev) => prev,
    enabled: opts.enabled ?? true,
  });
}

export function useTask(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.task(id ?? ""),
    queryFn: () => api.get<TaskDetail>(`/tasks/${id}`),
    enabled: !!id,
  });
}

/* ---------------- Approvals ---------------- */

export function useApprovals(status: ApprovalFilter = "pending") {
  return useQuery({
    queryKey: qk.approvals(status),
    queryFn: () => getList<Approval>("/approvals", { status }),
  });
}

/* ---------------- Knowledge ---------------- */

export function useCollections() {
  return useQuery({ queryKey: qk.collections, queryFn: () => getList<Collection>("/collections") });
}

export function useDocuments(params: DocumentListParams) {
  return useQuery({
    queryKey: qk.documents(params),
    queryFn: () => getList<KDocument>("/documents", { ...params }),
    placeholderData: (prev) => prev,
  });
}

export function useDocument(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.document(id ?? ""),
    queryFn: () => api.get<KDocumentDetail>(`/documents/${id}`),
    enabled: !!id,
  });
}

export function useSection(docId: string | null | undefined, sectionId: string | null | undefined) {
  return useQuery({
    queryKey: qk.section(docId ?? "", sectionId ?? ""),
    queryFn: () => api.get<SectionDetail>(`/documents/${docId}/sections/${sectionId}`),
    enabled: !!docId && !!sectionId,
    staleTime: 5 * 60_000,
  });
}

export function useKnowledgeStats() {
  return useQuery({ queryKey: qk.knowledgeStats, queryFn: () => api.get<KnowledgeStats>("/knowledge/stats") });
}

/* ---------------- Policies & alignment ---------------- */

export function usePolicies() {
  return useQuery({ queryKey: qk.policies, queryFn: () => getList<Policy>("/policies") });
}

export function useAlignmentStats() {
  return useQuery({ queryKey: qk.alignmentStats, queryFn: () => api.get<AlignmentStats>("/alignment/stats") });
}

export function useAlignmentStatus() {
  return useQuery({
    queryKey: qk.alignmentStatus,
    queryFn: () => api.get<AlignmentStatus>("/alignment/status"),
    refetchInterval: 60_000,
  });
}

export function useAlignmentChecks(params: { verdict?: string; agent_id?: string; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: qk.alignmentChecks(params),
    queryFn: () => getList<AlignmentCheck>("/alignment/checks", { ...params }),
    placeholderData: (prev) => prev,
  });
}

/* ---------------- Tools, connectors, MCP ---------------- */

export function useToolCatalog() {
  return useQuery({ queryKey: qk.toolCatalog, queryFn: () => getList<ToolGroup>("/tools/catalog") });
}

export function useConnectorKinds() {
  return useQuery({
    queryKey: qk.connectorKinds,
    queryFn: () => getList<ConnectorKind>("/connectors/kinds"),
    staleTime: 10 * 60_000,
  });
}

export function useConnectors() {
  return useQuery({ queryKey: qk.connectors, queryFn: () => getList<Connector>("/connectors") });
}

export function useMcpServers() {
  return useQuery({ queryKey: qk.mcpServers, queryFn: () => getList<McpServer>("/mcp/servers") });
}

export function useMcpPresets() {
  return useQuery({
    queryKey: qk.mcpPresets,
    queryFn: () => getList<McpPreset>("/mcp/presets"),
    staleTime: 10 * 60_000,
  });
}

/* ---------------- Skills, memory, audit, notifications ---------------- */

export function useSkills() {
  return useQuery({ queryKey: qk.skills, queryFn: () => getList<Skill>("/skills") });
}

export function useMemories(params: { q?: string; kind?: string; scope?: string }) {
  return useQuery({
    queryKey: qk.memories(params),
    queryFn: () => getList<Memory>("/memories", { ...params }),
    placeholderData: (prev) => prev,
  });
}

export function useAudit(params: Record<string, string | number | undefined>, enabled = true) {
  return useQuery({
    queryKey: qk.audit(params),
    queryFn: () => getList<AuditEntry>("/audit", params),
    placeholderData: (prev) => prev,
    enabled,
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: qk.notifications,
    queryFn: () => getList<Notification>("/notifications", { unread: false }),
  });
}

/* ---------------- Files & outputs ---------------- */

/** `GET /artifacts` — newest first; keeps the previous page on screen while filters change. */
export function useArtifacts(params: ArtifactListParams, enabled = true) {
  return useQuery({
    queryKey: qk.artifacts(params),
    queryFn: () =>
      getList<Artifact>("/artifacts", {
        q: params.q || undefined,
        kind: params.kind || undefined,
        source: params.source || undefined,
        task_id: params.task_id,
        conversation_id: params.conversation_id,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      }),
    placeholderData: (prev) => prev,
    enabled,
  });
}

/** In-app preview of an artifact or a workspace path. */
export function useFilePreview(source: FileSource | null) {
  return useQuery({
    queryKey: source ? qk.filePreview(source) : ["file-preview", "none"],
    queryFn: () =>
      source && "artifactId" in source
        ? api.get<FilePreview>(`/artifacts/${source.artifactId}/preview`)
        : api.get<FilePreview>("/files/preview", { path: source?.path }),
    enabled: !!source,
    staleTime: 60_000,
  });
}

/** One folder of the caller's workspace (`""` = the top-level folders). */
export function useWorkspaceFiles(path: string, enabled = true) {
  return useQuery({
    queryKey: qk.workspaceFiles(path),
    queryFn: () => getList<WorkspaceFile>("/files", { path }),
    enabled,
  });
}

/* ---------------- Schedules ---------------- */

export function useSchedules({ status = "active" }: { status?: ScheduleFilter } = {}, enabled = true) {
  return useQuery({
    queryKey: qk.scheduleList(status),
    queryFn: () => getList<ScheduledJob>("/schedules", { status }),
    // Runs show up via task events; this keeps "next run" accurate in between.
    refetchInterval: 60_000,
    enabled,
  });
}

/** `{ job, runs }` — runs are the latest 25 tasks started by the schedule. */
export function useSchedule(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.schedule(id ?? ""),
    queryFn: () => api.get<ScheduleDetail>(`/schedules/${id}`),
    enabled: !!id,
    refetchInterval: 60_000,
  });
}

/** Validate a cron / run_at and list the next runs (UTC ISO). */
export function useSchedulePreview(params: SchedulePreviewParams, enabled = true) {
  return useQuery({
    queryKey: qk.schedulePreview(params),
    queryFn: () =>
      api.get<SchedulePreview>("/schedules/preview", {
        cron: params.cron || undefined,
        run_at: params.run_at || undefined,
        timezone: params.timezone || undefined,
        count: params.count ?? 5,
      }),
    enabled: enabled && !!(params.cron || params.run_at),
    placeholderData: (prev) => prev,
    staleTime: 60_000,
  });
}
