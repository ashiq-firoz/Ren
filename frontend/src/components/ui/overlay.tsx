"use client";

import { X } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { Button, IconButton } from "./button";

const noopSubscribe = () => () => {};

/** True after hydration (portals need `document`). */
export function useMounted() {
  return useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
}

export function Portal({ children }: { children: ReactNode }) {
  const mounted = useMounted();
  if (!mounted) return null;
  return createPortal(children, document.body);
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

let scrollLocks = 0;

/** Esc to close, focus trap, focus restore and body scroll lock for modal surfaces. */
export function useModalBehavior(open: boolean, onClose: () => void, panelRef: RefObject<HTMLElement | null>) {
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    scrollLocks += 1;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusFirst = () => {
      const panel = panelRef.current;
      if (!panel) return;
      const auto = panel.querySelector<HTMLElement>("[data-autofocus]");
      const first = auto ?? panel.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? panel).focus({ preventScroll: true });
    };
    const raf = requestAnimationFrame(focusFirst);

    const onKeyDown = (e: KeyboardEvent) => {
      const panel = panelRef.current;
      if (!panel) return;
      // Only the top-most modal handles keys.
      const modals = document.querySelectorAll("[data-modal-panel]");
      if (modals[modals.length - 1] !== panel) return;
      if (e.key === "Escape") {
        // Let an open popover/menu inside the modal close first.
        if (document.querySelector("[data-floating-panel][data-dismissable]")) return;
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (n) => n.offsetParent !== null || n === document.activeElement,
      );
      if (nodes.length === 0) {
        e.preventDefault();
        return;
      }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKeyDown, true);
      scrollLocks -= 1;
      if (scrollLocks <= 0) {
        scrollLocks = 0;
        document.body.style.overflow = prevOverflow === "hidden" ? "" : prevOverflow;
      }
      previouslyFocused?.focus?.({ preventScroll: true });
    };
  }, [open, panelRef]);
}

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
  bodyClassName?: string;
  /** Prevent closing by clicking the backdrop (e.g. while a request is pending). */
  dismissable?: boolean;
  hideClose?: boolean;
}

const dialogSizes = {
  sm: "max-w-[420px]",
  md: "max-w-[540px]",
  lg: "max-w-[720px]",
  xl: "max-w-[920px]",
};

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
  className,
  bodyClassName,
  dismissable = true,
  hideClose,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  useModalBehavior(open, () => dismissable && onOpenChange(false), panelRef);
  if (!open) return null;
  return (
    <Portal>
      <div className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-6">
        <div
          className="absolute inset-0 animate-fade-in bg-[var(--overlay)] backdrop-blur-[1.5px]"
          onMouseDown={() => dismissable && onOpenChange(false)}
          aria-hidden
        />
        <div
          ref={panelRef}
          data-modal-panel
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          aria-describedby={description ? descId : undefined}
          tabIndex={-1}
          className={cn(
            "relative z-10 flex max-h-[92dvh] w-full animate-pop-in flex-col overflow-hidden rounded-t-2xl border border-border bg-surface shadow-popover outline-none sm:rounded-2xl",
            dialogSizes[size],
            className,
          )}
        >
          {(title || !hideClose) && (
            <div className="flex items-start justify-between gap-4 px-5 pt-5 pb-1">
              <div className="min-w-0">
                {title && (
                  <h2 id={titleId} className="font-serif-display text-[24px] leading-tight text-fg">
                    {title}
                  </h2>
                )}
                {description && (
                  <p id={descId} className="mt-1 text-[13px] text-muted">
                    {description}
                  </p>
                )}
              </div>
              {!hideClose && (
                <IconButton label="Close" icon={<X />} onClick={() => onOpenChange(false)} className="-mr-1.5 -mt-0.5" />
              )}
            </div>
          )}
          <div className={cn("min-h-0 flex-1 overflow-y-auto px-5 py-4", bodyClassName)}>{children}</div>
          {footer && (
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-surface px-5 py-3.5">
              {footer}
            </div>
          )}
        </div>
      </div>
    </Portal>
  );
}

