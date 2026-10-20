"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Cpu, KeyRound, Lock, Pencil, Plus, RefreshCw, Star, Trash2, XCircle, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/controls";
import { EmptyState, ErrorState, Notice, Skeleton } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/input";
import { confirm, Dialog } from "@/components/ui/overlay";
import { useProviderModels, useProviders } from "@/hooks/queries";
import { api, errorMessage } from "@/lib/api";
import { removeFromList, upsertInList } from "@/lib/cache";
import { formatMs } from "@/lib/format";
import { fallbackModelsOf, parseModelList } from "@/lib/providers";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { ListResponse, Org, Provider, ProviderKind, ProviderModel, ProviderTestResult } from "@/lib/types";
import { useRole } from "@/providers/auth";
import { SettingsSection, useUpdateOrg } from "./shared";

const KINDS: {
  value: ProviderKind;
  label: string;
  baseUrl?: string;
  needsUrl?: boolean;
  needsKey?: boolean;
  defaultModel?: string;
  keyPlaceholder?: string;
}[] = [
  { value: "openrouter", label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", needsKey: true, defaultModel: "openai/gpt-4.1-mini", keyPlaceholder: "sk-or-…" },
  {
    value: "google",
    label: "Google AI Studio",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    needsKey: true,
    defaultModel: "gemma-4-26b-a4b-it",
    keyPlaceholder: "AIza…",
  },
  { value: "vllm", label: "vLLM", needsUrl: true },
  { value: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1", needsKey: true, defaultModel: "gpt-4.1-mini", keyPlaceholder: "sk-…" },
  { value: "custom", label: "Custom (OpenAI-compatible)", needsUrl: true },
  { value: "mock", label: "Mock (testing)", defaultModel: "mock-1" },
];

const kindMeta = (k: ProviderKind) => KINDS.find((x) => x.value === k) ?? KINDS[0];
const kindLabel = (k: ProviderKind) => kindMeta(k).label.split(" (")[0];

function ProviderDialog({ provider, onClose }: { provider: Provider | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<ProviderKind>(provider?.kind ?? "openrouter");
  const [name, setName] = useState(provider?.name ?? "OpenRouter");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [model, setModel] = useState(provider?.default_model ?? "");
  const [manualModel, setManualModel] = useState(false);
  const [tools, setTools] = useState(provider?.supports_tools ?? true);
  const [isDefault, setIsDefault] = useState(provider?.is_default ?? false);
  const [fallbacks, setFallbacks] = useState(fallbackModelsOf(provider).join(", "));
  const meta = kindMeta(kind);
  const kindChanged = !!provider && kind !== provider.kind;
  // A stored key only belongs to the provider type it was saved for.
  const storedKeyUsable = !!provider?.has_key && !kindChanged && !clearKey;
  const keyMissing = !!meta.needsKey && !apiKey.trim() && !storedKeyUsable;
  const valid = name.trim() && model.trim() && (!meta.needsUrl || baseUrl.trim()) && !(kindChanged && keyMissing);

  const models = useMutation({
    mutationFn: () =>
      api.post<ListResponse<ProviderModel>>("/providers/models/preview", {
        kind,
        base_url: baseUrl.trim() || undefined,
        api_key: apiKey.trim() || undefined,
        provider_id: storedKeyUsable ? provider?.id : undefined,
      }),
    meta: { silent: true },
  });
  const loadModels = models.mutate;
  const resetModels = models.reset;
  const modelList = models.data?.items ?? [];
  const canLoadModels = (!meta.needsUrl || !!baseUrl.trim()) && (!meta.needsKey || !keyMissing);

  // Fetch the model list on open (saved key) and shortly after a key / endpoint is entered.
  useEffect(() => {
    if (!canLoadModels) return;
    const t = setTimeout(() => loadModels(), apiKey || baseUrl !== (provider?.base_url ?? "") ? 600 : 0);
    return () => clearTimeout(t);
  }, [kind, apiKey, baseUrl, canLoadModels, loadModels, provider?.base_url]);

  const changeKind = (k: ProviderKind) => {
    if (k === kind) return;
    // Keep a custom name; replace the auto-filled one.
    if (!name.trim() || name === kindLabel(kind) || name === provider?.name) setName(kindLabel(k));
    setKind(k);
    setBaseUrl(provider && k === provider.kind ? provider.base_url : "");
    setModel(provider && k === provider.kind ? provider.default_model : (kindMeta(k).defaultModel ?? ""));
    setApiKey("");
    setClearKey(false);
    setManualModel(false);
    resetModels();
  };

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: name.trim(),
        kind,
        base_url: baseUrl.trim() || undefined,
        default_model: model.trim(),
        supports_tools: tools,
        is_default: isDefault,
      };
      if (apiKey) body.api_key = apiKey.trim();
      else if (provider && (clearKey || kindChanged)) body.api_key = "";
      if (kind === "openrouter") {
        // Preserve other extra keys; drop fallback_models when the field is cleared.
        const rest = Object.fromEntries(Object.entries(provider?.extra ?? {}).filter(([k]) => k !== "fallback_models"));
        const list = parseModelList(fallbacks);
        body.extra = list.length ? { ...rest, fallback_models: list } : rest;
      }
      return provider ? api.patch<Provider>(`/providers/${provider.id}`, body) : api.post<Provider>("/providers", body);
    },
    onSuccess: (p) => {
      qc.setQueryData<ListResponse<Provider>>(qk.providers, (old) => (old ? upsertInList(old, p) : old));
      void qc.invalidateQueries({ queryKey: qk.providers });
      toast.success(provider ? "Provider updated" : "Provider added", { description: `${p.name} · ${p.default_model}` });
      onClose();
    },
    meta: { errorTitle: "Couldn't save the provider" },
  });

  const showPicker = modelList.length > 0 && !manualModel;
  const modelOptions = modelList.map((m) => ({ value: m.id, label: m.name && m.name !== m.id ? `${m.id} — ${m.name}` : m.id }));
  if (model && !modelList.some((m) => m.id === model)) modelOptions.unshift({ value: model, label: `${model} (not in list)` });

  let modelHint: string | undefined;
  if (models.isPending) modelHint = "Loading available models…";
  else if (models.isError) modelHint = undefined;
  else if (modelList.length) modelHint = `${modelList.length} models available with this key`;
  else if (!canLoadModels) modelHint = meta.needsKey ? "Enter an API key to load the available models." : "Enter the base URL to load the available models.";

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={provider ? `Edit ${provider.name}` : "Add model provider"}
      description="Any OpenAI-compatible endpoint works: OpenRouter, Google AI Studio, OpenAI, vLLM or your own gateway."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}>
            {provider ? "Save" : "Add provider"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Type">
            <Select
              value={kind}
              onChange={(e) => changeKind(e.target.value as ProviderKind)}
              options={KINDS.map((k) => ({ value: k.value, label: k.label }))}
            />
          </Field>
          <Field label="Name" required>
            <Input data-autofocus value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        {kindChanged && (
          <Notice tone="neutral" icon={<KeyRound />}>
            Switching from {kindLabel(provider.kind)} to {kindLabel(kind)}. The stored key is cleared, so enter the {kindLabel(kind)} key below.
          </Notice>
        )}
        <Field label="Base URL" required={meta.needsUrl} hint={meta.baseUrl ? `Defaults to ${meta.baseUrl}` : undefined}>
          <Input type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={meta.baseUrl ?? "http://localhost:8001/v1"} className="font-mono text-[13px]" />
        </Field>
        <Field
          label="API key"
          required={!!meta.needsKey && (!provider || kindChanged)}
          hint={
            storedKeyUsable
              ? "A key is stored. Leave empty to keep it."
              : provider?.has_key && clearKey
                ? "The stored key will be removed."
                : kind === "google"
                  ? "Create one at aistudio.google.com/apikey."
                  : kind === "vllm"
                    ? "Optional for vLLM."
                    : undefined
          }
          labelAside={
            provider?.has_key && !kindChanged ? (
              <button type="button" onClick={() => setClearKey((c) => !c)} className="text-xs font-medium text-muted hover:text-fg">
                {clearKey ? "Keep stored key" : "Remove stored key"}
              </button>
            ) : undefined
          }
        >
          <Input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={storedKeyUsable ? "•••• saved" : (meta.keyPlaceholder ?? "sk-…")}
            disabled={clearKey && !kindChanged}
          />
        </Field>
        <Field
          label="Default model"
          required
          hint={modelHint}
          labelAside={
            <div className="flex items-center gap-3">
              {modelList.length > 0 && (
                <button type="button" onClick={() => setManualModel((m) => !m)} className="text-xs font-medium text-muted hover:text-fg">
                  {manualModel ? "Pick from list" : "Type manually"}
                </button>
              )}
              <button
                type="button"
                onClick={() => loadModels()}
                disabled={!canLoadModels || models.isPending}
                className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg disabled:opacity-50"
              >
                <RefreshCw className={`size-3 ${models.isPending ? "animate-spin" : ""}`} />
                {modelList.length ? "Refresh" : "Load models"}
              </button>
            </div>
          }
        >
          {showPicker ? (
            <Select value={model} onChange={(e) => setModel(e.target.value)} placeholder="Choose a model…" options={modelOptions} className="font-mono text-[13px]" />
          ) : (
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={meta.defaultModel ?? "anthropic/claude-sonnet-4.5"} className="font-mono text-[13px]" />
          )}
        </Field>
        {models.isError && (
          <Notice tone="danger" icon={<XCircle />} title="Couldn't load models">
            {errorMessage(models.error)}
          </Notice>
        )}
        {kind === "openrouter" && (
          <Field label="Fallback models (optional)" hint="Comma-separated. Tried in order when the default model is rate-limited or unavailable.">
            <Input
              value={fallbacks}
              onChange={(e) => setFallbacks(e.target.value)}
              placeholder="openai/gpt-4.1-mini, meta-llama/llama-3.3-70b-instruct"
              className="font-mono text-[13px]"
            />
          </Field>
        )}
        <Switch checked={tools} onCheckedChange={setTools} label="Supports tool calling" description="Required for agents that use tools." />
        {!provider?.is_default && (
          <Switch checked={isDefault} onCheckedChange={setIsDefault} label="Make this the default provider" />
        )}
      </div>
    </Dialog>
  );
}

