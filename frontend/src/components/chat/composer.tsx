"use client";

import { ArrowUp, AtSign, FileText, Loader2, Paperclip, Square, X } from "lucide-react";
import {
  useCallback,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type Ref,
} from "react";
import { AgentAvatar } from "@/components/ui/avatar";
import { Kbd } from "@/components/ui/badge";
import { FloatingPanel } from "@/components/ui/floating";
import { useAgents } from "@/hooks/queries";
import { api, errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { Agent, Attachment, ConversationStatus } from "@/lib/types";
import { cn, uid } from "@/lib/utils";
import { isBusy, statusLine } from "./chat-utils";

export interface ComposerHandle {
  setText: (text: string) => void;
  focus: () => void;
}

interface UploadItem {
  localId: string;
  name: string;
  size: number;
  progress: number;
  status: "uploading" | "done" | "error";
  attachment?: Attachment;
  error?: string;
  controller: AbortController;
}

export interface ComposerSendPayload {
  content: string;
  attachments: Attachment[];
  mentionAgentId: string | null;
}

export interface ComposerProps {
  ref?: Ref<ComposerHandle>;
  status: ConversationStatus;
  statusDetail?: string | null;
  queued?: number;
  activeTasks?: number;
  onSend: (payload: ComposerSendPayload) => Promise<unknown> | void;
  onStop?: () => void;
  stopping?: boolean;
  /** Resolve (creating if needed) the conversation id for attachment uploads. */
  ensureConversation: () => Promise<string>;
  onOpenSwarm?: () => void;
  placeholder?: string;
}

interface MentionState {
  start: number;
  query: string;
  index: number;
}

export function Composer({
  ref,
  status,
  statusDetail,
  queued = 0,
  activeTasks = 0,
  onSend,
  onStop,
  stopping,
  ensureConversation,
  onOpenSwarm,
  placeholder = "Message Ren…",
}: ComposerProps) {
  const [text, setText] = useState("");
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [mentionAgent, setMentionAgent] = useState<Agent | null>(null);
  const [mention, setMention] = useState<MentionState | null>(null);
  const [dragging, setDragging] = useState(false);
  const [waitingUploads, setWaitingUploads] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [anchor, setAnchor] = useState<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const uploadPromises = useRef(new Map<string, Promise<void>>());
  /** localId → uploaded attachment, written from upload callbacks (always current, unlike state in a closure). */
  const uploaded = useRef(new Map<string, Attachment>());
  const agents = useAgents();

  const busy = isBusy(status);
  const line = statusLine(status, statusDetail);

  useImperativeHandle(
    ref,
    () => ({
      setText: (t: string) => {
        setText(t);
        requestAnimationFrame(() => {
          const el = textareaRef.current;
          if (!el) return;
          el.focus();
          el.setSelectionRange(t.length, t.length);
        });
      },
      focus: () => textareaRef.current?.focus(),
    }),
    [],
  );

  const candidates = useMemo(() => {
    if (!mention) return [];
    const q = mention.query.toLowerCase();
    return (agents.data?.items ?? [])
      .filter((a) => a.enabled && !a.is_orchestrator)
      .filter((a) => !q || a.name.toLowerCase().includes(q) || a.role.toLowerCase().includes(q) || a.slug.includes(q))
      .slice(0, 8);
  }, [mention, agents.data]);

  /* ---------------- autosize ---------------- */
  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
    el.style.overflowY = el.scrollHeight > 260 ? "auto" : "hidden";
  }, []);

  const setTextareaRef = useCallback((el: HTMLTextAreaElement | null) => {
    textareaRef.current = el;
  }, []);

  // Keep the textarea sized to its content for every change (typing, suggestions, restores).
  useLayoutEffect(() => {
    resize();
  }, [text, resize]);

  /* ---------------- mentions ---------------- */
  const detectMention = (value: string, caret: number) => {
    const before = value.slice(0, caret);
    const m = /(^|\s)@([\w.-]{0,30})$/.exec(before);
    if (m) {
      const start = caret - m[2].length - 1;
      setMention((prev) => ({ start, query: m[2], index: prev && prev.start === start ? prev.index : 0 }));
    } else if (mention) {
      setMention(null);
    }
  };

  const pickMention = (agent: Agent) => {
    if (!mention) return;
    const el = textareaRef.current;
    const caret = el?.selectionStart ?? text.length;
    const insert = `@${agent.name} `;
    const next = text.slice(0, mention.start) + insert + text.slice(caret);
    setText(next);
    setMentionAgent(agent);
    setMention(null);
    requestAnimationFrame(() => {
      if (!el) return;
      const pos = mention.start + insert.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  };

  /* ---------------- uploads ---------------- */
  const startUploads = async (files: File[]) => {
    if (files.length === 0) return;
    let convId: string;
    try {
      convId = await ensureConversation();
    } catch (err) {
      toast.error("Couldn't attach files", { description: errorMessage(err) });
      return;
    }
    for (const file of files) {
      const item: UploadItem = {
        localId: uid("u-"),
        name: file.name,
        size: file.size,
        progress: 0,
        status: "uploading",
        controller: new AbortController(),
      };
      setUploads((u) => [...u, item]);
      const form = new FormData();
      form.append("file", file);
      const update = (patch: Partial<UploadItem>) =>
        setUploads((u) => u.map((x) => (x.localId === item.localId ? { ...x, ...patch } : x)));
      const promise = api
        .upload<Attachment>(`/conversations/${convId}/attachments`, form, {
          signal: item.controller.signal,
          onProgress: (p) => update({ progress: p }),
        })
        .then((attachment) => {
          if (item.controller.signal.aborted) return;
          uploaded.current.set(item.localId, attachment);
          update({ status: "done", progress: 1, attachment });
        })
        .catch((err) => {
          if (err instanceof DOMException && err.name === "AbortError") return;
          update({ status: "error", error: errorMessage(err) });
        })
        .finally(() => uploadPromises.current.delete(item.localId));
      uploadPromises.current.set(item.localId, promise);
    }
  };

  const removeUpload = (localId: string) => {
    uploads.find((x) => x.localId === localId)?.controller.abort();
    uploaded.current.delete(localId);
    uploadPromises.current.delete(localId);
    setUploads((u) => u.filter((x) => x.localId !== localId));
  };

  /* ---------------- send ---------------- */
  const submit = async () => {
    const content = text.trim();
    const hasFiles = uploads.some((u) => u.status !== "error");
    if (!content && !hasFiles) return;
    if (uploadPromises.current.size > 0) {
      setWaitingUploads(true);
      await Promise.allSettled(Array.from(uploadPromises.current.values()));
      setWaitingUploads(false);
    }
    const attachments = Array.from(uploaded.current.values());
    if (!content && attachments.length === 0) return;
    const mentionId = mentionAgent?.id ?? null;
    uploaded.current.clear();
    setText("");
    setUploads([]);
    setMentionAgent(null);
    setMention(null);
    try {
      await onSend({
        content: content || `Attached: ${attachments.map((a) => a.name).join(", ")}`,
        attachments,
        mentionAgentId: mentionId,
      });
    } catch {
      // The sender already showed an error; restore the draft so nothing is lost.
      setText(content);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention && candidates.length > 0) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const delta = e.key === "ArrowDown" ? 1 : -1;
        setMention({ ...mention, index: (mention.index + delta + candidates.length) % candidates.length });
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pickMention(candidates[Math.min(mention.index, candidates.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files ?? []);
    if (files.length) {
      e.preventDefault();
      void startUploads(files);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) void startUploads(files);
  };

  const canSend = text.trim().length > 0 || uploads.some((u) => u.status === "done" || u.status === "uploading");

  return (
    <div className="shrink-0 px-3 pb-3 sm:px-6 sm:pb-5">
      <div className="mx-auto w-full max-w-[800px]">
        {(line || activeTasks > 0 || queued > 0) && (
          <div className="mb-2 flex min-h-6 items-center justify-between gap-3 px-1 text-xs">
            <span className="flex min-w-0 items-center gap-2 text-muted" aria-live="polite">
              {line && (
                <>
                  <span className="typing-dots inline-flex items-center">
                    <span />
                    <span />
                    <span />
                  </span>
                  <span className="truncate">{line}</span>
                </>
              )}
              {queued > 0 && (
                <span className="shrink-0 rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-muted">
                  {queued} queued
                </span>
              )}
            </span>
            {activeTasks > 0 && (
              <button
                type="button"
                onClick={onOpenSwarm}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-0.5 font-medium text-fg hover:bg-surface-2"
              >
                <span className="size-1.5 animate-pulse-dot rounded-full bg-accent text-accent" />
                {activeTasks} {activeTasks === 1 ? "task" : "tasks"} running
              </button>
            )}
          </div>
        )}

        <div
          ref={(el) => {
            containerRef.current = el;
            setAnchor(el);
          }}
          onDragEnter={(e) => {
            if (e.dataTransfer.types.includes("Files")) {
              e.preventDefault();
              setDragging(true);
            }
          }}
          onDragOver={(e) => e.dataTransfer.types.includes("Files") && e.preventDefault()}
          onDragLeave={(e) => {
            if (!containerRef.current?.contains(e.relatedTarget as Node)) setDragging(false);
          }}
          onDrop={onDrop}
          className={cn(
            "relative rounded-2xl border bg-surface shadow-[0_1px_0_rgb(0_0_0/0.02),0_8px_24px_-18px_rgb(0_0_0/0.25)] transition-colors",
            dragging ? "border-accent" : "border-border-strong focus-within:border-[color-mix(in_srgb,var(--focus)_60%,var(--border-strong))]",
          )}
        >
          {dragging && (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-[color-mix(in_srgb,var(--accent)_8%,var(--surface))] text-[13px] font-medium text-fg">
              Drop files to attach
            </div>
          )}

          {(uploads.length > 0 || mentionAgent) && (
            <div className="flex flex-wrap gap-1.5 px-3 pt-3">
              {mentionAgent && (
                <span className="inline-flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface-2 pr-1 pl-1 text-xs text-fg">
                  <AgentAvatar agent={mentionAgent} size="xs" />
                  <span>For {mentionAgent.name}</span>
                  <button
                    type="button"
                    aria-label="Remove mention"
                    onClick={() => setMentionAgent(null)}
                    className="flex size-5 items-center justify-center rounded-full text-subtle hover:bg-surface-3 hover:text-fg"
                  >
                    <X className="size-3" />
                  </button>
                </span>
              )}
              {uploads.map((u) => (
                <span
                  key={u.localId}
                  className={cn(
                    "relative inline-flex h-7 max-w-[240px] items-center gap-1.5 overflow-hidden rounded-full border pr-1 pl-2.5 text-xs",
                    u.status === "error" ? "border-[color-mix(in_srgb,var(--danger)_40%,transparent)] text-danger" : "border-border bg-surface-2 text-fg",
                  )}
                  title={u.error ?? u.name}
                >
                  {u.status === "uploading" && (
                    <span
                      aria-hidden
                      className="absolute inset-y-0 left-0 bg-[color-mix(in_srgb,var(--accent)_18%,transparent)] transition-[width]"
                      style={{ width: `${Math.round(u.progress * 100)}%` }}
                    />
                  )}
                  <span className="relative flex items-center gap-1.5 truncate">
                    {u.status === "uploading" ? <Loader2 className="spin size-3 shrink-0" /> : <FileText className="size-3 shrink-0" />}
                    <span className="truncate">{u.name}</span>
                    <span className="shrink-0 text-subtle">
                      {u.status === "uploading" ? `${Math.round(u.progress * 100)}%` : u.status === "error" ? "failed" : formatBytes(u.size)}
                    </span>
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${u.name}`}
                    onClick={() => removeUpload(u.localId)}
                    className="relative flex size-5 shrink-0 items-center justify-center rounded-full text-subtle hover:bg-surface-3 hover:text-fg"
                  >
                    <X className="size-3" />
                  </button>
                </span>
              ))}
            </div>
          )}

          <textarea
            ref={setTextareaRef}
            value={text}
            rows={1}
            onChange={(e) => {
              setText(e.target.value);
              detectMention(e.target.value, e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            onClick={(e) => detectMention(e.currentTarget.value, e.currentTarget.selectionStart ?? 0)}
            placeholder={busy ? "Ren is replying — you can keep typing…" : placeholder}
            aria-label="Message Ren"
            aria-autocomplete="list"
            className="block max-h-[260px] min-h-[52px] w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-[15px] leading-relaxed text-fg outline-none placeholder:text-subtle"
          />

          <div className="flex items-center justify-between gap-2 px-2 pb-2">
            <div className="flex items-center gap-0.5">
              <input
                ref={fileRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => {
                  void startUploads(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                aria-label="Attach files"
                title="Attach files"
                onClick={() => fileRef.current?.click()}
                className="flex size-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-fg"
              >
                <Paperclip className="size-4" />
              </button>
              <button
                type="button"
                aria-label="Mention an agent"
                title="Mention an agent"
                onClick={() => {
                  const el = textareaRef.current;
                  const caret = el?.selectionStart ?? text.length;
                  const needsSpace = caret > 0 && !/\s/.test(text[caret - 1] ?? "");
                  const next = `${text.slice(0, caret)}${needsSpace ? " " : ""}@${text.slice(caret)}`;
                  setText(next);
                  const pos = caret + (needsSpace ? 2 : 1);
                  setMention({ start: pos - 1, query: "", index: 0 });
                  requestAnimationFrame(() => {
                    el?.focus();
                    el?.setSelectionRange(pos, pos);
                  });
                }}
                className="flex size-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-fg"
              >
                <AtSign className="size-4" />
              </button>
              <span className="ml-1 hidden text-[11px] text-subtle md:inline">
                {busy ? (
                  "Ren will reply after the current response"
                ) : (
                  <>
                    <Kbd>Enter</Kbd> send · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> newline · <Kbd>@</Kbd> mention
                  </>
                )}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              {busy && onStop && (
                <button
                  type="button"
                  onClick={onStop}
                  disabled={stopping}
                  aria-label="Stop generating"
                  className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border-strong px-2.5 text-xs font-medium text-fg transition-colors hover:bg-surface-2 disabled:opacity-50"
                >
                  {stopping ? <Loader2 className="spin size-3" /> : <Square className="size-3 fill-current" />}
                  Stop
                </button>
              )}
              <button
                type="button"
                onClick={() => void submit()}
                disabled={!canSend}
                aria-label={busy ? "Queue message" : "Send message"}
                title={busy ? "Send (queued until Ren finishes)" : "Send"}
                className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-fg transition-opacity hover:opacity-90 disabled:opacity-30"
              >
                {waitingUploads ? <Loader2 className="spin size-4" /> : <ArrowUp className="size-4" strokeWidth={2.25} />}
              </button>
            </div>
          </div>
        </div>
        {busy && (
          <p className="mt-1.5 px-1 text-[11px] text-subtle md:hidden">Ren will reply after the current response.</p>
        )}
      </div>

      <FloatingPanel
        open={!!mention && candidates.length > 0}
        anchor={anchor}
        placement="top-start"
        offset={8}
        onDismiss={() => setMention(null)}
        role="listbox"
        aria-label="Mention an agent"
        className="w-[300px] overflow-hidden rounded-card border border-border bg-surface p-1 shadow-popover"
      >
        <p className="px-2.5 pt-1.5 pb-1 text-[10.5px] font-medium tracking-wide text-subtle uppercase">Hand this to…</p>
        {candidates.map((a, i) => (
          <button
            key={a.id}
            type="button"
            role="option"
            aria-selected={i === mention?.index}
            onMouseDown={(e) => e.preventDefault()}
            onMouseEnter={() => mention && setMention({ ...mention, index: i })}
            onClick={() => pickMention(a)}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left",
              i === mention?.index ? "bg-surface-2" : "hover:bg-surface-2/60",
            )}
          >
            <AgentAvatar agent={a} size="sm" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium text-fg">{a.name}</span>
              <span className="block truncate text-[11.5px] text-muted">{a.role}</span>
            </span>
            {(a.stats?.active_tasks ?? 0) > 0 && (
              <span className="text-[11px] text-subtle">{a.stats?.active_tasks} active</span>
            )}
          </button>
        ))}
      </FloatingPanel>
    </div>
  );
}
