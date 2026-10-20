"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox, SegmentedControl } from "@/components/ui/controls";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Dialog } from "@/components/ui/overlay";
import { useMembers } from "@/hooks/queries";
import { api } from "@/lib/api";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Collection } from "@/lib/types";

export function CollectionDialog({
  open,
  onOpenChange,
  collection,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  collection?: Collection | null;
  onSaved?: (c: Collection) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(collection?.name ?? "");
  const [description, setDescription] = useState(collection?.description ?? "");
  const [access, setAccess] = useState<"org" | "restricted">(collection?.access ?? "org");
  const [memberIds, setMemberIds] = useState<string[]>(collection?.member_ids ?? []);
  const members = useMembers(open && access === "restricted");

  const save = useMutation({
    mutationFn: () => {
      const body = { name: name.trim(), description: description.trim(), access, member_ids: access === "restricted" ? memberIds : [] };
      return collection ? api.patch<Collection>(`/collections/${collection.id}`, body) : api.post<Collection>("/collections", body);
    },
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: qk.collections });
      toast.success(collection ? "Collection updated" : "Collection created", { description: c.name });
      onSaved?.(c);
      onOpenChange(false);
    },
    meta: { errorTitle: "Couldn't save the collection" },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={collection ? "Edit collection" : "New collection"}
      description="Collections group documents and control who (and which agents) can search them."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!name.trim()} onClick={() => save.mutate()}>
            {collection ? "Save" : "Create collection"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name" required>
          <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="HR Policies" />
        </Field>
        <Field label="Description">
          <Textarea autosize minRows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Employee handbook, leave and benefits policies." />
        </Field>
        <Field label="Access">
          <SegmentedControl
            fullWidth
            value={access}
            onChange={setAccess}
            options={[
              { value: "org", label: "Whole workspace" },
              { value: "restricted", label: "Restricted" },
            ]}
          />
        </Field>
        {access === "restricted" && (
          <Field label="Members with access" hint="Admins always have access.">
            <div className="max-h-56 space-y-2.5 overflow-y-auto rounded-control border border-border p-3">
              {members.isLoading && <p className="text-xs text-muted">Loading members…</p>}
              {(members.data?.items ?? []).map((m) => (
                <Checkbox
                  key={m.user_id}
                  checked={memberIds.includes(m.user_id)}
                  onCheckedChange={(v) => setMemberIds((ids) => (v ? [...ids, m.user_id] : ids.filter((x) => x !== m.user_id)))}
                  label={m.name}
                  description={m.email}
                />
              ))}
            </div>
          </Field>
        )}
      </div>
    </Dialog>
  );
}
