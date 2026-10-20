"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, Pencil, Plus, Play, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, IconButton } from "@/components/ui/button";
import { Checkbox, SegmentedControl } from "@/components/ui/controls";
import { AppIcon } from "@/components/ui/icon";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Markdown } from "@/components/ui/markdown";
import { Dialog } from "@/components/ui/overlay";
import { useAgents, useConversations, useToolCatalog } from "@/hooks/queries";
import { api } from "@/lib/api";
import { upsertTask } from "@/lib/cache";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Skill, SkillInput, SkillRunResponse } from "@/lib/types";
import { slugify, uid } from "@/lib/utils";

/* ------------------------------------------------------------------ */
/* Run                                                                 */
/* ------------------------------------------------------------------ */

export function RunSkillDialog({ skill, onOpenChange }: { skill: Skill; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const router = useRouter();
  const conversations = useConversations();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(skill.inputs.map((i) => [i.key, i.type === "select" && i.options?.length ? i.options[0] : ""])),
  );
  const [conversationId, setConversationId] = useState("");
  const missing = skill.inputs.filter((i) => i.required && !String(values[i.key] ?? "").trim());

  const run = useMutation({
    mutationFn: () => {
      const inputs: Record<string, unknown> = {};
      for (const i of skill.inputs) {
        const v = values[i.key];
        if (v === undefined || v === "") continue;
        inputs[i.key] = i.type === "number" ? Number(v) : v;
      }
      return api.post<SkillRunResponse>(`/skills/${skill.id}/run`, { inputs, conversation_id: conversationId || undefined });
    },
    onSuccess: (res) => {
      upsertTask(qc, res.task);
      void qc.invalidateQueries({ queryKey: qk.conversationLists });
      toast.success(`${skill.name} is running`, {
        description: `${res.task.agent_name} picked up “${res.task.title}”.`,
        action: { label: "Open conversation", href: `/chat/${res.conversation_id}` },
        duration: 8000,
      });
      onOpenChange(false);
      if (conversationId) router.push(`/chat/${res.conversation_id}`);
    },
    meta: { errorTitle: "Couldn't run the skill" },
  });

  const field = (input: SkillInput) => {
    const common = {
      value: values[input.key] ?? "",
      placeholder: input.placeholder,
    };
    const set = (v: string) => setValues((s) => ({ ...s, [input.key]: v }));
    switch (input.type) {
      case "textarea":
        return <Textarea autosize minRows={3} {...common} onChange={(e) => set(e.target.value)} />;
      case "number":
        return <Input type="number" {...common} onChange={(e) => set(e.target.value)} />;
      case "date":
        return <Input type="date" {...common} onChange={(e) => set(e.target.value)} />;
      case "select":
        return (
          <Select value={common.value} onChange={(e) => set(e.target.value)}>
            {(input.options ?? []).map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </Select>
        );
      default:
        return <Input {...common} onChange={(e) => set(e.target.value)} />;
    }
  };

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={`Run ${skill.name}`}
      description={skill.description}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" icon={<Play className="size-4" />} loading={run.isPending} disabled={missing.length > 0} onClick={() => run.mutate()}>
            Run skill
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (missing.length === 0) run.mutate();
        }}
      >
        {skill.inputs.length === 0 && <p className="text-[13px] text-muted">This skill doesn&apos;t need any inputs.</p>}
        {skill.inputs.map((i) => (
          <Field key={i.key} label={i.label || i.key} required={i.required}>
            {field(i)}
          </Field>
        ))}
        <Field label="Post updates to" hint="Results and approvals for this run appear in the chosen conversation.">
          <Select value={conversationId} onChange={(e) => setConversationId(e.target.value)}>
            <option value="">A new conversation</option>
            {(conversations.data?.items ?? []).slice(0, 20).map((c) => (
              <option key={c.id} value={c.id}>
                {c.title || "Untitled conversation"}
              </option>
            ))}
          </Select>
        </Field>
      </form>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/* Create / edit                                                       */
/* ------------------------------------------------------------------ */

const INPUT_TYPES: SkillInput["type"][] = ["text", "textarea", "number", "date", "select"];