function ProviderCard({ provider, onEdit }: { provider: Provider; onEdit: () => void }) {
  const qc = useQueryClient();
  const { isAdmin } = useRole();
  const [result, setResult] = useState<ProviderTestResult | null>(null);
  const test = useMutation({
    mutationFn: () => api.post<ProviderTestResult>(`/providers/${provider.id}/test`),
    onSuccess: setResult,
    onError: (err) => setResult({ ok: false, latency_ms: 0, model: provider.default_model, sample: null, error: errorMessage(err) }),
    meta: { silent: true },
  });
  const makeDefault = useMutation({
    mutationFn: () => api.patch<Provider>(`/providers/${provider.id}`, { is_default: true }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.providers });
      toast.success(`${provider.name} is now the default`);
    },
    meta: { errorTitle: "Couldn't change the default" },
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/providers/${provider.id}`),
    onSuccess: () => {
      qc.setQueryData<ListResponse<Provider>>(qk.providers, (old) => (old ? removeFromList(old, provider.id) : old));
      toast.success("Provider removed", { description: provider.name });
    },
    meta: { errorTitle: "Couldn't remove the provider" },
  });

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-fg">
          <Cpu className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[14px] font-semibold text-fg">{provider.name}</p>
            <Badge tone="outline" size="xs">
              {provider.kind}
            </Badge>
            {provider.is_default && (
              <Badge tone="solid" size="xs" icon={<Star />}>
                Default
              </Badge>
            )}
          </div>
          <p className="mt-0.5 truncate font-mono text-[12px] text-muted">{provider.default_model}</p>
          {fallbackModelsOf(provider).length > 0 && (
            <p className="truncate text-[11.5px] text-muted" title={fallbackModelsOf(provider).join(", ")}>
              Fallbacks: <span className="font-mono">{fallbackModelsOf(provider).join(" → ")}</span>
            </p>
          )}
          <p className="truncate font-mono text-[11px] text-subtle">{provider.base_url}</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Badge tone={provider.has_key ? "success" : "neutral"} size="xs" icon={<KeyRound />}>
              {provider.has_key ? "Key saved" : "No key"}
            </Badge>
            <Badge tone={provider.supports_tools ? "info" : "outline"} size="xs">
              {provider.supports_tools ? "Tool calling" : "No tool calling"}
            </Badge>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <Button size="sm" variant="secondary" icon={<Zap className="size-3.5" />} loading={test.isPending} onClick={() => test.mutate()}>
            Test
          </Button>
          {isAdmin && !provider.is_default && (
            <Button size="sm" variant="ghost" loading={makeDefault.isPending} onClick={() => makeDefault.mutate()}>
              Set default
            </Button>
          )}
          {isAdmin && <IconButton label={`Edit ${provider.name}`} icon={<Pencil />} onClick={onEdit} />}
          {isAdmin && (
            <IconButton
              label={`Delete ${provider.name}`}
              icon={<Trash2 />}
              variant="danger"
              onClick={async () => {
                const ok = await confirm({
                  title: `Delete ${provider.name}?`,
                  description: "Agents using it fall back to the workspace default provider.",
                  confirmLabel: "Delete",
                  danger: true,
                });
                if (ok) remove.mutate();
              }}
            />
          )}
        </div>
      </div>
      {result && (
        <div className="mt-3">
          {result.ok ? (
            <Notice tone="success" icon={<CheckCircle2 />} title={`Connected · ${formatMs(result.latency_ms)} · ${result.model}`}>
              {result.sample ? <span className="italic">“{result.sample.slice(0, 200)}”</span> : "The model responded."}
            </Notice>
          ) : (
            <Notice tone="danger" icon={<XCircle />} title="Connection failed">
              {result.error ?? "No response."}
            </Notice>
          )}
        </div>
      )}
    </Card>
  );
}

function OrchestratorModel({ org, defaultProviderId }: { org: Org; defaultProviderId: string | null }) {
  const { isAdmin } = useRole();
  const update = useUpdateOrg("Orchestrator model saved");
  const [model, setModel] = useState(org.settings.orchestrator_model ?? "");
  const models = useProviderModels(defaultProviderId);
  const dirty = (model.trim() || null) !== (org.settings.orchestrator_model ?? null);
  return (
    <SettingsSection
      title="Orchestrator model"
      description="Override the model Ren itself uses for planning and delegation. Leave empty to use the default provider's model."
      footer={
        isAdmin && (
          <Button variant="primary" loading={update.isPending} disabled={!dirty} onClick={() => update.mutate({ settings: { orchestrator_model: model.trim() || null } })}>
            Save
          </Button>
        )
      }
    >
      <Field label="Model" hint={models.data ? `${models.data.items.length} models available from the default provider` : undefined}>
        <Input list="orchestrator-models" value={model} onChange={(e) => setModel(e.target.value)} placeholder="provider default" className="font-mono text-[13px]" disabled={!isAdmin} />
      </Field>
      <datalist id="orchestrator-models">
        {(models.data?.items ?? []).slice(0, 300).map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </datalist>
    </SettingsSection>
  );
}

export function ModelsTab({ org }: { org: Org }) {
  const { isAdmin } = useRole();
  const { data, isLoading, error, refetch } = useProviders();
  const [dialog, setDialog] = useState<{ provider: Provider | null } | null>(null);
  const items = data?.items ?? [];
  const defaultProvider = items.find((p) => p.is_default) ?? items[0] ?? null;
  return (
    <div className="space-y-5">
      {!isAdmin && (
        <Notice tone="neutral" icon={<Lock />}>
          Only admins can add or change model providers.
        </Notice>
      )}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[14px] font-semibold text-fg">Model providers</h2>
          <p className="text-[12.5px] text-muted">Ren and every agent think with these. Agents can pick their own provider and model.</p>
        </div>
        {isAdmin && (
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setDialog({ provider: null })}>
            Add provider
          </Button>
        )}
      </div>
      {isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-28 w-full rounded-card" />
          <Skeleton className="h-28 w-full rounded-card" />
        </div>
      ) : error && !data ? (
        <Card>
          <ErrorState error={error} onRetry={() => refetch()} />
        </Card>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Cpu />}
            title="No model provider"
            description="Connect OpenRouter, OpenAI or your own vLLM server so Ren and the swarm can think."
            action={
              isAdmin ? (
                <Button variant="primary" onClick={() => setDialog({ provider: null })}>
                  Add provider
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <div className="space-y-3">
          {items.map((p) => (
            <ProviderCard key={p.id} provider={p} onEdit={() => setDialog({ provider: p })} />
          ))}
        </div>
      )}
      <OrchestratorModel key={org.id} org={org} defaultProviderId={defaultProvider?.id ?? null} />
      {dialog && <ProviderDialog key={dialog.provider?.id ?? "new"} provider={dialog.provider} onClose={() => setDialog(null)} />}
    </div>
  );
}
