"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ModelsTab } from "@/components/settings/models-tab";
import { AlignmentTab, AppearanceTab, ProfileTab, RetrievalTab, WebSearchTab } from "@/components/settings/other-tabs";
import { WorkspaceTab } from "@/components/settings/workspace-tab";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Card } from "@/components/ui/card";
import { ErrorState, Skeleton } from "@/components/ui/feedback";
import { Tabs } from "@/components/ui/tabs";
import { useOrg } from "@/hooks/queries";

type Tab = "workspace" | "models" | "retrieval" | "alignment" | "web" | "profile" | "appearance";
const TABS: { value: Tab; label: string }[] = [
  { value: "workspace", label: "Workspace" },
  { value: "models", label: "Models" },
  { value: "retrieval", label: "Retrieval" },
  { value: "alignment", label: "Alignment" },
  { value: "web", label: "Web search" },
  { value: "profile", label: "Profile" },
  { value: "appearance", label: "Appearance" },
];
const ORG_TABS: Tab[] = ["workspace", "models", "retrieval", "alignment", "web"];

export default function SettingsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const raw = params.get("tab") as Tab | null;
  const tab: Tab = raw && TABS.some((t) => t.value === raw) ? raw : "workspace";
  const org = useOrg();

  const needsOrg = ORG_TABS.includes(tab);
  let content: React.ReactNode;
  if (needsOrg && org.isLoading) {
    content = (
      <div className="space-y-4">
        <Skeleton className="h-64 w-full rounded-card" />
        <Skeleton className="h-40 w-full rounded-card" />
      </div>
    );
  } else if (needsOrg && (org.error || !org.data)) {
    content = (
      <Card>
        <ErrorState error={org.error ?? new Error("Workspace unavailable")} onRetry={() => org.refetch()} />
      </Card>
    );
  } else if (tab === "profile") {
    content = <ProfileTab />;
  } else if (tab === "appearance") {
    content = <AppearanceTab />;
  } else {
    const o = org.data!;
    // Keyed by updated settings so forms re-seed after another admin's change is refetched.
    const k = `${o.id}`;
    content =
      tab === "workspace" ? (
        <WorkspaceTab key={k} org={o} />
      ) : tab === "models" ? (
        <ModelsTab org={o} />
      ) : tab === "retrieval" ? (
        <RetrievalTab key={k} org={o} />
      ) : tab === "alignment" ? (
        <AlignmentTab key={k} org={o} />
      ) : (
        <WebSearchTab key={k} org={o} />
      );
  }

  return (
    <PageContainer className="max-w-[1000px]">
      <PageHeader title="Settings" description="Workspace configuration, models, retrieval, alignment and your personal preferences." />
      <Tabs
        aria-label="Settings sections"
        value={tab}
        onValueChange={(t) => router.replace(t === "workspace" ? pathname : `${pathname}?tab=${t}`, { scroll: false })}
        className="mb-6"
        items={TABS}
      />
      {content}
    </PageContainer>
  );
}
