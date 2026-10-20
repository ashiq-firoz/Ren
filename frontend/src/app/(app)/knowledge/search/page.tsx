"use client";

import { useMutation } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronDown, FlaskConical, Layers, Search, Sparkles, Timer } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { AlignmentBadge } from "@/components/chat/alignment-badge";
import { citationHref, formatBreadcrumb, SourcesRow } from "@/components/domain/citations";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { SegmentedControl, Switch } from "@/components/ui/controls";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Markdown } from "@/components/ui/markdown";
import { useCollections } from "@/hooks/queries";
import { api } from "@/lib/api";
import { formatMs, formatPercent } from "@/lib/format";
import type { AnswerResponse, SearchMode, SearchResponse, SearchResult } from "@/lib/types";
import { cn } from "@/lib/utils";

const STAGES: { key: string; label: string }[] = [
  { key: "bm25", label: "BM25" },
  { key: "vector", label: "Vector" },
  { key: "stair", label: "STAIR" },
  { key: "questions", label: "Questions" },
  { key: "fusion", label: "Fusion" },
  { key: "rerank", label: "Rerank" },
];

function RankBadges({ sources }: { sources: SearchResult["sources"] }) {
  const badges: { label: string; value: string; on: boolean }[] = [
    { label: "BM25", value: sources.bm25 ? `#${sources.bm25}` : "—", on: !!sources.bm25 },
    { label: "Vector", value: sources.vector ? `#${sources.vector}` : "—", on: !!sources.vector },
    { label: "STAIR", value: sources.stair ? "✓" : "—", on: !!sources.stair },
    { label: "Q", value: sources.questions ? `#${sources.questions}` : "—", on: !!sources.questions },
  ];
  return (
    <div className="flex flex-wrap gap-1.5">
      {badges.map((b) => (
        <span
          key={b.label}
          title={b.on ? `${b.label} rank ${b.value}` : `Not retrieved by ${b.label}`}
          className={cn(
            "inline-flex h-5 items-center gap-1 rounded-full border px-2 text-[11px] tabular-nums",
            b.on ? "border-border-strong bg-surface-2 text-fg" : "border-dashed border-border text-subtle",
          )}
        >
          <span className="text-muted">{b.label}</span>
          <span className="font-semibold">{b.value}</span>
        </span>
      ))}
    </div>
  );
}

