"use client";

import { AlertCircle, CalendarClock, Loader2 } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/feedback";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Dialog } from "@/components/ui/overlay";
import { useCreateSchedule, useUpdateSchedule, type SchedulePatch } from "@/hooks/actions";
import { useAgents, useSchedulePreview } from "@/hooks/queries";
import { useBrowserTimeZone, useDebounced } from "@/hooks/use-browser";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatTimeZone, timeUntil } from "@/lib/format";
import type { SchedulePreviewParams } from "@/lib/query-keys";
import {
  buildSchedule,
  defaultDraft,
  draftFromJob,
  FREQUENCY_OPTIONS,
  localDateInput,
  timeZoneOptions,
  WEEKDAYS,
  zonedDateTime,
  type BuiltSchedule,
  type Frequency,
  type ScheduleDraft,
} from "@/lib/schedules";
import type { ScheduledJob } from "@/lib/types";
import { cn } from "@/lib/utils";

const DAYS_OF_MONTH = Array.from({ length: 31 }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }));

const normCron = (cron: string | null | undefined) => (cron ?? "").trim().split(/\s+/).filter(Boolean).join(" ");

/** Whether the built schedule differs from what the job has now. */
function scheduleChanged(job: ScheduledJob, built: BuiltSchedule, timezone: string): boolean {
  if (timezone !== job.timezone) return true;
  if (built.cron || job.cron) return normCron(built.cron) !== normCron(job.cron);
  const wall = zonedDateTime(job.run_at, job.timezone);
  return !wall || `${wall.date}T${wall.time}` !== built.run_at;
}

function missingReason(draft: ScheduleDraft): string {
  switch (draft.frequency) {
    case "once":
      return "Pick a date and time.";
    case "hourly":
      return "Enter a minute between 0 and 59.";
    case "weekly":
      return draft.weekdays.length === 0 ? "Pick at least one day." : "Pick a time.";
    case "custom":
      return "Enter a cron expression.";
    default:
      return "Pick a time.";
  }
}

/* ------------------------------------------------------------------ */
/* Builder fields                                                      */
/* ------------------------------------------------------------------ */

function WeekdayPicker({ value, onChange }: { value: number[]; onChange: (days: number[]) => void }) {
  const labelId = useId();
  const toggle = (d: number) => onChange(value.includes(d) ? value.filter((x) => x !== d) : [...value, d]);
  return (
    <div className="flex flex-col gap-1.5">
      <span id={labelId} className="text-[13px] font-medium text-fg">
        Days
      </span>
      <div role="group" aria-labelledby={labelId} className="flex flex-wrap gap-1">
        {WEEKDAYS.map((d) => {
          const on = value.includes(d.value);
          return (
            <button
              key={d.value}
              type="button"
              aria-pressed={on}
              aria-label={d.long}
              title={d.long}
              onClick={() => toggle(d.value)}
              className={cn(
                "h-8 min-w-[42px] rounded-control border px-2 text-[12.5px] font-medium transition-colors duration-150",
                on ? "border-primary bg-primary text-primary-fg" : "border-border-strong bg-surface text-muted hover:bg-surface-2 hover:text-fg",
              )}
            >
              {d.short}
            </button>
          );
        })}
      </div>
      {value.length === 0 && <p className="text-xs text-danger">Pick at least one day.</p>}
    </div>
  );
}

function TimeField({ value, onChange, className }: { value: string; onChange: (time: string) => void; className?: string }) {
  return (
    <Field label="Time" className={className}>
      <Input type="time" value={value} onChange={(e) => onChange(e.target.value)} required />
    </Field>
  );
}

