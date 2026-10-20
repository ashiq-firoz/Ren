"use client";

import { useMutation } from "@tanstack/react-query";
import { Check, Lock, Monitor, Moon, Radio, Sun } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SegmentedControl, Slider, Switch } from "@/components/ui/controls";
import { Notice, Skeleton } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { KV } from "@/components/ui/key-value";
import { useAlignmentStatus, useSystemSettings } from "@/hooks/queries";
import { useBrowserTimeZone } from "@/hooks/use-browser";
import { api } from "@/lib/api";
import { formatMs, formatTimeZone } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { Org, OrgSettings, User } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth, useRole } from "@/providers/auth";
import { useTheme, type ThemePreference } from "@/providers/theme";
import { SettingsSection, useUpdateOrg } from "./shared";

function ReadOnlyNotice() {
  const { isAdmin } = useRole();
  if (isAdmin) return null;
  return (
    <Notice tone="neutral" icon={<Lock />}>
      Only admins can change these settings.
    </Notice>
  );
}

/* ---------------- Retrieval ---------------- */

export function RetrievalTab({ org }: { org: Org }) {
  const { isAdmin } = useRole();
  const system = useSystemSettings();
  const update = useUpdateOrg("Retrieval settings saved");
  const [r, setR] = useState<OrgSettings["retrieval"]>(org.settings.retrieval);
  const set = <K extends keyof OrgSettings["retrieval"]>(k: K, v: OrgSettings["retrieval"][K]) => setR((x) => ({ ...x, [k]: v }));
  const dirty = JSON.stringify(r) !== JSON.stringify(org.settings.retrieval);
  return (
    <div className="space-y-5">
      <ReadOnlyNotice />
      <fieldset disabled={!isAdmin} className="min-w-0">
        <SettingsSection
          title="Retrieval (STAIR)"
          description="How documents are structured during ingestion and how agents retrieve from them."
          footer={
            isAdmin && (
              <Button variant="primary" loading={update.isPending} disabled={!dirty} onClick={() => update.mutate({ settings: { retrieval: r } })}>
                Save changes
              </Button>
            )
          }
        >
          <Switch
            checked={r.stair_enabled}
            onCheckedChange={(v) => set("stair_enabled", v)}
            label="Structure-aware retrieval"
            description="Pick relevant sections from each document's table of contents, fused with BM25 and vector search."
          />
          <Switch
            checked={r.generate_questions}
            onCheckedChange={(v) => set("generate_questions", v)}
            label="Generate section questions"
            description="Write synthetic questions per section during ingestion — improves recall and produces STAIR training data."
          />
          <Switch
            checked={r.structure_augmentation}
            onCheckedChange={(v) => set("structure_augmentation", v)}
            label="AI structure augmentation"
            description="Use the LLM to title sections of unstructured documents (no headings or native ToC)."
          />
          <Switch checked={r.rerank} onCheckedChange={(v) => set("rerank", v)} label="Rerank results" description="Re-score fused candidates with the cross-encoder." />
          <Field label="Results per query (top-k)">
            <Input
              type="number"
              min={1}
              max={50}
              value={r.top_k}
              onChange={(e) => set("top_k", Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
              wrapperClassName="max-w-[120px]"
              className="max-w-[120px]"
            />
          </Field>
        </SettingsSection>
      </fieldset>
      <SettingsSection title="Models" description="Configured on the server.">
        {system.isLoading || !system.data ? (
          <Skeleton className="h-16 w-full" />
        ) : (
          <div className="divide-y divide-border rounded-control border border-border px-3">
            <KV label="Embedding provider">{system.data.embedding.provider}</KV>
            <KV label="Embedding model">
              <span className="font-mono text-xs">{system.data.embedding.model}</span>
            </KV>
            <KV label="Dimensions">{system.data.embedding.dim}</KV>
            <KV label="Reranker">
              <span className="font-mono text-xs">{system.data.reranker.model ? `${system.data.reranker.provider} · ${system.data.reranker.model}` : "off"}</span>
            </KV>
          </div>
        )}
      </SettingsSection>
    </div>
  );
}

/* ---------------- Alignment ---------------- */

type AlignmentSettings = OrgSettings["alignment"];

/** Fill fields older backends may not send yet. */
function withAlignmentDefaults(a: Partial<AlignmentSettings>): AlignmentSettings {
  return {
    enabled: a.enabled ?? true,
    mode: a.mode ?? "post",
    block_threshold: a.block_threshold ?? 0.7,
    warn_threshold: a.warn_threshold ?? 0.45,
    judge_fallback: a.judge_fallback ?? true,
    laya_mode: a.laya_mode ?? "triage",
    escalate_threshold: a.escalate_threshold ?? 0.5,
  };
}

export function AlignmentTab({ org }: { org: Org }) {
  const { isAdmin } = useRole();
  const status = useAlignmentStatus();
  const update = useUpdateOrg("Alignment settings saved");
  const original = withAlignmentDefaults(org.settings.alignment ?? {});
  const [a, setA] = useState<AlignmentSettings>(original);
  const set = <K extends keyof AlignmentSettings>(k: K, v: AlignmentSettings[K]) => setA((x) => ({ ...x, [k]: v }));
  const dirty = JSON.stringify(a) !== JSON.stringify(original);
  const invalid = a.warn_threshold >= a.block_threshold;
  const laya = status.data?.laya;
  return (
    <div className="space-y-5">
      <ReadOnlyNotice />
      <fieldset disabled={!isAdmin} className="min-w-0">
        <SettingsSection
          title="Alignment"
          description="Every response and outbound action is checked against your policies."
          footer={
            isAdmin && (
              <Button variant="primary" loading={update.isPending} disabled={!dirty || invalid} onClick={() => update.mutate({ settings: { alignment: a } })}>
                Save changes
              </Button>
            )
          }
        >
          <Switch checked={a.enabled} onCheckedChange={(v) => set("enabled", v)} label="Policy checks enabled" description="Turning this off skips alignment entirely (not recommended)." />
          <Field
            label="Mode"
            hint={a.mode === "post" ? "Stream the reply, then check it; blocked replies are revised in place." : "Check before showing anything — safer, slower to first token."}
          >
            <SegmentedControl
              value={a.mode}
              onChange={(v) => set("mode", v)}
              options={[
                { value: "post", label: "Post (stream, then check)" },
                { value: "pre", label: "Pre (check, then show)" },
              ]}
            />
          </Field>
          <Field label="Warn threshold" labelAside={<span className="font-mono text-xs text-muted">{Math.round(a.warn_threshold * 100)}%</span>} hint="Violation probability at which a reply is flagged.">
            <Slider value={a.warn_threshold} onValueChange={(v) => set("warn_threshold", v)} min={0} max={1} step={0.01} aria-label="Warn threshold" />
          </Field>
          <Field
            label="Block threshold"
            labelAside={<span className="font-mono text-xs text-muted">{Math.round(a.block_threshold * 100)}%</span>}
            hint="Violation probability at which a reply is blocked (for blocking policies)."
            error={invalid ? "Block threshold must be higher than the warn threshold." : undefined}
          >
            <Slider value={a.block_threshold} onValueChange={(v) => set("block_threshold", v)} min={0} max={1} step={0.01} aria-label="Block threshold" />
          </Field>
        </SettingsSection>
      </fieldset>

      <fieldset disabled={!isAdmin} className="min-w-0">
        <SettingsSection
          title="Laya & the LLM judge"
          description="How Ren's fast aligner (Laya) and the slower LLM judge share the work."
          footer={
            isAdmin && (
              <Button variant="primary" loading={update.isPending} disabled={!dirty || invalid} onClick={() => update.mutate({ settings: { alignment: a } })}>
                Save changes
              </Button>
            )
          }
        >
          <Field
            label="Laya mode"
            hint={
              a.laya_mode === "triage"
                ? "Recommended. Laya screens every check in milliseconds; cases scoring at or above the escalation threshold go to the LLM judge to confirm."
                : "Laya's scores decide on their own — no judge round-trip. Use once Laya is fine-tuned on your policies (export training data from Policies)."
            }
          >
            <SegmentedControl
              value={a.laya_mode}
              onChange={(v) => set("laya_mode", v)}
              options={[
                { value: "triage", label: "Triage (Laya + judge)" },
                { value: "authoritative", label: "Authoritative (Laya decides)" },
              ]}
            />
          </Field>
          {a.laya_mode === "triage" && (
            <Field
              label="Escalation threshold"
              labelAside={<span className="font-mono text-xs text-muted">{Math.round(a.escalate_threshold * 100)}%</span>}
              hint="Laya score at or above which the LLM judge reviews the case. Lower sends more cases to the judge (safer, slower and costlier)."
            >
              <Slider value={a.escalate_threshold} onValueChange={(v) => set("escalate_threshold", v)} min={0} max={1} step={0.01} aria-label="Escalation threshold" />
            </Field>
          )}
          <Switch
            checked={a.judge_fallback}
            onCheckedChange={(v) => set("judge_fallback", v)}
            label="LLM judge fallback"
            description="Escalate uncertain Laya verdicts to an LLM judge for a second opinion."
          />
          {a.laya_mode === "triage" && !a.judge_fallback && (
            <p className="text-xs text-warning">Triage relies on the judge to confirm suspicious cases — with the judge off, Laya&apos;s score is used as-is.</p>
          )}
        </SettingsSection>
      </fieldset>

      <SettingsSection title="Laya status" description="Ren's fast policy aligner.">
        {status.isLoading || !laya ? (
          <Skeleton className="h-16 w-full" />
        ) : (
          <div className="space-y-3">
            <Badge tone={!laya.configured ? "neutral" : laya.reachable ? "success" : "danger"} size="md" dot pulse={laya.reachable} icon={<Radio />}>
              {!laya.configured ? "Not configured" : laya.reachable ? "Reachable" : "Unreachable"}
            </Badge>
            <div className="divide-y divide-border rounded-control border border-border px-3">
              <KV label="Mode">{laya.mode}</KV>
              <KV label="URL">
                <span className="font-mono text-xs">{laya.url ?? "—"}</span>
              </KV>
              <KV label="Latency">{formatMs(laya.latency_ms)}</KV>
              <KV label="Judge">{status.data?.judge.enabled ? "Enabled" : "Disabled"}</KV>
            </div>
            {laya.error && <p className="text-xs text-danger">{laya.error}</p>}
          </div>
        )}
      </SettingsSection>
    </div>
  );
}

/* ---------------- Web search ---------------- */

export function WebSearchTab({ org }: { org: Org }) {
  const { isAdmin } = useRole();
  const update = useUpdateOrg("Web search settings saved");
  const ws = org.settings.web_search;
  const [provider, setProvider] = useState(ws.provider);
  const [baseUrl, setBaseUrl] = useState(ws.base_url ?? "");
  const [key, setKey] = useState("");
  const needsKey = provider === "tavily" || provider === "brave";
  const dirty = provider !== ws.provider || (baseUrl || null) !== (ws.base_url ?? null) || key.length > 0;
  return (
    <div className="space-y-5">
      <ReadOnlyNotice />
      <fieldset disabled={!isAdmin} className="min-w-0">
        <SettingsSection
          title="Web search"
          description="The search engine agents use for research tasks."
          footer={
            isAdmin && (
              <Button
                variant="primary"
                loading={update.isPending}
                disabled={!dirty || (provider === "searxng" && !baseUrl.trim())}
                onClick={() =>
                  update.mutate(
                    {
                      settings: {
                        web_search: { provider, has_key: ws.has_key, base_url: provider === "searxng" ? baseUrl.trim() || null : null },
                        ...(key ? { web_search_api_key: key } : {}),
                      },
                    },
                    { onSuccess: () => setKey("") },
                  )
                }
              >
                Save changes
              </Button>
            )
          }
        >
          <Field label="Provider">
            <Select
              value={provider}
              onChange={(e) => setProvider(e.target.value as OrgSettings["web_search"]["provider"])}
              options={[
                { value: "duckduckgo", label: "DuckDuckGo (no key needed)" },
                { value: "tavily", label: "Tavily" },
                { value: "brave", label: "Brave Search" },
                { value: "searxng", label: "SearXNG (self-hosted)" },
              ]}
            />
          </Field>
          {provider === "searxng" && (
            <Field label="SearXNG URL" required>
              <Input type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://search.example.com" className="font-mono text-[13px]" />
            </Field>
          )}
          {(needsKey || ws.has_key) && (
            <Field label="API key" hint={ws.has_key ? "A key is saved. Enter a new one to replace it." : needsKey ? "Required for this provider." : undefined}>
              <Input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder={ws.has_key ? "•••• saved" : "Paste API key"} />
            </Field>
          )}
          {needsKey && !ws.has_key && !key && <p className="text-xs text-warning">Searches will fail until a key is saved.</p>}
        </SettingsSection>
      </fieldset>
    </div>
  );
}

/* ---------------- Profile ---------------- */

export function ProfileTab() {
  const { user, setUser } = useAuth();
  const timeZone = useBrowserTimeZone();
  const [name, setName] = useState(user?.name ?? "");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmPw, setConfirmPw] = useState("");

  const saveName = useMutation({
    mutationFn: () => api.patch<User>("/auth/me", { name: name.trim() }),
    onSuccess: (u) => {
      setUser(u);
      toast.success("Profile updated");
    },
    meta: { errorTitle: "Couldn't update your profile" },
  });
  const savePw = useMutation({
    mutationFn: () => api.patch<User>("/auth/me", { current_password: current, new_password: next }),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirmPw("");
      toast.success("Password changed");
    },
    meta: { errorTitle: "Couldn't change your password" },
  });
  const mismatch = confirmPw.length > 0 && confirmPw !== next;
  const short = next.length > 0 && next.length < 8;

  return (
    <div className="space-y-5">
      <SettingsSection
        title="Profile"
        description="How you appear to teammates and in audit logs."
        footer={
          <Button variant="primary" loading={saveName.isPending} disabled={!name.trim() || name.trim() === user?.name} onClick={() => saveName.mutate()}>
            Save
          </Button>
        }
      >
        <Field label="Name" required>
          <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
        </Field>
        <Field label="Email" hint="Contact an admin to change your sign-in email.">
          <Input value={user?.email ?? ""} readOnly disabled />
        </Field>
        <Field label="Time zone" hint="Detected from your browser; used for schedules and for “today” in Ren's replies.">
          <Input value={formatTimeZone(timeZone)} readOnly disabled />
        </Field>
      </SettingsSection>
      <SettingsSection
        title="Password"
        description="Use at least 8 characters."
        footer={
          <Button variant="primary" loading={savePw.isPending} disabled={!current || next.length < 8 || next !== confirmPw} onClick={() => savePw.mutate()}>
            Change password
          </Button>
        }
      >
        <Field label="Current password">
          <PasswordInput value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
        </Field>
        <Field label="New password" error={short ? "Use at least 8 characters." : undefined}>
          <PasswordInput value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" invalid={short} />
        </Field>
        <Field label="Confirm new password" error={mismatch ? "Passwords don't match." : undefined}>
          <PasswordInput value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} autoComplete="new-password" invalid={mismatch} />
        </Field>
      </SettingsSection>
    </div>
  );
}

