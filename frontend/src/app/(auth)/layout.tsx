import type { ReactNode } from "react";
import { LogoMark } from "@/components/brand/logo";
import { ThemeToggle } from "@/components/shell/theme-toggle";

function BrandPanel() {
  return (
    <aside className="relative hidden overflow-hidden bg-brand text-brand-fg lg:flex lg:w-[46%] lg:flex-col lg:justify-between lg:p-12 dark:border-r dark:border-border">
      {/* Large faint F69 mark */}
      <LogoMark className="pointer-events-none absolute -right-32 -bottom-20 w-[720px] opacity-[0.06]" />
      <div className="relative flex items-center gap-3.5">
        <LogoMark className="w-[54px]" title="f69 Labs" />
        <span aria-hidden className="h-7 w-px bg-brand-fg/25" />
        <div className="flex items-baseline gap-2">
          <span className="font-serif-display text-[28px] leading-none text-f69-ivory">Ren</span>
          <span className="small-caps text-[11px] text-brand-fg/60">by f69 Labs</span>
        </div>
      </div>
      <div className="relative max-w-lg">
        <p className="font-serif-display text-[52px] leading-[1.02] tracking-tight text-f69-ivory xl:text-[60px]">
          “A world of agents, <em className="text-f69-mint">working for you.</em>”
        </p>
        <p className="mt-6 max-w-md text-[14px] leading-relaxed text-brand-fg/70">
          Talk to Ren. A swarm of specialists — research, data, finance, writing, engineering, operations — takes the work
          into the background while you keep going. Every action governed, every answer grounded.
        </p>
      </div>
      <div className="relative space-y-4">
        <div className="flex items-center gap-6 text-[11.5px] text-brand-fg/55">
          <span>Orchestrated swarms</span>
          <span className="size-1 rounded-full bg-brand-fg/30" />
          <span>Grounded knowledge</span>
          <span className="size-1 rounded-full bg-brand-fg/30" />
          <span>Policy-aligned</span>
        </div>
        <p className="small-caps border-t border-brand-fg/15 pt-4 text-[11px] tracking-[0.18em] text-brand-fg/45">
          f69 Labs · Intelligence for All
        </p>
      </div>
    </aside>
  );
}

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh bg-bg">
      <BrandPanel />
      <main className="relative flex flex-1 flex-col items-center justify-center px-5 py-10 sm:px-10">
        <ThemeToggle className="absolute top-4 right-4" />
        <div className="mb-10 flex items-center gap-2.5 lg:hidden">
          <LogoMark className="w-[46px] text-mark" title="f69 Labs" />
          <span className="font-serif-display text-[26px] leading-none">Ren</span>
          <span className="small-caps text-[11px] text-muted">by f69 Labs</span>
        </div>
        <div className="w-full max-w-[400px]">{children}</div>
      </main>
    </div>
  );
}
