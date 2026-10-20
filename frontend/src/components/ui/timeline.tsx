import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type TimelineTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

const toneClass: Record<TimelineTone, string> = {
  neutral: "bg-surface-2 text-muted border-border",
  accent: "tint-accent border-[color-mix(in_srgb,var(--accent)_35%,transparent)]",
  success: "tint-success border-[color-mix(in_srgb,var(--success)_30%,transparent)]",
  warning: "tint-warning border-[color-mix(in_srgb,var(--warning)_30%,transparent)]",
  danger: "tint-danger border-[color-mix(in_srgb,var(--danger)_30%,transparent)]",
  info: "tint-info border-[color-mix(in_srgb,var(--info)_30%,transparent)]",
};

export function Timeline({ children, className }: { children: ReactNode; className?: string }) {
  return <ol className={cn("relative", className)}>{children}</ol>;
}

export function TimelineItem({
  icon,
  tone = "neutral",
  title,
  time,
  children,
  actions,
  last,
  className,
}: {
  icon: ReactNode;
  tone?: TimelineTone;
  title: ReactNode;
  time?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  last?: boolean;
  className?: string;
}) {
  return (
    <li className={cn("relative flex gap-3 pb-4", className)}>
      {!last && <span aria-hidden className="absolute top-7 bottom-0 left-[13px] w-px bg-border" />}
      <span
        className={cn(
          "relative z-[1] flex size-[27px] shrink-0 items-center justify-center rounded-full border [&_svg]:size-3.5",
          toneClass[tone],
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1 pt-[3px]">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 text-[13px] text-fg">{title}</div>
          <div className="flex shrink-0 items-center gap-2">
            {actions}
            {time && <span className="text-[11px] whitespace-nowrap text-subtle tabular-nums">{time}</span>}
          </div>
        </div>
        {children && <div className="mt-1.5">{children}</div>}
      </div>
    </li>
  );
}
