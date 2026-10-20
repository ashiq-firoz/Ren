"use client";

import { ChevronDown, ChevronLeft, ChevronRight, Lock, ScrollText, Search } from "lucide-react";
import { Fragment, useState } from "react";
import { ActorAvatar, actorName, AuditTarget, humanizeAction } from "@/components/domain/audit";
import { RiskBadge } from "@/components/domain/status";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/input";
import { JsonView } from "@/components/ui/json-view";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useAgents, useAgentsMap, useAudit, useMembers } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { formatDateTime, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

const PAGE = 50;

export default function ActivityPage() {
  const { isAdmin } = useRole();
  const agents = useAgents();
  const agentsMap = useAgentsMap();
  const members = useMembers(isAdmin);
  const [action, setAction] = useState("");
  const debouncedAction = useDebounced(action.trim(), 300);
  const [risk, setRisk] = useState("");
  const [agentId, setAgentId] = useState("");
  const [userId, setUserId] = useState("");
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data, isLoading, error, refetch, isPlaceholderData } = useAudit(
    {
      action: debouncedAction || undefined,
      risk: risk || undefined,
      agent_id: agentId || undefined,
      user_id: userId || undefined,
      limit: PAGE,
      offset: page * PAGE,
    },
    isAdmin,
  );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const resetPage = () => setPage(0);

  if (!isAdmin) {
    return (
      <PageContainer>
        <PageHeader title="Activity" description="The workspace audit log." />
        <Card>
          <EmptyState icon={<Lock />} title="Admins only" description="The audit log is available to workspace admins and owners." />
        </Card>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader title="Activity" description="Every action by people and agents — tool calls, approvals, changes to agents, policies and connectors — with risk levels." />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          leftIcon={<Search />}
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
            resetPage();
          }}
          placeholder="Filter by action (e.g. tool.call)"
          aria-label="Filter by action"
          wrapperClassName="w-full sm:w-64"
        />
        <Select
          value={risk}
          onChange={(e) => {
            setRisk(e.target.value);
            resetPage();
          }}
          aria-label="Filter by risk"
          wrapperClassName="w-full sm:w-40"
        >
          <option value="">Any risk</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="critical">Critical</option>
        </Select>
        <Select
          value={userId}
          onChange={(e) => {
            setUserId(e.target.value);
            resetPage();
          }}
          aria-label="Filter by person"
          wrapperClassName="w-full sm:w-48"
        >
          <option value="">Anyone</option>
          {(members.data?.items ?? []).map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.name}
            </option>
          ))}
        </Select>
        <Select
          value={agentId}
          onChange={(e) => {
            setAgentId(e.target.value);
            resetPage();
          }}
          aria-label="Filter by agent"
          wrapperClassName="w-full sm:w-48"
        >
          <option value="">Any agent</option>
          {(agents.data?.items ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </Select>
      </div>

      <Card className="overflow-hidden">
        {isLoading ? (
          <SkeletonRows rows={10} />
        ) : error && !data ? (
          <ErrorState error={error} onRetry={() => refetch()} />
        ) : items.length === 0 ? (
          <EmptyState icon={<ScrollText />} title="No activity" description="Nothing matches these filters yet." />
        ) : (
          <TableContainer className={isPlaceholderData ? "opacity-60" : undefined}>
            <Table>
              <THead>
                <tr>
                  <TH>Time</TH>
                  <TH>Actor</TH>
                  <TH>Action</TH>
                  <TH>Target</TH>
                  <TH>Risk</TH>
                  <TH className="w-8">
                    <span className="sr-only">Details</span>
                  </TH>
                </tr>
              </THead>
              <TBody>
                {items.map((e) => {
                  const open = expanded === e.id;
                  const hasDetails = e.details && Object.keys(e.details).length > 0;
                  return (
                    <Fragment key={e.id}>
                      <TR interactive={hasDetails} onClick={() => hasDetails && setExpanded(open ? null : e.id)}>
                        <TD className="whitespace-nowrap text-muted" title={formatDateTime(e.created_at)}>
                          {timeAgo(e.created_at)}
                        </TD>
                        <TD>
                          <span className="flex items-center gap-2 whitespace-nowrap">
                            <ActorAvatar entry={e} agent={e.agent_id ? agentsMap.get(e.agent_id) : undefined} size="xs" />
                            <span className="text-fg">{actorName(e)}</span>
                            {e.agent_name && e.user_name && <span className="text-xs text-subtle">for {e.user_name}</span>}
                          </span>
                        </TD>
                        <TD>
                          <span className="block text-fg">{humanizeAction(e.action)}</span>
                          <span className="block font-mono text-[11px] text-subtle">{e.action}</span>
                        </TD>
                        <TD className="max-w-[260px]">
                          <span className="flex min-w-0">
                            <AuditTarget entry={e} />
                          </span>
                        </TD>
                        <TD>{e.risk ? <RiskBadge risk={e.risk} size="xs" label={false} /> : <span className="text-subtle">—</span>}</TD>
                        <TD>
                          {hasDetails && (
                            <ChevronDown className={cn("size-4 text-subtle transition-transform", open && "rotate-180")} aria-label={open ? "Hide details" : "Show details"} />
                          )}
                        </TD>
                      </TR>
                      {open && (
                        <tr>
                          <td colSpan={6} className="bg-surface-2/40 px-4 py-3">
                            <JsonView data={e.details} defaultDepth={2} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
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
              <Button size="xs" variant="ghost" icon={<ChevronLeft className="size-3.5" />} disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                Previous
              </Button>
              <Button size="xs" variant="ghost" iconRight={<ChevronRight className="size-3.5" />} disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>
    </PageContainer>
  );
}
