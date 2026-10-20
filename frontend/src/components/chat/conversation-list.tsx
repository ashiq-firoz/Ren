"use client";

import { Archive, ArchiveRestore, MoreHorizontal, Pencil, Pin, PinOff, Plus, Search, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ButtonLink } from "@/components/ui/button";
import { ErrorState, Skeleton } from "@/components/ui/feedback";
import { DropdownMenu } from "@/components/ui/floating";
import { Input } from "@/components/ui/input";
import { useConversations } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { timeAgo } from "@/lib/format";
import type { Conversation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { isBusy } from "./chat-utils";
import { useConversationActions } from "./conversation-actions";

function bucket(c: Conversation): string {
  const d = new Date(c.updated_at);
  const now = new Date();
  const days = (now.getTime() - d.getTime()) / 86_400_000;
  if (d.toDateString() === now.toDateString()) return "Today";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

function ConversationRow({
  conv,
  active,
  onNavigate,
}: {
  conv: Conversation;
  active: boolean;
  onNavigate?: () => void;
}) {
  const actions = useConversationActions();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conv.title);
  const working = isBusy(conv.status);

  if (editing) {
    return (
      <form
        className="px-1 py-1"
        onSubmit={(e) => {
          e.preventDefault();
          const t = draft.trim();
          if (t && t !== conv.title) actions.rename(conv.id, t);
          setEditing(false);
        }}
      >
        <Input
          inputSize="sm"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
          aria-label="Conversation title"
        />
      </form>
    );
  }

  return (
    <div
      className={cn(
        "group relative flex items-center rounded-control transition-colors",
        active ? "bg-surface-2" : "hover:bg-surface-2/60",
      )}
    >
      <Link
        href={`/chat/${conv.id}`}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        className="flex min-w-0 flex-1 flex-col gap-0.5 px-2.5 py-2"
      >
        <span className="flex items-center gap-1.5">
          {conv.pinned && <Pin className="size-3 shrink-0 text-subtle" />}
          <span className={cn("truncate text-[13px]", active ? "font-medium text-fg" : "text-fg")}>
            {conv.title || "Untitled conversation"}
          </span>
        </span>
        <span className="flex items-center gap-1.5 text-[11.5px] text-subtle">
          {working ? (
            <span className="inline-flex items-center gap-1 text-muted">
              <span className="typing-dots inline-flex scale-75">
                <span />
                <span />
                <span />
              </span>
              Ren is replying
            </span>
          ) : (
            <span className="truncate">{conv.last_message_preview || "No messages yet"}</span>
          )}
        </span>
      </Link>
      <div className="flex shrink-0 items-center gap-1 pr-1.5">
        {conv.active_tasks > 0 && (
          <span
            title={`${conv.active_tasks} active ${conv.active_tasks === 1 ? "task" : "tasks"}`}
            className="size-1.5 animate-pulse-dot rounded-full bg-accent text-accent group-hover:hidden"
          />
        )}
        <span className="text-[10.5px] text-subtle group-hover:hidden">{timeAgo(conv.updated_at, { short: true })}</span>
        <DropdownMenu
          aria-label="Conversation actions"
          placement="bottom-end"
          items={[
            {
              label: conv.pinned ? "Unpin" : "Pin",
              icon: conv.pinned ? <PinOff /> : <Pin />,
              onSelect: () => actions.togglePin(conv),
            },
            {
              label: "Rename",
              icon: <Pencil />,
              onSelect: () => {
                setDraft(conv.title);
                setEditing(true);
              },
            },
            {
              label: conv.archived ? "Unarchive" : "Archive",
              icon: conv.archived ? <ArchiveRestore /> : <Archive />,
              onSelect: () => actions.toggleArchive(conv),
            },
            { type: "separator" },
            { label: "Delete", icon: <Trash2 />, danger: true, onSelect: () => void actions.remove(conv) },
          ]}
          trigger={
            <button
              type="button"
              aria-label={`Actions for ${conv.title || "conversation"}`}
              className="hidden size-6 items-center justify-center rounded-md text-muted group-focus-within:flex group-hover:flex hover:bg-surface-3 hover:text-fg aria-expanded:flex"
            >
              <MoreHorizontal className="size-4" />
            </button>
          }
        />
      </div>
    </div>
  );
}

export function ConversationList({ activeId, onNavigate }: { activeId?: string; onNavigate?: () => void }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 250);
  const [archived, setArchived] = useState(false);
  const { data, isLoading, error, refetch, isFetching } = useConversations({ q, archived });
  const items = data?.items ?? [];
  const pinned = items.filter((c) => c.pinned);
  const rest = items.filter((c) => !c.pinned);
  const groups: { label: string; items: Conversation[] }[] = [];
  for (const c of rest) {
    const label = bucket(c);
    const g = groups.find((x) => x.label === label);
    if (g) g.items.push(c);
    else groups.push({ label, items: [c] });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 space-y-2 p-3">
        <ButtonLink href="/chat" variant="primary" className="w-full" icon={<Plus className="size-4" />} onClick={onNavigate}>
          New chat
        </ButtonLink>
        <Input
          inputSize="sm"
          leftIcon={<Search />}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search conversations"
          aria-label="Search conversations"
          rightSlot={
            query ? (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => setQuery("")}
                className="flex size-6 items-center justify-center rounded text-subtle hover:text-fg"
              >
                <X className="size-3.5" />
              </button>
            ) : undefined
          }
        />
      </div>
      <nav aria-label="Conversations" className={cn("min-h-0 flex-1 overflow-y-auto px-2 pb-3", isFetching && !isLoading && "opacity-90")}>
        {isLoading ? (
          <div className="space-y-3 px-2 pt-2">
            {Array.from({ length: 7 }).map((_, i) => (
              <div key={i} className="space-y-1.5">
                <Skeleton className="h-3.5" style={{ width: `${60 + ((i * 13) % 30)}%` }} />
                <Skeleton className="h-3 w-4/5" />
              </div>
            ))}
          </div>
        ) : error ? (
          <ErrorState compact error={error} onRetry={() => refetch()} />
        ) : items.length === 0 ? (
          <div className="px-4 py-10 text-center">
            <p className="font-serif-display text-lg text-fg">{q ? "No matches" : archived ? "Nothing archived" : "No conversations yet"}</p>
            <p className="mt-1 text-xs text-muted">
              {q ? "Try a different search." : archived ? "Archived conversations show up here." : "Start a new chat to put the swarm to work."}
            </p>
          </div>
        ) : (
          <>
            {pinned.length > 0 && (
              <div className="mb-2">
                <p className="px-2.5 pt-2 pb-1 text-[10.5px] font-medium tracking-[0.06em] text-subtle uppercase">Pinned</p>
                {pinned.map((c) => (
                  <ConversationRow key={c.id} conv={c} active={c.id === activeId} onNavigate={onNavigate} />
                ))}
              </div>
            )}
            {groups.map((g) => (
              <div key={g.label} className="mb-2">
                <p className="px-2.5 pt-2 pb-1 text-[10.5px] font-medium tracking-[0.06em] text-subtle uppercase">{g.label}</p>
                {g.items.map((c) => (
                  <ConversationRow key={c.id} conv={c} active={c.id === activeId} onNavigate={onNavigate} />
                ))}
              </div>
            ))}
          </>
        )}
      </nav>
      <div className="shrink-0 border-t border-border px-3 py-2">
        <button
          type="button"
          onClick={() => setArchived((a) => !a)}
          className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-fg"
        >
          {archived ? <ArchiveRestore className="size-3.5" /> : <Archive className="size-3.5" />}
          {archived ? "Back to conversations" : "Archived"}
        </button>
      </div>
    </div>
  );
}
