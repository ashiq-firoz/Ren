"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, Lock, Plug, Save, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { AUTONOMY_HELP, AUTONOMY_LABEL, RiskMix } from "@/components/domain/status";
import { MobileNavButton } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Checkbox, Slider, Switch } from "@/components/ui/controls";
import { Notice, Skeleton } from "@/components/ui/feedback";
import { AppIcon } from "@/components/ui/icon";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { KV } from "@/components/ui/key-value";
import { confirm } from "@/components/ui/overlay";
import { Tabs } from "@/components/ui/tabs";
import {
  useCollections,
  usePolicies,
  useProviderModels,
  useProviders,
  useTasks,
  useToolCatalog,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import { upsertAgent } from "@/lib/cache";
import { formatDuration, formatNumber, formatPercent } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Agent, Autonomy, ListResponse } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

interface AgentForm {
  name: string;
  role: string;
  description: string;
  color: string;
  icon: string;
  system_prompt: string;
  provider_id: string | null;
  model: string;
  temperature: number;
  max_steps: number;
  tool_groups: string[];
  collection_ids: string[];
  policy_ids: string[];
  autonomy: Autonomy;
  enabled: boolean;
}

const COLORS = ["#8C6D46", "#4F6B5A", "#5A6487", "#875A5A", "#6B5A87", "#5A7D87", "#877A5A", "#3F5F7A", "#7A4F6B", "#5F7A3F", "#A0661A", "#2F6B4F"];

const ICONS = [
  "bot", "search", "telescope", "bar-chart-3", "calculator", "landmark", "pen-line", "code", "terminal", "database",
  "globe", "mail", "calendar", "briefcase", "scale", "shield-check", "megaphone", "users", "file-text", "sparkles",
  "wrench", "workflow", "lightbulb", "target",
];

type Section = "profile" | "instructions" | "model" | "tools" | "knowledge" | "policies" | "autonomy";

function toForm(a: Agent | null): AgentForm {
  return {
    name: a?.name ?? "",
    role: a?.role ?? "",
    description: a?.description ?? "",
    color: a?.color || COLORS[0],
    icon: a?.icon || "bot",
    system_prompt: a?.system_prompt ?? "",
    provider_id: a?.provider_id ?? null,
    model: a?.model ?? "",
    temperature: a?.temperature ?? 0.3,
    max_steps: a?.max_steps ?? 20,
    tool_groups: a?.tool_groups ?? [],
    collection_ids: a?.collection_ids ?? [],
    policy_ids: a?.policy_ids ?? [],
    autonomy: a?.autonomy ?? "balanced",
    enabled: a?.enabled ?? true,
  };
}

function toPayload(f: AgentForm) {
  return { ...f, model: f.model.trim() || null, name: f.name.trim(), role: f.role.trim() };
}

function diff(a: AgentForm, b: AgentForm): Partial<ReturnType<typeof toPayload>> {
  const pa = toPayload(a);
  const pb = toPayload(b);
  const out: Record<string, unknown> = {};
  (Object.keys(pb) as (keyof typeof pb)[]).forEach((k) => {
    if (JSON.stringify(pa[k]) !== JSON.stringify(pb[k])) out[k] = pb[k];
  });
  return out;
}

function toggle(list: string[], id: string) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

function SelectCard({
  selected,
  onToggle,
  disabled,
  children,
  className,
}: {
  selected: boolean;
  onToggle: () => void;
  disabled?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={selected}
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        "relative flex w-full flex-col items-start gap-1 rounded-card border p-3.5 text-left transition-colors disabled:cursor-not-allowed",
        selected ? "border-fg bg-surface" : "border-border bg-surface/60 hover:border-border-strong",
        disabled && "opacity-60",
        className,
      )}
    >
      <span
        className={cn(
          "absolute top-3 right-3 flex size-4 items-center justify-center rounded border",
          selected ? "border-primary bg-primary text-primary-fg" : "border-border-strong bg-surface",
        )}
      >
        {selected && <Check className="size-3" strokeWidth={3} />}
      </span>
      {children}
    </button>
  );
}

