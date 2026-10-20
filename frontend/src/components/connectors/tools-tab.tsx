"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronRight, Plug } from "lucide-react";
import { useMemo, useState } from "react";
import { RiskBadge, RiskMix } from "@/components/domain/status";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/controls";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { AppIcon } from "@/components/ui/icon";
import { Drawer } from "@/components/ui/overlay";
import { useToolCatalog } from "@/hooks/queries";
import { api } from "@/lib/api";
import { upsertInList } from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { ListResponse, ToolGroup } from "@/lib/types";
import { titleCase } from "@/lib/utils";
import { useRole } from "@/providers/auth";

const CATEGORY_ORDER = ["core", "knowledge", "agents", "productivity", "engineering", "data", "business", "infrastructure", "mcp"];
const categoryRank = (c: string) => {
  const i = CATEGORY_ORDER.indexOf(c);
  return i === -1 ? CATEGORY_ORDER.length : i;
};

export function useToggleToolGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.patch<ToolGroup>(`/tools/groups/${encodeURIComponent(id)}`, { enabled }),
    onMutate: ({ id, enabled }) => {
      qc.setQueryData<ListResponse<ToolGroup>>(qk.toolCatalog, (old) =>
        old ? { ...old, items: old.items.map((g) => (g.id === id ? { ...g, enabled } : g)) } : old,
      );
    },
    onSuccess: (g) => {
      qc.setQueryData<ListResponse<ToolGroup>>(qk.toolCatalog, (old) => (old ? upsertInList(old, g) : old));
      toast.success(`${g.name} ${g.enabled ? "enabled" : "disabled"}`);
    },
    onError: () => void qc.invalidateQueries({ queryKey: qk.toolCatalog }),
    meta: { errorTitle: "Couldn't update the tool group" },
  });
}

export function ToolGroupDrawer({ group, onClose }: { group: ToolGroup | null; onClose: () => void }) {
  return (
    <Drawer open={!!group} onOpenChange={(o) => !o && onClose()} title={group?.name} description={group?.description} width={520}>
      {group && (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <Badge tone="neutral">{titleCase(group.category)}</Badge>
            {group.connector_kind && (
              <Badge tone={group.connected ? "success" : "warning"} icon={<Plug />}>
                {group.connected ? "Connected" : "Needs connection"}
              </Badge>
            )}
            {!group.available && (
              <Badge tone="danger" icon={<AlertTriangle />}>
                Unavailable
              </Badge>
            )}
          </div>
          {!group.available && group.unavailable_reason && <p className="text-[13px] text-danger">{group.unavailable_reason}</p>}
          <ul className="divide-y divide-border rounded-card border border-border">
            {group.tools.map((t) => (
              <li key={t.name} className="px-4 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[13px] font-medium text-fg">{t.display_name}</p>
                    <p className="font-mono text-[11px] text-subtle">{t.name}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Badge tone="outline" size="xs">
                      {t.scope}
                    </Badge>
                    <RiskBadge risk={t.risk} size="xs" label={false} />
                  </div>
                </div>
                {t.description && <p className="mt-1.5 text-xs leading-relaxed text-muted">{t.description}</p>}
              </li>
            ))}
            {group.tools.length === 0 && <li className="px-4 py-6 text-center text-xs text-muted">No tools listed.</li>}
          </ul>
        </div>
      )}
    </Drawer>
  );
}

export function ToolGroupCard({ group, onOpen }: { group: ToolGroup; onOpen: () => void }) {
  const { isAdmin } = useRole();
  const toggle = useToggleToolGroup();
  const needsConnection = !!group.connector_kind && !group.connected;
  return (
    <Card className="flex flex-col p-4 transition-colors hover:border-border-strong">
      <div className="flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-fg">
          <AppIcon name={group.icon} fallback="wrench" className="size-4" />
        </span>
        <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
          <p className="truncate text-[13.5px] font-semibold text-fg hover:underline">{group.name}</p>
          <p className="text-[11.5px] text-muted">
            {group.tools.length} {group.tools.length === 1 ? "tool" : "tools"}
          </p>
        </button>
        <Switch
          size="sm"
          checked={group.enabled}
          disabled={!isAdmin || toggle.isPending}
          onCheckedChange={(v) => toggle.mutate({ id: group.id, enabled: v })}
          aria-label={`${group.enabled ? "Disable" : "Enable"} ${group.name}`}
        />
      </div>
      <p className="mt-2.5 line-clamp-2 flex-1 text-xs leading-relaxed text-muted">{group.description}</p>
      <RiskMix risks={group.tools.map((t) => t.risk)} className="mt-3" />
      <div className="mt-3 flex items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1.5">
          {needsConnection && (
            <Badge tone="warning" size="xs" icon={<Plug />}>
              Needs connection
            </Badge>
          )}
          {!group.available && (
            <Badge tone="danger" size="xs" icon={<AlertTriangle />} title={group.unavailable_reason ?? undefined}>
              Unavailable
            </Badge>
          )}
          {group.connector_kind && group.connected && (
            <Badge tone="success" size="xs" dot>
              Connected
            </Badge>
          )}
        </div>
        <button type="button" onClick={onOpen} className="inline-flex shrink-0 items-center gap-0.5 text-xs font-medium text-muted hover:text-fg">
          Tools <ChevronRight className="size-3.5" />
        </button>
      </div>
    </Card>
  );
}

export function ToolsTab() {
  const { data, isLoading, error, refetch } = useToolCatalog();
  const [open, setOpen] = useState<ToolGroup | null>(null);
  const groups = useMemo(() => (data?.items ?? []).filter((g) => g.category !== "mcp" && !g.id.startsWith("mcp:")), [data]);
  const byCategory = useMemo(() => {
    const map = new Map<string, ToolGroup[]>();
    for (const g of groups) map.set(g.category, [...(map.get(g.category) ?? []), g]);
    return Array.from(map.entries()).sort((a, b) => categoryRank(a[0]) - categoryRank(b[0]) || a[0].localeCompare(b[0]));
  }, [groups]);
  const live = open ? (data?.items.find((g) => g.id === open.id) ?? open) : null;

  if (isLoading)
    return (
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 9 }).map((_, i) => (
          <Skeleton key={i} className="h-40 rounded-card" />
        ))}
      </div>
    );
  if (error && !data)
    return (
      <Card>
        <ErrorState error={error} onRetry={() => refetch()} />
      </Card>
    );
  if (groups.length === 0)
    return (
      <Card>
        <EmptyState title="No tools" description="The tool catalog is empty." />
      </Card>
    );
  return (
    <div className="space-y-8">
      {byCategory.map(([cat, list]) => (
        <section key={cat}>
          <h2 className="mb-3 text-[11px] font-medium tracking-[0.08em] text-subtle uppercase">{titleCase(cat)}</h2>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {list.map((g) => (
              <ToolGroupCard key={g.id} group={g} onOpen={() => setOpen(g)} />
            ))}
          </div>
        </section>
      ))}
      <ToolGroupDrawer group={live} onClose={() => setOpen(null)} />
    </div>
  );
}
