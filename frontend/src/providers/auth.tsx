"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from "react";
import { api, ApiError, setUnauthorizedHandler } from "@/lib/api";
import { qk } from "@/lib/query-keys";
import { pickOrgId, sessionStore, useSessionSnapshot } from "@/lib/session-store";
import type { AuthResponse, MeResponse, OrgSummary, User, WorkspaceMode } from "@/lib/types";

/**
 * - loading: hydrating / verifying the stored session
 * - authenticated: token present (profile may come from cache while /me refreshes)
 * - unauthenticated: no token
 * - offline: token present, no cached profile, and the API is unreachable
 */
export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "offline";

export interface RegisterPayload {
  email: string;
  password: string;
  name: string;
  org_name?: string;
  mode: WorkspaceMode;
}

export interface AcceptInvitePayload {
  token: string;
  name?: string;
  password?: string;
}

interface AuthContextValue {
  status: AuthStatus;
  token: string | null;
  user: User | null;
  orgs: OrgSummary[];
  activeOrg: OrgSummary | null;
  activeOrgId: string | null;
  /** Set when the last /auth/me refresh failed for a non-auth reason (e.g. API down). */
  connectionError: string | null;
  login: (email: string, password: string) => Promise<AuthResponse>;
  register: (payload: RegisterPayload) => Promise<AuthResponse>;
  acceptInvite: (payload: AcceptInvitePayload, orgName?: string) => Promise<AuthResponse>;
  logout: () => void;
  switchOrg: (orgId: string) => void;
  addOrg: (org: OrgSummary, opts?: { activate?: boolean }) => void;
  refresh: () => Promise<unknown>;
  setUser: (user: User) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Detail pages whose ids are workspace-specific; on workspace switch we go up to the list. */
const DETAIL_ROUTE = /^\/(chat|tasks|agents|knowledge|outputs)\/(?!new$|search$)[^/]+/;

async function fetchMe(): Promise<MeResponse> {
  const me = await api.get<MeResponse>("/auth/me");
  const current = sessionStore.get();
  sessionStore.set({ user: me.user, orgs: me.orgs, orgId: pickOrgId(me.orgs, current.orgId) });
  return me;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const snap = useSessionSnapshot();
  const pathRef = useRef(pathname);

  useEffect(() => {
    pathRef.current = pathname;
  }, [pathname]);

  const meQuery = useQuery({
    queryKey: [...qk.me, snap?.token ?? ""],
    queryFn: fetchMe,
    enabled: !!snap?.token,
    staleTime: 5 * 60_000,
    gcTime: Infinity,
    retry: (count, err) => !(err instanceof ApiError && err.status > 0) && count < 1,
  });

  // 401 anywhere → clear the session and go to /login.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      qc.clear();
      sessionStore.clear();
      const current = pathRef.current || "/";
      const isPublic = /^\/(login|register|invite)/.test(current);
      if (!isPublic) {
        const next = current !== "/" ? `?next=${encodeURIComponent(current)}` : "";
        router.replace(`/login${next}`);
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [qc, router]);

  const applyAuth = useCallback(
    (res: AuthResponse, preferredOrgId?: string | null) => {
      qc.clear();
      sessionStore.set({
        token: res.access_token,
        user: res.user,
        orgs: res.orgs,
        orgId: pickOrgId(res.orgs, preferredOrgId ?? sessionStore.get().orgId),
      });
    },
    [qc],
  );

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.post<AuthResponse>("/auth/login", { email, password });
      applyAuth(res);
      return res;
    },
    [applyAuth],
  );

  const register = useCallback(
    async (payload: RegisterPayload) => {
      const res = await api.post<AuthResponse>("/auth/register", payload);
      applyAuth(res, res.orgs[0]?.id ?? null);
      return res;
    },
    [applyAuth],
  );

  const acceptInvite = useCallback(
    async (payload: AcceptInvitePayload, orgName?: string) => {
      const res = await api.post<AuthResponse>("/auth/accept-invite", payload);
      const invited = orgName ? res.orgs.find((o) => o.name === orgName) : undefined;
      applyAuth(res, invited?.id ?? res.orgs[res.orgs.length - 1]?.id ?? null);
      return res;
    },
    [applyAuth],
  );

  const logout = useCallback(() => {
    sessionStore.clear();
    qc.clear();
    router.replace("/login");
  }, [qc, router]);

  const switchOrg = useCallback(
    (orgId: string) => {
      if (sessionStore.get().orgId === orgId) return;
      // Queries aren't keyed by workspace — drop everything; the (app) layout remounts on org change.
      qc.clear();
      sessionStore.set({ orgId });
      const m = DETAIL_ROUTE.exec(pathRef.current);
      if (m) router.push(`/${m[1]}`);
    },
    [qc, router],
  );

  const addOrg = useCallback(
    (org: OrgSummary, opts: { activate?: boolean } = {}) => {
      const current = sessionStore.get();
      const orgs = (current.orgs ?? []).some((o) => o.id === org.id) ? current.orgs : [...(current.orgs ?? []), org];
      if (opts.activate) qc.clear();
      sessionStore.set({ orgs, ...(opts.activate ? { orgId: org.id } : {}) });
    },
    [qc],
  );

  const setUser = useCallback((user: User) => sessionStore.set({ user }), []);
  const { refetch } = meQuery;
  const refresh = useCallback(() => refetch(), [refetch]);

  const value = useMemo<AuthContextValue>(() => {
    const token = snap?.token ?? null;
    const user = snap?.user ?? null;
    const orgs = snap?.orgs ?? [];
    const activeOrgId = pickOrgId(snap?.orgs ?? null, snap?.orgId ?? null);
    const activeOrg = orgs.find((o) => o.id === activeOrgId) ?? null;
    const err = meQuery.error;
    const connectionError = err && !(err instanceof ApiError && err.status === 401) ? (err as Error).message : null;
    let status: AuthStatus;
    if (snap === null) status = "loading";
    else if (!token) status = "unauthenticated";
    else if (user && snap.orgs) status = "authenticated";
    else if (connectionError) status = "offline";
    else status = "loading";
    return {
      status,
      token,
      user,
      orgs,
      activeOrg,
      activeOrgId,
      connectionError,
      login,
      register,
      acceptInvite,
      logout,
      switchOrg,
      addOrg,
      refresh,
      setUser,
    };
  }, [snap, meQuery.error, login, register, acceptInvite, logout, switchOrg, addOrg, refresh, setUser]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

/** Role helpers for the active workspace. */
export function useRole() {
  const { activeOrg } = useAuth();
  const role = activeOrg?.role ?? "viewer";
  return {
    role,
    isOwner: role === "owner",
    isAdmin: role === "owner" || role === "admin",
    canWrite: role !== "viewer",
    mode: activeOrg?.mode ?? "business",
  };
}