function StatsPanel({ agent }: { agent: Agent }) {
  const s = agent.stats;
  const recent = useTasks({ agent_id: agent.id, limit: 6 });
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="Performance" description="All time" />
        <div className="px-4 py-2">
          {s ? (
            <>
              <KV label="Active tasks">{formatNumber(s.active_tasks)}</KV>
              <KV label="Tasks">{formatNumber(s.tasks_total)}</KV>
              <KV label="Completed">{formatNumber(s.tasks_completed)}</KV>
              <KV label="Failed">{formatNumber(s.tasks_failed)}</KV>
              <KV label="Success rate">{formatPercent(s.success_rate)}</KV>
              <KV label="Avg duration">{formatDuration(s.avg_duration_s)}</KV>
              <KV label="Tokens">{formatNumber(s.tokens_total, { compact: true })}</KV>
              <KV label="Alignment pass rate">{formatPercent(s.alignment_pass_rate)}</KV>
            </>
          ) : (
            <p className="py-4 text-center text-xs text-muted">No stats yet.</p>
          )}
        </div>
      </Card>
      <Card className="overflow-hidden">
        <CardHeader
          title="Recent tasks"
          actions={
            <Link href={`/tasks?agent_id=${agent.id}`} className="text-xs font-medium text-muted hover:text-fg">
              All →
            </Link>
          }
        />
        {recent.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
          </div>
        ) : (recent.data?.items.length ?? 0) === 0 ? (
          <p className="px-4 py-6 text-center text-xs text-muted">No tasks yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {recent.data!.items.map((t) => (
              <li key={t.id}>
                <Link href={`/tasks/${t.id}`} className="block px-4 py-2.5 hover:bg-surface-2/60">
                  <p className="truncate text-[12.5px] font-medium text-fg">{t.title}</p>
                  <p className="text-[11px] text-muted capitalize">{t.status.replace("_", " ")}</p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export function AgentEditor({ agent }: { agent: Agent | null }) {
  const qc = useQueryClient();
  const router = useRouter();
  const { isAdmin } = useRole();
  const readOnly = !isAdmin;
  const isNew = !agent;
  const initial = useMemo(() => toForm(agent), [agent]);
  const [form, setForm] = useState<AgentForm>(initial);
  const [baseline, setBaseline] = useState<AgentForm>(initial);
  const [section, setSection] = useState<Section>("profile");
  const set = <K extends keyof AgentForm>(k: K, v: AgentForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  const providers = useProviders();
  const catalog = useToolCatalog();
  const collections = useCollections();
  const policies = usePolicies();
  const defaultProvider = providers.data?.items.find((p) => p.is_default) ?? providers.data?.items[0];
  const modelProvider = form.provider_id ?? defaultProvider?.id ?? null;
  const models = useProviderModels(modelProvider);

  const changes = diff(baseline, form);
  const dirty = isNew || Object.keys(changes).length > 0;
  const valid = form.name.trim().length > 0 && form.role.trim().length > 0;

  const save = useMutation({
    mutationFn: () =>
      isNew ? api.post<Agent>("/agents", toPayload(form)) : api.patch<Agent>(`/agents/${agent!.id}`, changes),
    onSuccess: (saved) => {
      upsertAgent(qc, saved);
      void qc.invalidateQueries({ queryKey: qk.agents });
      toast.success(isNew ? "Agent created" : "Changes saved", { description: saved.name });
      if (isNew) router.replace(`/agents/${saved.id}`);
      else {
        const next = toForm(saved);
        setBaseline(next);
        setForm(next);
      }
    },
    meta: { errorTitle: isNew ? "Couldn't create the agent" : "Couldn't save changes" },
  });

  const remove = useMutation({
    mutationFn: () => api.delete(`/agents/${agent!.id}`),
    onSuccess: () => {
      qc.setQueryData<ListResponse<Agent>>(qk.agents, (old) =>
        old ? { items: old.items.filter((a) => a.id !== agent!.id), total: Math.max(0, old.total - 1) } : old,
      );
      toast.success("Agent deleted");
      router.replace("/agents");
    },
    meta: { errorTitle: "Couldn't delete the agent" },
  });

  const groups = catalog.data?.items ?? [];
  const agentPolicies = (policies.data?.items ?? []).filter((p) => p.scope === "agent");
  const orgPolicies = (policies.data?.items ?? []).filter((p) => p.scope === "org" && p.enabled);

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-5 pb-20 sm:px-6 lg:px-8">
      {/* Sticky header */}
      <div className="sticky top-0 z-20 -mx-4 mb-6 border-b border-border bg-bg/90 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <MobileNavButton className="-ml-1.5" />
            <Link href="/agents" aria-label="Back to agents" className="text-muted hover:text-fg">
              <ArrowLeft className="size-4" />
            </Link>
            <AgentAvatar agent={{ ...form, is_orchestrator: agent?.is_orchestrator }} size="md" />
            <div className="min-w-0">
              <h1 className="font-serif-display truncate text-[24px] leading-tight text-fg">
                {form.name.trim() || (isNew ? "New agent" : "Untitled agent")}
              </h1>
              <p className="truncate text-xs text-muted">{form.role || "Define what this specialist does"}</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {!readOnly && (
              <Switch
                checked={form.enabled}
                onCheckedChange={(v) => set("enabled", v)}
                label={form.enabled ? "Enabled" : "Disabled"}
                size="sm"
                disabled={agent?.is_orchestrator}
              />
            )}
            {!readOnly && !isNew && dirty && (
              <Button variant="ghost" onClick={() => setForm(baseline)}>
                Discard
              </Button>
            )}
            {!readOnly && (
              <Button
                variant="primary"
                icon={<Save className="size-4" />}
                loading={save.isPending}
                disabled={!dirty || !valid}
                onClick={() => save.mutate()}
              >
                {isNew ? "Create agent" : dirty ? "Save changes" : "Saved"}
              </Button>
            )}
          </div>
        </div>
      </div>

      {readOnly && (
        <Notice tone="neutral" icon={<Lock />} className="mb-5" title="Read-only">
          Only workspace admins can change agents.
        </Notice>
      )}

      <div className={cn("grid gap-6", !isNew && "xl:grid-cols-[minmax(0,1fr)_300px]")}>
        <div className="min-w-0">
          <Tabs
            aria-label="Agent settings"
            idPrefix="agent-tab"
            value={section}
            onValueChange={setSection}
            className="mb-5"
            items={[
              { value: "profile", label: "Profile" },
              { value: "instructions", label: "Instructions" },
              { value: "model", label: "Model" },
              { value: "tools", label: "Tools", count: form.tool_groups.length },
              { value: "knowledge", label: "Knowledge", count: form.collection_ids.length },
              { value: "policies", label: "Policies", count: form.policy_ids.length },
              { value: "autonomy", label: "Autonomy" },
            ]}
          />

          <fieldset disabled={readOnly} className="min-w-0">
            {section === "profile" && (
              <div className="space-y-5">
                <Card className="space-y-4 p-5">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="Name" required>
                      <Input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Research Analyst" />
                    </Field>
                    <Field label="Role" required hint="One line Ren uses to decide when to delegate.">
                      <Input value={form.role} onChange={(e) => set("role", e.target.value)} placeholder="Market & competitor research" />
                    </Field>
                  </div>
                  <Field label="Description">
                    <Textarea
                      autosize
                      minRows={3}
                      value={form.description}
                      onChange={(e) => set("description", e.target.value)}
                      placeholder="What this agent is good at, and what it should not do."
                    />
                  </Field>
                </Card>
                <Card className="space-y-5 p-5">
                  <Field label="Color">
                    <div className="flex flex-wrap items-center gap-2">
                      {COLORS.map((c) => (
                        <button
                          key={c}
                          type="button"
                          aria-label={`Color ${c}`}
                          aria-pressed={form.color.toLowerCase() === c.toLowerCase()}
                          onClick={() => set("color", c)}
                          className={cn(
                            "size-7 rounded-full ring-offset-2 ring-offset-surface transition-shadow",
                            form.color.toLowerCase() === c.toLowerCase() && "ring-2 ring-fg",
                          )}
                          style={{ background: c }}
                        />
                      ))}
                      <Input
                        value={form.color}
                        onChange={(e) => set("color", e.target.value)}
                        aria-label="Custom color (hex)"
                        inputSize="sm"
                        wrapperClassName="w-28"
                        className="w-28 font-mono"
                      />
                    </div>
                  </Field>
                  <Field label="Icon" hint="Any lucide icon name (kebab-case), e.g. telescope or bar-chart-3.">
                    <div className="space-y-3">
                      <div className="grid grid-cols-8 gap-1.5 sm:grid-cols-12">
                        {ICONS.map((name) => (
                          <button
                            key={name}
                            type="button"
                            title={name}
                            aria-label={`Icon ${name}`}
                            aria-pressed={form.icon === name}
                            onClick={() => set("icon", name)}
                            className={cn(
                              "flex aspect-square items-center justify-center rounded-lg border transition-colors",
                              form.icon === name ? "border-fg bg-surface-2" : "border-border hover:border-border-strong",
                            )}
                            style={{ color: form.color }}
                          >
                            <AppIcon name={name} className="size-4" />
                          </button>
                        ))}
                      </div>
                      <Input
                        value={form.icon}
                        onChange={(e) => set("icon", e.target.value.trim())}
                        aria-label="Icon name"
                        inputSize="sm"
                        wrapperClassName="max-w-xs"
                        className="font-mono"
                        leftIcon={<AppIcon name={form.icon} />}
                      />
                    </div>
                  </Field>
                </Card>
                {!isNew && !agent?.is_orchestrator && isAdmin && (
                  <Card className="border-[color-mix(in_srgb,var(--danger)_30%,transparent)] p-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-[13.5px] font-semibold text-fg">Delete this agent</p>
                        <p className="mt-0.5 text-xs text-muted">Its task history is kept. Ren will stop delegating to it.</p>
                      </div>
                      <Button
                        variant="danger-outline"
                        icon={<Trash2 className="size-4" />}
                        loading={remove.isPending}
                        onClick={async () => {
                          const ok = await confirm({
                            title: `Delete ${agent!.name}?`,
                            description: "This can't be undone.",
                            confirmLabel: "Delete agent",
                            danger: true,
                          });
                          if (ok) remove.mutate();
                        }}
                      >
                        Delete agent
                      </Button>
                    </div>
                  </Card>
                )}
              </div>
            )}

            {section === "instructions" && (
              <Card className="p-5">
                <Field
                  label="System prompt"
                  hint="How this agent thinks and works. Org policies and the task's instructions are added automatically."
                >
                  <Textarea
                    mono
                    autosize
                    minRows={16}
                    maxRows={40}
                    value={form.system_prompt}
                    onChange={(e) => set("system_prompt", e.target.value)}
                    placeholder={"You are a meticulous research analyst.\n\n- Cite every claim.\n- Prefer primary sources.\n- Finish with a short, decision-ready summary."}
                  />
                </Field>
                <p className="mt-2 text-right text-[11px] text-subtle tabular-nums">
                  {form.system_prompt.length.toLocaleString()} characters
                </p>
              </Card>
            )}

            {section === "model" && (
              <Card className="space-y-5 p-5">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Provider">
                    <Select value={form.provider_id ?? ""} onChange={(e) => set("provider_id", e.target.value || null)}>
                      <option value="">Workspace default{defaultProvider ? ` (${defaultProvider.name})` : ""}</option>
                      {(providers.data?.items ?? []).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} · {p.kind}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field
                    label="Model"
                    hint={models.isLoading ? "Loading models…" : models.data ? `${models.data.items.length} models available` : "Leave empty to use the provider default."}
                  >
                    <Input
                      list="agent-model-options"
                      value={form.model}
                      onChange={(e) => set("model", e.target.value)}
                      placeholder={defaultProvider?.default_model ?? "provider default"}
                      className="font-mono text-[13px]"
                    />
                  </Field>
                  <datalist id="agent-model-options">
                    {(models.data?.items ?? []).slice(0, 300).map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </datalist>
                </div>
                <Field label="Temperature" labelAside={<span className="font-mono text-xs text-muted">{form.temperature.toFixed(2)}</span>} hint="Lower is more precise and repeatable; higher is more creative.">
                  <Slider value={form.temperature} onValueChange={(v) => set("temperature", v)} min={0} max={2} step={0.05} aria-label="Temperature" />
                </Field>
                <Field label="Max steps" hint="Upper bound on reasoning/tool steps per task.">
                  <Input
                    type="number"
                    min={1}
                    max={200}
                    value={form.max_steps}
                    onChange={(e) => set("max_steps", Math.max(1, Number(e.target.value) || 1))}
                    wrapperClassName="max-w-[160px]"
                    className="max-w-[160px]"
                  />
                </Field>
              </Card>
            )}

            {section === "tools" && (
              <div className="space-y-3">
                <p className="text-[13px] text-muted">
                  Pick the tool groups this agent may use. Risky actions still go through approvals based on its autonomy.
                </p>
                {catalog.isLoading ? (
                  <div className="grid gap-3 sm:grid-cols-2">
                    {Array.from({ length: 6 }).map((_, i) => (
                      <Skeleton key={i} className="h-28 rounded-card" />
                    ))}
                  </div>
                ) : (
                  <div className="grid gap-3 sm:grid-cols-2">
                    {groups.map((g) => {
                      const selected = form.tool_groups.includes(g.id);
                      const needsConnection = !!g.connector_kind && !g.connected;
                      return (
                        <SelectCard key={g.id} selected={selected} onToggle={() => set("tool_groups", toggle(form.tool_groups, g.id))}>
                          <span className="flex items-center gap-2 pr-6">
                            <span className="flex size-7 items-center justify-center rounded-md bg-surface-2 text-fg">
                              <AppIcon name={g.icon} fallback="wrench" className="size-4" />
                            </span>
                            <span className="text-[13px] font-semibold text-fg">{g.name}</span>
                          </span>
                          <span className="line-clamp-2 text-xs text-muted">{g.description}</span>
                          <RiskMix risks={g.tools.map((t) => t.risk)} className="mt-1" />
                          <span className="mt-1 flex flex-wrap gap-1.5">
                            {needsConnection && (
                              <Badge tone="warning" size="xs" icon={<Plug />}>
                                Needs connection
                              </Badge>
                            )}
                            {!g.available && (
                              <Badge tone="danger" size="xs" icon={<AlertTriangle />} title={g.unavailable_reason ?? undefined}>
                                Unavailable
                              </Badge>
                            )}
                            {!g.enabled && (
                              <Badge tone="outline" size="xs">
                                Disabled in workspace
                              </Badge>
                            )}
                          </span>
                        </SelectCard>
                      );
                    })}
                  </div>
                )}
                {groups.some((g) => g.connector_kind && !g.connected && form.tool_groups.includes(g.id)) && (
                  <Notice tone="warning" icon={<Plug />} title="Some selected tools need a connection">
                    Connect them in{" "}
                    <Link href="/connectors" className="font-medium text-fg underline underline-offset-2">
                      Connectors
                    </Link>{" "}
                    before this agent can use them.
                  </Notice>
                )}
              </div>
            )}

            {section === "knowledge" && (
              <Card className="p-5">
                <p className="mb-4 text-[13px] text-muted">Collections this agent can search when grounding its answers.</p>
                {collections.isLoading ? (
                  <Skeleton className="h-24 w-full" />
                ) : (collections.data?.items.length ?? 0) === 0 ? (
                  <p className="text-[13px] text-muted">
                    No collections yet.{" "}
                    <Link href="/knowledge" className="font-medium text-fg underline underline-offset-2">
                      Add knowledge
                    </Link>
                  </p>
                ) : (
                  <div className="space-y-3">
                    {collections.data!.items.map((c) => (
                      <Checkbox
                        key={c.id}
                        checked={form.collection_ids.includes(c.id)}
                        onCheckedChange={() => set("collection_ids", toggle(form.collection_ids, c.id))}
                        label={
                          <span className="flex items-center gap-2">
                            {c.name}
                            <span className="text-xs text-subtle">{c.document_count} docs</span>
                            {c.access === "restricted" && <Lock className="size-3 text-subtle" />}
                          </span>
                        }
                        description={c.description || undefined}
                      />
                    ))}
                  </div>
                )}
              </Card>
            )}

            {section === "policies" && (
              <div className="space-y-4">
                <Card className="p-5">
                  <p className="mb-4 text-[13px] text-muted">Agent-scoped policies to enforce on this agent&apos;s responses and outbound actions.</p>
                  {policies.isLoading ? (
                    <Skeleton className="h-24 w-full" />
                  ) : agentPolicies.length === 0 ? (
                    <p className="text-[13px] text-muted">
                      No agent-scoped policies.{" "}
                      <Link href="/policies" className="font-medium text-fg underline underline-offset-2">
                        Create one
                      </Link>
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {agentPolicies.map((p) => (
                        <Checkbox
                          key={p.id}
                          checked={form.policy_ids.includes(p.id)}
                          onCheckedChange={() => set("policy_ids", toggle(form.policy_ids, p.id))}
                          label={
                            <span className="flex items-center gap-2">
                              {p.name}
                              <Badge tone={p.severity === "block" ? "danger" : "warning"} size="xs">
                                {p.severity}
                              </Badge>
                            </span>
                          }
                          description={`${p.rules.length} rules · ${p.category}`}
                        />
                      ))}
                    </div>
                  )}
                </Card>
                <Notice tone="info" title="Workspace policies always apply">
                  {orgPolicies.length > 0 ? (
                    <span className="mt-1 flex flex-wrap gap-1.5">
                      {orgPolicies.map((p) => (
                        <Badge key={p.id} tone="outline" size="xs">
                          {p.name}
                        </Badge>
                      ))}
                    </span>
                  ) : (
                    "Org-wide policies are enforced for every agent, in addition to the ones selected here."
                  )}
                </Notice>
              </div>
            )}

            {section === "autonomy" && (
              <div role="radiogroup" aria-label="Autonomy" className="grid gap-3 md:grid-cols-3">
                {(["supervised", "balanced", "autonomous"] as Autonomy[]).map((a) => (
                  <button
                    key={a}
                    type="button"
                    role="radio"
                    aria-checked={form.autonomy === a}
                    onClick={() => set("autonomy", a)}
                    className={cn(
                      "flex flex-col items-start gap-2 rounded-card border p-4 text-left transition-colors",
                      form.autonomy === a ? "border-fg bg-surface" : "border-border bg-surface/60 hover:border-border-strong",
                    )}
                  >
                    <span className="flex w-full items-center justify-between">
                      <span className="text-[14px] font-semibold text-fg">{AUTONOMY_LABEL[a]}</span>
                      <span
                        className={cn(
                          "flex size-4 items-center justify-center rounded-full border",
                          form.autonomy === a ? "border-primary bg-primary" : "border-border-strong",
                        )}
                      >
                        {form.autonomy === a && <span className="size-1.5 rounded-full bg-primary-fg" />}
                      </span>
                    </span>
                    <span className="text-xs leading-relaxed text-muted">{AUTONOMY_HELP[a]}</span>
                    <span className="mt-1 flex flex-wrap gap-1">
                      {(["low", "medium", "high", "critical"] as const).map((r) => {
                        const needs =
                          a === "supervised" ? r !== "low" : a === "balanced" ? r === "high" || r === "critical" : r === "critical";
                        return (
                          <Badge key={r} tone={needs ? "warning" : "neutral"} size="xs">
                            {r}: {needs ? "ask" : "auto"}
                          </Badge>
                        );
                      })}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </fieldset>
        </div>

        {!isNew && agent && (
          <aside className="min-w-0">
            <StatsPanel agent={agent} />
          </aside>
        )}
      </div>
    </div>
  );
}
