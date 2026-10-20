"use client";

import { useParams } from "next/navigation";
import { AgentEditor } from "@/components/agents/agent-editor";
import { PageContainer } from "@/components/shell/page-header";
import { ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton, SkeletonText } from "@/components/ui/feedback";
import { useAgent } from "@/hooks/queries";
import { ApiError } from "@/lib/api";

export default function AgentPage() {
  const { id } = useParams<{ id: string }>();
  const { data: agent, isLoading, error, refetch, isPlaceholderData } = useAgent(id);

  if (isLoading && !agent) {
    return (
      <PageContainer>
        <div className="flex items-center gap-3">
          <Skeleton className="size-10 rounded-lg" />
          <div className="space-y-2">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-3 w-32" />
          </div>
        </div>
        <Skeleton className="mt-8 h-10 w-full" />
        <SkeletonText lines={8} className="mt-6" />
      </PageContainer>
    );
  }

  if (!agent) {
    const notFound = error instanceof ApiError && (error.status === 404 || error.status === 403);
    return (
      <PageContainer>
        <Card>
          {notFound ? (
            <EmptyState
              title="Agent not found"
              description="It may have been deleted, or it belongs to another workspace."
              action={<ButtonLink href="/agents">Back to agents</ButtonLink>}
            />
          ) : (
            <ErrorState error={error ?? new Error("Agent unavailable")} onRetry={() => refetch()} />
          )}
        </Card>
      </PageContainer>
    );
  }

  // Remount the editor once the full record (with stats) replaces the list placeholder.
  return <AgentEditor key={`${agent.id}-${isPlaceholderData ? "p" : "f"}`} agent={agent} />;
}
