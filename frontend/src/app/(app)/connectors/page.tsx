"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { IntegrationsTab } from "@/components/connectors/integrations-tab";
import { McpTab } from "@/components/connectors/mcp-tab";
import { ToolsTab } from "@/components/connectors/tools-tab";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Tabs } from "@/components/ui/tabs";

type Tab = "tools" | "integrations" | "mcp";

export default function ConnectorsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = raw === "integrations" || raw === "mcp" ? raw : "tools";

  return (
    <PageContainer>
      <PageHeader
        title="Connectors"
        description="What your agents can touch: built-in tools, your business systems, and any MCP server."
      />
      <Tabs
        aria-label="Connector type"
        value={tab}
        onValueChange={(t) => router.replace(t === "tools" ? pathname : `${pathname}?tab=${t}`, { scroll: false })}
        className="mb-6"
        items={[
          { value: "tools", label: "Built-in tools" },
          { value: "integrations", label: "Integrations" },
          { value: "mcp", label: "MCP servers" },
        ]}
      />
      {tab === "tools" && <ToolsTab />}
      {tab === "integrations" && <IntegrationsTab />}
      {tab === "mcp" && <McpTab />}
    </PageContainer>
  );
}
