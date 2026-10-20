"use client";

import { usePathname } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocalPreference } from "@/hooks/use-browser";

interface ShellContextValue {
  mobileNavOpen: boolean;
  openMobileNav: () => void;
  closeMobileNav: () => void;
  collapsed: boolean;
  setCollapsed: (v: boolean) => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
}

const ShellContext = createContext<ShellContextValue | null>(null);

export function ShellProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  // The drawer is "open for" a pathname, so navigating closes it without an effect.
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sidebarPref, setSidebarPref] = useLocalPreference<"expanded" | "collapsed">("ren_sidebar", "expanded");

  const openMobileNav = useCallback(() => setNavOpenAt(pathname), [pathname]);
  const closeMobileNav = useCallback(() => setNavOpenAt(null), []);
  const setCollapsed = useCallback((v: boolean) => setSidebarPref(v ? "collapsed" : "expanded"), [setSidebarPref]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo(
    () => ({
      mobileNavOpen: navOpenAt !== null && navOpenAt === pathname,
      openMobileNav,
      closeMobileNav,
      collapsed: sidebarPref === "collapsed",
      setCollapsed,
      paletteOpen,
      setPaletteOpen,
    }),
    [navOpenAt, pathname, openMobileNav, closeMobileNav, sidebarPref, setCollapsed, paletteOpen],
  );
  return <ShellContext.Provider value={value}>{children}</ShellContext.Provider>;
}

export function useShell() {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell must be used inside ShellProvider");
  return ctx;
}
