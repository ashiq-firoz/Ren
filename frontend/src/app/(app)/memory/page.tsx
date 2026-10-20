"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Brain, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SegmentedControl, Slider } from "@/components/ui/controls";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { confirm, Dialog } from "@/components/ui/overlay";
import { useAgentsMap, useMemories } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { api } from "@/lib/api";
import { formatDateTime, timeAgo } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Memory, MemoryKind } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

const KINDS: { value: MemoryKind; label: string; tone: BadgeTone; help: string }[] = [
  { value: "preference", label: "Preference", tone: "accent", help: "How someone likes things done" },
  { value: "semantic", label: "Semantic", tone: "info", help: "Facts about the world or the business" },
  { value: "episodic", label: "Episodic", tone: "neutral", help: "Something that happened" },
  { value: "project", label: "Project", tone: "success", help: "Context about ongoing work" },
  { value: "org", label: "Org", tone: "warning", help: "Workspace-wide knowledge" },
];
const kindMeta = (k: MemoryKind) => KINDS.find((x) => x.value === k) ?? KINDS[0];

/** Importance may be 0..1 or 0..10 depending on the server; normalize for display. */
const normImportance = (v: number) => (v > 1 ? Math.min(1, v / 10) : Math.max(0, v));

function MemoryDialog({ memory, onClose }: { memory: Memory | null; onClose: () => void }) {
  const qc = useQueryClient();
  const { isAdmin } = useRole();
  const [content, setContent] = useState(memory?.content ?? "");
  const [kind, setKind] = useState<MemoryKind>(memory?.kind ?? "preference");
  const [scope, setScope] = useState<"user" | "org">(memory?.scope ?? "user");
  const tenScale = (memory?.importance ?? 0) > 1;
  const [importance, setImportance] = useState(memory?.importance ?? 0.5);

  const save = useMutation({
    mutationFn: () =>
      memory
        ? api.patch<Memory>(`/memories/${memory.id}`, { content: content.trim(), kind, importance })
        : api.post<Memory>("/memories", { content: content.trim(), kind, scope }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.memoryLists });
      toast.success(memory ? "Memory updated" : "Memory added");
      onClose();
    },
    meta: { errorTitle: "Couldn't save the memory" },
  });

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={memory ? "Edit memory" : "Add memory"}
      description="Memories are recalled by Ren and agents when relevant — keep each one short and specific."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!content.trim()} onClick={() => save.mutate()}>
            {memory ? "Save" : "Add memory"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Memory" required>
          <Textarea
            data-autofocus
            autosize
            minRows={3}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="Prefers board updates as a one-page memo with a metrics table up top."
          />
        </Field>
        <Field label="Kind" hint={kindMeta(kind).help}>
          <Select value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)} options={KINDS.map((k) => ({ value: k.value, label: k.label }))} />
        </Field>
        {!memory && (
          <Field label="Visible to" hint={scope === "org" ? "Everyone in the workspace (and their agents) can recall it." : "Only you and agents working for you."}>
            <SegmentedControl
              fullWidth
              value={scope}
              onChange={setScope}
              options={[
                { value: "user", label: "Just me" },
                { value: "org", label: "Workspace", title: isAdmin ? undefined : "May require admin rights" },
              ]}
            />
          </Field>
        )}
        {memory && (
          <Field label="Importance" labelAside={<span className="font-mono text-xs text-muted">{tenScale ? importance.toFixed(0) : importance.toFixed(2)}</span>}>
            <Slider value={importance} onValueChange={setImportance} min={0} max={tenScale ? 10 : 1} step={tenScale ? 1 : 0.05} aria-label="Importance" />
          </Field>
        )}
      </div>
    </Dialog>
  );
}

