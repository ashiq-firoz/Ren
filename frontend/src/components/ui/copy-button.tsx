"use client";

import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "@/lib/toast";
import { cn, copyToClipboard } from "@/lib/utils";

export function CopyButton({
  text,
  label = "Copy",
  showLabel = false,
  className,
  size = "sm",
}: {
  text: string;
  label?: string;
  showLabel?: boolean;
  className?: string;
  size?: "xs" | "sm";
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      onClick={async (e) => {
        e.stopPropagation();
        const ok = await copyToClipboard(text);
        if (!ok) {
          toast.error("Couldn't copy to clipboard");
          return;
        }
        setCopied(true);
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1600);
      }}
      className={cn(
        "inline-flex items-center gap-1 rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-fg",
        size === "xs" ? "h-6 px-1.5 text-[11px] [&_svg]:size-3" : "h-7 px-2 text-xs [&_svg]:size-3.5",
        !showLabel && (size === "xs" ? "w-6 justify-center px-0" : "w-7 justify-center px-0"),
        className,
      )}
    >
      {copied ? <Check className="text-success" /> : <Copy />}
      {showLabel && <span>{copied ? "Copied" : label}</span>}
    </button>
  );
}
