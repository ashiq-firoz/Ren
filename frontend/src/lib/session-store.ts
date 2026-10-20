import { useSyncExternalStore } from "react";
import { session } from "./api";
import type { OrgSummary, User } from "./types";

/**
 * Browser-only session snapshot (token, active workspace, cached profile) exposed
 * through useSyncExternalStore. During SSR/hydration the snapshot is `null`, so
 * server and client markup match; the client then re-renders with real values.
 */
export interface SessionSnapshot {
  token: string | null;
  orgId: string | null;
  user: User | null;
  orgs: OrgSummary[] | null;
}

let snapshot: SessionSnapshot | null = null;
const listeners = new Set<() => void>();

function read(): SessionSnapshot {
  return {
    token: session.getToken(),
    orgId: session.getOrg(),
    user: session.getCachedUser(),
    orgs: session.getCachedOrgs(),
  };
}

function emit() {
  listeners.forEach((l) => l());
}

function onStorage(e: StorageEvent) {
  if (e.key === null || e.key.startsWith("ren_")) {
    snapshot = read();
    emit();
  }
}

export const sessionStore = {
  get(): SessionSnapshot {
    if (!snapshot) snapshot = read();
    return snapshot;
  },
  getServer(): SessionSnapshot | null {
    return null;
  },
  subscribe(listener: () => void) {
    if (listeners.size === 0 && typeof window !== "undefined") window.addEventListener("storage", onStorage);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0 && typeof window !== "undefined") window.removeEventListener("storage", onStorage);
    };
  },
  /** Persist and publish a partial update. */
  set(patch: Partial<SessionSnapshot>) {
    const next = { ...sessionStore.get(), ...patch };
    if ("token" in patch) session.setToken(next.token);
    if ("orgId" in patch) session.setOrg(next.orgId);
    if (("user" in patch || "orgs" in patch) && next.user && next.orgs) session.cacheProfile(next.user, next.orgs);
    snapshot = next;
    emit();
  },
  clear() {
    session.clear();
    snapshot = { token: null, orgId: null, user: null, orgs: null };
    emit();
  },
};

export function useSessionSnapshot(): SessionSnapshot | null {
  return useSyncExternalStore(sessionStore.subscribe, sessionStore.get, sessionStore.getServer);
}

/** Choose a valid active workspace: the preferred/stored one if still a member, else the first. */
export function pickOrgId(orgs: OrgSummary[] | null, preferred?: string | null): string | null {
  if (!orgs || orgs.length === 0) return null;
  if (preferred && orgs.some((o) => o.id === preferred)) return preferred;
  return orgs[0].id;
}
