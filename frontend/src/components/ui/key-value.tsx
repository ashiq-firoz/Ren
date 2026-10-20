import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface KeyValueItem {
  label: ReactNode;
  value: ReactNode;
  hidden?: boolean;
}

export function KeyValue({
  items,
  columns = 1,
  className,
  dense,
}: {
  items: KeyValueItem[];
  columns?: 1 | 2 | 3 | 4;
  className?: string;
  dense?: boolean;
}) {
  return (
    <dl
      className={cn(
        "grid gap-x-6",
        dense ? "gap-y-2" : "gap-y-3",
        columns === 2 && "sm:grid-cols-2",
        columns === 3 && "sm:grid-cols-2 lg:grid-cols-3",
        columns === 4 && "grid-cols-2 lg:grid-cols-4",
        className,
      )}
    >
      {items
        .filter((i) => !i.hidden)
        .map((item, idx) => (
          <div key={idx} className="min-w-0">
            <dt className="text-[11px] font-medium tracking-wide text-subtle uppercase">{item.label}</dt>
            <dd className="mt-0.5 truncate text-[13px] text-fg">{item.value}</dd>
          </div>
        ))}
    </dl>
  );
}

/** Horizontal label/value row used in side panels. */
export function KV({ label, children, className }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-center justify-between gap-3 py-1.5 text-[13px]", className)}>
      <span className="text-muted">{label}</span>
      <span className="min-w-0 truncate text-right text-fg">{children}</span>
    </div>
  );
}
