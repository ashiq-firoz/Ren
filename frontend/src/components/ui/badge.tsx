import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";

export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info" | "accent" | "outline" | "solid";

const tones: Record<BadgeTone, string> = {
  neutral: "tint-neutral",
  success: "tint-success",
  warning: "tint-warning",
  danger: "tint-danger",
  info: "tint-info",
  accent: "tint-accent",
  outline: "border border-border-strong text-muted bg-transparent",
  solid: "bg-primary text-primary-fg",
};

export interface BadgeProps extends ComponentProps<"span"> {
  tone?: BadgeTone;
  size?: "xs" | "sm" | "md";
  dot?: boolean;
  pulse?: boolean;
  icon?: ReactNode;
}

export function Badge({ tone = "neutral", size = "sm", dot, pulse, icon, className, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full shrink-0 items-center gap-1 rounded-full font-medium whitespace-nowrap [&_svg]:shrink-0",
        size === "xs" && "h-[18px] px-1.5 text-[10.5px] [&_svg]:size-3",
        size === "sm" && "h-5 px-2 text-[11.5px] [&_svg]:size-3",
        size === "md" && "h-6 px-2.5 text-xs [&_svg]:size-3.5",
        tones[tone],
        className,
      )}
      {...props}
    >
      {dot && (
        <span
          aria-hidden
          className={cn("inline-block size-1.5 rounded-full bg-current", pulse && "animate-pulse-dot")}
        />
      )}
      {icon}
      <span className="truncate">{children}</span>
    </span>
  );
}

export type DotTone = "running" | "success" | "warning" | "danger" | "info" | "idle" | "accent";

const dotColors: Record<DotTone, string> = {
  running: "text-accent bg-accent",
  success: "text-success bg-success",
  warning: "text-warning bg-warning",
  danger: "text-danger bg-danger",
  info: "text-info bg-info",
  idle: "text-subtle bg-subtle",
  accent: "text-accent bg-accent",
};

export function StatusDot({
  tone = "idle",
  pulse,
  size = 8,
  className,
  label,
}: {
  tone?: DotTone;
  pulse?: boolean;
  size?: number;
  className?: string;
  label?: string;
}) {
  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("inline-block shrink-0 rounded-full", dotColors[tone], pulse && "animate-pulse-dot", className)}
      style={{ width: size, height: size }}
    />
  );
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded border border-border bg-surface-2 px-1 font-mono text-[10.5px] text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
