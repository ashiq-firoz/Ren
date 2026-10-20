"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Download, FlaskConical, Plus, Radio, Scale, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { Fragment, useMemo, useState } from "react";
import { HBarList, MiniColumns } from "@/components/charts/charts";
import { PolicyEditor } from "@/components/policies/policy-editor";
import { checkerLabel, VerdictBadge } from "@/components/domain/status";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { SegmentedControl, Switch } from "@/components/ui/controls";
import { EmptyState, ErrorState, Notice, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { Tooltip } from "@/components/ui/floating";
import { Field, Select, Textarea } from "@/components/ui/input";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs } from "@/components/ui/tabs";
import { useAgents, useAgentsMap, useAlignmentChecks, useAlignmentStats, useAlignmentStatus, useOrg, usePolicies } from "@/hooks/queries";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, formatMs, formatNumber, formatPercent, timeAgo } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { ListResponse, Policy, PolicyTestResponse } from "@/lib/types";
import { cn, titleCase } from "@/lib/utils";
import { useRole } from "@/providers/auth";

function LayaPill() {
  const { data, isLoading } = useAlignmentStatus();
  if (isLoading || !data) return <Skeleton className="h-6 w-36 rounded-full" />;
  const { laya } = data;
  const tone = !laya.configured ? "neutral" : laya.reachable ? "success" : "danger";
  const label = !laya.configured ? "Laya not configured" : laya.reachable ? "Laya reachable" : "Laya unreachable";
  const detail = [
    laya.mode !== "none" ? `Mode: ${laya.mode}` : null,
    laya.url ? `URL: ${laya.url}` : null,
    laya.latency_ms !== null ? `Latency: ${formatMs(laya.latency_ms)}` : null,
    laya.error ? `Error: ${laya.error}` : null,
    `LLM judge fallback: ${data.judge.enabled ? "on" : "off"}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Tooltip content={detail} placement="bottom">
      <span tabIndex={0} className="inline-flex">
        <Badge tone={tone} size="md" dot pulse={laya.reachable} icon={<Radio />}>
          {label}
          {laya.reachable && laya.latency_ms !== null ? ` · ${formatMs(laya.latency_ms)}` : ""}
        </Badge>
      </span>
    </Tooltip>
  );
}

function StatsRow() {
  const { data, isLoading } = useAlignmentStats();
  const tiles: { label: string; value: string; sub?: string }[] = data
    ? [
        { label: "Pass rate", value: formatPercent(data.pass_rate), sub: `${formatNumber(data.passed)} passed` },
        { label: "Checks", value: formatNumber(data.total), sub: `${formatNumber(data.warned)} warned` },
        { label: "Blocked", value: formatNumber(data.blocked), sub: "Revised or stopped" },
        { label: "Laya p50 latency", value: formatMs(data.p50_latency_ms), sub: "Per check" },
      ]
    : [];
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {isLoading || !data
        ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[92px] rounded-card" />)
        : tiles.map((t) => (
            <Card key={t.label} className="p-4">
              <p className="text-[12.5px] text-muted">{t.label}</p>
              <p className="mt-2 text-[26px] leading-none font-semibold tracking-tight text-fg">{t.value}</p>
              {t.sub && <p className="mt-1.5 text-xs text-muted">{t.sub}</p>}
            </Card>
          ))}
    </div>
  );
}

function Insights() {
  const { data } = useAlignmentStats();
  if (!data || data.total === 0) return null;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card>
        <CardHeader title="Violations by policy" />
        <div className="p-4">
          <HBarList
            emptyLabel="No violations recorded."
            items={data.by_policy
              .slice()
              .sort((a, b) => b.violations - a.violations)
              .slice(0, 6)
              .map((p) => ({ id: p.policy_id, label: p.policy_name, value: p.violations, color: "var(--warning)" }))}
          />
        </div>
      </Card>
      <Card>
        <CardHeader title="Checks per day" description="Hover a column for the count" />
        <div className="p-4 pt-6">
          <MiniColumns label="Alignment checks per day" data={data.series.map((d) => ({ date: d.date, value: d.checks }))} height={96} />
        </div>
      </Card>
    </div>
  );
}

function PoliciesTab({ onEdit }: { onEdit: (p: Policy | null) => void }) {
  const qc = useQueryClient();
  const { isAdmin } = useRole();
  const { data, isLoading, error, refetch } = usePolicies();
  const toggle = useMutation({
    mutationFn: (p: Policy) => api.patch<Policy>(`/policies/${p.id}`, { enabled: !p.enabled }),
    onMutate: (p) =>
      qc.setQueryData<ListResponse<Policy>>(qk.policies, (old) =>
        old ? { ...old, items: old.items.map((x) => (x.id === p.id ? { ...x, enabled: !p.enabled } : x)) } : old,
      ),
    onSuccess: (p) => toast.success(`${p.name} ${p.enabled ? "enabled" : "disabled"}`),
    onError: () => void qc.invalidateQueries({ queryKey: qk.policies }),
    meta: { errorTitle: "Couldn't update the policy" },
  });

  if (isLoading)
    return (
      <Card>
        <SkeletonRows rows={6} />
      </Card>
    );
  if (error && !data)
    return (
      <Card>
        <ErrorState error={error} onRetry={() => refetch()} />
      </Card>
    );
  const items = data?.items ?? [];
  if (items.length === 0)
    return (
      <Card>
        <EmptyState
          icon={<Scale />}
          title="No policies yet"
          description="Write short, atomic rules — “Never share customer PII outside the company” — and every response and outbound action is checked against them."
          action={
            isAdmin ? (
              <Button variant="primary" onClick={() => onEdit(null)}>
                Create a policy
              </Button>
            ) : undefined
          }
        />
      </Card>
    );
  return (
    <Card className="overflow-hidden">
      <TableContainer>
        <Table>
          <THead>
            <tr>
              <TH>Policy</TH>
              <TH>Category</TH>
              <TH>Severity</TH>
              <TH>Scope</TH>
              <TH>Applies to</TH>
              <TH className="text-right">Rules</TH>
              <TH className="text-right">Enabled</TH>
            </tr>
          </THead>
          <TBody>
            {items.map((p) => (
              <TR key={p.id} interactive onClick={() => onEdit(p)}>
                <TD className="max-w-[320px]">
                  <p className="truncate font-medium text-fg">{p.name}</p>
                  {p.description && <p className="truncate text-xs text-muted">{p.description}</p>}
                </TD>
                <TD className="text-muted">{titleCase(p.category)}</TD>
                <TD>
                  <Badge tone={p.severity === "block" ? "danger" : "warning"} size="xs" dot>
                    {p.severity === "block" ? "Block" : "Warn"}
                  </Badge>
                </TD>
                <TD className="whitespace-nowrap text-muted">{p.scope === "org" ? "Workspace" : `${p.agent_ids.length} agent${p.agent_ids.length === 1 ? "" : "s"}`}</TD>
                <TD className="text-muted">{titleCase(p.applies_to)}</TD>
                <TD className="text-right text-muted tabular-nums">
                  {p.rules.length}
                  {p.patterns.length > 0 && <span className="text-subtle"> +{p.patterns.length} rx</span>}
                </TD>
                <TD className="text-right" onClick={(e) => e.stopPropagation()}>
                  <Switch size="sm" checked={p.enabled} disabled={!isAdmin} onCheckedChange={() => toggle.mutate(p)} aria-label={`Toggle ${p.name}`} />
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </TableContainer>
    </Card>
  );
}

function TestConsole() {
  const policies = usePolicies();
  const agents = useAgents();
  const org = useOrg();
  const [text, setText] = useState("");
  const [request, setRequest] = useState("");
  const [kind, setKind] = useState<"response" | "outbound">("response");
  const [agentId, setAgentId] = useState("");
  const [policyIds, setPolicyIds] = useState<string[]>([]);
  const warn = org.data?.settings.alignment.warn_threshold ?? 0.45;
  const block = org.data?.settings.alignment.block_threshold ?? 0.7;

  const test = useMutation({
    mutationFn: () =>
      api.post<PolicyTestResponse>("/policies/test", {
        text,
        user_request: request.trim() || undefined,
        agent_id: agentId || undefined,
        policy_ids: policyIds.length ? policyIds : undefined,
        kind,
      }),
    meta: { errorTitle: "Policy test failed" },
  });
  const r = test.data;
  const rules = useMemo(() => (r?.per_rule ?? []).slice().sort((a, b) => b.violation_probability - a.violation_probability), [r]);

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="space-y-4 p-5">
        <Field label="Response or action to check" required>
          <Textarea
            autosize
            minRows={7}
            maxRows={18}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste an agent reply, an email draft, or any outbound text…"
          />
        </Field>
        <Field label="User request (optional)" hint="Gives the aligner context about what was asked.">
          <Textarea autosize minRows={2} value={request} onChange={(e) => setRequest(e.target.value)} placeholder="What did the user ask for?" />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Kind">
            <SegmentedControl
              fullWidth
              size="sm"
              value={kind}
              onChange={setKind}
              options={[
                { value: "response", label: "Response" },
                { value: "outbound", label: "Outbound" },
              ]}
            />
          </Field>
          <Field label="As agent" hint="Applies that agent's policies.">
            <Select value={agentId} onChange={(e) => setAgentId(e.target.value)} selectSize="sm">
              <option value="">Any (workspace policies)</option>
              {(agents.data?.items ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        {(policies.data?.items.length ?? 0) > 0 && (
          <div>
            <p className="mb-1.5 text-[13px] font-medium text-fg">Policies</p>
            <div className="flex flex-wrap gap-1.5">
              {policies.data!.items.map((p) => {
                const on = policyIds.includes(p.id);
                return (
                  <button
                    key={p.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setPolicyIds((ids) => (on ? ids.filter((x) => x !== p.id) : [...ids, p.id]))}
                    className={cn(
                      "h-6 rounded-full border px-2.5 text-xs transition-colors",
                      on ? "border-primary bg-primary text-primary-fg" : "border-border text-muted hover:text-fg",
                    )}
                  >
                    {p.name}
                  </button>
                );
              })}
            </div>
            <p className="mt-1.5 text-[11.5px] text-subtle">{policyIds.length ? `${policyIds.length} selected` : "All applicable policies"}</p>
          </div>
        )}
        <div className="flex justify-end">
          <Button variant="primary" icon={<FlaskConical className="size-4" />} loading={test.isPending} disabled={!text.trim()} onClick={() => test.mutate()}>
            Run check
          </Button>
        </div>
      </Card>

      <Card className="p-5">
        {!r ? (
          <EmptyState compact icon={<ShieldCheck />} title="Verdict appears here" description="See the overall verdict and the violation probability for every rule." />
        ) : (
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <VerdictBadge verdict={r.verdict} size="md" />
              <span className="text-xs text-muted">
                {checkerLabel(r.checker)} · {formatMs(r.latency_ms)}
              </span>
              {r.revised && (
                <Badge tone="info" size="xs">
                  Would be revised
                </Badge>
              )}
            </div>
            {r.violations.length > 0 && (
              <Notice tone={r.verdict === "block" ? "danger" : "warning"} className="mt-4" title={`${r.violations.length} violation${r.violations.length === 1 ? "" : "s"}`}>
                <ul className="mt-1 list-disc space-y-0.5 pl-4">
                  {r.violations.map((v, i) => (
                    <li key={i}>
                      <span className="font-medium text-fg">{v.policy_name}</span>
                      {v.rule ? ` — ${v.rule}` : ""}
                    </li>
                  ))}
                </ul>
              </Notice>
            )}
            <div className="mt-5">
              <div className="mb-2 flex items-center justify-between text-[11px] text-subtle">
                <span className="font-medium tracking-wide uppercase">Per-rule violation probability</span>
                <span>
                  warn ≥ {Math.round(warn * 100)}% · block ≥ {Math.round(block * 100)}%
                </span>
              </div>
              {rules.length === 0 ? (
                <p className="text-[13px] text-muted">No rules were evaluated.</p>
              ) : (
                <ul className="space-y-3">
                  {rules.map((rule, i) => {
                    const p = rule.violation_probability;
                    const color = p >= block ? "var(--danger)" : p >= warn ? "var(--warning)" : "var(--success)";
                    return (
                      <li key={`${rule.policy_id}-${i}`}>
                        <div className="flex items-start justify-between gap-3 text-[12.5px]">
                          <span className="min-w-0 text-fg">
                            {rule.rule}
                            <span className="block text-[11px] text-muted">
                              {rule.policy_name} · {checkerLabel(rule.checker)}
                            </span>
                          </span>
                          <span className="shrink-0 font-semibold text-fg tabular-nums">{Math.round(p * 100)}%</span>
                        </div>
                        <div className="relative mt-1.5 h-2 rounded-full bg-surface-2">
                          <div className="h-full rounded-full transition-[width]" style={{ width: `${Math.max(1.5, p * 100)}%`, background: color }} />
                          <span aria-hidden className="absolute -top-0.5 h-3 w-px bg-warning" style={{ left: `${warn * 100}%` }} />
                          <span aria-hidden className="absolute -top-0.5 h-3 w-px bg-danger" style={{ left: `${block * 100}%` }} />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

const PAGE = 50;

function AlignmentLog() {
  const agents = useAgents();
  const agentsMap = useAgentsMap();
  const [verdict, setVerdict] = useState("");
  const [agentId, setAgentId] = useState("");
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, isLoading, error, refetch, isPlaceholderData } = useAlignmentChecks({
    verdict: verdict || undefined,
    agent_id: agentId || undefined,
    limit: PAGE,
    offset: page * PAGE,
  });
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <SegmentedControl
          size="sm"
          aria-label="Filter by verdict"
          value={verdict || "all"}
          onChange={(v) => {
            setVerdict(v === "all" ? "" : v);
            setPage(0);
          }}
          options={[
            { value: "all", label: "All" },
            { value: "pass", label: "Pass" },
            { value: "warn", label: "Warn" },
            { value: "block", label: "Block" },
          ]}
        />
        <Select
          selectSize="sm"
          value={agentId}
          onChange={(e) => {
            setAgentId(e.target.value);
            setPage(0);
          }}
          aria-label="Filter by agent"
          wrapperClassName="w-48"
        >
          <option value="">All agents</option>
          {(agents.data?.items ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </Select>
      </div>
      <Card className="overflow-hidden">
        {isLoading ? (
          <SkeletonRows rows={8} />
        ) : error && !data ? (
          <ErrorState error={error} onRetry={() => refetch()} />
        ) : items.length === 0 ? (
          <EmptyState compact title="No checks" description="Alignment checks show up here as agents respond and act." />
        ) : (
          <TableContainer className={isPlaceholderData ? "opacity-60" : undefined}>
            <Table>
              <THead>
                <tr>
                  <TH>Time</TH>
                  <TH>Agent</TH>
                  <TH>Kind</TH>
                  <TH>Verdict</TH>
                  <TH>Checker</TH>
                  <TH className="text-right">Latency</TH>
                  <TH>Excerpt</TH>
                </tr>
              </THead>
              <TBody>
                {items.map((c) => (
                  <Fragment key={c.id}>
                    <TR interactive onClick={() => setExpanded((x) => (x === c.id ? null : c.id))}>
                      <TD className="whitespace-nowrap text-muted" title={formatDateTime(c.created_at)}>
                        {timeAgo(c.created_at)}
                      </TD>
                      <TD>
                        <span className="flex items-center gap-2 whitespace-nowrap">
                          <AgentAvatar agent={c.agent_id ? agentsMap.get(c.agent_id) : undefined} name={c.agent_name ?? "—"} size="xs" />
                          <span className="text-muted">{c.agent_name ?? "—"}</span>
                        </span>
                      </TD>
                      <TD className="text-muted">{titleCase(c.kind)}</TD>
                      <TD>
                        <VerdictBadge verdict={c.verdict} size="xs" />
                      </TD>
                      <TD className="text-muted">{checkerLabel(c.checker)}</TD>
                      <TD className="text-right text-muted tabular-nums">{formatMs(c.latency_ms)}</TD>
                      <TD className="max-w-[320px]">
                        <span className="flex items-center gap-1.5">
                          <span className="truncate text-muted">{c.excerpt}</span>
                          {c.violations.length > 0 && (
                            <ChevronDown className={cn("size-3.5 shrink-0 text-subtle transition-transform", expanded === c.id && "rotate-180")} />
                          )}
                        </span>
                      </TD>
                    </TR>
                    {expanded === c.id && (
                      <tr>
                        <td colSpan={7} className="bg-surface-2/40 px-4 py-3">
                          <p className="text-[13px] whitespace-pre-wrap text-fg">{c.excerpt}</p>
                          {c.violations.length > 0 && (
                            <ul className="mt-2 space-y-1">
                              {c.violations.map((v, i) => (
                                <li key={i} className="text-xs text-muted">
                                  <span className="font-medium text-fg">{v.policy_name}</span> · {Math.round(v.score * 100)}%{v.rule ? ` — ${v.rule}` : ""}
                                  {v.explanation ? ` (${v.explanation})` : ""}
                                </li>
                              ))}
                            </ul>
                          )}
                          {c.task_id && (
                            <Link href={`/tasks/${c.task_id}`} className="mt-2 inline-block text-xs font-medium text-muted hover:text-fg">
                              Open task →
                            </Link>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </TBody>
            </Table>
          </TableContainer>
        )}
        {total > PAGE && (
          <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted">
            <span className="tabular-nums">
              {page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of {total}
            </span>
            <div className="flex gap-1">
              <Button size="xs" variant="ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                Previous
              </Button>
              <Button size="xs" variant="ghost" disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function ExportLayaButton() {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      icon={<Download className="size-4" />}
      loading={busy}
      title="Labelled alignment checks as NDJSON, for fine-tuning Laya on your policies"
      onClick={async () => {
        setBusy(true);
        try {
          await api.download("/alignment/training-data", undefined, "laya-training.ndjson");
          toast.success("Laya training data exported");
        } catch (err) {
          toast.error("Export failed", { description: errorMessage(err) });
        } finally {
          setBusy(false);
        }
      }}
    >
      Export Laya training data
    </Button>
  );
}

export default function PoliciesPage() {
  const { isAdmin } = useRole();
  const [tab, setTab] = useState<"policies" | "test" | "log">("policies");
  const [editing, setEditing] = useState<{ policy: Policy | null } | null>(null);
  return (
    <PageContainer>
      <PageHeader
        title="Policies"
        description="Alignment rules every response and outbound action is checked against — by Laya, Ren's fast policy aligner, with an optional LLM judge."
        actions={
          <>
            <LayaPill />
            {isAdmin && <ExportLayaButton />}
            {isAdmin && (
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing({ policy: null })}>
                New policy
              </Button>
            )}
          </>
        }
      />
      <div className="space-y-5">
        <StatsRow />
        <Tabs
          aria-label="Policies sections"
          value={tab}
          onValueChange={setTab}
          items={[
            { value: "policies", label: "Policies" },
            { value: "test", label: "Test console" },
            { value: "log", label: "Alignment log" },
          ]}
        />
        {tab === "policies" && (
          <div className="space-y-5">
            <PoliciesTab onEdit={(p) => setEditing({ policy: p })} />
            <Insights />
          </div>
        )}
        {tab === "test" && <TestConsole />}
        {tab === "log" && <AlignmentLog />}
      </div>
      {editing && <PolicyEditor key={editing.policy?.id ?? "new"} policy={editing.policy} readOnly={!isAdmin} onClose={() => setEditing(null)} />}
    </PageContainer>
  );
}
