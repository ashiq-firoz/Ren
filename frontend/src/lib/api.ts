/* eslint-disable @typescript-eslint/no-explicit-any */
import { browserTimeZone } from "./format";
import type { OrgSummary, User } from "./types";

/**
 * Backend origin. Every route lives under `/api`.
 * An empty NEXT_PUBLIC_API_URL means "same origin" (e.g. behind the Caddy proxy that routes /api to the backend).
 */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").trim().replace(/\/+$/, "");

/** Absolute API base for display and URL building: the configured origin, else the page's origin (SSR-safe). */
export function getApiBase(): string {
  if (API_URL) return API_URL;
  if (typeof window !== "undefined") return window.location.origin;
  return "";
}

const TOKEN_KEY = "ren_token";
const ORG_KEY = "ren_org";
const USER_CACHE_KEY = "ren_user";
const ORGS_CACHE_KEY = "ren_orgs";

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Session persistence (token + active workspace) in localStorage. */
export const session = {
  getToken(): string | null {
    return storage()?.getItem(TOKEN_KEY) ?? null;
  },
  setToken(token: string | null) {
    const s = storage();
    if (!s) return;
    if (token) s.setItem(TOKEN_KEY, token);
    else s.removeItem(TOKEN_KEY);
  },
  getOrg(): string | null {
    return storage()?.getItem(ORG_KEY) ?? null;
  },
  setOrg(orgId: string | null) {
    const s = storage();
    if (!s) return;
    if (orgId) s.setItem(ORG_KEY, orgId);
    else s.removeItem(ORG_KEY);
  },
  getCachedUser(): User | null {
    try {
      const raw = storage()?.getItem(USER_CACHE_KEY);
      return raw ? (JSON.parse(raw) as User) : null;
    } catch {
      return null;
    }
  },
  getCachedOrgs(): OrgSummary[] | null {
    try {
      const raw = storage()?.getItem(ORGS_CACHE_KEY);
      return raw ? (JSON.parse(raw) as OrgSummary[]) : null;
    } catch {
      return null;
    }
  },
  cacheProfile(user: User, orgs: OrgSummary[]) {
    const s = storage();
    if (!s) return;
    try {
      s.setItem(USER_CACHE_KEY, JSON.stringify(user));
      s.setItem(ORGS_CACHE_KEY, JSON.stringify(orgs));
    } catch {
      /* quota — ignore */
    }
  },
  clear() {
    const s = storage();
    if (!s) return;
    s.removeItem(TOKEN_KEY);
    s.removeItem(ORG_KEY);
    s.removeItem(USER_CACHE_KEY);
    s.removeItem(ORGS_CACHE_KEY);
  },
};

/** Error carrying the backend's `detail` message. `status === 0` means the API was unreachable. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly data: unknown;

  constructor(status: number, detail: string, data: unknown = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.data = data;
  }

  get isNetwork() {
    return this.status === 0;
  }
  get isNotFound() {
    return this.status === 404;
  }
  get isForbidden() {
    return this.status === 403;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/** Human-readable message for any thrown value. */
export function errorMessage(err: unknown, fallback = "Something went wrong"): string {
  if (err instanceof ApiError) return err.detail || fallback;
  if (err instanceof Error) return err.message || fallback;
  if (typeof err === "string") return err;
  return fallback;
}

/** FastAPI returns `detail` as a string, or an array of validation errors (422). */
export function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: any) => {
        if (d && typeof d === "object") {
          const loc = Array.isArray(d.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
          const msg = d.msg ?? JSON.stringify(d);
          return loc ? `${loc}: ${msg}` : String(msg);
        }
        return String(d);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return "Request failed";
}

function unreachableMessage() {
  const base = getApiBase();
  return `Can't reach the Ren API${base ? ` at ${base}` : ""}. Check that the backend is running.`;
}

/** Routes where a 401 means "bad credentials", not "session expired". */
const PUBLIC_AUTH_PATHS = ["/auth/login", "/auth/register", "/auth/accept-invite", "/auth/invite/"];

let unauthorizedHandler: (() => void) | null = null;

/** Register what happens on 401 (the auth provider clears state and routes to /login). */
export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

export function handleUnauthorized(path = "") {
  if (PUBLIC_AUTH_PATHS.some((p) => path.startsWith(p))) return;
  session.clear();
  // The AuthProvider registers a handler that resets state and routes to /login.
  unauthorizedHandler?.();
}

export type QueryParams = Record<string, string | number | boolean | null | undefined>;

function normalizePath(path: string) {
  const p = path.startsWith("/") ? path : `/${path}`;
  return p.startsWith("/api/") || p === "/api" ? p.slice(4) : p;
}

export function buildUrl(path: string, query?: QueryParams): string {
  const route = `/api${normalizePath(path)}`;
  // Same-origin mode (empty API_URL): resolve against the page origin; on the server use a
  // placeholder base and return a relative URL so nothing throws during SSR.
  const base = API_URL || (typeof window !== "undefined" ? window.location.origin : "http://localhost");
  const url = API_URL ? new URL(`${API_URL}${route}`) : new URL(route, base);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }
  if (!API_URL && typeof window === "undefined") return `${url.pathname}${url.search}`;
  return url.toString();
}

