"use client";

import { Check, ChevronDown, ExternalLink, MessageSquare, ShieldQuestion, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { JsonView } from "@/components/ui/json-view";
import { Markdown } from "@/components/ui/markdown";
import { useApprovalDecision } from "@/hooks/actions";
import { useAgentsMap } from "@/hooks/queries";
import { formatDateTime, timeAgo, timeUntil } from "@/lib/format";
import type { Approval } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth, useRole } from "@/providers/auth";
import { RiskBadge } from "./status";

export function ApprovalDecisionBadge({ approval }: { approval: Pick<Approval, "status" | "decided_by_name" | "decided_at"> }) {
  if (approval.status === "pending") return <Badge tone="warning" dot pulse>Pending</Badge>;
  const tone = approval.status === "approved" ? "success" : approval.status === "rejected" ? "danger" : "outline";
  return (
    <Badge tone={tone} dot>
      {approval.status[0].toUpperCase() + approval.status.slice(1)}
      {approval.decided_by_name ? ` by ${approval.decided_by_name}` : ""}
    </Badge>
  );
}

function useCanDecide(approval: Approval) {
  const { user } = useAuth();
  const { isAdmin, canWrite } = useRole();
  if (approval.status !== "pending") return false;
  if (isAdmin) return true;
  return canWrite && (!approval.requested_for_user_id || approval.requested_for_user_id === user?.id);
}

