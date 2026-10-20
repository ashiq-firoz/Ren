"use client";

import { ArrowLeft, FileQuestion } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { FileViewer } from "@/components/files/file-viewer";
import { NotificationBell } from "@/components/shell/notification-bell";
import { MobileNavButton, PageContainer } from "@/components/shell/page-header";
import { ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, Skeleton, SkeletonText } from "@/components/ui/feedback";
import type { FileSource } from "@/lib/files";

function TopBar() {
  const router = useRouter();
  const back = () => {
    // Opened in a new tab there's nothing to go back to.
    if (window.history.length > 1) router.back();
    else router.push("/outputs");
  };
  return (
    <div className="mb-5 flex items-center justify-between gap-3">
      <div className="flex items-center gap-1">
        <MobileNavButton className="-ml-1.5" />
        <button type="button" onClick={back} className="inline-flex items-center gap-1.5 rounded-md text-[13px] text-muted hover:text-fg">
          <ArrowLeft className="size-3.5" /> Back
        </button>
        <span aria-hidden className="mx-1.5 text-subtle">
          /
        </span>
        <Link href="/outputs" className="text-[13px] text-muted hover:text-fg">
          Outputs
        </Link>
      </div>
      <NotificationBell />
    </div>
  );
}

function FileViewContent() {
  const params = useSearchParams();
  const artifact = params.get("artifact");
  const path = params.get("path");
  const source: FileSource | null = artifact ? { artifactId: artifact } : path ? { path } : null;
  return (
    <PageContainer>
      <TopBar />
      {source ? (
        <FileViewer key={artifact ? `a:${artifact}` : `p:${path}`} source={source} layout="page" />
      ) : (
        <Card>
          <EmptyState
            icon={<FileQuestion />}
            title="No file selected"
            description="Open a file from Outputs, a task or a chat message."
            action={<ButtonLink href="/outputs">Go to Outputs</ButtonLink>}
          />
        </Card>
      )}
    </PageContainer>
  );
}

function FileViewFallback() {
  return (
    <PageContainer>
      <Skeleton className="h-4 w-32" />
      <div className="mt-6 flex items-center gap-3">
        <Skeleton className="size-11 rounded-[10px]" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-6 w-1/2" />
          <Skeleton className="h-3 w-1/3" />
        </div>
      </div>
      <SkeletonText lines={8} className="mt-8" />
    </PageContainer>
  );
}

/** Full-page file viewer: `/outputs/view?artifact=<id>` or `/outputs/view?path=<workspace path>`. */
export default function FileViewPage() {
  // useSearchParams needs a Suspense boundary for prerendering.
  return (
    <Suspense fallback={<FileViewFallback />}>
      <FileViewContent />
    </Suspense>
  );
}
