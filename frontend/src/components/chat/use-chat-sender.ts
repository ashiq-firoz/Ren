"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { api, errorMessage } from "@/lib/api";
import {
  patchMessage,
  reconcileOptimistic,
  removeMessage,
  TEMP_PREFIX,
  upsertConversation,
} from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Attachment, Conversation, ConversationDetail, Message } from "@/lib/types";
import { uid } from "@/lib/utils";
import { useAuth } from "@/providers/auth";

export interface OutgoingMessage {
  content: string;
  attachments: Attachment[];
  mentionAgentId?: string | null;
  /** Ren is mid-reply: show the optimistic copy as queued. */
  busy: boolean;
}

function titleFrom(content: string) {
  const line = content.replace(/\s+/g, " ").trim();
  return line.length > 72 ? `${line.slice(0, 69).trimEnd()}…` : line || "New conversation";
}

/** Create a conversation and seed it into the cache (list + empty detail). */
export async function createConversation(
  qc: ReturnType<typeof useQueryClient>,
  title?: string,
): Promise<Conversation> {
  const conv = await api.post<Conversation>("/conversations", title ? { title } : {});
  if (!qc.getQueryData(qk.conversation(conv.id))) {
    qc.setQueryData<ConversationDetail>(qk.conversation(conv.id), { ...conv, messages: [] });
  }
  upsertConversation(qc, conv);
  return conv;
}

/**
 * Sending never blocks: insert an optimistic copy, POST (202), then reconcile by id.
 * For a new chat, the conversation is created on first send and we navigate to it.
 */
export function useChatSender(conversationId: string | null) {
  const qc = useQueryClient();
  const router = useRouter();
  const { user } = useAuth();

  return useCallback(
    async (msg: OutgoingMessage, opts: { draftConversationId?: string | null } = {}) => {
      const tempId = `${TEMP_PREFIX}${uid()}`;
      const optimistic = (convId: string): Message => ({
        id: tempId,
        conversation_id: convId,
        role: "user",
        content: msg.content,
        agent_id: null,
        task_id: null,
        status: msg.busy ? "queued" : "complete",
        metadata: {
          attachments: msg.attachments.length ? msg.attachments : undefined,
          mention_agent_id: msg.mentionAgentId ?? undefined,
        },
        created_at: new Date().toISOString(),
        user_id: user?.id ?? null,
      });

      let convId = conversationId ?? opts.draftConversationId ?? null;
      const isNew = !conversationId;
      try {
        if (!convId) {
          const conv = await createConversation(qc, titleFrom(msg.content));
          convId = conv.id;
        }
        const id = convId;
        qc.setQueryData<ConversationDetail>(qk.conversation(id), (old) =>
          old ? { ...old, messages: [...old.messages, optimistic(id)] } : old,
        );
        if (isNew) router.replace(`/chat/${id}`);
      } catch (err) {
        toast.error("Couldn't start a conversation", { description: errorMessage(err) });
        throw err;
      }

      const id = convId;
      try {
        const saved = await api.post<Message>(`/conversations/${id}/messages`, {
          content: msg.content,
          attachment_ids: msg.attachments.length ? msg.attachments.map((a) => a.id) : undefined,
          mention_agent_id: msg.mentionAgentId || undefined,
        });
        if (saved && saved.id) reconcileOptimistic(qc, id, tempId, saved);
      } catch (err) {
        patchMessage(qc, id, tempId, { status: "error" });
        toast.error("Message not sent", { description: errorMessage(err) });
      }
      return id;
    },
    [qc, router, conversationId, user?.id],
  );
}

/** Retry a failed optimistic message: drop it and send again. */
export function useRetryMessage(conversationId: string | null) {
  const qc = useQueryClient();
  const send = useChatSender(conversationId);
  return useCallback(
    (message: Message) => {
      if (!conversationId) return;
      removeMessage(qc, conversationId, message.id);
      void send({
        content: message.content,
        attachments: message.metadata.attachments ?? [],
        mentionAgentId: message.metadata.mention_agent_id ?? null,
        busy: false,
      }).catch(() => undefined);
    },
    [qc, conversationId, send],
  );
}
