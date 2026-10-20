"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { confirm } from "@/components/ui/overlay";
import { api } from "@/lib/api";
import { patchConversation, removeConversation, upsertConversation } from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Conversation } from "@/lib/types";

type Patch = Partial<Pick<Conversation, "title" | "pinned" | "archived">>;

export function useConversationActions() {
  const qc = useQueryClient();
  const router = useRouter();
  const params = useParams<{ id?: string }>();

  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Patch }) => api.patch<Conversation>(`/conversations/${id}`, patch),
    onMutate: ({ id, patch }) => patchConversation(qc, id, patch),
    onSuccess: (conv) => upsertConversation(qc, conv),
    onError: () => void qc.invalidateQueries({ queryKey: qk.conversationLists }),
    meta: { errorTitle: "Couldn't update the conversation" },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/conversations/${id}`),
    onSuccess: (_d, id) => {
      removeConversation(qc, id);
      if (params.id === id) router.push("/chat");
      toast.success("Conversation deleted");
    },
    meta: { errorTitle: "Couldn't delete the conversation" },
  });

  return {
    rename: (id: string, title: string) => update.mutate({ id, patch: { title } }),
    togglePin: (c: Conversation) => update.mutate({ id: c.id, patch: { pinned: !c.pinned } }),
    toggleArchive: (c: Conversation) => {
      update.mutate({ id: c.id, patch: { archived: !c.archived } });
      if (!c.archived) toast.info("Conversation archived", { description: c.title });
      if (!c.archived && params.id === c.id) router.push("/chat");
    },
    remove: async (c: Conversation) => {
      const ok = await confirm({
        title: "Delete conversation?",
        description: `“${c.title || "Untitled"}” and its messages will be permanently deleted. Tasks it started keep running.`,
        confirmLabel: "Delete",
        danger: true,
      });
      if (ok) remove.mutate(c.id);
    },
    updating: update.isPending,
  };
}