export default function MemoryPage() {
  const qc = useQueryClient();
  const agents = useAgentsMap();
  const { canWrite } = useRole();
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 300);
  const [kind, setKind] = useState<MemoryKind | "">("");
  const [scope, setScope] = useState<"all" | "user" | "org">("all");
  const [dialog, setDialog] = useState<{ memory: Memory | null } | null>(null);
  const { data, isLoading, error, refetch, isPlaceholderData } = useMemories({ q: q || undefined, kind: kind || undefined, scope: scope === "all" ? undefined : scope });
  const items = data?.items ?? [];

  const remove = useMutation({
    mutationFn: (m: Memory) => api.delete(`/memories/${m.id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.memoryLists });
      toast.success("Memory forgotten");
    },
    meta: { errorTitle: "Couldn't delete the memory" },
  });

  return (
    <PageContainer>
      <PageHeader
        title="Memory"
        description="What Ren remembers about you, your work and your workspace. Edit or delete anything — it changes how agents respond."
        actions={
          canWrite && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setDialog({ memory: null })}>
              Add memory
            </Button>
          )
        }
      />

      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="scrollbar-none flex gap-1.5 overflow-x-auto" role="group" aria-label="Filter by kind">
          {[{ value: "" as const, label: "All" }, ...KINDS].map((k) => (
            <button
              key={k.value || "all"}
              type="button"
              aria-pressed={kind === k.value}
              onClick={() => setKind(k.value)}
              className={cn(
                "h-7 shrink-0 rounded-full border px-3 text-[12.5px] transition-colors",
                kind === k.value ? "border-primary bg-primary text-primary-fg" : "border-border text-muted hover:text-fg",
              )}
            >
              {k.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl
            size="sm"
            aria-label="Scope"
            value={scope}
            onChange={setScope}
            options={[
              { value: "all", label: "All" },
              { value: "user", label: "Mine" },
              { value: "org", label: "Workspace" },
            ]}
          />
          <Input leftIcon={<Search />} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search memories" aria-label="Search memories" wrapperClassName="w-full sm:w-64" />
        </div>
      </div>

      <Card className="overflow-hidden">
        {isLoading ? (
          <SkeletonRows rows={6} />
        ) : error && !data ? (
          <ErrorState error={error} onRetry={() => refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Brain />}
            title={q || kind || scope !== "all" ? "Nothing matches" : "No memories yet"}
            description={q || kind || scope !== "all" ? "Try a different search or filter." : "Ren learns preferences and facts as you work together. You can also add memories yourself."}
          />
        ) : (
          <ul className={cn("divide-y divide-border", isPlaceholderData && "opacity-60")}>
            {items.map((m) => {
              const meta = kindMeta(m.kind);
              const agent = m.agent_id ? agents.get(m.agent_id) : undefined;
              const imp = normImportance(m.importance);
              return (
                <li key={m.id} className="group flex items-start gap-4 px-4 py-3.5">
                  <div className="min-w-0 flex-1">
                    <p className="text-[13.5px] leading-relaxed text-fg">{m.content}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11.5px] text-muted">
                      <Badge tone={meta.tone} size="xs">
                        {meta.label}
                      </Badge>
                      <Badge tone="outline" size="xs">
                        {m.scope === "org" ? "Workspace" : "Personal"}
                      </Badge>
                      {agent && (
                        <span className="inline-flex items-center gap-1">
                          <AgentAvatar agent={agent} size="xs" /> {agent.name}
                        </span>
                      )}
                      {m.source && <span className="truncate">from {m.source}</span>}
                      <span title={formatDateTime(m.updated_at)}>· {timeAgo(m.updated_at)}</span>
                      <span className="inline-flex items-center gap-1.5" title={`Importance ${m.importance}`}>
                        · importance
                        <span className="inline-block h-1 w-12 overflow-hidden rounded-full bg-surface-3">
                          <span className="block h-full rounded-full bg-accent" style={{ width: `${imp * 100}%` }} />
                        </span>
                      </span>
                    </div>
                  </div>
                  {canWrite && (
                    <div className="flex shrink-0 items-center opacity-60 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                      <IconButton label="Edit memory" icon={<Pencil />} onClick={() => setDialog({ memory: m })} />
                      <IconButton
                        label="Delete memory"
                        icon={<Trash2 />}
                        variant="danger"
                        onClick={async () => {
                          const ok = await confirm({ title: "Forget this memory?", description: m.content.slice(0, 160), confirmLabel: "Forget", danger: true });
                          if (ok) remove.mutate(m);
                        }}
                      />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
      {dialog && <MemoryDialog key={dialog.memory?.id ?? "new"} memory={dialog.memory} onClose={() => setDialog(null)} />}
    </PageContainer>
  );
}
