"use client";

import { AlertCircle } from "lucide-react";
import type { ReactNode } from "react";

export function AuthHeading({ title, subtitle }: { title: ReactNode; subtitle?: ReactNode }) {
  return (
    <div className="mb-7">
      <h1 className="font-serif-display text-[38px] leading-[1.05] text-fg">{title}</h1>
      {subtitle && <p className="mt-2 text-[13.5px] text-muted">{subtitle}</p>}
    </div>
  );
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-control border border-[color-mix(in_srgb,var(--danger)_30%,transparent)] bg-[color-mix(in_srgb,var(--danger)_7%,transparent)] px-3 py-2.5 text-[13px] text-danger"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

export { PasswordInput } from "@/components/ui/password-input";

/** Only allow same-origin relative redirects. */
export function safeNext(next: string | null | undefined, fallback = "/chat") {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login")) return fallback;
  return next;
}
