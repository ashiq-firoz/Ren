"use client";

import { CornerDownLeft, MessageSquare, MessageSquarePlus, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Kbd } from "@/components/ui/badge";
import { useModalBehavior, Portal } from "@/components/ui/overlay";
import { useConversations } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";
import { NAV_GROUPS } from "./nav-config";
import { useShell } from "./shell-context";

interface PaletteItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: ReactNode;
  run: () => void;
}

function PaletteBody({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const { isAdmin, mode } = useRole();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const q = useDebounced(query.trim(), 200);
  const conversations = useConversations({ q });

  const items = useMemo<PaletteItem[]>(() => {
    const go = (href: string) => () => {
      onClose();
      router.push(href);
    };
    const needle = query.trim().toLowerCase();
    const match = (s: string) => !needle || s.toLowerCase().includes(needle);
    const actions: PaletteItem[] = [
      { id: "new-chat", group: "Actions", label: "New chat", icon: <MessageSquarePlus />, run: go("/chat") },
    ].filter((a) => match(a.label));
    const pages: PaletteItem[] = NAV_GROUPS.flatMap((g) => g.items)
      .filter((i) => (!i.adminOnly || isAdmin) && (!i.businessOnly || mode === "business"))
      .filter((i) => match(i.label) || match(i.description))
      .map((i) => {
        const Icon = i.icon;
        return { id: i.href, group: "Pages", label: i.label, hint: i.description, icon: <Icon />, run: go(i.href) };
      });
    const convs: PaletteItem[] = (conversations.data?.items ?? []).slice(0, 8).map((c) => ({
      id: `c-${c.id}`,
      group: "Conversations",
      label: c.title || "Untitled conversation",
      hint: timeAgo(c.updated_at),
      icon: <MessageSquare />,
      run: go(`/chat/${c.id}`),
    }));
    return [...actions, ...pages, ...convs];
  }, [query, conversations.data, isAdmin, mode, onClose, router]);

  const clamped = Math.min(active, Math.max(0, items.length - 1));

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = Math.min(items.length - 1, clamped + 1);
      setActive(next);
      listRef.current?.querySelector(`[data-index="${next}"]`)?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = Math.max(0, clamped - 1);
      setActive(next);
      listRef.current?.querySelector(`[data-index="${next}"]`)?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[clamped]?.run();
    }
  };

  return (
    <>
      <div className="flex items-center gap-2.5 border-b border-border px-4">
        <Search className="size-4 text-subtle" />
        <input
          data-autofocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Search pages and conversations…"
          aria-label="Search pages and conversations"
          className="h-12 flex-1 bg-transparent text-[14px] text-fg outline-none placeholder:text-subtle"
        />
        <Kbd>Esc</Kbd>
      </div>
      <div ref={listRef} role="listbox" aria-label="Results" className="max-h-[min(60vh,420px)] overflow-y-auto p-1.5">
        {items.length === 0 && <p className="px-3 py-8 text-center text-[13px] text-muted">No results for “{query}”.</p>}
        {items.map((item, i) => {
          const header = i === 0 || items[i - 1].group !== item.group ? item.group : null;
          return (
            <div key={item.id}>
              {header && <p className="px-2.5 pt-2.5 pb-1 text-[10.5px] font-medium tracking-wide text-subtle uppercase">{header}</p>}
              <button
                type="button"
                role="option"
                aria-selected={i === clamped}
                data-index={i}
                onMouseMove={() => setActive(i)}
                onClick={item.run}
                className={cn(
                  "flex w-full items-center gap-3 rounded-md px-2.5 py-2 text-left text-[13px] [&_svg]:size-4",
                  i === clamped ? "bg-surface-2 text-fg" : "text-muted",
                )}
              >
                <span className="text-muted">{item.icon}</span>
                <span className="min-w-0 flex-1 truncate text-fg">{item.label}</span>
                {item.hint && <span className="hidden max-w-[45%] truncate text-xs text-subtle sm:block">{item.hint}</span>}
                {i === clamped && <CornerDownLeft className="size-3.5 text-subtle" />}
              </button>
            </div>
          );
        })}
      </div>
    </>
  );
}

/** ⌘K / Ctrl+K: jump to pages, start a chat, search conversations. */
export function CommandPalette() {
  const { paletteOpen, setPaletteOpen } = useShell();
  const panelRef = useRef<HTMLDivElement>(null);
  useModalBehavior(paletteOpen, () => setPaletteOpen(false), panelRef);
  if (!paletteOpen) return null;
  return (
    <Portal>
      <div className="fixed inset-0 z-[60] flex items-start justify-center px-4 pt-[12vh]">
        <div className="absolute inset-0 animate-fade-in bg-[var(--overlay)]" onMouseDown={() => setPaletteOpen(false)} />
        <div
          ref={panelRef}
          data-modal-panel
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
          className="relative w-full max-w-[560px] animate-pop-in overflow-hidden rounded-2xl border border-border bg-surface shadow-popover"
        >
          <PaletteBody onClose={() => setPaletteOpen(false)} />
        </div>
      </div>
    </Portal>
  );
}
