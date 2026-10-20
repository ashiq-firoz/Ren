"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { browserTimeZone } from "@/lib/format";

/* ---------------- Ticking clock (for live durations) ---------------- */

let now = typeof window === "undefined" ? 0 : Date.now();
const clockListeners = new Set<() => void>();
let clockTimer: ReturnType<typeof setInterval> | null = null;

function subscribeClock(listener: () => void) {
  clockListeners.add(listener);
  now = Date.now();
  if (!clockTimer) {
    clockTimer = setInterval(() => {
      now = Date.now();
      clockListeners.forEach((l) => l());
    }, 1000);
  }
  return () => {
    clockListeners.delete(listener);
    if (clockListeners.size === 0 && clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

const noopSubscribe = () => () => {};

/** Current time (ms), refreshed every second while `enabled`. Returns 0 during SSR. */
export function useNow(enabled = true): number {
  return useSyncExternalStore(
    enabled ? subscribeClock : noopSubscribe,
    () => now,
    () => 0,
  );
}

/* ---------------- Media queries ---------------- */

export function useMediaQuery(query: string, serverValue = false): boolean {
  const subscribe = useCallback(
    (listener: () => void) => {
      const mq = window.matchMedia(query);
      mq.addEventListener("change", listener);
      return () => mq.removeEventListener("change", listener);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => serverValue,
  );
}

/* ---------------- Time zone ---------------- */

const readTimeZone = () => browserTimeZone() ?? "UTC";

/** The browser's IANA time zone ("UTC" during SSR). */
export function useBrowserTimeZone(): string {
  return useSyncExternalStore(noopSubscribe, readTimeZone, () => "UTC");
}

/* ---------------- localStorage-backed preference ---------------- */

const prefListeners = new Map<string, Set<() => void>>();

function readPref(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** A small persisted UI preference (e.g. collapsed panels). Server snapshot = `fallback`. */
export function useLocalPreference<T extends string>(key: string, fallback: T): [T, (v: T) => void] {
  const subscribe = useCallback(
    (listener: () => void) => {
      let set = prefListeners.get(key);
      if (!set) {
        set = new Set();
        prefListeners.set(key, set);
      }
      set.add(listener);
      const onStorage = (e: StorageEvent) => {
        if (e.key === key) listener();
      };
      window.addEventListener("storage", onStorage);
      return () => {
        set?.delete(listener);
        window.removeEventListener("storage", onStorage);
      };
    },
    [key],
  );
  const value = useSyncExternalStore(
    subscribe,
    () => (readPref(key) as T | null) ?? fallback,
    () => fallback,
  );
  const setValue = useCallback(
    (v: T) => {
      try {
        window.localStorage.setItem(key, v);
      } catch {
        /* ignore */
      }
      prefListeners.get(key)?.forEach((l) => l());
    },
    [key],
  );
  return [value, setValue];
}

/** Debounce a changing value. */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
