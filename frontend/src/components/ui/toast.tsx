"use client";

import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import Link from "next/link";
import { toast, useToasts, type ToastItem } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { ConfirmHost, Portal } from "./overlay";

const icons = {
  success: <CheckCircle2 className="size-4 text-success" />,
  error: <XCircle className="size-4 text-danger" />,
  warning: <AlertTriangle className="size-4 text-warning" />,
  info: <Info className="size-4 text-info" />,
};

function ToastCard({ item }: { item: ToastItem }) {
  return (
    <div
      role={item.variant === "error" ? "alert" : "status"}
      onMouseEnter={() => toast.hold(item.id)}
      onMouseLeave={() => toast.release(item.id)}
      className={cn(
        "pointer-events-auto flex w-full animate-slide-up items-start gap-3 rounded-card border border-border bg-surface px-3.5 py-3 shadow-popover",
      )}
    >
      <span className="mt-0.5 shrink-0">{icons[item.variant]}</span>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-medium text-fg">{item.title}</p>
        {item.description && <p className="mt-0.5 line-clamp-3 text-xs break-words text-muted">{item.description}</p>}
        {item.action &&
          (item.action.href ? (
            <Link
              href={item.action.href}
              onClick={() => toast.dismiss(item.id)}
              className="mt-1.5 inline-block text-xs font-medium text-fg underline underline-offset-2"
            >
              {item.action.label}
            </Link>
          ) : (
            <button
              type="button"
              onClick={() => {
                item.action?.onClick?.();
                toast.dismiss(item.id);
              }}
              className="mt-1.5 text-xs font-medium text-fg underline underline-offset-2"
            >
              {item.action.label}
            </button>
          ))}
      </div>
      <button
        type="button"
        aria-label="Dismiss notification"
        onClick={() => toast.dismiss(item.id)}
        className="-mr-1 -mt-0.5 rounded p-0.5 text-subtle hover:text-fg"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}

/** Bottom-right toast stack + the global confirm dialog host. */
export function Toaster() {
  const items = useToasts();
  return (
    <Portal>
      <div
        aria-live="polite"
        className="pointer-events-none fixed right-4 bottom-4 z-[100] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2"
      >
        {items.map((t) => (
          <ToastCard key={t.id} item={t} />
        ))}
      </div>
      <ConfirmHost />
    </Portal>
  );
}
