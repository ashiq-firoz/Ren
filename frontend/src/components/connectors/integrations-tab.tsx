"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, CircleHelp, Pencil, Plug, PlugZap, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Notice, Skeleton } from "@/components/ui/feedback";
import { AppIcon } from "@/components/ui/icon";
import { Field, Input } from "@/components/ui/input";
import { confirm, Dialog } from "@/components/ui/overlay";
import { useConnectorKinds, useConnectors } from "@/hooks/queries";
import { api } from "@/lib/api";
import { removeFromList, upsertInList } from "@/lib/cache";
import { timeAgo } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Connector, ConnectorKind, ConnectorTestResult, ListResponse } from "@/lib/types";
import { cn, titleCase } from "@/lib/utils";
import { useRole } from "@/providers/auth";
import { DocsLink, DynamicFields, isMasked, missingRequired } from "./forms";

function ConnectorDialog({
  kind,
  connector,
  onOpenChange,
}: {
  kind: ConnectorKind;
  connector: Connector | null;
  onOpenChange: (o: boolean) => void;
}) {
  const qc = useQueryClient();
  const savedSecrets = useMemo(
    () => new Set(kind.fields.filter((f) => f.secret && connector && (isMasked(connector.config?.[f.key]) || connector.config?.[f.key])).map((f) => f.key)),
    [kind.fields, connector],
  );
  const [name, setName] = useState(connector?.name ?? kind.name);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const v: Record<string, string> = {};
    for (const f of kind.fields) {
      const raw = connector?.config?.[f.key];
      if (f.secret || isMasked(raw)) v[f.key] = "";
      else if (raw !== undefined && raw !== null) v[f.key] = String(raw);
      else v[f.key] = f.type === "select" && f.required && f.options?.length ? f.options[0] : "";
    }
    return v;
  });
  const missing = missingRequired(kind.fields, values, connector ? savedSecrets : undefined);

  const save = useMutation({
    mutationFn: async () => {
      const config: Record<string, unknown> = {};
      for (const f of kind.fields) {
        const v = values[f.key];
        if (f.secret && connector && !v) continue; // keep stored secret
        if (v === "" && !f.required) {
          if (connector) config[f.key] = null;
          continue;
        }
        config[f.key] = f.type === "number" && v !== "" ? Number(v) : v;
      }
      const saved = connector
        ? await api.patch<Connector>(`/connectors/${connector.id}`, { name: name.trim(), config })
        : await api.post<Connector>("/connectors", { kind: kind.kind, name: name.trim(), config });
      let test: ConnectorTestResult | null = null;
      try {
        test = await api.post<ConnectorTestResult>(`/connectors/${saved.id}/test`);
      } catch {
        test = null;
      }
      return { saved, test };
    },
    onSuccess: ({ saved, test }) => {
      qc.setQueryData<ListResponse<Connector>>(qk.connectors, (old) => (old ? upsertInList(old, saved, { prepend: true }) : old));
      void qc.invalidateQueries({ queryKey: qk.connectors });
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
      if (test && !test.ok) toast.warning(`${saved.name} saved, but the test failed`, { description: test.message });
      else toast.success(connector ? "Connection updated" : `${kind.name} connected`, { description: test?.message });
      onOpenChange(false);
    },
    meta: { errorTitle: "Couldn't save the connection" },
  });

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={connector ? `Edit ${connector.name}` : `Connect ${kind.name}`}
      description={kind.description}
      footer={
        <>
          <span className="mr-auto">
            <DocsLink href={kind.docs_url} />
          </span>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" icon={<PlugZap className="size-4" />} loading={save.isPending} disabled={!name.trim() || missing.length > 0} onClick={() => save.mutate()}>
            {connector ? "Save & test" : "Connect & test"}
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim() && missing.length === 0) save.mutate();
        }}
      >
        <Field label="Connection name" required>
          <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <DynamicFields
          fields={kind.fields}
          values={values}
          onChange={(k, v) => setValues((s) => ({ ...s, [k]: v }))}
          editing={!!connector}
          savedSecrets={savedSecrets}
        />
        {kind.tool_groups.length > 0 && (
          <Notice tone="neutral" icon={<Plug />}>
            Unlocks tool groups: {kind.tool_groups.map((g) => titleCase(g)).join(", ")}
          </Notice>
        )}
      </form>
    </Dialog>
  );
}

function StatusBadge({ status }: { status: Connector["status"] }) {
  if (status === "connected")
    return (
      <Badge tone="success" icon={<CheckCircle2 />}>
        Connected
      </Badge>
    );
  if (status === "error")
    return (
      <Badge tone="danger" icon={<CircleAlert />}>
        Error
      </Badge>
    );
  return (
    <Badge tone="neutral" icon={<CircleHelp />}>
      Unknown
    </Badge>
  );
}

