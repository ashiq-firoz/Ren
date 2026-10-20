"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Button, ButtonLink } from "@/components/ui/button";

export default function AppError({
  error,
  retry,
  reset,
}: {
  error: Error & { digest?: string };
  retry?: () => void;
  reset?: () => void;
}) {
  useEffect(() => {
    console.error("[ren] page crashed", error);
  }, [error]);

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-20 text-center">
      <div className="flex size-11 items-center justify-center rounded-xl tint-danger">
        <AlertTriangle className="size-5" />
      </div>
      <h1 className="font-serif-display text-[30px] leading-tight text-fg">This page hit a snag</h1>
      <p className="max-w-md text-[13px] text-muted">
        {error.message || "An unexpected error occurred while rendering this page."}
        {error.digest && <span className="mt-1 block font-mono text-[11px] text-subtle">ref {error.digest}</span>}
      </p>
      <div className="mt-2 flex gap-2">
        <ButtonLink href="/chat" variant="ghost">
          Go to chat
        </ButtonLink>
        <Button variant="primary" icon={<RefreshCw className="size-4" />} onClick={() => (retry ?? reset)?.()}>
          Try again
        </Button>
      </div>
    </div>
  );
}
