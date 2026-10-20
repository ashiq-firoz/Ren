"use client";

import { LogoMark } from "@/components/brand/logo";
import type { Agent } from "@/lib/types";
import { cn, hashColor, initials, tint } from "@/lib/utils";
import { AppIcon } from "./icon";

type Size = "xs" | "sm" | "md" | "lg" | "xl";

const box: Record<Size, string> = {
  xs: "size-5 rounded-[5px]",
  sm: "size-6 rounded-md",
  md: "size-8 rounded-lg",
  lg: "size-10 rounded-[10px]",
  xl: "size-14 rounded-xl",
};

const glyph: Record<Size, string> = {
  xs: "size-3",
  sm: "size-3.5",
  md: "size-4",
  lg: "size-5",
  xl: "size-7",
};

/** Width of the F69 mark inside Ren's tile, per avatar size. */
const markWidth: Record<Size, string> = {
  xs: "w-[14px]",
  sm: "w-[17px]",
  md: "w-[23px]",
  lg: "w-[29px]",
  xl: "w-[40px]",
};

const text: Record<Size, string> = {
  xs: "text-[9px]",
  sm: "text-[10px]",
  md: "text-[11.5px]",
  lg: "text-[13px]",
  xl: "text-lg",
};

/** User avatar with initials. */
export function Avatar({ name, size = "md", className }: { name?: string | null; size?: Size; className?: string }) {
  const color = hashColor(name ?? "?");
  return (
    <span
      aria-hidden
      className={cn("inline-flex shrink-0 items-center justify-center rounded-full font-semibold", box[size], "rounded-full", text[size], className)}
      style={{ background: tint(color, 18), color }}
    >
      {initials(name)}
    </span>
  );
}

export type AgentLike = Partial<Pick<Agent, "name" | "color" | "icon" | "is_orchestrator">> | null | undefined;

/**
 * Rounded-square agent avatar: tint of the agent color with its lucide icon.
 * The orchestrator (Ren) uses the f69 Labs mark on a brand tile.
 */
export function AgentAvatar({
  agent,
  name,
  color,
  icon,
  orchestrator,
  size = "md",
  className,
  title,
}: {
  agent?: AgentLike;
  name?: string | null;
  color?: string | null;
  icon?: string | null;
  orchestrator?: boolean;
  size?: Size;
  className?: string;
  title?: string;
}) {
  const isOrch = orchestrator ?? agent?.is_orchestrator ?? false;
  const c = color ?? agent?.color ?? null;
  const ic = icon ?? agent?.icon ?? null;
  const label = title ?? name ?? agent?.name ?? undefined;
  if (isOrch) {
    return (
      <span
        title={label}
        className={cn(
          "inline-flex shrink-0 items-center justify-center bg-brand text-brand-fg ring-1 ring-fg/5 dark:ring-fg/10",
          box[size],
          className,
        )}
      >
        <LogoMark className={markWidth[size]} />
      </span>
    );
  }
  const resolved = c || hashColor(label ?? "agent");
  return (
    <span
      title={label}
      className={cn("inline-flex shrink-0 items-center justify-center", box[size], className)}
      style={{
        background: tint(resolved, 15),
        color: resolved,
        boxShadow: `inset 0 0 0 1px ${tint(resolved, 22)}`,
      }}
    >
      {ic ? (
        <AppIcon name={ic} className={glyph[size]} />
      ) : (
        <span className={cn("font-semibold", text[size])}>{initials(label)}</span>
      )}
    </span>
  );
}
