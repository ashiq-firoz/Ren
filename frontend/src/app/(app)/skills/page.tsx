"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Pencil, Play, Plus, Search, Sparkles, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { RunSkillDialog, SkillEditorDialog } from "@/components/skills/skill-dialogs";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, SkeletonCards } from "@/components/ui/feedback";
import { DropdownMenu } from "@/components/ui/floating";
import { AppIcon } from "@/components/ui/icon";
import { Input } from "@/components/ui/input";
import { confirm } from "@/components/ui/overlay";
import { useAgentsMap, useSkills } from "@/hooks/queries";
import { api } from "@/lib/api";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Skill } from "@/lib/types";
import { cn, titleCase } from "@/lib/utils";
import { useRole } from "@/providers/auth";

export default function SkillsPage() {
  const qc = useQueryClient();
  const { isAdmin, canWrite } = useRole();
  const { data, isLoading, error, refetch } = useSkills();
  const agents = useAgentsMap();
  const [category, setCategory] = useState("all");
  const [query, setQuery] = useState("");
  const [running, setRunning] = useState<Skill | null>(null);
  const [editing, setEditing] = useState<{ skill: Skill | null } | null>(null);

  const skills = useMemo(() => data?.items ?? [], [data]);
  const categories = useMemo(() => Array.from(new Set(skills.map((s) => s.category).filter(Boolean))).sort(), [skills]);
  const q = query.trim().toLowerCase();
  const visible = skills.filter(
    (s) =>
      (category === "all" || s.category === category) &&
      (!q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)),
  );

  const remove = useMutation({
    mutationFn: (s: Skill) => api.delete(`/skills/${s.id}`),
    onSuccess: (_d, s) => {
      void qc.invalidateQueries({ queryKey: qk.skills });
      toast.success("Skill deleted", { description: s.name });
    },
    meta: { errorTitle: "Couldn't delete the skill" },
  });

  return (
    <PageContainer>
      <PageHeader
        title="Skills"
        description="Reusable playbooks your swarm can run on demand — fill in a few inputs and Ren hands the work to the right agent."
        actions={
          isAdmin && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing({ skill: null })}>
              New skill
            </Button>
          )
        }
      />

      <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="scrollbar-none flex gap-1.5 overflow-x-auto" role="group" aria-label="Filter by category">
          {["all", ...categories].map((c) => (
            <button
              key={c}
              type="button"
              aria-pressed={category === c}
              onClick={() => setCategory(c)}
              className={cn(
                "h-7 shrink-0 rounded-full border px-3 text-[12.5px] transition-colors",
                category === c ? "border-primary bg-primary text-primary-fg" : "border-border text-muted hover:border-border-strong hover:text-fg",
              )}
            >
              {c === "all" ? "All" : titleCase(c)}
            </button>
          ))}
        </div>
        <Input leftIcon={<Search />} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search skills" aria-label="Search skills" wrapperClassName="w-full sm:w-64" />
      </div>

      {isLoading ? (
        <SkeletonCards count={6} />
      ) : error && !data ? (
        <Card>
          <ErrorState error={error} onRetry={() => refetch()} />
        </Card>
      ) : visible.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Sparkles />}
            title={skills.length === 0 ? "No skills yet" : "No matching skills"}
            description={skills.length === 0 ? "Package a recurring job — a weekly digest, a contract review, a board prep pack — as a one-click skill." : "Try another category or search."}
            action={
              isAdmin && skills.length === 0 ? (
                <Button variant="primary" onClick={() => setEditing({ skill: null })}>
                  Create a skill
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((s) => {
            const agent = s.agent_id ? agents.get(s.agent_id) : undefined;
            return (
              <Card key={s.id} className="flex flex-col p-4 transition-colors hover:border-border-strong">
                <div className="flex items-start gap-3">
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-[10px] tint-accent">
                    <AppIcon name={s.icon} fallback="sparkles" className="size-5" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-[14px] font-semibold text-fg">{s.name}</h3>
                      {s.builtin && (
                        <Badge tone="outline" size="xs">
                          Built-in
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted">{titleCase(s.category || "custom")}</p>
                  </div>
                  {isAdmin && (
                    <DropdownMenu
                      aria-label={`Actions for ${s.name}`}
                      items={[
                        { label: "Edit", icon: <Pencil />, onSelect: () => setEditing({ skill: s }) },
                        {
                          label: "Delete",
                          icon: <Trash2 />,
                          danger: true,
                          disabled: s.builtin,
                          description: s.builtin ? "Built-in skills can only be edited" : undefined,
                          onSelect: async () => {
                            const ok = await confirm({ title: `Delete ${s.name}?`, confirmLabel: "Delete skill", danger: true });
                            if (ok) remove.mutate(s);
                          },
                        },
                      ]}
                      trigger={<IconButton label="Skill actions" icon={<MoreHorizontal />} />}
                    />
                  )}
                </div>
                <p className="mt-3 line-clamp-3 flex-1 text-[12.5px] leading-relaxed text-muted">{s.description}</p>
                <div className="mt-4 flex items-center justify-between gap-2 border-t border-border pt-3">
                  <span className="flex min-w-0 items-center gap-2 text-xs text-muted">
                    {agent ? (
                      <>
                        <AgentAvatar agent={agent} size="xs" />
                        <span className="truncate">{agent.name}</span>
                      </>
                    ) : (
                      <span>Ren picks the agent</span>
                    )}
                    {s.inputs.length > 0 && <span className="shrink-0 text-subtle">· {s.inputs.length} inputs</span>}
                  </span>
                  {canWrite && (
                    <Button size="sm" variant="primary" icon={<Play className="size-3.5" />} onClick={() => setRunning(s)}>
                      Run
                    </Button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {running && <RunSkillDialog key={running.id} skill={running} onOpenChange={(o) => !o && setRunning(null)} />}
      {editing && (
        <SkillEditorDialog key={editing.skill?.id ?? "new"} skill={editing.skill} categories={categories} onOpenChange={(o) => !o && setEditing(null)} />
      )}
    </PageContainer>
  );
}
