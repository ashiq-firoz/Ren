"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { Button, IconButton } from "@/components/ui/button";
import { Checkbox, SegmentedControl, Switch } from "@/components/ui/controls";
import { Field, Input, Textarea } from "@/components/ui/input";
import { confirm, Drawer } from "@/components/ui/overlay";
import { useAgents } from "@/hooks/queries";
import { api } from "@/lib/api";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Policy } from "@/lib/types";
import { cn, uid } from "@/lib/utils";

const CATEGORIES = ["confidentiality", "tone", "compliance", "safety", "custom"];

interface Line {
  id: string;
  text: string;
}

const lines = (xs: string[]): Line[] => xs.map((text) => ({ id: uid("l-"), text }));

function regexError(p: string): string | null {
  if (!p.trim()) return null;
  try {
    new RegExp(p);
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : "Invalid pattern";
  }
}

export function PolicyEditor({ policy, onClose, readOnly }: { policy: Policy | null; onClose: () => void; readOnly?: boolean }) {
  const qc = useQueryClient();
  const agents = useAgents();
  const [name, setName] = useState(policy?.name ?? "");
  const [description, setDescription] = useState(policy?.description ?? "");
  const [category, setCategory] = useState(policy?.category ?? "custom");
  const [severity, setSeverity] = useState<Policy["severity"]>(policy?.severity ?? "warn");
  const [scope, setScope] = useState<Policy["scope"]>(policy?.scope ?? "org");
  const [appliesTo, setAppliesTo] = useState<Policy["applies_to"]>(policy?.applies_to ?? "all");
  const [agentIds, setAgentIds] = useState<string[]>(policy?.agent_ids ?? []);
  const [rules, setRules] = useState<Line[]>(() => lines(policy?.rules?.length ? policy.rules : [""]));
  const [patterns, setPatterns] = useState<Line[]>(() => lines(policy?.patterns ?? []));
  const [enabled, setEnabled] = useState(policy?.enabled ?? true);

  const cleanRules = rules.map((r) => r.text.trim()).filter(Boolean);
  const cleanPatterns = patterns.map((p) => p.text.trim()).filter(Boolean);
  // Patterns run on the server's regex engine; a JS parse error is only a warning.
  const valid = name.trim() && (cleanRules.length > 0 || cleanPatterns.length > 0) && (scope === "org" || agentIds.length > 0);

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        description: description.trim(),
        category: category.trim() || "custom",
        severity,
        scope,
        applies_to: appliesTo,
        agent_ids: scope === "agent" ? agentIds : [],
        rules: cleanRules,
        patterns: cleanPatterns,
        enabled,
      };
      return policy ? api.patch<Policy>(`/policies/${policy.id}`, body) : api.post<Policy>("/policies", body);
    },
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: qk.policies });
      toast.success(policy ? "Policy saved" : "Policy created", { description: p.name });
      onClose();
    },
    meta: { errorTitle: "Couldn't save the policy" },
  });

  const remove = useMutation({
    mutationFn: () => api.delete(`/policies/${policy!.id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.policies });
      toast.success("Policy deleted");
      onClose();
    },
    meta: { errorTitle: "Couldn't delete the policy" },
  });

  return (
    <Drawer
      open
      onOpenChange={(o) => !o && onClose()}
      width={600}
      title={policy ? policy.name : "New policy"}
      description="Each rule is a short, atomic statement. Ren's aligner (Laya) scores every rule against responses and outbound actions."
      footer={
        readOnly ? (
          <Button onClick={onClose}>Close</Button>
        ) : (
          <>
            {policy && (
              <Button
                variant="danger-outline"
                className="mr-auto"
                icon={<Trash2 className="size-4" />}
                loading={remove.isPending}
                onClick={async () => {
                  const ok = await confirm({ title: `Delete “${policy.name}”?`, confirmLabel: "Delete policy", danger: true });
                  if (ok) remove.mutate();
                }}
              >
                Delete
              </Button>
            )}
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}>
              {policy ? "Save policy" : "Create policy"}
            </Button>
          </>
        )
      }
    >
      <fieldset disabled={readOnly} className="space-y-5">
        <Switch checked={enabled} onCheckedChange={setEnabled} label="Enabled" description="Disabled policies are kept but not enforced." />
        <Field label="Name" required>
          <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="No confidential financials" />
        </Field>
        <Field label="Description">
          <Textarea autosize minRows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Why this policy exists and what it protects." />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Category">
            <Input list="policy-categories" value={category} onChange={(e) => setCategory(e.target.value)} />
          </Field>
          <datalist id="policy-categories">
            {CATEGORIES.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          <Field label="Severity" hint={severity === "block" ? "Violations are blocked (and revised in post mode)." : "Violations are allowed but flagged."}>
            <SegmentedControl
              fullWidth
              value={severity}
              onChange={setSeverity}
              options={[
                { value: "warn", label: "Warn" },
                { value: "block", label: "Block" },
              ]}
            />
          </Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Applies to">
            <SegmentedControl
              fullWidth
              size="sm"
              value={appliesTo}
              onChange={setAppliesTo}
              options={[
                { value: "responses", label: "Responses" },
                { value: "outbound", label: "Outbound" },
                { value: "all", label: "All" },
              ]}
            />
          </Field>
          <Field label="Scope">
            <SegmentedControl
              fullWidth
              size="sm"
              value={scope}
              onChange={setScope}
              options={[
                { value: "org", label: "Whole workspace" },
                { value: "agent", label: "Specific agents" },
              ]}
            />
          </Field>
        </div>
        {scope === "agent" && (
          <Field label="Agents" required error={agentIds.length === 0 ? "Pick at least one agent." : undefined}>
            <div className="grid max-h-48 gap-2.5 overflow-y-auto rounded-control border border-border p-3 sm:grid-cols-2">
              {(agents.data?.items ?? []).map((a) => (
                <Checkbox
                  key={a.id}
                  checked={agentIds.includes(a.id)}
                  onCheckedChange={(v) => setAgentIds((ids) => (v ? [...ids, a.id] : ids.filter((x) => x !== a.id)))}
                  label={a.name}
                />
              ))}
            </div>
          </Field>
        )}

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[13px] font-medium text-fg">
              Rules <span className="font-normal text-muted">· each becomes one Laya question</span>
            </p>
            <Button size="xs" variant="secondary" icon={<Plus className="size-3" />} onClick={() => setRules((r) => [...r, { id: uid("l-"), text: "" }])}>
              Add rule
            </Button>
          </div>
          <ol className="space-y-2">
            {rules.map((r, i) => (
              <li key={r.id} className="flex items-start gap-2">
                <span className="mt-2 w-5 shrink-0 text-right text-xs text-subtle tabular-nums">{i + 1}.</span>
                <Textarea
                  autosize
                  minRows={1}
                  maxRows={4}
                  value={r.text}
                  onChange={(e) => setRules((rs) => rs.map((x) => (x.id === r.id ? { ...x, text: e.target.value } : x)))}
                  placeholder="Never disclose unreleased revenue figures."
                  aria-label={`Rule ${i + 1}`}
                  className="flex-1 text-[13px]"
                />
                <IconButton label={`Remove rule ${i + 1}`} icon={<X />} className="mt-1" onClick={() => setRules((rs) => rs.filter((x) => x.id !== r.id))} />
              </li>
            ))}
          </ol>
          {rules.some((r) => r.text.length > 200) && <p className="mt-1.5 text-xs text-warning">Shorter, atomic rules are scored more reliably.</p>}
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[13px] font-medium text-fg">
              Patterns <span className="font-normal text-muted">· regex, a match is an instant violation</span>
            </p>
            <Button size="xs" variant="secondary" icon={<Plus className="size-3" />} onClick={() => setPatterns((p) => [...p, { id: uid("l-"), text: "" }])}>
              Add pattern
            </Button>
          </div>
          {patterns.length === 0 ? (
            <p className="text-xs text-muted">Optional — e.g. credit card numbers or internal project codenames.</p>
          ) : (
            <div className="space-y-2">
              {patterns.map((p) => {
                const err = regexError(p.text);
                return (
                  <div key={p.id}>
                    <div className="flex items-center gap-2">
                      <Input
                        inputSize="sm"
                        value={p.text}
                        onChange={(e) => setPatterns((ps) => ps.map((x) => (x.id === p.id ? { ...x, text: e.target.value } : x)))}
                        placeholder={String.raw`\b(?:\d[ -]*?){13,16}\b`}
                        aria-label="Pattern"
                        className="font-mono"
                        wrapperClassName="flex-1"
                      />
                      <IconButton label="Remove pattern" icon={<X />} onClick={() => setPatterns((ps) => ps.filter((x) => x.id !== p.id))} />
                    </div>
                    {err && <p className={cn("mt-1 text-xs text-warning")}>Looks invalid: {err}</p>}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </fieldset>
    </Drawer>
  );
}
