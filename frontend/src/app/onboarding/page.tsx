"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, CheckCircle2, Cloud, Cpu, Server, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { LogoMark, Wordmark } from "@/components/brand/logo";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { AutonomyPill } from "@/components/domain/status";
import { AgentAvatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { ErrorState, FullScreenLoader, Notice, SkeletonCards } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/input";
import { useAgents, useProviders } from "@/hooks/queries";
import { useLocalPreference } from "@/hooks/use-browser";
import { api, errorMessage } from "@/lib/api";
import { formatMs } from "@/lib/format";
import { parseModelList } from "@/lib/providers";
import { qk } from "@/lib/query-keys";
import type { Provider, ProviderKind, ProviderTestResult } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth";

type Choice = "openrouter" | "vllm" | "openai";

const CHOICES: { id: Choice; title: string; description: string; icon: ReactNode }[] = [
  {
    id: "openrouter",
    title: "OpenRouter",
    description: "One key, hundreds of hosted models. Fastest way to start.",
    icon: <Cloud />,
  },
  {
    id: "vllm",
    title: "vLLM",
    description: "Your own self-hosted model server (OpenAI-compatible API).",
    icon: <Server />,
  },
  {
    id: "openai",
    title: "OpenAI-compatible",
    description: "OpenAI or any endpoint that speaks the OpenAI API.",
    icon: <Cpu />,
  },
];

const MODEL_HINTS: Record<Choice, string> = {
  openrouter: "anthropic/claude-sonnet-4.5",
  vllm: "meta-llama/Llama-3.3-70B-Instruct",
  openai: "gpt-4.1",
};

function Steps({ step }: { step: number }) {
  const labels = ["Connect a model", "Meet your swarm", "Start working"];
  return (
    <ol className="flex items-center gap-2 text-xs">
      {labels.map((l, i) => (
        <li key={l} className="flex items-center gap-2">
          <span
            className={cn(
              "flex size-5 items-center justify-center rounded-full text-[10.5px] font-semibold",
              i < step ? "bg-primary text-primary-fg" : i === step ? "border border-fg text-fg" : "border border-border-strong text-subtle",
            )}
          >
            {i < step ? <Check className="size-3" strokeWidth={3} /> : i + 1}
          </span>
          <span className={cn("hidden sm:inline", i === step ? "font-medium text-fg" : "text-muted")}>{l}</span>
          {i < labels.length - 1 && <span className="mx-1 h-px w-6 bg-border-strong sm:w-10" />}
        </li>
      ))}
    </ol>
  );
}

function ProviderStep({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState<Choice>("openrouter");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [fallbacks, setFallbacks] = useState("");
  const [provider, setProvider] = useState<Provider | null>(null);
  const [result, setResult] = useState<ProviderTestResult | null>(null);

  const kind: ProviderKind =
    choice === "openai" && baseUrl.trim() && !baseUrl.includes("api.openai.com") ? "custom" : choice;

  const valid =
    model.trim().length > 0 &&
    (choice === "openrouter" ? apiKey.trim().length > 0 || !!provider?.has_key : true) &&
    (choice === "vllm" ? baseUrl.trim().length > 0 : true);

  const saveAndTest = useMutation({
    mutationFn: async () => {
      const body = {
        name: choice === "openrouter" ? "OpenRouter" : choice === "vllm" ? "vLLM" : kind === "custom" ? "Custom endpoint" : "OpenAI",
        kind,
        base_url: baseUrl.trim() || undefined,
        api_key: apiKey.trim() || undefined,
        default_model: model.trim(),
        is_default: true,
        supports_tools: true,
        ...(choice === "openrouter" ? { extra: { fallback_models: parseModelList(fallbacks) } } : {}),
      };
      const saved = provider
        ? await api.patch<Provider>(`/providers/${provider.id}`, body)
        : await api.post<Provider>("/providers", body);
      setProvider(saved);
      const test = await api.post<ProviderTestResult>(`/providers/${saved.id}/test`);
      return test;
    },
    onSuccess: (test) => {
      setResult(test);
      void qc.invalidateQueries({ queryKey: qk.providers });
    },
    onError: (err) => setResult({ ok: false, latency_ms: 0, model: model, sample: null, error: errorMessage(err) }),
    meta: { silent: true },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-serif-display text-[36px] leading-tight text-fg">Connect a model provider</h1>
        <p className="mt-1.5 text-[13.5px] text-muted">
          Ren and your agents think with a large language model. Pick where it runs — you can add more later in Settings.
        </p>
      </div>
      <div role="radiogroup" aria-label="Provider" className="grid gap-3 sm:grid-cols-3">
        {CHOICES.map((c) => (
          <button
            key={c.id}
            type="button"
            role="radio"
            aria-checked={choice === c.id}
            onClick={() => {
              setChoice(c.id);
              setResult(null);
              setProvider(null);
            }}
            className={cn(
              "flex flex-col items-start gap-2 rounded-card border p-4 text-left transition-colors",
              choice === c.id ? "border-fg bg-surface" : "border-border bg-surface/60 hover:border-border-strong",
            )}
          >
            <span className="flex w-full items-center justify-between">
              <span className="flex size-8 items-center justify-center rounded-lg bg-surface-2 text-fg [&_svg]:size-4">{c.icon}</span>
              {choice === c.id && (
                <span className="flex size-4 items-center justify-center rounded-full bg-primary text-primary-fg">
                  <Check className="size-2.5" strokeWidth={3} />
                </span>
              )}
            </span>
            <span className="text-[13.5px] font-semibold text-fg">{c.title}</span>
            <span className="text-xs leading-snug text-muted">{c.description}</span>
          </button>
        ))}
      </div>

      <form
        className="space-y-4 rounded-card border border-border bg-surface p-5"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) saveAndTest.mutate();
        }}
      >
        {choice !== "openrouter" && (
          <Field
            label="Base URL"
            required={choice === "vllm"}
            hint={choice === "vllm" ? "e.g. http://gpu-box:8000/v1" : "Leave empty for https://api.openai.com/v1"}
          >
            <Input
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={choice === "vllm" ? "http://localhost:8001/v1" : "https://api.openai.com/v1"}
            />
          </Field>
        )}
        <Field
          label={choice === "vllm" ? "API key (optional)" : "API key"}
          required={choice === "openrouter"}
          hint={provider?.has_key && !apiKey ? "A key is saved. Leave empty to keep it." : "Stored encrypted on the server; never shown again."}
        >
          <Input type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…" />
        </Field>
        <Field label="Default model" required hint="Used by Ren and any agent without its own model.">
          <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={MODEL_HINTS[choice]} />
        </Field>
        {choice === "openrouter" && (
          <Field label="Fallback models (optional)" hint="Comma-separated. Tried in order when the default model is rate-limited.">
            <Input value={fallbacks} onChange={(e) => setFallbacks(e.target.value)} placeholder="openai/gpt-4.1-mini, meta-llama/llama-3.3-70b-instruct" />
          </Field>
        )}

        {result &&
          (result.ok ? (
            <Notice tone="success" icon={<CheckCircle2 />} title={`Connected · ${formatMs(result.latency_ms)} · ${result.model}`}>
              {result.sample ? <span className="italic">“{result.sample.slice(0, 160)}”</span> : "The model responded."}
            </Notice>
          ) : (
            <Notice tone="danger" icon={<XCircle />} title="Connection failed">
              {result.error ?? "The provider did not respond."}
            </Notice>
          ))}

        <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
          <Button type="submit" variant={result?.ok ? "secondary" : "primary"} loading={saveAndTest.isPending} disabled={!valid}>
            {provider ? "Save & test again" : "Test connection"}
          </Button>
          {(result?.ok || provider) && (
            <Button variant="primary" iconRight={<ArrowRight className="size-4" />} onClick={onDone}>
              Continue
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}

function SwarmStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const agents = useAgents();
  const list = (agents.data?.items ?? []).slice().sort((a, b) => Number(b.is_orchestrator) - Number(a.is_orchestrator));
  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-serif-display text-[36px] leading-tight text-fg">Meet your swarm</h1>
        <p className="mt-1.5 text-[13.5px] text-muted">
          Ren orchestrates. Specialists take on work in the background — each with its own tools, knowledge and autonomy.
        </p>
      </div>
      {agents.isLoading ? (
        <SkeletonCards count={6} />
      ) : agents.error ? (
        <ErrorState error={agents.error} onRetry={() => agents.refetch()} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {list.map((a) => (
            <div
              key={a.id}
              className={cn("rounded-card border bg-surface p-4", a.is_orchestrator ? "border-fg" : "border-border")}
            >
              <div className="flex items-center gap-3">
                <AgentAvatar agent={a} size="lg" />
                <div className="min-w-0">
                  <p className="truncate text-[13.5px] font-semibold text-fg">{a.name}</p>
                  <p className="truncate text-xs text-muted">{a.is_orchestrator ? "Orchestrator" : a.role}</p>
                </div>
              </div>
              <p className="mt-3 line-clamp-3 text-xs leading-relaxed text-muted">{a.description}</p>
              {!a.is_orchestrator && (
                <div className="mt-3">
                  <AutonomyPill autonomy={a.autonomy} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="flex justify-between">
        <Button variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={onBack}>
          Back
        </Button>
        <Button variant="primary" iconRight={<ArrowRight className="size-4" />} onClick={onDone}>
          Continue
        </Button>
      </div>
    </div>
  );
}

function FinishStep({ onBack, onFinish }: { onBack: () => void; onFinish: () => void }) {
  return (
    <div className="space-y-6 text-center">
      <div className="mx-auto flex size-14 items-center justify-center rounded-2xl bg-brand text-brand-fg">
        <LogoMark className="w-10" />
      </div>
      <div>
        <h1 className="font-serif-display text-[40px] leading-tight text-fg">Your swarm is ready</h1>
        <p className="mx-auto mt-2 max-w-md text-[13.5px] text-muted">
          Ask Ren for anything — research, analysis, drafts, operations. Keep chatting while agents work; approve anything
          sensitive before it happens.
        </p>
      </div>
      <div className="flex justify-center gap-2">
        <Button variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={onBack}>
          Back
        </Button>
        <Button variant="primary" size="lg" iconRight={<ArrowRight className="size-4" />} onClick={onFinish}>
          Open chat
        </Button>
      </div>
    </div>
  );
}

export default function OnboardingPage() {
  const { status, activeOrgId } = useAuth();
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [, setSkipped] = useLocalPreference<string>(`ren_onboarding_skipped:${activeOrgId ?? ""}`, "");
  const providers = useProviders(status === "authenticated");
  const hasProvider = (providers.data?.items.length ?? 0) > 0;

  useEffect(() => {
    if (status === "unauthenticated") router.replace("/login?next=/onboarding");
  }, [status, router]);

  if (status !== "authenticated") return <FullScreenLoader />;

  const finish = () => {
    setSkipped("1");
    router.replace("/chat");
  };

  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-10 border-b border-border bg-bg/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-4xl items-center justify-between gap-4 px-5">
          <Wordmark size="sm" />
          <Steps step={step} />
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <Button variant="ghost" size="sm" onClick={finish}>
              Skip for now
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-5 py-10">
        {step === 0 && (
          <>
            {hasProvider && (
              <Notice tone="info" className="mb-6" title="A provider is already configured">
                You can add another one, or continue to meet your swarm.{" "}
                <button type="button" className="font-medium text-fg underline underline-offset-2" onClick={() => setStep(1)}>
                  Continue
                </button>
              </Notice>
            )}
            <ProviderStep onDone={() => setStep(1)} />
          </>
        )}
        {step === 1 && <SwarmStep onBack={() => setStep(0)} onDone={() => setStep(2)} />}
        {step === 2 && <FinishStep onBack={() => setStep(1)} onFinish={finish} />}
      </main>
    </div>
  );
}
