"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Briefcase, Lock, Trash2, User } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { AUTONOMY_HELP, AUTONOMY_LABEL } from "@/components/domain/status";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/controls";
import { Notice, Skeleton } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/input";
import { KV } from "@/components/ui/key-value";
import { Dialog } from "@/components/ui/overlay";
import { useSystemSettings } from "@/hooks/queries";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { Autonomy, Org, WorkspaceMode } from "@/lib/types";
import { useAuth, useRole } from "@/providers/auth";
import { SettingsSection, useUpdateOrg } from "./shared";

function DeleteWorkspace({ org }: { org: Org }) {
  const qc = useQueryClient();
  const router = useRouter();
  const { orgs, switchOrg, refresh } = useAuth();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const del = useMutation({
    mutationFn: () => api.delete("/orgs/current"),
    onSuccess: async () => {
      setOpen(false);
      toast.success("Workspace deleted", { description: org.name });
      const next = orgs.find((o) => o.id !== org.id);
      qc.clear();
      if (next) switchOrg(next.id);
      await refresh();
      router.replace("/chat");
    },
    meta: { errorTitle: "Couldn't delete the workspace" },
  });
  return (
    <>
      <Card className="border-[color-mix(in_srgb,var(--danger)_30%,transparent)] p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-[14px] font-semibold text-fg">
              <AlertTriangle className="size-4 text-danger" /> Delete workspace
            </p>
            <p className="mt-1 text-[12.5px] text-muted">Permanently deletes agents, conversations, tasks, knowledge and settings for everyone.</p>
          </div>
          <Button variant="danger-outline" icon={<Trash2 className="size-4" />} onClick={() => setOpen(true)}>
            Delete workspace
          </Button>
        </div>
      </Card>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        size="sm"
        title="Delete this workspace?"
        description="This cannot be undone."
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" loading={del.isPending} disabled={typed !== org.name} onClick={() => del.mutate()}>
              Delete forever
            </Button>
          </>
        }
      >
        <Field label={`Type “${org.name}” to confirm`}>
          <Input data-autofocus value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        </Field>
      </Dialog>
    </>
  );
}

function SystemInfo() {
  const { data, isLoading } = useSystemSettings();
  return (
    <SettingsSection title="System" description="Read-only details about this Ren deployment.">
      {isLoading || !data ? (
        <Skeleton className="h-28 w-full" />
      ) : (
        <div className="divide-y divide-border rounded-control border border-border px-3">
          <KV label="Version">{data.version}</KV>
          <KV label="Database">{data.db === "postgresql" ? "PostgreSQL" : "SQLite"}</KV>
          <KV label="Sandbox">{data.sandbox}</KV>
          <KV label="Browser automation">{data.browser ? "Available" : "Not installed"}</KV>
          <KV label="Embeddings">
            <span className="font-mono text-xs">
              {data.embedding.provider} · {data.embedding.model} · {data.embedding.dim}d
            </span>
          </KV>
          <KV label="Reranker">
            <span className="font-mono text-xs">{data.reranker.model ? `${data.reranker.provider} · ${data.reranker.model}` : "off"}</span>
          </KV>
          <KV label="Laya">
            {data.laya.mode} {data.laya.reachable ? "· reachable" : "· unreachable"}
          </KV>
        </div>
      )}
    </SettingsSection>
  );
}

export function WorkspaceTab({ org }: { org: Org }) {
  const { isAdmin, isOwner } = useRole();
  const { refresh } = useAuth();
  const update = useUpdateOrg("Workspace saved");
  const [name, setName] = useState(org.name);
  const [mode, setMode] = useState<WorkspaceMode>(org.mode);
  const [autonomy, setAutonomy] = useState<Autonomy>(org.settings.autonomy_default);
  const [maxTasks, setMaxTasks] = useState(org.settings.max_concurrent_tasks);
  const dirty =
    name.trim() !== org.name || mode !== org.mode || autonomy !== org.settings.autonomy_default || maxTasks !== org.settings.max_concurrent_tasks;

  const save = () =>
    update.mutate(
      { name: name.trim(), mode, settings: { autonomy_default: autonomy, max_concurrent_tasks: maxTasks } },
      { onSuccess: () => void refresh() },
    );

  return (
    <div className="space-y-5">
      {!isAdmin && (
        <Notice tone="neutral" icon={<Lock />}>
          Only admins can change workspace settings.
        </Notice>
      )}
      <fieldset disabled={!isAdmin} className="min-w-0">
        <SettingsSection
          title="Workspace"
          description={`Created ${formatDate(org.created_at)} · ${org.member_count} ${org.member_count === 1 ? "member" : "members"}`}
          footer={
            isAdmin && (
              <Button variant="primary" loading={update.isPending} disabled={!dirty || !name.trim()} onClick={save}>
                Save changes
              </Button>
            )
          }
        >
          <Field label="Name" required>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Mode" hint={mode === "business" ? "Teams, roles and shared knowledge." : "A private swarm — the Team page is hidden."}>
            <SegmentedControl
              value={mode}
              onChange={setMode}
              options={[
                { value: "business", label: "Business", icon: <Briefcase /> },
                { value: "personal", label: "Personal", icon: <User /> },
              ]}
            />
          </Field>
          <Field label="Default autonomy" hint={AUTONOMY_HELP[autonomy]}>
            <SegmentedControl
              value={autonomy}
              onChange={setAutonomy}
              options={(["supervised", "balanced", "autonomous"] as Autonomy[]).map((a) => ({ value: a, label: AUTONOMY_LABEL[a] }))}
            />
          </Field>
          <Field label="Max concurrent tasks" hint="How many agent tasks may run at once; the rest queue.">
            <Input
              type="number"
              min={1}
              max={100}
              value={maxTasks}
              onChange={(e) => setMaxTasks(Math.max(1, Number(e.target.value) || 1))}
              wrapperClassName="max-w-[140px]"
              className="max-w-[140px]"
            />
          </Field>
        </SettingsSection>
      </fieldset>
      <SystemInfo />
      {isOwner && <DeleteWorkspace org={org} />}
    </div>
  );
}
