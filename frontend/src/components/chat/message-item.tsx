"use client";

import { AlertCircle, AtSign, Clock, Eye, Loader2, RotateCcw } from "lucide-react";
import Link from "next/link";
import { memo } from "react";
import { SourcesRow } from "@/components/domain/citations";
import { FileKindIcon } from "@/components/files/file-icon";
import { useFileClick } from "@/components/files/file-viewer-context";
import { AgentAvatar } from "@/components/ui/avatar";
import { CopyButton } from "@/components/ui/copy-button";
import { Markdown } from "@/components/ui/markdown";
import { useAgentsMap } from "@/hooks/queries";
import { api, errorMessage } from "@/lib/api";
import { isTempId } from "@/lib/cache";
import { viewerHref } from "@/lib/files";
import { formatBytes, formatDateTime, formatTime } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { Attachment, Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AlignmentBadge } from "./alignment-badge";
import { EventCard } from "./event-card";
import { ToolCallChips } from "./tool-calls";

export function AttachmentChip({ attachment, className }: { attachment: Attachment; className?: string }) {
  const fileClick = useFileClick();
  const download = async () => {
    try {
      await api.download("/files/download", { path: attachment.path }, attachment.name);
    } catch (err) {
      toast.error("Download failed", { description: errorMessage(err) });
    }
  };
  return (
    <span
      className={cn(
        "inline-flex h-8 max-w-[260px] items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-xs",
        className,
      )}
    >
      <FileKindIcon name={attachment.name} className="size-3.5 text-muted" />
      {attachment.document_id ? (
        <Link href={`/knowledge/${attachment.document_id}`} className="truncate font-medium text-fg hover:underline">
          {attachment.name}
        </Link>
      ) : (
        <button type="button" onClick={download} title="Download" className="truncate font-medium text-fg hover:underline">
          {attachment.name}
        </button>
      )}
      <span className="shrink-0 text-subtle">{formatBytes(attachment.size)}</span>
      {attachment.path && (
        <Link
          href={viewerHref({ path: attachment.path })}
          prefetch={false}
          onClick={fileClick({ path: attachment.path })}
          aria-label={`View ${attachment.name}`}
          title="View"
          className="-mr-1 inline-flex size-6 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-fg"
        >
          <Eye className="size-3.5" />
        </Link>
      )}
    </span>
  );
}

function UserMessage({ message, onRetry }: { message: Message; onRetry?: (m: Message) => void }) {
  const agents = useAgentsMap();
  const mention = message.metadata?.mention_agent_id ? agents.get(message.metadata.mention_agent_id) : undefined;
  const attachments = message.metadata?.attachments ?? [];
  const sending = isTempId(message.id) && message.status !== "error";
  return (
    <div className="group flex justify-end">
      <div className="flex max-w-[min(85%,640px)] flex-col items-end gap-1.5">
        {attachments.length > 0 && (
          <div className="flex flex-wrap justify-end gap-1.5">
            {attachments.map((a) => (
              <AttachmentChip key={a.id} attachment={a} />
            ))}
          </div>
        )}
        {mention && (
          <span className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 py-0.5 text-[11.5px] text-muted">
            <AtSign className="size-3" /> for {mention.name}
          </span>
        )}
        {message.content && (
          <div
            className={cn(
              "rounded-2xl rounded-br-md px-4 py-2.5 text-[15px] leading-relaxed break-words whitespace-pre-wrap text-fg",
              message.status === "error" ? "border border-[color-mix(in_srgb,var(--danger)_35%,transparent)] bg-surface" : "bg-surface-2",
            )}
          >
            {message.content}
          </div>
        )}
        <div className="flex items-center gap-2 text-[11px] text-subtle">
          {message.status === "queued" && (
            <span className="inline-flex items-center gap-1 text-muted">
              <Clock className="size-3" /> Queued — Ren will reply after the current response
            </span>
          )}
          {message.status === "error" && (
            <span className="inline-flex items-center gap-1.5 text-danger">
              <AlertCircle className="size-3" /> Not sent
              {onRetry && (
                <button
                  type="button"
                  onClick={() => onRetry(message)}
                  className="inline-flex items-center gap-1 font-medium underline underline-offset-2"
                >
                  <RotateCcw className="size-3" /> Retry
                </button>
              )}
            </span>
          )}
          {sending && message.status !== "queued" && (
            <span className="inline-flex items-center gap-1">
              <Loader2 className="spin size-3" /> Sending
            </span>
          )}
          <time
            dateTime={message.created_at}
            title={formatDateTime(message.created_at)}
            className="opacity-0 transition-opacity group-hover:opacity-100"
          >
            {formatTime(message.created_at)}
          </time>
        </div>
      </div>
    </div>
  );
}

function AssistantMessage({ message }: { message: Message }) {
  const agents = useAgentsMap();
  const agent = message.agent_id ? agents.get(message.agent_id) : undefined;
  const isRen = !agent || agent.is_orchestrator;
  const name = isRen ? "Ren" : agent.name;
  const streaming = message.status === "streaming";
  const citations = message.metadata?.citations ?? [];
  const toolCalls = message.metadata?.tool_calls ?? [];
  return (
    <div className="group flex gap-3">
      {isRen ? <AgentAvatar orchestrator size="md" title="Ren" /> : <AgentAvatar agent={agent} size="md" />}
      <div className="min-w-0 flex-1">
        <div className="flex h-8 items-center gap-2">
          <span className="text-[13px] font-semibold text-fg">{name}</span>
          {!isRen && agent && <span className="truncate text-xs text-muted">{agent.role}</span>}
          <time dateTime={message.created_at} title={formatDateTime(message.created_at)} className="text-[11px] text-subtle">
            {formatTime(message.created_at)}
          </time>
        </div>
        {toolCalls.length > 0 && <ToolCallChips calls={toolCalls} className="mt-1 mb-2" />}
        {message.content ? (
          <Markdown content={message.content} citations={citations} streaming={streaming} />
        ) : streaming ? (
          <div className="typing-dots flex h-6 items-center" aria-label="Ren is typing">
            <span />
            <span />
            <span />
          </div>
        ) : null}
        {message.status === "error" && (
          <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-danger">
            <AlertCircle className="size-3.5" /> This reply was interrupted.
          </p>
        )}
        {citations.length > 0 && !streaming && <SourcesRow citations={citations} className="mt-3" />}
        {!streaming && (message.content || message.metadata?.alignment) && (
          <div className="mt-1.5 -ml-2 flex flex-wrap items-center gap-0.5">
            <AlignmentBadge summary={message.metadata?.alignment} />
            {message.content && (
              <CopyButton
                text={message.content}
                size="xs"
                label="Copy reply"
                className="opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MessageItemImpl({ message, onRetry }: { message: Message; onRetry?: (m: Message) => void }) {
  if (message.role === "user") return <UserMessage message={message} onRetry={onRetry} />;
  if (message.role === "assistant") return <AssistantMessage message={message} />;
  if (message.role === "event") return <EventCard message={message} />;
  return (
    <div className="flex items-center justify-center gap-2 py-1 text-center text-xs text-subtle">
      <span className="max-w-xl">{message.content}</span>
    </div>
  );
}

export const MessageItem = memo(MessageItemImpl);
