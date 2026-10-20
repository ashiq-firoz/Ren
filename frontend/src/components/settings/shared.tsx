"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { api } from "@/lib/api";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Org, OrgSettings } from "@/lib/types";

export type OrgPatch = {
  name?: string;
  mode?: Org["mode"];
  settings?: Partial<OrgSettings> & { web_search_api_key?: string };
};

/** PATCH /orgs/current and refresh the cached org. */
export function useUpdateOrg(successMessage = "Settings saved") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: OrgPatch) => api.patch<Org>("/orgs/current", patch),
    onSuccess: (org) => {
      qc.setQueryData(qk.org, org);
      toast.success(successMessage);
    },
    meta: { errorTitle: "Couldn't save settings" },
  });
}

export function SettingsSection({
  title,
  description,
  children,
  footer,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="grid gap-5 p-5 md:grid-cols-[220px_minmax(0,1fr)] md:gap-8">
        <div>
          <h2 className="text-[14px] font-semibold text-fg">{title}</h2>
          {description && <p className="mt-1 text-[12.5px] leading-relaxed text-muted">{description}</p>}
        </div>
        <div className="min-w-0 space-y-4">{children}</div>
      </div>
      {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-surface px-5 py-3">{footer}</div>}
    </Card>
  );
}