/** Compact approval with inline Approve / Reject — used in chat and the swarm panel. */
export function ApprovalInline({ approval, className }: { approval: Approval; className?: string }) {
  const decide = useApprovalDecision();
  const canDecide = useCanDecide(approval);
  const [expanded, setExpanded] = useState(false);
  const [comment, setComment] = useState("");
  const [showComment, setShowComment] = useState(false);
  const pendingDecision = decide.isPending ? decide.variables?.decision : null;

  return (
    <div className={cn("space-y-2.5", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <RiskBadge risk={approval.risk} size="xs" />
        <span className="text-[13px] font-medium text-fg">{approval.tool_display || approval.tool}</span>
        {approval.agent_name && <span className="text-xs text-muted">· {approval.agent_name}</span>}
      </div>
      {approval.reason && <p className="text-xs text-muted">{approval.reason}</p>}
      {approval.preview && (
        <div className="relative">
          <div
            className={cn(
              "overflow-hidden rounded-lg border border-border bg-bg/60 px-3 py-2.5",
              !expanded && "max-h-36",
            )}
          >
            <Markdown content={approval.preview} size="sm" />
          </div>
          {!expanded && approval.preview.length > 280 && (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="absolute inset-x-0 bottom-0 flex h-10 items-end justify-center rounded-b-lg bg-gradient-to-t from-surface to-transparent pb-1.5 text-[11.5px] font-medium text-muted hover:text-fg"
            >
              <span className="inline-flex items-center gap-1">
                Show all <ChevronDown className="size-3" />
              </span>
            </button>
          )}
        </div>
      )}
      {approval.status === "pending" ? (
        canDecide ? (
          <div className="space-y-2">
            {showComment && (
              <Textarea
                autosize
                minRows={1}
                maxRows={5}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="Optional note for the agent…"
                className="text-[13px]"
              />
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                icon={<Check className="size-3.5" />}
                loading={pendingDecision === "approve"}
                disabled={decide.isPending}
                onClick={() => decide.mutate({ id: approval.id, decision: "approve", comment })}
              >
                Approve
              </Button>
              <Button
                size="sm"
                variant="danger-outline"
                icon={<X className="size-3.5" />}
                loading={pendingDecision === "reject"}
                disabled={decide.isPending}
                onClick={() => decide.mutate({ id: approval.id, decision: "reject", comment })}
              >
                Reject
              </Button>
              {!showComment && (
                <Button size="sm" variant="ghost" icon={<MessageSquare className="size-3.5" />} onClick={() => setShowComment(true)}>
                  Note
                </Button>
              )}
              <Link href="/approvals" className="ml-auto inline-flex items-center gap-1 text-xs text-muted hover:text-fg">
                Review <ExternalLink className="size-3" />
              </Link>
            </div>
          </div>
        ) : (
          <p className="text-xs text-muted">Waiting for an approver.</p>
        )
      ) : (
        <div className="flex items-center gap-2 text-xs text-muted">
          <ApprovalDecisionBadge approval={approval} />
          {approval.comment && <span className="truncate italic">“{approval.comment}”</span>}
        </div>
      )}
    </div>
  );
}

/** Full approval card for the Approvals page: preview, editable args, comment, decision. */
export function ApprovalCard({ approval }: { approval: Approval }) {
  const decide = useApprovalDecision();
  const canDecide = useCanDecide(approval);
  const agents = useAgentsMap();
  const agent = approval.agent_id ? agents.get(approval.agent_id) : undefined;
  const [showArgs, setShowArgs] = useState(false);
  const [argsText, setArgsText] = useState(() => JSON.stringify(approval.args ?? {}, null, 2));
  const [argsError, setArgsError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const original = JSON.stringify(approval.args ?? {}, null, 2);
  const pendingDecision = decide.isPending ? decide.variables?.decision : null;

  const approve = () => {
    let modified: Record<string, unknown> | undefined;
    if (argsText.trim() !== original.trim()) {
      try {
        const parsed = JSON.parse(argsText);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Arguments must be a JSON object.");
        modified = parsed;
      } catch (e) {
        setArgsError(e instanceof Error ? e.message : "Invalid JSON");
        setShowArgs(true);
        return;
      }
    }
    decide.mutate({ id: approval.id, decision: "approve", comment, modified_args: modified });
  };

  return (
    <article className="overflow-hidden rounded-card border border-border bg-surface">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3.5">
        <div className="flex min-w-0 items-start gap-3">
          <AgentAvatar agent={agent} name={approval.agent_name} size="md" />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[14px] font-semibold text-fg">{approval.tool_display || approval.tool}</h3>
              <RiskBadge risk={approval.risk} size="xs" />
            </div>
            <p className="mt-0.5 text-xs text-muted">
              {approval.agent_name ?? "An agent"} wants to run <span className="font-mono">{approval.tool}</span>
              {approval.task_id && (
                <>
                  {" "}
                  for{" "}
                  <Link href={`/tasks/${approval.task_id}`} className="font-medium text-fg hover:underline">
                    {approval.task_title ?? "a task"}
                  </Link>
                </>
              )}
              {" · "}
              <span title={formatDateTime(approval.created_at)}>{timeAgo(approval.created_at)}</span>
            </p>
          </div>
        </div>
        <ApprovalDecisionBadge approval={approval} />
      </header>

      <div className="space-y-4 px-4 py-4">
        {approval.reason && (
          <div className="flex items-start gap-2 text-[13px] text-muted">
            <ShieldQuestion className="mt-0.5 size-4 shrink-0 text-warning" />
            <p>{approval.reason}</p>
          </div>
        )}
        {approval.preview && (
          <div>
            <p className="mb-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">Preview</p>
            <div className="rounded-lg border border-border bg-bg/50 px-4 py-3">
              <Markdown content={approval.preview} size="sm" />
            </div>
          </div>
        )}
        <div>
          <button
            type="button"
            onClick={() => setShowArgs((s) => !s)}
            className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg"
            aria-expanded={showArgs}
          >
            <ChevronDown className={cn("size-3.5 transition-transform", showArgs && "rotate-180")} />
            Raw arguments{canDecide ? " (editable)" : ""}
          </button>
          {showArgs && (
            <div className="mt-2">
              {canDecide ? (
                <>
                  <Textarea
                    mono
                    autosize
                    minRows={4}
                    maxRows={18}
                    value={argsText}
                    invalid={!!argsError}
                    onChange={(e) => {
                      setArgsText(e.target.value);
                      setArgsError(null);
                    }}
                    aria-label="Tool arguments (JSON)"
                  />
                  <div className="mt-1 flex items-center justify-between text-[11.5px]">
                    <span className={argsError ? "text-danger" : "text-subtle"}>
                      {argsError ?? (argsText.trim() !== original.trim() ? "Modified — the agent will run with your edits." : "Edit to change what the agent runs.")}
                    </span>
                    {argsText.trim() !== original.trim() && (
                      <button
                        type="button"
                        className="font-medium text-muted hover:text-fg"
                        onClick={() => {
                          setArgsText(original);
                          setArgsError(null);
                        }}
                      >
                        Reset
                      </button>
                    )}
                  </div>
                </>
              ) : (
                <JsonView data={approval.args} />
              )}
            </div>
          )}
        </div>

        {approval.status === "pending" && canDecide && (
          <div className="space-y-3 border-t border-border pt-4">
            <Textarea
              autosize
              minRows={2}
              maxRows={6}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Add a comment for the agent (optional)…"
              aria-label="Comment"
            />
            <div className="flex flex-wrap items-center justify-end gap-2">
              {approval.expires_at && (
                <span className="mr-auto text-xs text-subtle">Expires {timeUntil(approval.expires_at)}</span>
              )}
              <Button
                variant="danger-outline"
                icon={<X className="size-4" />}
                loading={pendingDecision === "reject"}
                disabled={decide.isPending}
                onClick={() => decide.mutate({ id: approval.id, decision: "reject", comment })}
              >
                Reject
              </Button>
              <Button
                variant="primary"
                icon={<Check className="size-4" />}
                loading={pendingDecision === "approve"}
                disabled={decide.isPending}
                onClick={approve}
              >
                Approve
              </Button>
            </div>
          </div>
        )}
        {approval.status === "pending" && !canDecide && (
          <p className="border-t border-border pt-3 text-xs text-muted">You can view this request, but an admin or the task owner must decide.</p>
        )}
        {approval.status !== "pending" && approval.comment && (
          <p className="border-t border-border pt-3 text-[13px] text-muted italic">“{approval.comment}”</p>
        )}
      </div>
    </article>
  );
}
