"use client";

import { ArrowDown, Loader2 } from "lucide-react";
import { Fragment, memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { dayHeading, isSameDay } from "@/lib/format";
import type { Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MessageItem } from "./message-item";

export interface MessageListProps {
  conversationKey: string;
  messages: Message[];
  hasMore?: boolean;
  loadingEarlier?: boolean;
  onLoadEarlier?: () => Promise<void> | void;
  onRetry?: (m: Message) => void;
  /** Increment to force-scroll to the bottom (e.g. after the user sends). */
  pinSignal?: number;
  header?: ReactNode;
  footer?: ReactNode;
}

const NEAR_BOTTOM = 96;

function MessageListImpl({
  conversationKey,
  messages,
  hasMore,
  loadingEarlier,
  onLoadEarlier,
  onRetry,
  pinSignal = 0,
  header,
  footer,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const anchorFromBottom = useRef<number | null>(null);
  const [showJump, setShowJump] = useState(false);

  const scrollToBottom = (smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    atBottom.current = true;
  };

  // New conversation → start at the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    atBottom.current = true;
  }, [conversationKey]);

  // After messages change: keep position when older messages were prepended, else stick to bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (anchorFromBottom.current !== null) {
      el.scrollTop = el.scrollHeight - anchorFromBottom.current;
      anchorFromBottom.current = null;
      return;
    }
    if (atBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // The user sent something → always jump down.
  useLayoutEffect(() => {
    if (pinSignal > 0) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
      atBottom.current = true;
    }
  }, [pinSignal]);

  // Streaming text, images and late layout shifts grow the content: follow it while pinned.
  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const ro = new ResizeObserver(() => {
      if (atBottom.current && anchorFromBottom.current === null) el.scrollTop = el.scrollHeight;
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    atBottom.current = dist < NEAR_BOTTOM;
    const jump = dist > 360;
    setShowJump((prev) => (prev === jump ? prev : jump));
    if (el.scrollTop < 120 && hasMore && !loadingEarlier && onLoadEarlier) {
      anchorFromBottom.current = el.scrollHeight - el.scrollTop;
      void onLoadEarlier();
    }
  };

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="absolute inset-0 overflow-y-auto overscroll-contain"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Conversation"
      >
        <div ref={contentRef} className="mx-auto w-full max-w-[800px] px-4 pt-6 pb-8 sm:px-6">
          {header}
          {hasMore && (
            <div className="mb-6 flex justify-center">
              <button
                type="button"
                disabled={loadingEarlier}
                onClick={() => {
                  const el = scrollRef.current;
                  if (el) anchorFromBottom.current = el.scrollHeight - el.scrollTop;
                  void onLoadEarlier?.();
                }}
                className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted hover:text-fg disabled:opacity-60"
              >
                {loadingEarlier && <Loader2 className="spin size-3" />}
                {loadingEarlier ? "Loading earlier messages…" : "Load earlier messages"}
              </button>
            </div>
          )}
          <div className="space-y-6">
            {messages.map((m, i) => {
              const prev = messages[i - 1];
              const newDay = !prev || !isSameDay(prev.created_at, m.created_at);
              return (
                <Fragment key={m.id}>
                  {newDay && (
                    <div className="flex items-center gap-3 py-1" role="separator">
                      <span className="h-px flex-1 bg-border" />
                      <span className="text-[11px] font-medium text-subtle">{dayHeading(m.created_at)}</span>
                      <span className="h-px flex-1 bg-border" />
                    </div>
                  )}
                  <div className={cn(m.role === "event" && "pl-0")}>
                    <MessageItem message={m} onRetry={onRetry} />
                  </div>
                </Fragment>
              );
            })}
          </div>
          {footer}
        </div>
      </div>
      {showJump && (
        <button
          type="button"
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-4 left-1/2 z-10 inline-flex -translate-x-1/2 animate-pop-in items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-xs font-medium text-fg shadow-popover hover:bg-surface-2"
        >
          <ArrowDown className="size-3.5" /> Jump to latest
        </button>
      )}
    </div>
  );
}

export const MessageList = memo(MessageListImpl);
