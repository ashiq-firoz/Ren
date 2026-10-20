import { canonicalTimeZone } from "./format";
import type { ScheduleFilter } from "./query-keys";
import type { ScheduledJob, ScheduleStatus } from "./types";

/* ------------------------------------------------------------------ */
/* Status                                                              */
/* ------------------------------------------------------------------ */

export const ACTIVE_SCHEDULE_STATUSES: ScheduleStatus[] = ["scheduled", "paused"];
export const isScheduleActive = (s: ScheduleStatus) => ACTIVE_SCHEDULE_STATUSES.includes(s);

export const SCHEDULE_STATUS_LABEL: Record<ScheduleStatus, string> = {
  scheduled: "Scheduled",
  paused: "Paused",
  done: "Done",
  cancelled: "Cancelled",
  failed: "Failed",
};

/** Whether a job belongs in a `GET /schedules?status=` list (mirrors the backend filters). */
export function scheduleMatchesFilter(job: Pick<ScheduledJob, "status">, filter: ScheduleFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "active":
      return isScheduleActive(job.status);
    case "history":
      return !isScheduleActive(job.status);
    default:
      return job.status === filter;
  }
}

/* ------------------------------------------------------------------ */
/* Cron                                                                */
/* ------------------------------------------------------------------ */

const DAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DAY_NAMES: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };

/** Weekday toggles in business order (cron numbers: Sun = 0 … Sat = 6). */
export const WEEKDAYS: { value: number; short: string; long: string }[] = [
  { value: 1, short: "Mon", long: "Monday" },
  { value: 2, short: "Tue", long: "Tuesday" },
  { value: 3, short: "Wed", long: "Wednesday" },
  { value: 4, short: "Thu", long: "Thursday" },
  { value: 5, short: "Fri", long: "Friday" },
  { value: 6, short: "Sat", long: "Saturday" },
  { value: 0, short: "Sun", long: "Sunday" },
];

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Human summary for common 5-field cron expressions; null when it's not a simple pattern. */
export function describeCron(cron: string | null | undefined): string | null {
  if (!cron) return null;
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, mon, dow] = parts;
  const num = (s: string) => /^\d+$/.test(s);
  const step = (s: string) => /^\*\/\d+$/.test(s);
  const time = num(min) && num(hour) ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;
  const anyDay = dom === "*" && mon === "*";
  if (step(min) && hour === "*" && anyDay && dow === "*") return `Every ${min.slice(2)} minutes`;
  if (num(min) && step(hour) && anyDay && dow === "*") return `Every ${hour.slice(2)} hours`;
  if (num(min) && hour === "*" && anyDay && dow === "*") return `Hourly at :${min.padStart(2, "0")}`;
  if (time && anyDay) {
    if (dow === "*") return `Daily at ${time}`;
    if (dow === "1-5") return `Weekdays at ${time}`;
    if (dow === "0,6" || dow === "6,0") return `Weekends at ${time}`;
    if (/^[0-7](,[0-7])*$/.test(dow)) return `${dow.split(",").map((d) => DAY_SHORT[Number(d) % 7]).join(", ")} at ${time}`;
  }
  if (time && num(dom) && mon === "*" && dow === "*") return `Monthly on day ${dom} at ${time}`;
  return null;
}

/** The server's description, else a client-side summary of the cron, else a generic label. */
export function scheduleSummary(job: Pick<ScheduledJob, "description" | "cron">): string {
  return job.description || describeCron(job.cron) || (job.cron ? "Recurring" : "One-off");
}

/** The summary without its trailing "(Zone/Name)", for places that show the zone as a chip. */
export function scheduleSummaryWithoutZone(job: Pick<ScheduledJob, "description" | "cron" | "timezone">): string {
  const summary = scheduleSummary(job);
  const suffix = ` (${job.timezone})`;
  return job.timezone && summary.endsWith(suffix) ? summary.slice(0, -suffix.length) : summary;
}

function dayValue(token: string): number | null {
  const t = token.toLowerCase();
  if (t in DAY_NAMES) return DAY_NAMES[t];
  if (!/^\d$/.test(t)) return null;
  const n = Number(t);
  return n <= 7 ? n % 7 : null;
}

/** "1-5", "mon,wed,fri", "0,6" → sorted unique weekday numbers; null for steps or anything unusual. */
function parseWeekdays(field: string): number[] | null {
  const out = new Set<number>();
  for (const part of field.split(",")) {
    const [a, b, extra] = part.split("-");
    if (extra !== undefined) return null;
    const lo = dayValue(a);
    if (lo === null) return null;
    if (b === undefined) {
      out.add(lo);
      continue;
    }
    // "1-7" ends on Sunday; keep the raw upper bound so the range is ascending.
    const hiRaw = b.toLowerCase() in DAY_NAMES ? DAY_NAMES[b.toLowerCase()] : /^\d$/.test(b) ? Number(b) : null;
    if (hiRaw === null || hiRaw > 7 || hiRaw < lo) return null;
    for (let d = lo; d <= hiRaw; d++) out.add(d % 7);
  }
  return Array.from(out).sort((x, y) => x - y);
}

/* ------------------------------------------------------------------ */
/* Friendly schedule builder                                           */
/* ------------------------------------------------------------------ */

export type Frequency = "once" | "hourly" | "daily" | "weekdays" | "weekly" | "monthly" | "custom";

export const FREQUENCY_OPTIONS: { value: Frequency; label: string }[] = [
  { value: "once", label: "Once" },
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "custom", label: "Custom (cron)" },
];

