"use client";

import { useMutation } from "@tanstack/react-query";
import { Briefcase, ChevronsUpDown, Plus, User as UserIcon } from "lucide-react";
import { useState } from "react";
import { RoleBadge } from "@/components/domain/status";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/controls";
import { DropdownMenu, type MenuItem } from "@/components/ui/floating";
import { Field, Input } from "@/components/ui/input";
import { Dialog } from "@/components/ui/overlay";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";
import type { OrgSummary, WorkspaceMode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth";

function OrgGlyph({ org, size = "md" }: { org: OrgSummary | null; size?: "sm" | "md" }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-md border border-border bg-surface-2 font-semibold text-fg",
        size === "sm" ? "size-6 text-[10px]" : "size-7 text-[11px]",
      )}
    >
      {(org?.name ?? "?").slice(0, 1).toUpperCase()}
    </span>
  );
}

export function CreateWorkspaceDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { addOrg } = useAuth();
  const [name, setName] = useState("");
  const [mode, setMode] = useState<WorkspaceMode>("business");
  const create = useMutation({
    mutationFn: () => api.post<OrgSummary>("/orgs", { name: name.trim(), mode }),
    onSuccess: (org) => {
      addOrg(org, { activate: true });
      toast.success("Workspace created", { description: org.name });
      onOpenChange(false);
      setName("");
    },
    meta: { errorTitle: "Couldn't create workspace" },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create workspace"
      description="Each workspace has its own agents, knowledge, policies and members."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!name.trim()} onClick={() => create.mutate()}>
            Create workspace
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate();
        }}
      >
        <Field label="Name" required>
          <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Operations" />
        </Field>
        <Field label="Mode">
          <SegmentedControl
            fullWidth
            value={mode}
            onChange={setMode}
            options={[
              { value: "business", label: "Business", icon: <Briefcase /> },
              { value: "personal", label: "Personal", icon: <UserIcon /> },
            ]}
          />
        </Field>
      </form>
    </Dialog>
  );
}

export function WorkspaceSwitcher({ collapsed }: { collapsed?: boolean }) {
  const { orgs, activeOrg, switchOrg } = useAuth();
  const [createOpen, setCreateOpen] = useState(false);

  const items: MenuItem[] = [
    { type: "label", label: "Workspaces" },
    ...orgs.map<MenuItem>((o) => ({
      label: o.name,
      description: `${o.mode === "business" ? "Business" : "Personal"} · ${o.role}`,
      icon: <OrgGlyph org={o} size="sm" />,
      checked: o.id === activeOrg?.id,
      onSelect: () => switchOrg(o.id),
    })),
    { type: "separator" },
    { label: "Create workspace", icon: <Plus />, onSelect: () => setCreateOpen(true) },
  ];

  return (
    <>
      <DropdownMenu
        placement={collapsed ? "right-start" : "bottom-start"}
        className="w-[260px]"
        aria-label="Switch workspace"
        items={items}
        trigger={
          <button
            type="button"
            aria-label="Switch workspace"
            className={cn(
              "flex w-full items-center gap-2.5 rounded-control border border-transparent text-left transition-colors hover:border-border hover:bg-surface-2",
              collapsed ? "justify-center p-1.5" : "px-2 py-1.5",
            )}
          >
            <OrgGlyph org={activeOrg} />
            {!collapsed && (
              <>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-fg">{activeOrg?.name ?? "No workspace"}</span>
                  <span className="flex items-center gap-1.5 text-[11px] text-muted">
                    {activeOrg?.mode === "personal" ? "Personal" : "Business"}
                    {activeOrg && <RoleBadge role={activeOrg.role} />}
                  </span>
                </span>
                <ChevronsUpDown className="size-3.5 shrink-0 text-subtle" />
              </>
            )}
          </button>
        }
      />
      <CreateWorkspaceDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}