function ResultCard({ r, rank }: { r: SearchResult; rank: number }) {
  const [open, setOpen] = useState(false);
  const crumb = formatBreadcrumb(r.breadcrumb);
  return (
    <Card className="p-4">
      <div className="flex items-start gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-2 text-[12px] font-semibold text-muted tabular-nums">{rank}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <Link href={citationHref(r)} className="text-[13.5px] font-semibold text-fg hover:underline">
                {r.document_title}
              </Link>
              {(crumb || r.section_path) && (
                <p className="mt-0.5 truncate text-xs text-muted">
                  {r.section_path && <span className="mr-1.5 font-mono text-subtle">§{r.section_path}</span>}
                  {crumb ?? r.section_title}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-3 text-xs text-muted tabular-nums">
              <span title="Fused score">
                fused <span className="font-semibold text-fg">{r.score.toFixed(3)}</span>
              </span>
              {r.rerank_score !== null && r.rerank_score !== undefined && (
                <span title="Reranker score">
                  rerank <span className="font-semibold text-fg">{r.rerank_score.toFixed(3)}</span>
                </span>
              )}
            </div>
          </div>
          <div className="mt-2.5">
            <RankBadges sources={r.sources ?? {}} />
          </div>
          <p className={cn("mt-3 text-[13px] leading-relaxed whitespace-pre-wrap text-muted", !open && "line-clamp-4")}>{r.content}</p>
          {r.content.length > 320 && (
            <button type="button" onClick={() => setOpen((o) => !o)} className="mt-1 text-xs font-medium text-muted hover:text-fg">
              {open ? "Show less" : "Show full chunk"}
            </button>
          )}
        </div>
      </div>
    </Card>
  );
}

function Timings({ timings }: { timings: Record<string, number> }) {
  const stages = STAGES.filter((s) => timings[s.key] !== undefined && timings[s.key] !== null);
  const total = timings.total ?? stages.reduce((s, x) => s + (timings[x.key] ?? 0), 0);
  const max = Math.max(1, ...stages.map((s) => timings[s.key] ?? 0));
  return (
    <div>
      <ul className="space-y-2">
        {stages.map((s) => (
          <li key={s.key} className="grid grid-cols-[72px_1fr_64px] items-center gap-2 text-xs">
            <span className="text-muted">{s.label}</span>
            <span className="h-1.5 overflow-hidden rounded-full bg-surface-2">
              <span className="block h-full rounded-r-[4px] bg-accent" style={{ width: `${Math.max(2, ((timings[s.key] ?? 0) / max) * 100)}%` }} />
            </span>
            <span className="text-right text-fg tabular-nums">{formatMs(timings[s.key])}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 flex items-center justify-between border-t border-border pt-2 text-xs">
        <span className="inline-flex items-center gap-1 text-muted">
          <Timer className="size-3.5" /> Total
        </span>
        <span className="font-semibold text-fg tabular-nums">{formatMs(total)}</span>
      </p>
    </div>
  );
}

export default function RetrievalPlaygroundPage() {
  const params = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [rerank, setRerank] = useState(true);
  const [topK, setTopK] = useState(8);
  const [collectionIds, setCollectionIds] = useState<string[]>([]);
  const [showContext, setShowContext] = useState(false);
  const collections = useCollections();

  const search = useMutation({
    mutationFn: (q: string) =>
      api.post<SearchResponse>("/knowledge/search", {
        query: q,
        mode,
        rerank,
        top_k: topK,
        collection_ids: collectionIds.length ? collectionIds : undefined,
      }),
    meta: { silent: true },
  });

  const answer = useMutation({
    mutationFn: (q: string) =>
      api.post<AnswerResponse>("/knowledge/answer", { query: q, collection_ids: collectionIds.length ? collectionIds : undefined }),
    meta: { errorTitle: "Couldn't generate an answer" },
  });

  const run = () => {
    const q = query.trim();
    if (!q) return;
    answer.reset();
    search.mutate(q);
  };

  const result = search.data;
  const context = answer.data?.retrieval?.context ?? result?.context ?? "";

  return (
    <PageContainer wide>
      <PageHeader
        eyebrow={
          <Link href="/knowledge" className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3" /> Knowledge
          </Link>
        }
        title="Retrieval playground"
        description="See exactly what your agents retrieve: per-retriever ranks, STAIR's structural picks, fusion, reranking and timings — then generate a grounded answer."
      />

      <Card className="mb-5 p-4">
        <form
          className="flex flex-col gap-3 sm:flex-row"
          onSubmit={(e) => {
            e.preventDefault();
            run();
          }}
        >
          <Input
            leftIcon={<Search />}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="How many days of parental leave do employees get?"
            aria-label="Query"
            wrapperClassName="flex-1"
            className="h-10 text-[14px]"
            autoFocus
          />
          <Button type="submit" variant="primary" size="lg" loading={search.isPending} disabled={!query.trim()} icon={<FlaskConical className="size-4" />}>
            Search
          </Button>
        </form>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-3">
          <SegmentedControl
            aria-label="Retrieval mode"
            value={mode}
            onChange={setMode}
            options={[
              { value: "hybrid", label: "Hybrid" },
              { value: "semantic", label: "Semantic" },
              { value: "lexical", label: "Lexical" },
              { value: "structure", label: "Structure" },
            ]}
          />
          <Switch checked={rerank} onCheckedChange={setRerank} label="Rerank" size="sm" />
          <label className="flex items-center gap-2 text-[13px] text-fg">
            Top-k
            <Input
              type="number"
              min={1}
              max={50}
              inputSize="sm"
              value={topK}
              onChange={(e) => setTopK(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
              className="w-16"
              wrapperClassName="w-16"
            />
          </label>
        </div>
        {(collections.data?.items.length ?? 0) > 0 && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-xs text-muted">Collections</span>
            {collections.data!.items.map((c) => {
              const on = collectionIds.includes(c.id);
              return (
                <button
                  key={c.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() => setCollectionIds((ids) => (on ? ids.filter((x) => x !== c.id) : [...ids, c.id]))}
                  className={cn(
                    "inline-flex h-6 items-center gap-1 rounded-full border px-2.5 text-xs transition-colors",
                    on ? "border-fg bg-primary text-primary-fg" : "border-border text-muted hover:text-fg",
                  )}
                >
                  {on && <Check className="size-3" />}
                  {c.name}
                </button>
              );
            })}
            {collectionIds.length === 0 && <span className="text-xs text-subtle">(all)</span>}
          </div>
        )}
      </Card>

      {search.isPending ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-36 w-full rounded-card" />
            ))}
          </div>
          <Skeleton className="h-64 rounded-card" />
        </div>
      ) : search.error ? (
        <Card>
          <ErrorState error={search.error} title="Search failed" onRetry={run} />
        </Card>
      ) : !result ? (
        <Card>
          <EmptyState
            icon={<FlaskConical />}
            title="Ask the knowledge base"
            description="Results show which retrievers found each chunk (BM25, vector, STAIR structure, synthetic questions), how they were fused and reranked, and how long each stage took."
          />
        </Card>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 space-y-4">
            {/* Answer */}
            <Card className="p-4">
              {answer.data ? (
                <div>
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <p className="flex items-center gap-1.5 text-[13px] font-semibold text-fg">
                      <Sparkles className="size-4 text-accent" /> Grounded answer
                    </p>
                    <AlignmentBadge summary={answer.data.alignment} />
                  </div>
                  <Markdown content={answer.data.answer} citations={answer.data.citations} />
                  {answer.data.citations.length > 0 && <SourcesRow citations={answer.data.citations} className="mt-4" />}
                </div>
              ) : (
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="text-[13px] text-muted">Generate an answer from these sources, with citations and a policy check.</p>
                  <Button variant="primary" icon={<Sparkles className="size-4" />} loading={answer.isPending} onClick={() => answer.mutate(result.query)}>
                    Generate answer
                  </Button>
                </div>
              )}
            </Card>

            <div className="flex items-center justify-between">
              <p className="text-[13px] text-muted">
                {result.results.length} results for <span className="font-medium text-fg">“{result.query}”</span>
              </p>
            </div>
            {result.results.length === 0 ? (
              <Card>
                <EmptyState compact title="Nothing found" description="Try another phrasing, a different mode, or more collections." />
              </Card>
            ) : (
              result.results.map((r, i) => <ResultCard key={r.chunk_id} r={r} rank={i + 1} />)
            )}

            {context && (
              <Card>
                <button
                  type="button"
                  onClick={() => setShowContext((s) => !s)}
                  aria-expanded={showContext}
                  className="flex w-full items-center justify-between px-4 py-3 text-left text-[13px] font-medium text-fg"
                >
                  Context sent to the model
                  <ChevronDown className={cn("size-4 text-muted transition-transform", showContext && "rotate-180")} />
                </button>
                {showContext && (
                  <pre className="max-h-[480px] overflow-auto border-t border-border bg-[var(--code-bg)] px-4 py-3 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted">
                    {context}
                  </pre>
                )}
              </Card>
            )}
          </div>

          <aside className="space-y-4">
            <Card>
              <CardHeader
                icon={<Layers />}
                title="STAIR selected sections"
                description={
                  result.stair.enabled
                    ? `${result.stair.method} · ${result.stair.candidates_considered} candidates · ${formatMs(result.stair.latency_ms)}`
                    : "Structure-aware retrieval is off"
                }
              />
              <div className="p-3">
                {!result.stair.enabled ? (
                  <p className="px-1 py-2 text-xs text-muted">Enable STAIR in Settings → Retrieval to let Ren pick sections from each document&apos;s table of contents.</p>
                ) : result.stair.selected.length === 0 ? (
                  <p className="px-1 py-2 text-xs text-muted">No sections selected for this query.</p>
                ) : (
                  <ul className="space-y-1.5">
                    {result.stair.selected.map((s) => (
                      <li key={`${s.document_id}-${s.section_id}`}>
                        <Link
                          href={citationHref(s)}
                          className="block rounded-md border border-border bg-surface px-2.5 py-2 transition-colors hover:border-border-strong"
                        >
                          <span className="flex items-center justify-between gap-2">
                            <span className="truncate text-[12.5px] font-medium text-fg">{s.title}</span>
                            {s.confidence !== null && (
                              <Badge tone="accent" size="xs">
                                {formatPercent(s.confidence)}
                              </Badge>
                            )}
                          </span>
                          <span className="mt-0.5 block truncate text-[11px] text-muted">
                            <span className="font-mono text-subtle">§{s.path}</span> · {formatBreadcrumb(s.breadcrumb) ?? s.document_title}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </Card>
            <Card>
              <CardHeader icon={<Timer />} title="Stage timings" />
              <div className="p-4">
                <Timings timings={result.timings ?? {}} />
              </div>
            </Card>
          </aside>
        </div>
      )}
    </PageContainer>
  );
}