export function authHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  const token = session.getToken();
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  const org = session.getOrg();
  if (org && !headers.has("X-Org-Id")) headers.set("X-Org-Id", org);
  // The backend remembers it on the user for schedules made in chat and "today" in Ren's prompt.
  const tz = browserTimeZone();
  if (tz && !headers.has("X-Timezone")) headers.set("X-Timezone", tz);
  return headers;
}

async function toApiError(res: Response): Promise<ApiError> {
  let data: unknown = null;
  let detail = res.statusText || `Request failed (${res.status})`;
  try {
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
  } catch {
    /* ignore body read errors */
  }
  if (data && typeof data === "object" && "detail" in (data as object)) {
    detail = formatDetail((data as { detail: unknown }).detail);
  } else if (typeof data === "string" && data.trim() && data.length < 400) {
    detail = data;
  }
  if (res.status === 403 && detail === "Forbidden") detail = "You don't have permission to do that.";
  return new ApiError(res.status, detail, data);
}

interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  form?: FormData;
  headers?: HeadersInit;
  signal?: AbortSignal;
}

/** Low-level fetch with auth headers, error mapping and 401 handling. Returns the raw Response. */
export async function rawFetch(method: string, path: string, opts: RequestOptions = {}): Promise<Response> {
  const headers = authHeaders(opts.headers);
  let body: BodyInit | undefined;
  if (opts.form) {
    body = opts.form; // the browser sets the multipart boundary
  } else if (opts.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, unreachableMessage());
  }
  if (res.status === 401) {
    const error = await toApiError(res);
    handleUnauthorized(normalizePath(path));
    throw error;
  }
  if (!res.ok) throw await toApiError(res);
  return res;
}

async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers = new Headers(opts.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  const res = await rawFetch(method, path, { ...opts, headers });
  if (res.status === 204) return undefined as T;
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("json")) return (await res.json()) as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as unknown as T;
  }
}

export interface UploadOptions {
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/** Multipart upload via XHR so we can report upload progress. */
function upload<T>(path: string, form: FormData, opts: UploadOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildUrl(path));
    authHeaders({ Accept: "application/json" }).forEach((value, key) => xhr.setRequestHeader(key, value));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data: any = null;
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        data = xhr.responseText;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        opts.onProgress?.(1);
        resolve(data as T);
        return;
      }
      if (xhr.status === 401) handleUnauthorized(normalizePath(path));
      const detail =
        data && typeof data === "object" && "detail" in data
          ? formatDetail(data.detail)
          : `Upload failed (${xhr.status || "network error"})`;
      reject(new ApiError(xhr.status, xhr.status === 0 ? unreachableMessage() : detail, data));
    };
    xhr.onerror = () => reject(new ApiError(0, unreachableMessage()));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    if (opts.signal) {
      if (opts.signal.aborted) {
        xhr.abort();
        return;
      }
      opts.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(form);
  });
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename\s*=\s*("?)([^";]+)\1/i.exec(header);
  return plain ? plain[2].trim() : null;
}

/** Trigger a browser download for a Blob. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/**
 * Download a file endpoint. Downloads need the bearer header, so we fetch the
 * bytes ourselves and then hand them to the browser.
 */
async function download(path: string, query?: QueryParams, fallbackName = "download"): Promise<void> {
  const res = await rawFetch("GET", path, { query });
  const blob = await res.blob();
  const name = filenameFromDisposition(res.headers.get("content-disposition")) ?? fallbackName;
  saveBlob(blob, name);
}

export const api = {
  get: <T>(path: string, query?: QueryParams, init?: { signal?: AbortSignal }) =>
    request<T>("GET", path, { query, signal: init?.signal }),
  post: <T>(path: string, body?: unknown, init?: { query?: QueryParams; signal?: AbortSignal }) =>
    request<T>("POST", path, { body: body ?? {}, ...init }),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, { body: body ?? {} }),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, { body: body ?? {} }),
  delete: <T = { ok: true }>(path: string, query?: QueryParams) => request<T>("DELETE", path, { query }),
  postForm: <T>(path: string, form: FormData) => request<T>("POST", path, { form }),
  upload,
  download,
};
