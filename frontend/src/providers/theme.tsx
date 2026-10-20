"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

export type ThemePreference = "light" | "dark" | "system";
type Resolved = "light" | "dark";

const STORAGE_KEY = "ren_theme";
/** Beige is the default; dark (or following the OS) is opt-in. */
const DEFAULT_PREFERENCE: ThemePreference = "light";
/** Browser-chrome colour per theme; keep in sync with --bg in globals.css. */
const THEME_COLOR: Record<Resolved, string> = { light: "#efe7da", dark: "#0b1612" };

/* Preference store (localStorage + cross-tab sync) */
let preference: ThemePreference | null = null;
const prefListeners = new Set<() => void>();

function readPreference(): ThemePreference {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" || v === "system" ? v : DEFAULT_PREFERENCE;
  } catch {
    return DEFAULT_PREFERENCE;
  }
}

/** Write the resolved theme to <html data-theme>. The inline script in the root layout does this before first paint. */
function applyResolved() {
  const pref = preference ?? readPreference();
  const dark = pref === "dark" || (pref === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const resolved: Resolved = dark ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", resolved);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLOR[resolved]);
}

function onStorage(e: StorageEvent) {
  if (e.key !== STORAGE_KEY) return;
  preference = readPreference();
  applyResolved();
  prefListeners.forEach((l) => l());
}

const prefStore = {
  subscribe(l: () => void) {
    if (prefListeners.size === 0) window.addEventListener("storage", onStorage);
    prefListeners.add(l);
    return () => {
      prefListeners.delete(l);
      if (prefListeners.size === 0) window.removeEventListener("storage", onStorage);
    };
  },
  get(): ThemePreference {
    if (preference === null) preference = readPreference();
    return preference;
  },
  getServer(): ThemePreference {
    return DEFAULT_PREFERENCE;
  },
  set(t: ThemePreference) {
    try {
      window.localStorage.setItem(STORAGE_KEY, t);
    } catch {
      /* ignore */
    }
    preference = t;
    applyResolved();
    prefListeners.forEach((l) => l());
  },
};

/* System color-scheme store */
const systemStore = {
  subscribe(l: () => void) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", l);
    return () => mq.removeEventListener("change", l);
  },
  get(): boolean {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  },
  getServer(): boolean {
    return false;
  },
};

interface ThemeContextValue {
  theme: ThemePreference;
  resolved: Resolved;
  setTheme: (t: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const theme = useSyncExternalStore(prefStore.subscribe, prefStore.get, prefStore.getServer);
  const systemDark = useSyncExternalStore(systemStore.subscribe, systemStore.get, systemStore.getServer);
  const resolved: Resolved = theme === "system" ? (systemDark ? "dark" : "light") : theme;

  // Sync <meta name="theme-color"> on mount, then follow OS changes while the preference is "system".
  useEffect(() => {
    applyResolved();
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", applyResolved);
    return () => mq.removeEventListener("change", applyResolved);
  }, []);

  const setTheme = useCallback((t: ThemePreference) => prefStore.set(t), []);
  const value = useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}
