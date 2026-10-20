"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Pencil, Plus, RefreshCw, Server, Trash2, Waypoints } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { SegmentedControl, Switch } from "@/components/ui/controls";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field, Input, Textarea } from "@/components/ui/input";
import { confirm, Dialog } from "@/components/ui/overlay";
import { useMcpPresets, useMcpServers, useSystemSettings } from "@/hooks/queries";
import { api, getApiBase } from "@/lib/api";
import { removeFromList, upsertInList } from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { ListResponse, McpPreset, McpServer } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";
import { DocsLink, KeyValueEditor, kvToRecord, newKVRow, type KVRow } from "./forms";

type Transport = "stdio" | "http" | "sse";

interface Draft {
  name: string;
  transport: Transport;
  command: string;
  argsText: string;
  url: string;
  env: KVRow[];
  headers: KVRow[];
}

function draftFromServer(s: McpServer): Draft {
  return {
    name: s.name,
    transport: s.transport,
    command: s.command ?? "",
    argsText: (s.args ?? []).join("\n"),
    url: s.url ?? "",
    env: (s.env_keys ?? []).map((k) => newKVRow({ key: k, stored: true })),
    headers: (s.header_keys ?? []).map((k) => newKVRow({ key: k, stored: true })),
  };
}

function draftFromPreset(p: McpPreset): Draft {
  return {
    name: p.name,
    transport: p.transport,
    command: p.command ?? "",
    argsText: (p.args ?? []).join("\n"),
    url: p.url ?? "",
    env: p.env_fields.map((f) => newKVRow({ key: f.key, label: f.label, placeholder: f.placeholder, help: f.help, required: f.required })),
    headers: [],
  };
}

const EMPTY: Draft = { name: "", transport: "stdio", command: "", argsText: "", url: "", env: [], headers: [] };

function McpServerDialog({
  server,
  initial,
  docsUrl,
  onOpenChange,
}: {
  server: McpServer | null;
  initial: Draft;
  docsUrl?: string;
  onOpenChange: (o: boolean) => void;
}) {
  const qc = useQueryClient();
  const [d, setD] = useState<Draft>(initial);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }));
  const isStdio = d.transport === "stdio";
  const missingEnv = d.env.filter((r) => r.required && !r.value && !r.stored);
  const valid = d.name.trim() && (isStdio ? d.command.trim() : /^https?:\/\//.test(d.url.trim())) && missingEnv.length === 0;

  const save = useMutation({
    mutationFn: () => {
      const args = d.argsText
        .split("\n")
        .map((a) => a.trim())
        .filter(Boolean);
      const env = kvToRecord(d.env);
      const headers = kvToRecord(d.headers);
      const body: Record<string, unknown> = {
        name: d.name.trim(),
        transport: d.transport,
        command: isStdio ? d.command.trim() : undefined,
        args: isStdio ? args : undefined,
        url: isStdio ? undefined : d.url.trim(),
      };
      if (isStdio) {
        if (!server || Object.keys(env).length) body.env = env;
      } else if (!server || Object.keys(headers).length) {
        body.headers = headers;
      }
      return server ? api.patch<McpServer>(`/mcp/servers/${server.id}`, body) : api.post<McpServer>("/mcp/servers", body);
    },
    onSuccess: (s) => {
      qc.setQueryData<ListResponse<McpServer>>(qk.mcpServers, (old) => (old ? upsertInList(old, s, { prepend: true }) : old));
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
      if (s.status === "error") toast.warning(`${s.name} saved, but it couldn't connect`, { description: s.error ?? undefined });
      else toast.success(server ? "Server updated" : "MCP server added", { description: `${s.name} · ${s.tools.length} tools` });
      onOpenChange(false);
    },
    meta: { errorTitle: "Couldn't save the MCP server" },
  });

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      size="lg"
      title={server ? `Edit ${server.name}` : "Add MCP server"}
      description="Ren connects to the server, lists its tools and makes them available as a tool group."
      footer={
        <>
          <span className="mr-auto">
            <DocsLink href={docsUrl} />
          </span>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}>
            {server ? "Save & reconnect" : "Add server"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
          <Field label="Name" required>
            <Input data-autofocus value={d.name} onChange={(e) => set("name", e.target.value)} placeholder="GitHub" />
          </Field>
          <Field label="Transport">
            <SegmentedControl
              value={d.transport}
              onChange={(v) => set("transport", v)}
              options={[
                { value: "stdio", label: "stdio" },
                { value: "http", label: "HTTP" },
                { value: "sse", label: "SSE" },
              ]}
            />
          </Field>
        </div>
        {isStdio ? (
          <>
            <Field label="Command" required hint="Executable started by the Ren server, e.g. npx, uvx, docker, or a path.">
              <Input value={d.command} onChange={(e) => set("command", e.target.value)} placeholder="npx" className="font-mono" />
            </Field>
            <Field label="Arguments" hint="One argument per line.">
              <Textarea
                mono
                autosize
                minRows={3}
                value={d.argsText}
                onChange={(e) => set("argsText", e.target.value)}
                placeholder={"-y\n@modelcontextprotocol/server-github"}
              />
            </Field>
            <Field label="Environment variables" hint={server ? "Stored values are never shown. Enter a value to replace it." : undefined}>
              <KeyValueEditor rows={d.env} onChange={(rows) => set("env", rows)} keyPlaceholder="GITHUB_TOKEN" />
            </Field>
          </>
        ) : (
          <>
            <Field label="Server URL" required hint={d.transport === "sse" ? "The server's SSE endpoint." : "Streamable HTTP endpoint, e.g. https://mcp.example.com/mcp"}>
              <Input type="url" value={d.url} onChange={(e) => set("url", e.target.value)} placeholder="https://mcp.example.com/mcp" className="font-mono" />
            </Field>
            <Field label="Headers" hint={server ? "Stored values are never shown. Enter a value to replace it." : "e.g. Authorization: Bearer …"}>
              <KeyValueEditor
                rows={d.headers}
                onChange={(rows) => set("headers", rows)}
                keyPlaceholder="Authorization"
                valuePlaceholder="Bearer …"
                addLabel="Add header"
              />
            </Field>
          </>
        )}
      </div>
    </Dialog>
  );
}

