"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  MessageSquareDashed,
  MoreHorizontal,
  PanelLeft,
  PanelRight,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { MobileNavButton } from "@/components/shell/page-header";
import { NotificationBell } from "@/components/shell/notification-bell";
import { ButtonLink, IconButton } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { DropdownMenu } from "@/components/ui/floating";
import { Drawer } from "@/components/ui/overlay";
import { isTaskActive } from "@/components/domain/status";
import { getList, useApprovals, useConversation, useConversationLiveStatus, useConversationTasks } from "@/hooks/queries";
import { useLocalPreference, useMediaQuery } from "@/hooks/use-browser";
import { api, ApiError } from "@/lib/api";
import { isTempId, upsertConversation } from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import type { Conversation, ConversationDetail, Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth";
import { useChatLayout } from "./chat-layout-context";
import { isBusy } from "./chat-utils";
import { Composer, type ComposerHandle, type ComposerSendPayload } from "./composer";
import { useConversationActions } from "./conversation-actions";
import { EmptyChat } from "./empty-chat";
import { MessageList } from "./message-list";
import { SwarmPanel } from "./swarm-panel";
import { createConversation, useChatSender, useRetryMessage } from "./use-chat-sender";

const PAGE = 50;

function TitleEditor({ conv }: { conv: Conversation }) {
  const actions = useConversationActions();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conv.title);
  if (editing) {
    return (
      <form
        className="min-w-0 flex-1"
        onSubmit={(e) => {
          e.preventDefault();
          const t = draft.trim();
          if (t && t !== conv.title) actions.rename(conv.id, t);
          setEditing(false);
        }}
      >
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
          aria-label="Conversation title"
          className="font-serif-display w-full max-w-xl rounded-md border border-border-strong bg-surface px-2 py-0.5 text-[21px] text-fg outline-none focus:border-focus"
        />
      </form>
    );
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(conv.title);
        setEditing(true);
      }}
      title="Rename conversation"
      className="group flex min-w-0 items-center gap-2 rounded-md px-1 text-left"
    >
      <h1 className="font-serif-display truncate text-[21px] leading-tight text-fg">{conv.title || "Untitled conversation"}</h1>
      <Pencil className="size-3.5 shrink-0 text-subtle opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

function MessagesSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[800px] flex-1 space-y-8 px-4 pt-8 sm:px-6" aria-hidden>
      <div className="flex justify-end">
        <Skeleton className="h-10 w-2/5 rounded-2xl" />
      </div>
      <div className="flex gap-3">
        <Skeleton className="size-8 rounded-lg" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-3.5 w-24" />
          <Skeleton className="h-3.5 w-11/12" />
          <Skeleton className="h-3.5 w-4/5" />
          <Skeleton className="h-3.5 w-3/5" />
        </div>
      </div>
      <div className="flex justify-end">
        <Skeleton className="h-10 w-1/3 rounded-2xl" />
      </div>
      <div className="flex gap-3">
        <Skeleton className="size-8 rounded-lg" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-3.5 w-20" />
          <Skeleton className="h-16 w-full rounded-card" />
        </div>
      </div>
    </div>
  );
}

