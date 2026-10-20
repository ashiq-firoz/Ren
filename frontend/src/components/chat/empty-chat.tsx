"use client";

import { ArrowUpRight } from "lucide-react";
import { LogoMark } from "@/components/brand/logo";
import { useAuth } from "@/providers/auth";
import { SUGGESTIONS } from "./chat-utils";

export function EmptyChat({ onPick }: { onPick: (prompt: string) => void }) {
  const { user } = useAuth();
  const first = user?.name?.split(/\s+/)[0];
  return (
    <div className="flex min-h-0 flex-1 overflow-y-auto">
      <div className="m-auto w-full max-w-[820px] px-4 py-10 sm:px-6">
        <div className="flex flex-col items-center text-center">
          <span className="mb-5 flex size-12 items-center justify-center rounded-2xl bg-brand text-brand-fg ring-1 ring-border">
            <LogoMark className="w-[34px]" />
          </span>
          <p className="text-[13px] text-muted">{first ? `Good to see you, ${first}.` : "Welcome."}</p>
          <h2 className="font-serif-display mt-1 text-[38px] leading-[1.08] text-fg sm:text-[46px]">
            What should the swarm work on?
          </h2>
          <p className="mt-3 max-w-lg text-[13.5px] leading-relaxed text-muted">
            Ask Ren anything. Specialist agents pick up the work in the background while you keep chatting — you approve
            anything sensitive before it happens.
          </p>
        </div>
        <div className="mt-9 grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
          {SUGGESTIONS.map((s) => (
            <button
              key={s.title}
              type="button"
              onClick={() => onPick(s.prompt)}
              className="group flex flex-col items-start gap-2 rounded-card border border-border bg-surface p-3.5 text-left transition-[border-color,transform] duration-150 hover:-translate-y-px hover:border-border-strong"
            >
              <span className="flex w-full items-center justify-between">
                <span className="text-[10.5px] font-medium tracking-[0.08em] text-subtle uppercase">{s.tag}</span>
                <ArrowUpRight className="size-3.5 text-subtle transition-colors group-hover:text-fg" />
              </span>
              <span className="text-[13px] leading-snug text-fg">{s.title}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