export interface DrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  side?: "right" | "left";
  width?: number | string;
  className?: string;
  bodyClassName?: string;
  headerActions?: ReactNode;
  /** Render without padding/header chrome (caller lays out everything). */
  bare?: boolean;
  /** Accessible name when there's no `title` (e.g. bare drawers). */
  "aria-label"?: string;
}

export function Drawer({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  side = "right",
  width = 480,
  className,
  bodyClassName,
  headerActions,
  bare,
  "aria-label": ariaLabel,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalBehavior(open, () => onOpenChange(false), panelRef);
  if (!open) return null;
  return (
    <Portal>
      <div className="fixed inset-0 z-50">
        <div
          className="absolute inset-0 animate-fade-in bg-[var(--overlay)]"
          onMouseDown={() => onOpenChange(false)}
          aria-hidden
        />
        <div
          ref={panelRef}
          data-modal-panel
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          aria-label={title ? undefined : ariaLabel}
          tabIndex={-1}
          style={{ width: typeof width === "number" ? `min(${width}px, 100vw)` : width }}
          className={cn(
            "absolute inset-y-0 flex max-w-full flex-col border-border bg-surface shadow-popover outline-none",
            side === "right" ? "right-0 animate-slide-in-right border-l" : "left-0 animate-slide-in-left border-r",
            className,
          )}
        >
          {bare ? (
            children
          ) : (
            <>
              <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
                <div className="min-w-0">
                  {title && (
                    <h2 id={titleId} className="font-serif-display text-[24px] leading-tight text-fg">
                      {title}
                    </h2>
                  )}
                  {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {headerActions}
                  <IconButton label="Close" icon={<X />} onClick={() => onOpenChange(false)} />
                </div>
              </div>
              <div className={cn("min-h-0 flex-1 overflow-y-auto px-5 py-4", bodyClassName)}>{children}</div>
              {footer && (
                <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-3.5">
                  {footer}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </Portal>
  );
}

/* ------------------------------------------------------------------ */
/* Imperative confirm()                                                */
/* ------------------------------------------------------------------ */

interface ConfirmRequest {
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  resolve: (ok: boolean) => void;
}

let pendingConfirm: ConfirmRequest | null = null;
const confirmListeners = new Set<() => void>();

function emitConfirm() {
  confirmListeners.forEach((l) => l());
}

/** `await confirm({ title, description, danger: true })` → boolean. */
export function confirm(opts: Omit<ConfirmRequest, "resolve">): Promise<boolean> {
  return new Promise((resolve) => {
    pendingConfirm?.resolve(false);
    pendingConfirm = { ...opts, resolve };
    emitConfirm();
  });
}

export function ConfirmHost() {
  const req = useSyncExternalStore(
    (l) => {
      confirmListeners.add(l);
      return () => confirmListeners.delete(l);
    },
    () => pendingConfirm,
    () => null,
  );
  const [busy, setBusy] = useState(false);
  const close = (ok: boolean) => {
    if (!req) return;
    req.resolve(ok);
    pendingConfirm = null;
    setBusy(false);
    emitConfirm();
  };
  return (
    <Dialog
      open={!!req}
      onOpenChange={(o) => !o && close(false)}
      title={req?.title}
      description={req?.description}
      size="sm"
      bodyClassName="hidden"
      footer={
        <>
          <Button variant="ghost" onClick={() => close(false)}>
            {req?.cancelLabel ?? "Cancel"}
          </Button>
          <Button
            data-autofocus
            variant={req?.danger ? "danger" : "primary"}
            loading={busy}
            onClick={() => {
              setBusy(true);
              close(true);
            }}
          >
            {req?.confirmLabel ?? "Confirm"}
          </Button>
        </>
      }
    />
  );
}
