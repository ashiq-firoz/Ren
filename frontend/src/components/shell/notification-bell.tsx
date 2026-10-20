"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, CheckCheck, CheckCircle2, Info, ShieldAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ErrorState, Skeleton } from "@/components/ui/feedback";
import { Popover } from "@/components/ui/floating";
import { useNotifications } from "@/hooks/queries";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import type { ListResponse, Notification } from "@/lib/types";
import { cn } from "@/lib/utils";

function kindIcon(kind: string) {
  if (/fail|error|block/i.test(kind)) return <AlertTriangle className="size-3.5 text-danger" />;
  if (/approval/i.test(kind)) return <ShieldAlert className="size-3.5 text-warning" />;
  if (/complete|success|done/i.test(kind)) return <CheckCircle2 className="size-3.5 text-success" />;
  return <Info className="size-3.5 text-info" />;
}

export function NotificationBell() {
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const { data, isLoading, error, refetch } = useNotifications();
  const items = data?.items ?? [];
  const unread = items.filter((n) => !n.read).length;

  const setRead = (ids: Set<string> | "all") =>
    qc.setQueryData<ListResponse<Notification>>(qk.notifications, (old) =>
      old ? { ...old, items: old.items.map((n) => (ids === "all" || ids.has(n.id) ? { ...n, read: true } : n)) } : old,
    );

  const markAll = useMutation({
    mutationFn: () => api.post("/notifications/read-all"),
    onMutate: () => setRead("all"),
    onError: () => void qc.invalidateQueries({ queryKey: qk.notifications }),
    meta: { errorTitle: "Couldn't mark notifications as read" },
  });

  const markOne = useMutation({
    mutationFn: (id: string) => api.post(`/notifications/${id}/read`),
    onMutate: (id) => setRead(new Set([id])),
    meta: { silent: true },
  });

  const openItem = (n: Notification) => {
    if (!n.read) markOne.mutate(n.id);
    if (n.link) {
      setOpen(false);
      if (/^https?:\/\//.test(n.link)) window.open(n.link, "_blank", "noopener");
      else router.push(n.link);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      placement="bottom-end"
      aria-label="Notifications"
      className="w-[min(380px,calc(100vw-1.5rem))] overflow-hidden"
      trigger={
        <button
          type="button"
          aria-label={unread ? `Notifications (${unread} unread)` : "Notifications"}
          title="Notifications"
          className="relative inline-flex size-8 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-surface-2 hover:text-fg aria-expanded:bg-surface-2 aria-expanded:text-fg"
        >
          <Bell className="size-4" strokeWidth={1.75} />
          {unread > 0 && (
            <span className="pointer-events-none absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[9.5px] font-bold text-primary-fg ring-2 ring-bg">
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </button>
      }
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <p className="text-[13px] font-semibold text-fg">Notifications</p>
        <button
          type="button"
          disabled={unread === 0 || markAll.isPending}
          onClick={() => markAll.mutate()}
          className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg disabled:opacity-40"
        >
          <CheckCheck className="size-3.5" /> Mark all read
        </button>
      </div>
      <div className="max-h-[420px] overflow-y-auto">
        {isLoading ? (
          <div className="space-y-3 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="space-y-1.5">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-full" />
              </div>
            ))}
          </div>
        ) : error ? (
          <ErrorState compact error={error} onRetry={() => refetch()} />
        ) : items.length === 0 ? (
          <div className="px-6 py-10 text-center">
            <p className="font-serif-display text-xl text-fg">All quiet</p>
            <p className="mt-1 text-xs text-muted">Task results, approvals and alerts show up here.</p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {items.slice(0, 50).map((n) => (
              <li key={n.id}>
                <button
                  type="button"
                  onClick={() => openItem(n)}
                  className={cn(
                    "flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2",
                    !n.read && "bg-[color-mix(in_srgb,var(--accent)_6%,transparent)]",
                  )}
                >
                  <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md bg-surface-2">
                    {kindIcon(n.kind)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-start justify-between gap-2">
                      <span className={cn("text-[13px] text-fg", !n.read && "font-semibold")}>{n.title}</span>
                      <span className="shrink-0 text-[11px] text-subtle">{timeAgo(n.created_at, { short: true })}</span>
                    </span>
                    {n.body && <span className="mt-0.5 line-clamp-2 block text-xs text-muted">{n.body}</span>}
                  </span>
                  {!n.read && <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" aria-label="Unread" />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Popover>
  );
}