export function ChatView({ conversationId }: { conversationId: string | null }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const { listVisible, toggleList } = useChatLayout();
  const composerRef = useRef<ComposerHandle>(null);
  const conv = useConversation(conversationId);
  const live = useConversationLiveStatus(conversationId);
  const tasks = useConversationTasks(conversationId);
  const approvals = useApprovals("pending");
  const send = useChatSender(conversationId);
  const retry = useRetryMessage(conversationId);
  const actions = useConversationActions();

  const [draftId, setDraftId] = useState<string | null>(null);
  const [pendingFirst, setPendingFirst] = useState<Message | null>(null);
  const [pinSignal, setPinSignal] = useState(0);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [exhausted, setExhausted] = useState(false);

  const wide = useMediaQuery("(min-width: 1280px)", true);
  const [panelPref, setPanelPref] = useLocalPreference<"open" | "closed">("ren_swarm_panel", "open");
  const [sheetOpen, setSheetOpen] = useState(false);
  const panelInline = wide && panelPref === "open";

  const detail = conv.data;
  const status = live?.status ?? detail?.status ?? "idle";
  const statusDetail = live?.detail ?? null;
  const queued = live?.queued ?? 0;
  const busy = isBusy(status);

  const messages = useMemo<Message[]>(
    () => detail?.messages ?? (pendingFirst ? [pendingFirst] : []),
    [detail?.messages, pendingFirst],
  );

  const taskItems = tasks.data?.items ?? [];
  const activeTasks = taskItems.filter((t) => isTaskActive(t.status)).length;
  const taskIds = new Set(taskItems.map((t) => t.id));
  const pendingApprovals = (approvals.data?.items ?? []).filter(
    (a) => a.status === "pending" && (a.conversation_id === conversationId || (a.task_id && taskIds.has(a.task_id))),
  ).length;

  const hasMore = !!conversationId && !exhausted && (detail?.messages.filter((m) => !isTempId(m.id)).length ?? 0) >= 200;

  const stop = useMutation({
    mutationFn: () => api.post(`/conversations/${conversationId}/stop`),
    meta: { errorTitle: "Couldn't stop Ren" },
  });

  const openSwarm = useCallback(() => {
    if (wide) setPanelPref("open");
    else setSheetOpen(true);
  }, [wide, setPanelPref]);

  const toggleSwarm = () => {
    if (wide) setPanelPref(panelPref === "open" ? "closed" : "open");
    else setSheetOpen((o) => !o);
  };

  const ensureConversation = useCallback(async () => {
    if (conversationId) return conversationId;
    if (draftId) return draftId;
    const created = await createConversation(qc);
    setDraftId(created.id);
    return created.id;
  }, [conversationId, draftId, qc]);

  const onSend = useCallback(
    async (payload: ComposerSendPayload) => {
      if (!conversationId) {
        // Show the message instantly while the conversation is created.
        setPendingFirst({
          id: "tmp-pending-first",
          conversation_id: draftId ?? "",
          role: "user",
          content: payload.content,
          agent_id: null,
          task_id: null,
          status: "complete",
          metadata: { attachments: payload.attachments.length ? payload.attachments : undefined },
          created_at: new Date().toISOString(),
          user_id: user?.id ?? null,
        });
      }
      setPinSignal((n) => n + 1);
      try {
        const id = await send({ ...payload, busy }, { draftConversationId: draftId });
        if (!conversationId && draftId && id === draftId) {
          // Draft conversations were created untitled (for attachments) — title them now.
          const title = payload.content.replace(/\s+/g, " ").trim().slice(0, 72);
          if (title) {
            void api
              .patch<Conversation>(`/conversations/${id}`, { title })
              .then((c) => upsertConversation(qc, c))
              .catch(() => undefined);
          }
        }
      } catch (err) {
        setPendingFirst(null);
        throw err;
      }
    },
    [conversationId, draftId, busy, send, qc, user?.id],
  );

  const loadEarlier = useCallback(async () => {
    if (!conversationId || loadingEarlier) return;
    const current = qc.getQueryData<ConversationDetail>(qk.conversation(conversationId));
    const first = current?.messages.find((m) => !isTempId(m.id));
    if (!first) return;
    setLoadingEarlier(true);
    try {
      const page = await getList<Message>(`/conversations/${conversationId}/messages`, { before: first.id, limit: PAGE });
      qc.setQueryData<ConversationDetail>(qk.conversation(conversationId), (old) => {
        if (!old) return old;
        const known = new Set(old.messages.map((m) => m.id));
        return { ...old, messages: [...page.items.filter((m) => !known.has(m.id)), ...old.messages] };
      });
      if (page.items.length < PAGE) setExhausted(true);
    } catch {
      setExhausted(true);
    } finally {
      setLoadingEarlier(false);
    }
  }, [conversationId, loadingEarlier, qc]);

  const notFound = conv.error instanceof ApiError && (conv.error.status === 404 || conv.error.status === 403);
  const showEmpty = !conversationId && messages.length === 0;

  let body;
  if (conversationId && conv.isLoading) body = <MessagesSkeleton />;
  else if (conversationId && notFound)
    body = (
      <div className="flex flex-1 items-center justify-center">
        <EmptyState
          icon={<MessageSquareDashed />}
          title="Conversation not found"
          description="It may have been deleted, or it belongs to another workspace."
          action={
            <ButtonLink href="/chat" variant="primary">
              Start a new chat
            </ButtonLink>
          }
        />
      </div>
    );
  else if (conversationId && conv.error && !detail)
    body = (
      <div className="flex flex-1 items-center justify-center">
        <ErrorState error={conv.error} onRetry={() => conv.refetch()} />
      </div>
    );
  else if (showEmpty) body = <EmptyChat onPick={(p) => composerRef.current?.setText(p)} />;
  else
    body = (
      <MessageList
        conversationKey={conversationId ?? "new"}
        messages={messages}
        hasMore={hasMore}
        loadingEarlier={loadingEarlier}
        onLoadEarlier={loadEarlier}
        onRetry={retry}
        pinSignal={pinSignal}
      />
    );

  return (
    <div className="flex h-full min-h-0 flex-1">
      <section aria-label="Chat" className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-1.5 border-b border-border px-2 sm:px-3">
          <MobileNavButton />
          <IconButton
            label={listVisible ? "Hide conversations" : "Show conversations"}
            icon={<PanelLeft />}
            size="md"
            onClick={toggleList}
          />
          <div className="flex min-w-0 flex-1 items-center gap-2">
            {detail ? (
              <TitleEditor key={detail.id} conv={detail} />
            ) : (
              <h1 className="font-serif-display truncate px-1 text-[21px] leading-tight text-fg">
                {conversationId ? (conv.isLoading ? "" : "Conversation") : "New chat"}
              </h1>
            )}
            {busy && (
              <span className="hidden shrink-0 items-center gap-1.5 rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-muted sm:inline-flex">
                <span className="size-1.5 animate-pulse-dot rounded-full bg-accent text-accent" />
                {status === "thinking" ? "Thinking" : status === "responding" ? "Responding" : "Using tools"}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={toggleSwarm}
            aria-label="Toggle swarm activity"
            aria-pressed={wide ? panelPref === "open" : sheetOpen}
            className={cn(
              "relative inline-flex h-8 items-center gap-1.5 rounded-control px-2 text-[12.5px] font-medium transition-colors",
              (wide ? panelPref === "open" : sheetOpen) ? "bg-surface-2 text-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
            )}
          >
            <PanelRight className="size-4" />
            <span className="hidden md:inline">Swarm</span>
            {(activeTasks > 0 || pendingApprovals > 0) && (
              <span
                className={cn(
                  "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 text-[10.5px] font-semibold",
                  pendingApprovals > 0 ? "bg-warning text-primary-fg" : "bg-accent text-accent-fg",
                )}
              >
                {activeTasks + pendingApprovals}
              </span>
            )}
          </button>
          {detail && (
            <DropdownMenu
              aria-label="Conversation actions"
              items={[
                {
                  label: detail.pinned ? "Unpin" : "Pin",
                  icon: detail.pinned ? <PinOff /> : <Pin />,
                  onSelect: () => actions.togglePin(detail),
                },
                {
                  label: detail.archived ? "Unarchive" : "Archive",
                  icon: detail.archived ? <ArchiveRestore /> : <Archive />,
                  onSelect: () => actions.toggleArchive(detail),
                },
                { type: "separator" },
                { label: "Delete", icon: <Trash2 />, danger: true, onSelect: () => void actions.remove(detail) },
              ]}
              trigger={
                <button
                  type="button"
                  aria-label="Conversation actions"
                  className="inline-flex size-8 items-center justify-center rounded-control text-muted hover:bg-surface-2 hover:text-fg"
                >
                  <MoreHorizontal className="size-4" />
                </button>
              }
            />
          )}
          <NotificationBell />
        </header>

        {body}

        <Composer
          ref={composerRef}
          status={status}
          statusDetail={statusDetail}
          queued={queued}
          activeTasks={activeTasks}
          onSend={onSend}
          onStop={conversationId ? () => stop.mutate() : undefined}
          stopping={stop.isPending}
          ensureConversation={ensureConversation}
          onOpenSwarm={openSwarm}
        />
      </section>

      {panelInline && (
        <aside aria-label="Swarm activity" className="w-[340px] shrink-0 border-l border-border bg-bg">
          <SwarmPanel conversationId={conversationId} onClose={() => setPanelPref("closed")} />
        </aside>
      )}
      {!wide && (
        <Drawer open={sheetOpen} onOpenChange={setSheetOpen} width={380} bare className="bg-bg">
          <SwarmPanel conversationId={conversationId} onClose={() => setSheetOpen(false)} />
        </Drawer>
      )}
    </div>
  );
}
