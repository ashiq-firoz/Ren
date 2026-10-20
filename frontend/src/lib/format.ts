function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "just now", "5m ago", "3h ago", "Yesterday", "Mar 4". */
export function timeAgo(value: string | null | undefined, opts: { short?: boolean } = {}): string {
  const d = toDate(value);
  if (!d) return "—";
  const diff = (Date.now() - d.getTime()) / 1000;
  const suffix = opts.short ? "" : " ago";
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m${suffix}`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h${suffix}`;
  if (diff < 172800) return "Yesterday";
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)}d${suffix}`;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

export interface DateTimeOptions {
  /** IANA zone to show the time in (default: the browser's). */
  timeZone?: string | null;
  /** Prefix the weekday: "Tue, Oct 6, 9:00 AM". */
  weekday?: boolean;
}

export function formatDateTime(value: string | null | undefined, opts: DateTimeOptions = {}): string {
  const d = toDate(value);
  if (!d) return "—";
  const options: Intl.DateTimeFormatOptions = {
    ...(opts.weekday ? { weekday: "short" } : {}),
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (opts.timeZone) {
    try {
      return d.toLocaleString(undefined, { ...options, timeZone: opts.timeZone });
    } catch {
      /* unknown zone — fall back to local time */
    }
  }
  return d.toLocaleString(undefined, options);
}

/**
 * Browsers report CLDR's canonical zone IDs, which keep pre-rename names (Chrome: "Asia/Calcutta"); the backend stores
 * current IANA names, so translate before sending or comparing. Mirrors `_LEGACY_ZONES` in app/services/scheduler.py.
 */
const LEGACY_ZONES: Record<string, string> = {
  "Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh",
  "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
  "Asia/Ujung_Pandang": "Asia/Makassar", "Asia/Macao": "Asia/Macau", "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
  "Asia/Chongqing": "Asia/Shanghai", "Asia/Chungking": "Asia/Shanghai", "Asia/Harbin": "Asia/Shanghai",
  "Asia/Kashgar": "Asia/Urumqi", "Asia/Tel_Aviv": "Asia/Jerusalem", "Asia/Istanbul": "Europe/Istanbul",
  "Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv", "Europe/Zaporozhye": "Europe/Kyiv",
  "Europe/Belfast": "Europe/London", "Atlantic/Faeroe": "Atlantic/Faroe", "Africa/Asmera": "Africa/Asmara",
  "America/Buenos_Aires": "America/Argentina/Buenos_Aires", "America/Catamarca": "America/Argentina/Catamarca",
  "America/Cordoba": "America/Argentina/Cordoba", "America/Jujuy": "America/Argentina/Jujuy",
  "America/Mendoza": "America/Argentina/Mendoza", "America/Indianapolis": "America/Indiana/Indianapolis",
  "America/Louisville": "America/Kentucky/Louisville", "America/Coral_Harbour": "America/Atikokan",
  "America/Godthab": "America/Nuuk", "America/Montreal": "America/Toronto", "America/Santa_Isabel": "America/Tijuana",
  "Australia/Currie": "Australia/Hobart", "Pacific/Truk": "Pacific/Chuuk", "Pacific/Ponape": "Pacific/Pohnpei",
  "Pacific/Enderbury": "Pacific/Kanton", "Pacific/Samoa": "Pacific/Pago_Pago",
};

export function canonicalTimeZone(tz: string): string {
  return LEGACY_ZONES[tz] ?? tz;
}

/** The browser's IANA time zone (e.g. "Asia/Kolkata"); null on the server or if unavailable. */
export function browserTimeZone(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return tz ? canonicalTimeZone(tz) : null;
  } catch {
    return null;
  }
}

/** "America/New_York" → "America/New York". */
export function formatTimeZone(tz: string | null | undefined): string {
  return tz ? tz.replace(/_/g, " ") : "—";
}

/** True for timestamps at or before now (e.g. a schedule that is due). */
export function isPast(value: string | null | undefined): boolean {
  const d = toDate(value);
  return !!d && d.getTime() <= Date.now();
}

export function formatDate(value: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function formatTime(value: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function formatTimeWithSeconds(value: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Seconds → "42s", "3m 12s", "1h 4m". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

export function durationBetween(start: string | null | undefined, end?: string | null): number | null {
  const a = toDate(start);
  if (!a) return null;
  const b = toDate(end) ?? new Date();
  return Math.max(0, (b.getTime() - a.getTime()) / 1000);
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** 1234 → "1,234"; with compact: 12_300 → "12.3k". */
export function formatNumber(n: number | null | undefined, opts: { compact?: boolean } = {}): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (opts.compact && Math.abs(n) >= 1000) {
    return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
  }
  return new Intl.NumberFormat().format(n);
}

/** Accepts 0..1 ratios (or 0..100 when `isPercent`). */
export function formatPercent(v: number | null | undefined, opts: { digits?: number; isPercent?: boolean } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const pct = opts.isPercent ? v : v * 100;
  return `${pct.toFixed(opts.digits ?? (pct % 1 === 0 ? 0 : 1))}%`;
}

export function dayLabel(value: string): string {
  const d = toDate(value.length === 10 ? `${value}T00:00:00` : value);
  if (!d) return value;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function isSameDay(a: string, b: string) {
  const da = toDate(a);
  const db = toDate(b);
  if (!da || !db) return false;
  return da.toDateString() === db.toDateString();
}

export function dayHeading(value: string): string {
  const d = toDate(value);
  if (!d) return "";
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

/** Future timestamps: "in 5m", "in 3h", "in 2d" (or "expired"). */
export function timeUntil(value: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "—";
  const diff = (d.getTime() - Date.now()) / 1000;
  if (diff <= 0) return "expired";
  if (diff < 3600) return `in ${Math.max(1, Math.round(diff / 60))}m`;
  if (diff < 86400) return `in ${Math.round(diff / 3600)}h`;
  return `in ${Math.round(diff / 86400)}d`;
}