export function IntegrationsTab() {
  const qc = useQueryClient();
  const { isAdmin } = useRole();
  const kinds = useConnectorKinds();
  const connectors = useConnectors();
  const [dialog, setDialog] = useState<{ kind: ConnectorKind; connector: Connector | null } | null>(null);
  const [category, setCategory] = useState("all");

  const kindMap = useMemo(() => new Map((kinds.data?.items ?? []).map((k) => [k.kind, k])), [kinds.data]);
  const categories = useMemo(() => Array.from(new Set((kinds.data?.items ?? []).map((k) => k.category))).sort(), [kinds.data]);
  const connectedKinds = new Set((connectors.data?.items ?? []).map((c) => c.kind));

  const test = useMutation({
    mutationFn: (c: Connector) => api.post<ConnectorTestResult>(`/connectors/${c.id}/test`),
    onSuccess: (r, c) => {
      void qc.invalidateQueries({ queryKey: qk.connectors });
      if (r.ok) toast.success(`${c.name} is working`, { description: r.message });
      else toast.error(`${c.name} test failed`, { description: r.message });
    },
    meta: { errorTitle: "Test failed" },
  });

  const remove = useMutation({
    mutationFn: (c: Connector) => api.delete(`/connectors/${c.id}`),
    onSuccess: (_d, c) => {
      qc.setQueryData<ListResponse<Connector>>(qk.connectors, (old) => (old ? removeFromList(old, c.id) : old));
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
      toast.success("Disconnected", { description: c.name });
    },
    meta: { errorTitle: "Couldn't remove the connection" },
  });

  const visibleKinds = (kinds.data?.items ?? []).filter((k) => category === "all" || k.category === category);

  return (
    <div className="space-y-8">
      <section>
        <h2 className="mb-3 text-sm font-semibold text-fg">Connected</h2>
        {connectors.isLoading ? (
          <Skeleton className="h-20 w-full rounded-card" />
        ) : connectors.error && !connectors.data ? (
          <Card>
            <ErrorState compact error={connectors.error} onRetry={() => connectors.refetch()} />
          </Card>
        ) : (connectors.data?.items.length ?? 0) === 0 ? (
          <Card className="border-dashed">
            <EmptyState compact icon={<Plug />} title="Nothing connected yet" description="Connect email, calendars, docs, CRMs and databases so agents can act on them — with approvals for anything risky." />
          </Card>
        ) : (
          <Card className="divide-y divide-border">
            {connectors.data!.items.map((c) => {
              const k = kindMap.get(c.kind);
              return (
                <div key={c.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <span className="flex size-9 items-center justify-center rounded-lg bg-surface-2 text-fg">
                    <AppIcon name={k?.icon ?? c.kind} fallback="plug" className="size-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13.5px] font-medium text-fg">{c.name}</p>
                    <p className="truncate text-xs text-muted">
                      {k?.name ?? titleCase(c.kind)} · added {timeAgo(c.created_at)}
                      {c.error && <span className="text-danger"> · {c.error}</span>}
                    </p>
                  </div>
                  <StatusBadge status={c.status} />
                  <div className="flex items-center gap-1">
                    <Button size="sm" variant="secondary" loading={test.isPending && test.variables?.id === c.id} onClick={() => test.mutate(c)}>
                      Test
                    </Button>
                    {isAdmin && k && <IconButton label={`Edit ${c.name}`} icon={<Pencil />} onClick={() => setDialog({ kind: k, connector: c })} />}
                    {isAdmin && (
                      <IconButton
                        label={`Remove ${c.name}`}
                        icon={<Trash2 />}
                        variant="danger"
                        onClick={async () => {
                          const ok = await confirm({
                            title: `Disconnect ${c.name}?`,
                            description: "Agents lose access to its tools immediately. Stored credentials are deleted.",
                            confirmLabel: "Disconnect",
                            danger: true,
                          });
                          if (ok) remove.mutate(c);
                        }}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </Card>
        )}
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-fg">Available integrations</h2>
          <div className="scrollbar-none flex gap-1.5 overflow-x-auto">
            {["all", ...categories].map((c) => (
              <button
                key={c}
                type="button"
                aria-pressed={category === c}
                onClick={() => setCategory(c)}
                className={cn(
                  "h-7 shrink-0 rounded-full border px-3 text-xs transition-colors",
                  category === c ? "border-primary bg-primary text-primary-fg" : "border-border text-muted hover:text-fg",
                )}
              >
                {c === "all" ? "All" : titleCase(c)}
              </button>
            ))}
          </div>
        </div>
        {kinds.isLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 9 }).map((_, i) => (
              <Skeleton key={i} className="h-32 rounded-card" />
            ))}
          </div>
        ) : kinds.error ? (
          <Card>
            <ErrorState compact error={kinds.error} onRetry={() => kinds.refetch()} />
          </Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {visibleKinds.map((k) => (
              <Card key={k.kind} className="flex flex-col p-4">
                <div className="flex items-start gap-3">
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-fg">
                    <AppIcon name={k.icon || k.kind} fallback="plug" className="size-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13.5px] font-semibold text-fg">{k.name}</p>
                    <p className="text-[11.5px] text-muted">{titleCase(k.category)}</p>
                  </div>
                  {connectedKinds.has(k.kind) && (
                    <Badge tone="success" size="xs" dot>
                      Connected
                    </Badge>
                  )}
                </div>
                <p className="mt-2.5 line-clamp-2 flex-1 text-xs leading-relaxed text-muted">{k.description}</p>
                <div className="mt-3 flex items-center justify-between">
                  <DocsLink href={k.docs_url} />
                  {isAdmin ? (
                    <Button size="sm" variant={connectedKinds.has(k.kind) ? "secondary" : "primary"} onClick={() => setDialog({ kind: k, connector: null })}>
                      {connectedKinds.has(k.kind) ? "Add another" : "Connect"}
                    </Button>
                  ) : (
                    <span className="text-[11px] text-subtle">Admins connect</span>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>

      {dialog && (
        <ConnectorDialog
          key={`${dialog.kind.kind}-${dialog.connector?.id ?? "new"}`}
          kind={dialog.kind}
          connector={dialog.connector}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
    </div>
  );
}
