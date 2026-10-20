import type { Provider } from "./types";

/** `extra.fallback_models` (OpenRouter): models tried in order when the primary is rate-limited. */
export function fallbackModelsOf(p: Pick<Provider, "extra"> | null | undefined): string[] {
  const v = p?.extra?.fallback_models;
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && x.trim().length > 0) : [];
}

/** "a, b\nc" → ["a", "b", "c"] (trimmed, de-duplicated, order kept). */
export function parseModelList(text: string): string[] {
  return Array.from(new Set(text.split(/[,\n]/).map((m) => m.trim()).filter(Boolean)));
}