function FrequencyFields({ draft, onChange, today }: { draft: ScheduleDraft; onChange: (p: Partial<ScheduleDraft>) => void; today: string }) {
  const setTime = (time: string) => onChange({ time });
  switch (draft.frequency) {
    case "once":
      return (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Date">
            <Input type="date" value={draft.date} min={today} onChange={(e) => onChange({ date: e.target.value })} required />
          </Field>
          <TimeField value={draft.time} onChange={setTime} />
        </div>
      );
    case "hourly":
      return (
        <Field label="Minute past the hour" hint="Runs every hour at this minute (0–59).">
          <Input
            type="number"
            min={0}
            max={59}
            step={1}
            inputMode="numeric"
            value={Number.isFinite(draft.minute) ? draft.minute : ""}
            onChange={(e) => onChange({ minute: e.target.value === "" ? Number.NaN : Number(e.target.value) })}
            className="sm:w-40"
          />
        </Field>
      );
    case "daily":
    case "weekdays":
      return <TimeField value={draft.time} onChange={setTime} className="sm:max-w-[200px]" />;
    case "weekly":
      return (
        <div className="space-y-3">
          <WeekdayPicker value={draft.weekdays} onChange={(weekdays) => onChange({ weekdays })} />
          <TimeField value={draft.time} onChange={setTime} className="sm:max-w-[200px]" />
        </div>
      );
    case "monthly":
      return (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Day of month" hint={draft.dayOfMonth > 28 ? "Months without this day are skipped." : undefined}>
            <Select value={String(draft.dayOfMonth)} onChange={(e) => onChange({ dayOfMonth: Number(e.target.value) })} options={DAYS_OF_MONTH} />
          </Field>
          <TimeField value={draft.time} onChange={setTime} />
        </div>
      );
    case "custom":
      return (
        <Field label="Cron expression" hint="minute · hour · day of month · month · day of week — e.g. 0 9 * * 1-5">
          <Input
            value={draft.cron}
            onChange={(e) => onChange({ cron: e.target.value })}
            placeholder="0 9 * * 1-5"
            spellCheck={false}
            autoComplete="off"
            className="font-mono"
          />
        </Field>
      );
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/* Live preview                                                        */
/* ------------------------------------------------------------------ */

function PreviewBox({
  built,
  reason,
  preview,
  busy,
  timezone,
  browserTz,
}: {
  built: BuiltSchedule | null;
  reason: string;
  preview: ReturnType<typeof useSchedulePreview>;
  busy: boolean;
  timezone: string;
  browserTz: string;
}) {
  const data = preview.data;
  let content: ReactNode;
  if (!built) content = <p className="text-muted">{reason}</p>;
  else if (preview.error && !data) content = <p className="text-danger">{errorMessage(preview.error)}</p>;
  else if (!data)
    content = (
      <div className="space-y-1.5 py-0.5">
        <Skeleton className="h-3.5 w-2/3" />
        <Skeleton className="h-3 w-1/2" />
      </div>
    );
  else if (!data.valid)
    content = (
      <p className="flex items-start gap-1.5 text-danger">
        <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
        {data.error || "That schedule isn't valid."}
      </p>
    );
  else
    content = (
      <>
        <p className="font-medium text-fg">{data.description}</p>
        {data.next.length > 0 && (
          <ul className="mt-1.5 space-y-0.5 text-xs text-muted">
            {data.next.slice(0, 3).map((iso, i) => (
              <li key={iso} className="flex items-center justify-between gap-3">
                <span>
                  {i === 0 ? "Next" : "Then"} · <span className="text-fg">{formatDateTime(iso, { weekday: true })}</span>
                </span>
                {i === 0 && <span className="shrink-0 text-subtle">{timeUntil(iso)}</span>}
              </li>
            ))}
          </ul>
        )}
        {timezone !== browserTz && (
          <p className="mt-1.5 text-[11px] text-subtle">Times shown in your time zone ({formatTimeZone(browserTz)}).</p>
        )}
      </>
    );
  return (
    <div className="rounded-card border border-border bg-surface-2/60 px-3.5 py-3 text-[13px]" aria-live="polite">
      <div className="flex items-start gap-2.5">
        <CalendarClock className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
        <div className="min-w-0 flex-1">{content}</div>
        {built && busy && <Loader2 className="spin mt-0.5 size-3.5 shrink-0 text-subtle" aria-label="Updating preview" />}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Dialog                                                              */
/* ------------------------------------------------------------------ */

/** Create (job = null) or edit a schedule. Mount it only while open so state starts from the job. */
export function ScheduleEditor({ job, onClose }: { job: ScheduledJob | null; onClose: () => void }) {
  const agents = useAgents();
  const browserTz = useBrowserTimeZone();
  const create = useCreateSchedule();
  const update = useUpdateSchedule();
  const isReminder = job?.kind === "notification";

  const [title, setTitle] = useState(job?.title ?? "");
  const [agentId, setAgentId] = useState(job?.agent_id ?? "");
  const [instructions, setInstructions] = useState(job?.instructions ?? "");
  const [draft, setDraft] = useState<ScheduleDraft>(() => (job ? draftFromJob(job) : defaultDraft()));
  const [timezone, setTimezone] = useState(job?.timezone || browserTz);
  const [scheduleTouched, setScheduleTouched] = useState(false);
  const [today] = useState(() => localDateInput(new Date()));

  const patchDraft = (p: Partial<ScheduleDraft>) => {
    setDraft((d) => ({ ...d, ...p }));
    setScheduleTouched(true);
  };

  const options = (agents.data?.items ?? []).filter((a) => a.enabled && !a.is_orchestrator);
  const currentUnavailable = !!job?.agent_id && !!agents.data && !options.some((a) => a.id === job.agent_id);
  const chosenAgent = isReminder ? "" : agentId || options[0]?.id || "";

  const zones = useMemo(() => timeZoneOptions([browserTz, job?.timezone]), [browserTz, job?.timezone]);
  const suggested = useMemo(() => Array.from(new Set([browserTz, job?.timezone, "UTC"].filter((z): z is string => !!z))), [browserTz, job?.timezone]);
  const zoneLabel = (tz: string) =>
    tz === browserTz ? `${formatTimeZone(tz)} — your time zone` : tz === job?.timezone ? `${formatTimeZone(tz)} — current` : formatTimeZone(tz);

  // Live preview, debounced on a primitive key so typing doesn't spam the API.
  const built = buildSchedule(draft);
  const previewInput = built ? JSON.stringify({ cron: built.cron, run_at: built.run_at, timezone }) : "";
  const debounced = useDebounced(previewInput, 400);
  const previewParams = useMemo<SchedulePreviewParams>(
    () => (debounced ? { ...(JSON.parse(debounced) as SchedulePreviewParams), count: 3 } : {}),
    [debounced],
  );
  const preview = useSchedulePreview(previewParams, !!debounced);
  const settled = !!previewInput && previewInput === debounced && !preview.isPlaceholderData && !preview.isFetching;
  // An untouched schedule isn't re-sent, so e.g. a past one-off doesn't block renaming it.
  const invalidNow = settled && preview.data?.valid === false && (!job || scheduleTouched);

  const pending = create.isPending || update.isPending;
  const incomplete = !title.trim() || !instructions.trim() || !built || (!isReminder && !chosenAgent);

  const submit = () => {
    if (!built || incomplete) return;
    if (!job) {
      create.mutate(
        {
          title: title.trim(),
          agent_id: chosenAgent,
          instructions: instructions.trim(),
          ...(built.cron ? { cron: built.cron } : { run_at: built.run_at }),
          timezone,
        },
        { onSuccess: onClose },
      );
      return;
    }
    // Send only what changed (an untouched schedule isn't re-sent, so its next run isn't recomputed).
    const patch: SchedulePatch = {};
    if (title.trim() !== job.title) patch.title = title.trim();
    if (instructions.trim() !== (job.instructions ?? "").trim()) patch.instructions = instructions.trim();
    if (!isReminder && chosenAgent && chosenAgent !== job.agent_id) patch.agent_id = chosenAgent;
    if (scheduleTouched && scheduleChanged(job, built, timezone)) {
      Object.assign(patch, built.cron ? { cron: built.cron, timezone } : { cron: null, run_at: built.run_at, timezone });
    }
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    update.mutate({ id: job.id, patch }, { onSuccess: onClose });
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      size="lg"
      dismissable={!pending}
      title={job ? (isReminder ? "Edit reminder" : "Edit schedule") : "New schedule"}
      description={
        job
          ? undefined
          : "Have an agent do this on a schedule. Each run shows up in Tasks, and its files in Outputs."
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="primary" loading={pending} disabled={incomplete || invalidNow} onClick={submit}>
            {job ? "Save changes" : "Create schedule"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Title" required>
          <Input
            data-autofocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={isReminder ? "Review the pipeline" : "Tech & AI newsletter"}
            maxLength={300}
          />
        </Field>
        {!isReminder && (
          <Field label="Agent" required hint="The specialist that does the work on every run.">
            <Select
              value={chosenAgent}
              onChange={(e) => setAgentId(e.target.value)}
              disabled={agents.isLoading || (options.length === 0 && !currentUnavailable)}
            >
              {options.length === 0 && !currentUnavailable && (
                <option value="">{agents.isLoading ? "Loading agents…" : "No agents available"}</option>
              )}
              {currentUnavailable && job?.agent_id && (
                <option value={job.agent_id}>{job.agent_name ?? "Current agent"} (unavailable)</option>
              )}
              {options.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} — {a.role}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field
          label={isReminder ? "Message" : "Instructions"}
          required
          hint={isReminder ? "What the reminder says." : "What to do on every run — be specific about the deliverable and sources."}
        >
          <Textarea
            autosize
            minRows={4}
            maxRows={12}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder={
              isReminder
                ? "Time to review this week's pipeline."
                : "Find the most important tech & AI news from the last 24 hours and write a short newsletter with links."
            }
          />
        </Field>

        <fieldset className="space-y-3 rounded-card border border-border p-4">
          <legend className="px-1 text-[13px] font-medium text-fg">When</legend>
          <div className="grid gap-3 sm:grid-cols-[170px_minmax(0,1fr)]">
            <Field label="Frequency">
              <Select
                value={draft.frequency}
                onChange={(e) => patchDraft({ frequency: e.target.value as Frequency })}
                options={FREQUENCY_OPTIONS}
              />
            </Field>
            <FrequencyFields draft={draft} onChange={patchDraft} today={today} />
          </div>
          <Field
            label="Time zone"
            hint={timezone === browserTz ? "Your browser's time zone." : `Runs follow the clock in ${formatTimeZone(timezone)}.`}
          >
            <Select
              value={timezone}
              onChange={(e) => {
                setTimezone(e.target.value);
                setScheduleTouched(true);
              }}
            >
              <optgroup label="Suggested">
                {suggested.map((tz) => (
                  <option key={tz} value={tz}>
                    {zoneLabel(tz)}
                  </option>
                ))}
              </optgroup>
              <optgroup label="All time zones">
                {zones
                  .filter((tz) => !suggested.includes(tz))
                  .map((tz) => (
                    <option key={tz} value={tz}>
                      {formatTimeZone(tz)}
                    </option>
                  ))}
              </optgroup>
            </Select>
          </Field>
          <PreviewBox
            built={built}
            reason={missingReason(draft)}
            preview={preview}
            busy={!settled}
            timezone={timezone}
            browserTz={browserTz}
          />
        </fieldset>
      </div>
    </Dialog>
  );
}
