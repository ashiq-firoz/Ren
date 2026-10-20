"use client";

import { RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import { Drawer } from "@/components/ui/overlay";
import { useAuth } from "@/providers/auth";
import { CommandPalette } from "./command-palette";
import { useShell } from "./shell-context";
import { Sidebar, SidebarContent } from "./sidebar";

function ConnectionBanner() {
  const { connectionError, refresh } = useAuth();
  if (!connectionError) return null;
  return (
    <div className="sticky top-0 z-30 flex items-center justify-center gap-2 border-b border-[color-mix(in_srgb,var(--warning)_30%,transparent)] bg-[color-mix(in_srgb,var(--warning)_10%,var(--bg))] px-4 py-1.5 text-xs text-fg">
      <WifiOff className="size-3.5 text-warning" />
      <span className="truncate">Can&apos;t reach the Ren API — showing your cached session.</span>
      <button
        type="button"
        onClick={() => void refresh()}
        className="inline-flex items-center gap-1 font-medium underline underline-offset-2"
      >
        <RefreshCw className="size-3" /> Retry
      </button>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { mobileNavOpen, closeMobileNav } = useShell();
  return (
    <div className="flex h-dvh overflow-hidden bg-bg">
      <a
        href="#main"
        className="sr-only z-[100] rounded-md bg-primary px-3 py-2 text-primary-fg focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <Sidebar />
      <Drawer
        open={mobileNavOpen}
        onOpenChange={(o) => !o && closeMobileNav()}
        side="left"
        width={284}
        bare
        className="bg-bg"
      >
        <SidebarContent onNavigate={closeMobileNav} />
      </Drawer>
      <main id="main" className="relative flex min-w-0 flex-1 flex-col overflow-y-auto">
        <ConnectionBanner />
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      </main>
      <CommandPalette />
    </div>
  );
}
