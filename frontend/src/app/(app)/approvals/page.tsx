"use client";

import { History, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ApprovalCard, ApprovalDecisionBadge } from "@/components/domain/approval-card";
import { RiskBadge } from "@/components/domain/status";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Card } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/controls";
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs } from "@/components/ui/tabs";
import { useAgentsMap, useApprovals } from "@/hooks/queries";
import { formatDateTime, timeAgo } from "@/lib/format";
import type { Risk } from "@/lib/types";

const RISK_ORDER: Record<Risk, number> = { critical: 0, high: 1, medium: 2, low: 3 };

function PendingList() {
  const { data, isLoading, error, refetch } = useApprovals("pending");
  const [sort, setSort] = useState<"risk" | "newest">("risk");
  const items = (data?.items ?? [])
    .filter((a) => a.status === "pending")
    .sort((a, b) =>
      sort === "risk" ? RISK_ORDER[a.risk] - RISK_ORDER[b.risk] || b.created_at.localeCompare(a.created_at) : b.created_at.localeCompare(a.created_at),
    );

  if (isLoading)
    return (
      <div className="space-y-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-56 w-full rounded-card" />
        ))}
      </div>
    );
  if (error && !data)
    return (
      <Card>
        <ErrorState error={error} onRetry={() => refetch()} />
      </Card>
    );
  if (items.length === 0)
    return (
      <Card>
        <EmptyState
          icon={<ShieldCheck />}
          title="Nothing waiting on you"
          description="When an agent wants to take a sensitive action — send an email, change a record, spend money — it pauses here for your decision."
        />
      </Card>
    );
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[13px] text-muted">
          {items.length} {items.length === 1 ? "request" : "requests"} waiting
        </p>
        <SegmentedControl
          size="sm"
          aria-label="Sort"
          value={sort}
          onChange={setSort}
          options={[
            { value: "risk", label: "Highest risk" },
            { value: "newest", label: "Newest" },
          ]}
        />
      </div>
      {items.map((a) => (
        <ApprovalCard key={a.id} approval={a} />
      ))}
    </div>
  );
}

function HistoryList() {
  const { data, isLoading, error, refetch } = useApprovals("all");
  const agents = useAgentsMap();
  const items = (data?.items ?? []).filter((a) => a.status !== "pending").sort((a, b) => (b.decided_at ?? b.created_at).localeCompare(a.decided_at ?? a.created_at));
  if (isLoading)
    return (
      <Card>
        <SkeletonRows rows={8} />
      </Card>
    );
  if (error && !data)
    return (
      <Card>
        <ErrorState error={error} onRetry={() => refetch()} />
      </Card>
    );
  if (items.length === 0)
    return (
      <Card>
        <EmptyState icon={<History />} title="No decisions yet" description="Approved, rejected and expired requests are kept here for the record." />
      </Card>
    );
  return (
    <Card className="overflow-hidden">
      <TableContainer>
        <Table>
          <THead>
            <tr>
              <TH>Action</TH>
              <TH>Agent</TH>
              <TH>Risk</TH>
              <TH>Decision</TH>
              <TH>Requested</TH>
              <TH>Comment</TH>
            </tr>
          </THead>
          <TBody>
            {items.map((a) => (
              <TR key={a.id}>
                <TD className="max-w-[280px]">
                  <p className="truncate font-medium text-fg">{a.tool_display || a.tool}</p>
                  {a.task_id && (
                    <Link href={`/tasks/${a.task_id}`} className="block truncate text-xs text-muted hover:text-fg">
                      {a.task_title ?? "View task"}
                    </Link>
                  )}
                </TD>
                <TD>
                  <span className="flex items-center gap-2 whitespace-nowrap text-muted">
                    <AgentAvatar agent={a.agent_id ? agents.get(a.agent_id) : undefined} name={a.agent_name} size="xs" />
                    {a.agent_name ?? "—"}
                  </span>
                </TD>
                <TD>
                  <RiskBadge risk={a.risk} size="xs" label={false} />
                </TD>
                <TD>
                  <div className="flex flex-col gap-0.5">
                    <ApprovalDecisionBadge approval={a} />
                    {a.decided_at && <span className="text-[11px] text-subtle">{formatDateTime(a.decided_at)}</span>}
                  </div>
                </TD>
                <TD className="whitespace-nowrap text-muted" title={formatDateTime(a.created_at)}>
                  {timeAgo(a.created_at)}
                </TD>
                <TD className="max-w-[240px] truncate text-muted italic">{a.comment ?? "—"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </TableContainer>
    </Card>
  );
}

export default function ApprovalsPage() {
  const [tab, setTab] = useState<"pending" | "history">("pending");
  const pending = useApprovals("pending");
  const pendingCount = pending.data?.items.filter((a) => a.status === "pending").length;
  return (
    <PageContainer className="max-w-[960px]">
      <PageHeader
        title="Approvals"
        description="Sensitive actions wait here for a human decision. Review the preview, adjust the arguments if needed, then approve or reject."
      />
      <Tabs
        aria-label="Approvals"
        value={tab}
        onValueChange={setTab}
        className="mb-5"
        items={[
          { value: "pending", label: "Pending", count: pendingCount ?? null },
          { value: "history", label: "History" },
        ]}
      />
      {tab === "pending" ? <PendingList /> : <HistoryList />}
    </PageContainer>
  );
}
