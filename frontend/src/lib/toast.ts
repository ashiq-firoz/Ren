import { useSyncExternalStore } from "react";
import { uid } from "./utils";

export type ToastVariant = "success" | "error" | "info" | "warning";

export interface ToastAction {
  label: string;
  href?: string;
  onClick?: () => void;
}

export interface ToastItem {
  id: string;
  variant: ToastVariant;
  title: string;
  description?: string;
  action?: ToastAction;
  duration: number;
}

type ToastOptions = Partial<Omit<ToastItem, "id" | "variant" | "title">>;

let items: ToastItem[] = [];
const listeners = new Set<() => void>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const EMPTY: ToastItem[] = [];

function emit() {
  listeners.forEach((l) => l());
}

function dismiss(id: string) {
  const t = timers.get(id);
  if (t) clearTimeout(t);
  timers.delete(id);
  items = items.filter((x) => x.id !== id);
  emit();
}

function show(variant: ToastVariant, title: string, opts: ToastOptions = {}): string {
  const id = uid("t-");
  const duration = opts.duration ?? (variant === "error" ? 7000 : 4500);
  // Collapse identical consecutive errors (e.g. many requests failing at once).
  const dup = items.find((x) => x.title === title && x.variant === variant && x.description === opts.description);
  if (dup) return dup.id;
  items = [...items, { id, variant, title, duration, ...opts }].slice(-5);
  emit();
  if (duration > 0) timers.set(id, setTimeout(() => dismiss(id), duration));
  return id;
}

export const toast = {
  show,
  success: (title: string, opts?: ToastOptions) => show("success", title, opts),
  error: (title: string, opts?: ToastOptions) => show("error", title, opts),
  info: (title: string, opts?: ToastOptions) => show("info", title, opts),
  warning: (title: string, opts?: ToastOptions) => show("warning", title, opts),
  dismiss,
  /** Pause auto-dismiss while hovered. */
  hold(id: string) {
    const t = timers.get(id);
    if (t) clearTimeout(t);
    timers.delete(id);
  },
  release(id: string) {
    const item = items.find((x) => x.id === id);
    if (!item || item.duration <= 0 || timers.has(id)) return;
    timers.set(id, setTimeout(() => dismiss(id), 2500));
  },
};

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(
    subscribe,
    () => items,
    () => EMPTY,
  );
}
