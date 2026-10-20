"use client";

import { ExternalLink, Plus, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton, Button } from "@/components/ui/button";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import type { ConnectorField } from "@/lib/types";
import { uid } from "@/lib/utils";

/* ------------------------------------------------------------------ */
/* Form generated from ConnectorKind.fields                            */
/* ------------------------------------------------------------------ */

export const SECRET_MASK = "••••";

export function isMasked(v: unknown) {
  return typeof v === "string" && /^[•*]{3,}$/.test(v);
}

export function DynamicFields({
  fields,
  values,
  onChange,
  editing,
  savedSecrets,
}: {
  fields: ConnectorField[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  /** When editing, secret fields left empty keep their stored value. */
  editing?: boolean;
  savedSecrets?: Set<string>;
}) {
  return (
    <div className="space-y-4">
      {fields.map((f) => {
        const value = values[f.key] ?? "";
        const keepsSecret = editing && f.secret && savedSecrets?.has(f.key);
        const placeholder = keepsSecret ? `${SECRET_MASK} saved — leave empty to keep` : f.placeholder;
        const required = f.required && !keepsSecret;
        let control: ReactNode;
        if (f.type === "select") {
          control = (
            <Select value={value} onChange={(e) => onChange(f.key, e.target.value)} placeholder={f.required ? undefined : "—"}>
              {(f.options ?? []).map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </Select>
          );
        } else if (f.type === "textarea") {
          control = (
            <Textarea autosize minRows={3} mono={f.secret} value={value} placeholder={placeholder} onChange={(e) => onChange(f.key, e.target.value)} />
          );
        } else {
          control = (
            <Input
              type={f.type === "password" || f.secret ? "password" : f.type === "number" ? "number" : f.type === "url" ? "url" : "text"}
              autoComplete="off"
              value={value}
              placeholder={placeholder}
              onChange={(e) => onChange(f.key, e.target.value)}
            />
          );
        }
        return (
          <Field key={f.key} label={f.label} required={required} hint={f.help}>
            {control}
          </Field>
        );
      })}
    </div>
  );
}

export function missingRequired(fields: ConnectorField[], values: Record<string, string>, savedSecrets?: Set<string>) {
  return fields.filter((f) => f.required && !String(values[f.key] ?? "").trim() && !(f.secret && savedSecrets?.has(f.key)));
}

export function DocsLink({ href }: { href?: string }) {
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg">
      Setup guide <ExternalLink className="size-3" />
    </a>
  );
}

/* ------------------------------------------------------------------ */
/* Key / value rows (env vars, headers)                                */
/* ------------------------------------------------------------------ */

export interface KVRow {
  id: string;
  key: string;
  value: string;
  /** Existing key whose value is stored server-side (not returned). */
  stored?: boolean;
  label?: string;
  placeholder?: string;
  help?: string;
  required?: boolean;
}

export const newKVRow = (patch: Partial<KVRow> = {}): KVRow => ({ id: uid("kv-"), key: "", value: "", ...patch });

export function KeyValueEditor({
  rows,
  onChange,
  keyPlaceholder = "KEY",
  valuePlaceholder = "value",
  addLabel = "Add variable",
  secretValues = true,
}: {
  rows: KVRow[];
  onChange: (rows: KVRow[]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  addLabel?: string;
  secretValues?: boolean;
}) {
  const update = (id: string, patch: Partial<KVRow>) => onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.id}>
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto] items-center gap-2">
            <Input
              inputSize="sm"
              value={r.key}
              onChange={(e) => update(r.id, { key: e.target.value.trim() })}
              placeholder={keyPlaceholder}
              aria-label="Name"
              className="font-mono"
              readOnly={!!r.label}
            />
            <Input
              inputSize="sm"
              type={secretValues ? "password" : "text"}
              autoComplete="off"
              value={r.value}
              onChange={(e) => update(r.id, { value: e.target.value })}
              placeholder={r.stored ? `${SECRET_MASK} saved — leave empty to keep` : r.placeholder ?? valuePlaceholder}
              aria-label={`Value for ${r.key || "variable"}`}
            />
            <IconButton label="Remove" icon={<Trash2 />} onClick={() => onChange(rows.filter((x) => x.id !== r.id))} />
          </div>
          {(r.label || r.help) && (
            <p className="mt-1 text-[11.5px] text-muted">
              {r.label}
              {r.required && <span className="text-danger"> *</span>}
              {r.help ? ` — ${r.help}` : ""}
            </p>
          )}
        </div>
      ))}
      <Button size="xs" variant="ghost" icon={<Plus className="size-3" />} onClick={() => onChange([...rows, newKVRow()])}>
        {addLabel}
      </Button>
    </div>
  );
}

/** Rows → object with only entered values. */
export function kvToRecord(rows: KVRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (k && r.value !== "") out[k] = r.value;
  }
  return out;
}