function ServerRow({ server, onEdit }: { server: McpServer; onEdit: () => void }) {
  const qc = useQueryClient();
  const { isAdmin } = useRole();
  const [open, setOpen] = useState(false);
  const patchList = (s: McpServer) => qc.setQueryData<ListResponse<McpServer>>(qk.mcpServers, (old) => (old ? upsertInList(old, s) : old));

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch<McpServer>(`/mcp/servers/${server.id}`, { enabled }),
    onSuccess: (s) => {
      patchList(s);
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
    },
    meta: { errorTitle: "Couldn't update the server" },
  });
  const refresh = useMutation({
    mutationFn: () => api.post<McpServer>(`/mcp/servers/${server.id}/refresh`),
    onSuccess: (s) => {
      patchList(s);
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
      if (s.status === "connected") toast.success(`${s.name} reconnected`, { description: `${s.tools.length} tools` });
      else toast.error(`${s.name} couldn't connect`, { description: s.error ?? undefined });
    },
    meta: { errorTitle: "Refresh failed" },
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/mcp/servers/${server.id}`),
    onSuccess: () => {
      qc.setQueryData<ListResponse<McpServer>>(qk.mcpServers, (old) => (old ? removeFromList(old, server.id) : old));
      void qc.invalidateQueries({ queryKey: qk.toolCatalog });
      toast.success("Server removed", { description: server.name });
    },
    meta: { errorTitle: "Couldn't remove the server" },
  });

  const statusTone = server.status === "connected" ? "success" : server.status === "error" ? "danger" : "neutral";
  return (
    <div className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex size-9 items-center justify-center rounded-lg bg-surface-2 text-fg">
          <Server className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="truncate text-[13.5px] font-medium text-fg">{server.name}</p>
            <Badge tone="outline" size="xs">
              {server.transport}
            </Badge>
            <Badge tone={statusTone} size="xs" dot>
              {server.status}
            </Badge>
          </div>
          <p className="truncate font-mono text-[11.5px] text-muted">
            {server.transport === "stdio" ? [server.command, ...(server.args ?? [])].join(" ") : server.url}
          </p>
          {server.error && <p className="mt-0.5 truncate text-xs text-danger">{server.error}</p>}
        </div>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg"
        >
          {server.tools.length} tools <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>
        <Switch
          size="sm"
          checked={server.enabled}
          disabled={!isAdmin || toggle.isPending}
          onCheckedChange={(v) => toggle.mutate(v)}
          aria-label={`${server.enabled ? "Disable" : "Enable"} ${server.name}`}
        />
        <div className="flex items-center">
          <IconButton label="Reconnect and refresh tools" icon={<RefreshCw />} loading={refresh.isPending} disabled={!isAdmin} onClick={() => refresh.mutate()} />
          {isAdmin && <IconButton label={`Edit ${server.name}`} icon={<Pencil />} onClick={onEdit} />}
          {isAdmin && (
            <IconButton
              label={`Remove ${server.name}`}
              icon={<Trash2 />}
              variant="danger"
              onClick={async () => {
                const ok = await confirm({ title: `Remove ${server.name}?`, description: "Its tools disappear from every agent.", confirmLabel: "Remove", danger: true });
                if (ok) remove.mutate();
              }}
            />
          )}
        </div>
      </div>
      {open && (
        <ul className="mt-3 grid gap-1.5 pl-12 sm:grid-cols-2">
          {server.tools.map((t) => (
            <li key={t.name} className="rounded-md border border-border bg-surface px-2.5 py-1.5">
              <p className="font-mono text-[11.5px] text-fg">{t.name}</p>
              {t.description && <p className="line-clamp-2 text-[11px] text-muted">{t.description}</p>}
            </li>
          ))}
          {server.tools.length === 0 && <li className="text-xs text-muted">No tools reported.</li>}
        </ul>
      )}
    </div>
  );
}

export function McpTab() {
  const { isAdmin } = useRole();
  const servers = useMcpServers();
  const presets = useMcpPresets();
  const system = useSystemSettings();
  const [dialog, setDialog] = useState<{ server: McpServer | null; initial: Draft; docs?: string; key: string } | null>(null);
  const endpoint = system.data?.mcp_endpoint || `${getApiBase()}/mcp`;
  const snippet = JSON.stringify({ mcpServers: { ren: { url: endpoint, headers: { Authorization: "Bearer <your-ren-token>" } } } }, null, 2);

  return (
    <div className="space-y-8">
      <Card>
        <CardHeader
          icon={<Waypoints />}
          title="Ren as an MCP server"
          description="Expose Ren's tools to Claude Desktop, IDEs and other agents over Streamable HTTP, authenticated with a bearer token."
        />
        <div className="grid gap-4 p-4 lg:grid-cols-2">
          <div>
            <p className="mb-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">Endpoint</p>
            <div className="flex items-center gap-2 rounded-control border border-border bg-[var(--code-bg)] px-3 py-2">
              <code className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-fg">{endpoint}</code>
              <CopyButton text={endpoint} label="Copy endpoint" />
            </div>
            <p className="mt-2 text-xs text-muted">Calls run with the permissions of the token&apos;s user in the active workspace (send X-Org-Id to pick one).</p>
          </div>
          <div>
            <p className="mb-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">Client config</p>
            <div className="relative">
              <pre className="overflow-auto rounded-control border border-border bg-[var(--code-bg)] px-3 py-2 font-mono text-[11.5px] leading-relaxed text-muted">{snippet}</pre>
              <div className="absolute top-1.5 right-1.5">
                <CopyButton text={snippet} label="Copy config" size="xs" />
              </div>
            </div>
          </div>
        </div>
      </Card>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-fg">Connected servers</h2>
          {isAdmin && (
            <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} onClick={() => setDialog({ server: null, initial: EMPTY, key: "new" })}>
              Add server
            </Button>
          )}
        </div>
        {servers.isLoading ? (
          <Skeleton className="h-20 w-full rounded-card" />
        ) : servers.error && !servers.data ? (
          <Card>
            <ErrorState compact error={servers.error} onRetry={() => servers.refetch()} />
          </Card>
        ) : (servers.data?.items.length ?? 0) === 0 ? (
          <Card className="border-dashed">
            <EmptyState compact icon={<Server />} title="No MCP servers" description="Add any Model Context Protocol server — local (stdio) or remote (HTTP) — or start from a preset below." />
          </Card>
        ) : (
          <Card className="divide-y divide-border">
            {servers.data!.items.map((s) => (
              <ServerRow key={s.id} server={s} onEdit={() => setDialog({ server: s, initial: draftFromServer(s), key: s.id })} />
            ))}
          </Card>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-fg">Presets</h2>
        {presets.isLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-28 rounded-card" />
            ))}
          </div>
        ) : presets.error ? (
          <Card>
            <ErrorState compact error={presets.error} onRetry={() => presets.refetch()} />
          </Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {(presets.data?.items ?? []).map((p) => (
              <Card key={p.id} className="flex flex-col p-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="truncate text-[13.5px] font-semibold text-fg">{p.name}</p>
                  <Badge tone="outline" size="xs">
                    {p.transport}
                  </Badge>
                </div>
                <p className="mt-1.5 line-clamp-2 flex-1 text-xs leading-relaxed text-muted">{p.description}</p>
                <div className="mt-3 flex items-center justify-between">
                  <DocsLink href={p.docs_url} />
                  {isAdmin && (
                    <Button size="sm" onClick={() => setDialog({ server: null, initial: draftFromPreset(p), docs: p.docs_url, key: `preset-${p.id}` })}>
                      Use preset
                    </Button>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>

      {dialog && (
        <McpServerDialog
          key={dialog.key}
          server={dialog.server}
          initial={dialog.initial}
          docsUrl={dialog.docs}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
    </div>
  );
}