interface InputRow extends SkillInput {
  _id: string;
  optionsText: string;
}

const toRow = (i: SkillInput): InputRow => ({ ...i, _id: uid("in-"), optionsText: (i.options ?? []).join(", ") });

export function SkillEditorDialog({
  skill,
  categories,
  onOpenChange,
}: {
  skill: Skill | null;
  categories: string[];
  onOpenChange: (o: boolean) => void;
}) {
  const qc = useQueryClient();
  const agents = useAgents();
  const catalog = useToolCatalog();
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [category, setCategory] = useState(skill?.category ?? "");
  const [icon, setIcon] = useState(skill?.icon ?? "sparkles");
  const [agentId, setAgentId] = useState(skill?.agent_id ?? "");
  const [toolGroups, setToolGroups] = useState<string[]>(skill?.tool_groups ?? []);
  const [inputs, setInputs] = useState<InputRow[]>(() => (skill?.inputs ?? []).map(toRow));
  const [instructions, setInstructions] = useState(skill?.instructions ?? "");
  const [preview, setPreview] = useState<"write" | "preview">("write");

  const keys = inputs.map((i) => i.key.trim());
  const dupKey = keys.some((k, idx) => k && keys.indexOf(k) !== idx);
  const valid = name.trim() && !dupKey && inputs.every((i) => i.key.trim() && i.label.trim());

  const save = useMutation({
    mutationFn: () => {
      const body: Partial<Skill> = {
        name: name.trim(),
        description: description.trim(),
        category: category.trim() || "custom",
        icon: icon.trim() || "sparkles",
        agent_id: agentId || null,
        tool_groups: toolGroups,
        instructions,
        inputs: inputs.map((i) => ({
          key: i.key.trim(),
          label: i.label.trim(),
          type: i.type,
          required: i.required,
          placeholder: i.placeholder?.trim() || undefined,
          options:
            i.type === "select"
              ? i.optionsText
                  .split(",")
                  .map((o) => o.trim())
                  .filter(Boolean)
              : undefined,
        })),
      };
      if (!skill) body.slug = slugify(name);
      return skill ? api.patch<Skill>(`/skills/${skill.id}`, body) : api.post<Skill>("/skills", body);
    },
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: qk.skills });
      toast.success(skill ? "Skill saved" : "Skill created", { description: s.name });
      onOpenChange(false);
    },
    meta: { errorTitle: "Couldn't save the skill" },
  });

  const updateRow = (id: string, patch: Partial<InputRow>) => setInputs((rows) => rows.map((r) => (r._id === id ? { ...r, ...patch } : r)));

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      size="lg"
      title={skill ? `Edit ${skill.name}` : "New skill"}
      description="A skill is a reusable playbook: inputs, instructions, and the agent and tools best suited to run it."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}>
            {skill ? "Save skill" : "Create skill"}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" required>
            <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Weekly competitor digest" />
          </Field>
          <Field label="Category">
            <Input list="skill-categories" value={category} onChange={(e) => setCategory(e.target.value)} placeholder="research" />
          </Field>
          <datalist id="skill-categories">
            {categories.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </div>
        <Field label="Description">
          <Textarea autosize minRows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What it produces and when to use it." />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Icon" hint="lucide icon name">
            <Input value={icon} onChange={(e) => setIcon(e.target.value)} leftIcon={<AppIcon name={icon} fallback="sparkles" />} className="font-mono" />
          </Field>
          <Field label="Preferred agent">
            <Select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              <option value="">Let Ren decide</option>
              {(agents.data?.items ?? [])
                .filter((a) => !a.is_orchestrator)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
            </Select>
          </Field>
        </div>

        <Field label="Tool groups">
          <div className="grid max-h-44 gap-2.5 overflow-y-auto rounded-control border border-border p-3 sm:grid-cols-2">
            {(catalog.data?.items ?? []).map((g) => (
              <Checkbox
                key={g.id}
                checked={toolGroups.includes(g.id)}
                onCheckedChange={(v) => setToolGroups((t) => (v ? [...t, g.id] : t.filter((x) => x !== g.id)))}
                label={g.name}
              />
            ))}
            {catalog.isLoading && <p className="text-xs text-muted">Loading tools…</p>}
          </div>
        </Field>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[13px] font-medium text-fg">Inputs</p>
            <Button
              size="xs"
              variant="secondary"
              icon={<Plus className="size-3" />}
              onClick={() => setInputs((rows) => [...rows, toRow({ key: "", label: "", type: "text", required: false })])}
            >
              Add input
            </Button>
          </div>
          {inputs.length === 0 ? (
            <p className="rounded-control border border-dashed border-border px-3 py-4 text-center text-xs text-muted">
              No inputs — the skill runs as-is.
            </p>
          ) : (
            <div className="space-y-2">
              {inputs.map((row) => (
                <div key={row._id} className="rounded-control border border-border bg-surface-2/40 p-3">
                  <div className="grid gap-2 sm:grid-cols-[1fr_1.4fr_130px_auto]">
                    <Input
                      inputSize="sm"
                      value={row.key}
                      onChange={(e) => updateRow(row._id, { key: e.target.value.replace(/\s+/g, "_") })}
                      placeholder="key"
                      aria-label="Input key"
                      className="font-mono"
                      invalid={!!row.key && keys.filter((k) => k === row.key.trim()).length > 1}
                    />
                    <Input inputSize="sm" value={row.label} onChange={(e) => updateRow(row._id, { label: e.target.value })} placeholder="Label" aria-label="Input label" />
                    <Select
                      selectSize="sm"
                      value={row.type}
                      onChange={(e) => updateRow(row._id, { type: e.target.value as SkillInput["type"] })}
                      aria-label="Input type"
                      options={INPUT_TYPES.map((t) => ({ value: t, label: t }))}
                    />
                    <IconButton label="Remove input" icon={<Trash2 />} onClick={() => setInputs((rows) => rows.filter((r) => r._id !== row._id))} />
                  </div>
                  <div className="mt-2 grid items-center gap-2 sm:grid-cols-[1fr_1fr_auto]">
                    <Input
                      inputSize="sm"
                      value={row.placeholder ?? ""}
                      onChange={(e) => updateRow(row._id, { placeholder: e.target.value })}
                      placeholder="Placeholder (optional)"
                      aria-label="Placeholder"
                    />
                    {row.type === "select" ? (
                      <Input
                        inputSize="sm"
                        value={row.optionsText}
                        onChange={(e) => updateRow(row._id, { optionsText: e.target.value })}
                        placeholder="Options, comma separated"
                        aria-label="Options"
                      />
                    ) : (
                      <span />
                    )}
                    <Checkbox checked={row.required} onCheckedChange={(v) => updateRow(row._id, { required: v })} label="Required" />
                  </div>
                </div>
              ))}
              {dupKey && <p className="text-xs text-danger">Input keys must be unique.</p>}
            </div>
          )}
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[13px] font-medium text-fg">Instructions</p>
            <SegmentedControl
              size="sm"
              value={preview}
              onChange={setPreview}
              aria-label="Instructions view"
              options={[
                { value: "write", label: "Write", icon: <Pencil /> },
                { value: "preview", label: "Preview", icon: <Eye /> },
              ]}
            />
          </div>
          {preview === "write" ? (
            <Textarea
              mono
              autosize
              minRows={10}
              maxRows={24}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder={"## Goal\nProduce a weekly digest of competitor news.\n\n## Steps\n1. Search the web for {{competitors}}…\n2. Summarize…\n\n## Output\nA markdown brief with sources."}
              aria-label="Instructions (markdown)"
            />
          ) : (
            <div className="min-h-[200px] rounded-control border border-border bg-surface px-4 py-3">
              {instructions.trim() ? <Markdown content={instructions} size="sm" /> : <p className="text-[13px] text-muted">Nothing to preview.</p>}
            </div>
          )}
          <p className="mt-1 text-[11.5px] text-subtle">Markdown. Reference inputs with their keys, e.g. {"{{topic}}"}.</p>
        </div>
      </div>
    </Dialog>
  );
}
