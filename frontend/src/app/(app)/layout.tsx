"use client";

import { Building2, LogOut, RefreshCw, WifiOff } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { Suspense, useEffect, useState, type ReactNode } from "react";
import { Wordmark } from "@/components/brand/logo";
import { FileViewerProvider } from "@/components/files/file-viewer-provider";
import { AppShell } from "@/components/shell/app-shell";
import { ShellProvider } from "@/components/shell/shell-context";
import { CreateWorkspaceDialog } from "@/components/shell/workspace-switcher";
import { Button } from "@/components/ui/button";
import { FullScreenLoader } from "@/components/ui/feedback";
import { useProviders } from "@/hooks/queries";
import { useLocalPreference } from "@/hooks/use-browser";
import { useAuth, useRole } from "@/providers/auth";
import { StreamProvider } from "@/providers/stream-provider";

/** Admins of a workspace without any LLM provider are sent to onboarding (unless they skipped it). */
function OnboardingGate() {
  const { isAdmin } = useRole();
  const { activeOrgId } = useAuth();
  const router = useRouter();
  const providers = useProviders(isAdmin);
  const [skipped] = useLocalPreference<string>(`ren_onboarding_skipped:${activeOrgId ?? ""}`, "");
  const empty = providers.data !== undefined && providers.data.items.length === 0;
  useEffect(() => {
    if (isAdmin && !skipped && empty) router.replace("/onboarding");
  }, [isAdmin, skipped, empty, router]);
  return null;
}

function CenteredCard({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-8 bg-bg px-4">
      <Wordmark />
      <div className="w-full max-w-md rounded-2xl border border-border bg-surface p-7 text-center">{children}</div>
    </div>
  );
}

function OfflineScreen() {
  const { refresh, logout, connectionError } = useAuth();
  return (
    <CenteredCard>
      <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-xl tint-danger">
        <WifiOff className="size-5" />
      </div>
      <h1 className="font-serif-display text-[28px] leading-tight">Can&apos;t reach Ren</h1>
      <p className="mt-2 text-[13px] text-muted">{connectionError}</p>
      <div className="mt-6 flex justify-center gap-2">
        <Button variant="ghost" icon={<LogOut className="size-4" />} onClick={logout}>
          Sign out
        </Button>
        <Button variant="primary" icon={<RefreshCw className="size-4" />} onClick={() => void refresh()}>
          Try again
        </Button>
      </div>
    </CenteredCard>
  );
}

function NoWorkspaceScreen() {
  const { logout, user } = useAuth();
  const [open, setOpen] = useState(false);
  return (
    <CenteredCard>
      <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-xl border border-border bg-surface-2">
        <Building2 className="size-5 text-muted" />
      </div>
      <h1 className="font-serif-display text-[28px] leading-tight">No workspace yet</h1>
      <p className="mt-2 text-[13px] text-muted">
        {user?.name ? `${user.name}, you` : "You"} aren&apos;t a member of any workspace. Create one to start your swarm, or
        ask an admin for an invite.
      </p>
      <div className="mt-6 flex justify-center gap-2">
        <Button variant="ghost" onClick={logout}>
          Sign out
        </Button>
        <Button variant="primary" onClick={() => setOpen(true)}>
          Create workspace
        </Button>
      </div>
      <CreateWorkspaceDialog open={open} onOpenChange={setOpen} />
    </CenteredCard>
  );
}

export default function AppLayout({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (auth.status !== "unauthenticated") return;
    const next = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
    router.replace(`/login${next}`);
  }, [auth.status, pathname, router]);

  if (auth.status === "loading" || auth.status === "unauthenticated") return <FullScreenLoader />;
  if (auth.status === "offline") return <OfflineScreen />;
  if (!auth.activeOrg) return <NoWorkspaceScreen />;

  return (
    // Remount everything (fresh queries, fresh stream) when the workspace changes.
    <StreamProvider key={auth.activeOrgId}>
      <ShellProvider>
        {/* One file viewer for chat, tasks, schedules and outputs. */}
        <FileViewerProvider>
          <OnboardingGate />
          <AppShell>
            <Suspense fallback={<FullScreenLoader label="Loading…" />}>{children}</Suspense>
          </AppShell>
        </FileViewerProvider>
      </ShellProvider>
    </StreamProvider>
  );
}
