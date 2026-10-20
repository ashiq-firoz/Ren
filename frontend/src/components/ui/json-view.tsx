"use client";

import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { CopyButton } from "./copy-button";

type Json = unknown;

function Primitive({ value }: { value: Json }) {
  if (value === null) return <span className="text-subtle">null</span>;
  if (value === undefined) return <span className="text-subtle">undefined</span>;
  if (typeof value === "string")
    return <span className="break-all whitespace-pre-wrap text-success">&quot;{value}&quot;</span>;
  if (typeof value === "number") return <span className="text-info">{String(value)}</span>;
  if (typeof value === "boolean") return <span className="text-warning">{String(value)}</span>;
  return <span>{String(value)}</span>;
}

function Node({ name, value, depth, defaultDepth }: { name?: string; value: Json; depth: number; defaultDepth: number }) {
  const isArray = Array.isArray(value);
  const isObject = value !== null && typeof value === "object";
  const [open, setOpen] = useState(depth < defaultDepth);

  const label =
    name !== undefined ? (
      <span className="text-muted">
        {/^\d+$/.test(name) ? name : <>&quot;{name}&quot;</>}
        <span className="text-subtle">: </span>
      </span>
    ) : null;

  if (!isObject) {
    return (
      <div className="pl-4">
        {label}
        <Primitive value={value} />
      </div>
    );
  }

  const entries = isArray ? (value as Json[]).map((v, i) => [String(i), v] as const) : Object.entries(value as object);
  const [openBr, closeBr] = isArray ? ["[", "]"] : ["{", "}"];

  if (entries.length === 0) {
    return (
      <div className="pl-4">
        {label}
        <span className="text-subtle">
          {openBr}
          {closeBr}
        </span>
      </div>
    );
  }

  return (
    <div className={depth === 0 ? "" : "pl-4"}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="-ml-4 inline-flex items-center text-left hover:text-fg"
        aria-expanded={open}
      >
        <ChevronRight className={cn("size-3.5 text-subtle transition-transform", open && "rotate-90")} />
        {label}
        <span className="text-subtle">{openBr}</span>
        {!open && (
          <span className="text-subtle">
            {" "}
            {entries.length} {isArray ? (entries.length === 1 ? "item" : "items") : entries.length === 1 ? "key" : "keys"} {closeBr}
          </span>
        )}
      </button>
      {open && (
        <>
          {entries.map(([k, v]) => (
            <Node key={k} name={k} value={v} depth={depth + 1} defaultDepth={defaultDepth} />
          ))}
          <div className="text-subtle">{closeBr}</div>
        </>
      )}
    </div>
  );
}

/** Collapsible, pretty-printed JSON. */
export function JsonView({
  data,
  defaultDepth = 2,
  className,
  copyable = true,
  maxHeight = 360,
}: {
  data: Json;
  defaultDepth?: number;
  className?: string;
  copyable?: boolean;
  maxHeight?: number;
}) {
  let text = "";
  try {
    text = JSON.stringify(data, null, 2) ?? String(data);
  } catch {
    text = String(data);
  }
  const isComposite = data !== null && typeof data === "object";
  return (
    <div className={cn("relative rounded-lg border border-border bg-[var(--code-bg)]", className)}>
      {copyable && (
        <div className="absolute top-1.5 right-1.5 z-10">
          <CopyButton text={text} size="xs" label="Copy JSON" />
        </div>
      )}
      <div className="overflow-auto px-3 py-2.5 pl-7 font-mono text-[12px] leading-[1.6]" style={{ maxHeight }}>
        {isComposite ? (
          <Node value={data} depth={0} defaultDepth={defaultDepth} />
        ) : (
          <div className="-ml-4">
            <Primitive value={data} />
          </div>
        )}
      </div>
    </div>
  );
}