/* ---------------- Appearance ---------------- */

const THEMES: { value: ThemePreference; label: string; icon: React.ReactNode; swatch: [string, string, string] }[] = [
  { value: "light", label: "Beige", icon: <Sun />, swatch: ["#EFE7DA", "#F8F6F1", "#0F2D24"] },
  { value: "dark", label: "Dark", icon: <Moon />, swatch: ["#0B1612", "#10201A", "#E8DCC8"] },
  { value: "system", label: "System", icon: <Monitor />, swatch: ["#EFE7DA", "#0B1612", "#1F5A45"] },
];

export function AppearanceTab() {
  const { theme, setTheme } = useTheme();
  return (
    <SettingsSection title="Theme" description="f69 Labs beige by default. Switch to dark, or follow your system.">
      <div role="radiogroup" aria-label="Theme" className="grid gap-3 sm:grid-cols-3">
        {THEMES.map((t) => (
          <button
            key={t.value}
            type="button"
            role="radio"
            aria-checked={theme === t.value}
            onClick={() => setTheme(t.value)}
            className={cn(
              "overflow-hidden rounded-card border text-left transition-colors",
              theme === t.value ? "border-fg" : "border-border hover:border-border-strong",
            )}
          >
            <div className="flex h-20 items-end gap-1.5 p-3" style={{ background: t.swatch[0] }}>
              <span className="h-10 flex-1 rounded-md" style={{ background: t.swatch[1], border: `1px solid ${t.swatch[2]}22` }} />
              <span className="size-4 rounded-full" style={{ background: t.swatch[2] }} />
            </div>
            <div className="flex items-center justify-between border-t border-border bg-surface px-3 py-2">
              <span className="flex items-center gap-2 text-[13px] font-medium text-fg [&_svg]:size-3.5">
                {t.icon}
                {t.label}
              </span>
              {theme === t.value && <Check className="size-4 text-fg" />}
            </div>
          </button>
        ))}
      </div>
    </SettingsSection>
  );
}
