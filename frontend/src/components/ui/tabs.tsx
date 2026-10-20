"use client";

import type { KeyboardEvent, ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface TabItem<T extends string = string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  count?: number | null;
  disabled?: boolean;
}

export interface TabsProps<T extends string> {
  value: T;
  onValueChange: (v: T) => void;
  items: TabItem<T>[];
  variant?: "underline" | "pills";
  className?: string;
  "aria-label"?: string;
  idPrefix?: string;
}

export function Tabs<T extends string>({
  value,
  onValueChange,
  items,
  variant = "underline",
  className,
  idPrefix = "tab",
  ...rest
}: TabsProps<T>) {
  const enabled = items.filter((i) => !i.disabled);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const idx = enabled.findIndex((i) => i.value === value);
    let next = idx;
    if (e.key === "ArrowRight") next = (idx + 1) % enabled.length;
    if (e.key === "ArrowLeft") next = (idx - 1 + enabled.length) % enabled.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = enabled.length - 1;
    const item = enabled[next];
    if (!item) return;
    onValueChange(item.value);
    e.currentTarget.querySelector<HTMLButtonElement>(`#${CSS.escape(`${idPrefix}-${item.value}`)}`)?.focus();
  };

  return (
    <div
      role="tablist"
      aria-label={rest["aria-label"]}
      onKeyDown={onKeyDown}
      className={cn(
        "scrollbar-none flex items-center overflow-x-auto",
        variant === "underline" ? "gap-5 border-b border-border" : "gap-1",
        className,
      )}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            id={`${idPrefix}-${item.value}`}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            disabled={item.disabled}
            onClick={() => onValueChange(item.value)}
            className={cn(
              "relative inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-[13px] font-medium transition-colors duration-150 disabled:opacity-40 [&_svg]:size-3.5",
              variant === "underline"
                ? cn(
                    "-mb-px h-10 border-b-2",
                    active ? "border-fg text-fg" : "border-transparent text-muted hover:text-fg",
                  )
                : cn(
                    "h-8 rounded-full px-3",
                    active ? "bg-primary text-primary-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
                  ),
            )}
          >
            {item.icon}
            {item.label}
            {item.count !== undefined && item.count !== null && (
              <span
                className={cn(
                  "ml-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1.5 text-[10.5px] font-semibold",
                  variant === "pills" && active ? "bg-primary-fg/20 text-primary-fg" : "bg-surface-3 text-muted",
                )}
              >
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