/** Editor state. Every field keeps a sensible value so switching frequency never loses the time. */
export interface ScheduleDraft {
  frequency: Frequency;
  /** "YYYY-MM-DD" (once) */
  date: string;
  /** "HH:MM" 24h (once / daily / weekdays / weekly / monthly) */
  time: string;
  /** 0–59 (hourly) */
  minute: number;
  /** cron weekday numbers, Sun = 0 (weekly) */
  weekdays: number[];
  /** 1–31 (monthly) */
  dayOfMonth: number;
  /** raw 5-field expression (custom) */
  cron: string;
}

/** "YYYY-MM-DD" for a date input, in the browser's time zone. */
export function localDateInput(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function defaultDraft(): ScheduleDraft {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  return {
    frequency: "daily",
    date: localDateInput(tomorrow),
    time: "09:00",
    minute: 0,
    weekdays: [1],
    dayOfMonth: 1,
    cron: "0 9 * * 1-5",
  };
}

/** Map a cron expression onto the builder when it's a simple pattern; null → use "Custom". */
export function cronToDraft(cron: string): Partial<ScheduleDraft> | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, mon, dow] = parts;
  const int = (s: string, lo: number, hi: number) => (/^\d{1,2}$/.test(s) && +s >= lo && +s <= hi ? +s : null);
  const minute = int(min, 0, 59);
  if (minute === null || mon !== "*") return null;
  if (hour === "*") return dom === "*" && dow === "*" ? { frequency: "hourly", minute } : null;
  const h = int(hour, 0, 23);
  if (h === null) return null;
  const time = `${pad2(h)}:${pad2(minute)}`;
  if (dom === "*") {
    if (dow === "*") return { frequency: "daily", time };
    const days = parseWeekdays(dow);
    if (!days || days.length === 0) return null;
    if (days.length === 7) return { frequency: "daily", time };
    if (days.join(",") === "1,2,3,4,5") return { frequency: "weekdays", time };
    return { frequency: "weekly", time, weekdays: days };
  }
  const day = int(dom, 1, 31);
  return day !== null && dow === "*" ? { frequency: "monthly", time, dayOfMonth: day } : null;
}

/** Wall-clock date/time of an instant in `timeZone` ("YYYY-MM-DD", "HH:MM"). */
export function zonedDateTime(iso: string | null | undefined, timeZone: string | null | undefined): { date: string; time: string } | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone || undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(d);
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
    const hour = get("hour") === "24" ? "00" : get("hour");
    return { date: `${get("year")}-${get("month")}-${get("day")}`, time: `${hour}:${get("minute")}` };
  } catch {
    return null;
  }
}

/** Builder state for an existing job (recurring jobs fall back to "Custom" for unusual crons). */
export function draftFromJob(job: Pick<ScheduledJob, "cron" | "run_at" | "timezone">): ScheduleDraft {
  const base = defaultDraft();
  const wall = zonedDateTime(job.run_at, job.timezone);
  if (wall) {
    base.date = wall.date;
    base.time = wall.time;
  }
  if (!job.cron) return { ...base, frequency: "once" };
  const parsed = cronToDraft(job.cron);
  return parsed ? { ...base, ...parsed, cron: job.cron } : { ...base, frequency: "custom", cron: job.cron };
}

export interface BuiltSchedule {
  cron: string | null;
  /** Local wall-clock "YYYY-MM-DDTHH:MM" (no offset) — the backend reads it in the schedule's time zone. */
  run_at: string | null;
}

function splitTime(time: string): [number, number] | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(time.trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  return h <= 23 && min <= 59 ? [h, min] : null;
}

/** The `cron` / `run_at` pair for the API; null while the draft is incomplete. */
export function buildSchedule(d: ScheduleDraft): BuiltSchedule | null {
  if (d.frequency === "custom") {
    const cron = d.cron.trim().split(/\s+/).filter(Boolean).join(" ");
    return cron ? { cron, run_at: null } : null;
  }
  if (d.frequency === "hourly") {
    const minute = Math.round(d.minute);
    return Number.isFinite(minute) && minute >= 0 && minute <= 59 ? { cron: `${minute} * * * *`, run_at: null } : null;
  }
  const hm = splitTime(d.time);
  if (!hm) return null;
  const [h, m] = hm;
  switch (d.frequency) {
    case "once":
      return /^\d{4}-\d{2}-\d{2}$/.test(d.date) ? { cron: null, run_at: `${d.date}T${pad2(h)}:${pad2(m)}` } : null;
    case "daily":
      return { cron: `${m} ${h} * * *`, run_at: null };
    case "weekdays":
      return { cron: `${m} ${h} * * 1-5`, run_at: null };
    case "weekly": {
      const days = Array.from(new Set(d.weekdays)).sort((a, b) => a - b);
      return days.length ? { cron: `${m} ${h} * * ${days.join(",")}`, run_at: null } : null;
    }
    case "monthly":
      return d.dayOfMonth >= 1 && d.dayOfMonth <= 31 ? { cron: `${m} ${h} ${d.dayOfMonth} * *`, run_at: null } : null;
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/* Time zones                                                          */
/* ------------------------------------------------------------------ */

/** Unique zone names for a picker: the given ones first, then UTC and every zone the browser knows. */
export function timeZoneOptions(preferred: (string | null | undefined)[]): string[] {
  const all = new Set<string>();
  for (const tz of preferred) if (tz) all.add(tz);
  all.add("UTC");
  try {
    if (typeof Intl.supportedValuesOf === "function")
      Intl.supportedValuesOf("timeZone").forEach((tz) => all.add(canonicalTimeZone(tz)));
  } catch {
    /* older browsers: the preferred zones + UTC are enough */
  }
  return Array.from(all);
}
