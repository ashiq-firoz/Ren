/* eslint-disable @typescript-eslint/no-explicit-any */
import { buildUrl } from "./api";
import { browserTimeZone } from "./format";
import type { StreamEvent } from "./types";

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export type StreamListener = (event: StreamEvent) => void;

export interface StreamClientOptions {
  getToken: () => string | null;
  getOrg: () => string | null;
  /** Called after every successful (re)connect. */
  onOpen?: (info: { isReconnect: boolean }) => void;
  /** Called when the stream answers 401 — the session is gone. */
  onUnauthorized?: () => void;
}

const MIN_BACKOFF = 1_000;
const MAX_BACKOFF = 30_000;
/** Heartbeat arrives every 15 s; give it some slack before assuming the socket is dead. */
const WATCHDOG_MS = 45_000;

/**
 * One SSE connection to `GET /api/stream`, implemented with fetch + ReadableStream
 * (EventSource can't send the Authorization header). Reconnects with exponential
 * backoff (1s → 30s) and a heartbeat watchdog.
 */
export class StreamClient {
  private listeners = new Set<StreamListener>();
  private statusListeners = new Set<(status: StreamStatus) => void>();
  private controller: AbortController | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = true;
  private hasConnected = false;
  private _status: StreamStatus = "idle";

  constructor(private readonly opts: StreamClientOptions) {}

  get status() {
    return this._status;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    if (typeof window !== "undefined") window.addEventListener("online", this.handleOnline);
    void this.connect();
  }

  stop() {
    this.stopped = true;
    if (typeof window !== "undefined") window.removeEventListener("online", this.handleOnline);
    this.clearTimers();
    this.controller?.abort();
    this.controller = null;
    this.setStatus("closed");
  }

  /** Force an immediate reconnect (e.g. after network comes back). */
  reconnectNow() {
    if (this.stopped) return;
    this.clearTimers();
    this.controller?.abort();
    this.attempt = 0;
    void this.connect();
  }

  subscribe(listener: StreamListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  subscribeStatus(listener: (status: StreamStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  private handleOnline = () => {
    if (this._status !== "open") this.reconnectNow();
  };

  private setStatus(status: StreamStatus) {
    if (this._status === status) return;
    this._status = status;
    this.statusListeners.forEach((l) => l(status));
  }

  private clearTimers() {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.watchdog) clearTimeout(this.watchdog);
    this.retryTimer = null;
    this.watchdog = null;
  }

  private armWatchdog(controller: AbortController) {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => controller.abort(), WATCHDOG_MS);
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    this.setStatus("reconnecting");
    const base = Math.min(MAX_BACKOFF, MIN_BACKOFF * 2 ** this.attempt);
    const delay = Math.round(base * (0.85 + Math.random() * 0.3));
    this.attempt += 1;
    this.retryTimer = setTimeout(() => void this.connect(), delay);
  }

  private async connect() {
    if (this.stopped) return;
    const token = this.opts.getToken();
    if (!token) {
      this.setStatus("closed");
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    this.setStatus(this.hasConnected || this.attempt > 0 ? "reconnecting" : "connecting");

    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      Authorization: `Bearer ${token}`,
    };
    const org = this.opts.getOrg();
    if (org) headers["X-Org-Id"] = org;
    const tz = browserTimeZone();
    if (tz) headers["X-Timezone"] = tz;

    try {
      const res = await fetch(buildUrl("/stream"), {
        headers,
        signal: controller.signal,
        cache: "no-store",
      });
      if (res.status === 401) {
        this.stop();
        this.opts.onUnauthorized?.();
        return;
      }
      if (!res.ok || !res.body) throw new Error(`stream responded ${res.status}`);

      const isReconnect = this.hasConnected;
      this.hasConnected = true;
      this.attempt = 0;
      this.setStatus("open");
      this.armWatchdog(controller);
      this.opts.onOpen?.({ isReconnect });

      await this.read(res.body, controller);
      // The server closed the stream — fall through to reconnect.
    } catch {
      /* network error, abort by watchdog, or HTTP error → reconnect below */
    } finally {
      if (this.watchdog) clearTimeout(this.watchdog);
    }
    if (this.controller === controller) this.controller = null;
    if (!this.stopped && !(controller.signal.aborted && this.controller !== null)) {
      this.scheduleReconnect();
    }
  }

  private async read(body: ReadableStream<Uint8Array>, controller: AbortController) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let dataLines: string[] = [];
    let eventName = "";

    const dispatch = () => {
      if (dataLines.length === 0) {
        eventName = "";
        return;
      }
      const raw = dataLines.join("\n");
      dataLines = [];
      const fallbackType = eventName;
      eventName = "";
      let parsed: any;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return;
      }
      if (!parsed || typeof parsed !== "object") return;
      const type: string | undefined = parsed.type ?? (fallbackType || undefined);
      if (!type) return;
      const event = { type, data: parsed.data ?? {} } as StreamEvent;
      this.listeners.forEach((l) => {
        try {
          l(event);
        } catch (err) {
          console.error("[stream] listener failed", err);
        }
      });
    };

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        this.armWatchdog(controller);
        buffer += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          let line = buffer.slice(0, nl);
          buffer = buffer.slice(nl + 1);
          if (line.endsWith("\r")) line = line.slice(0, -1);
          if (line === "") {
            dispatch();
            continue;
          }
          if (line.startsWith(":")) continue; // comment / keep-alive
          const colon = line.indexOf(":");
          const field = colon === -1 ? line : line.slice(0, colon);
          let val = colon === -1 ? "" : line.slice(colon + 1);
          if (val.startsWith(" ")) val = val.slice(1);
          if (field === "data") dataLines.push(val);
          else if (field === "event") eventName = val;
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        /* ignore */
      }
    }
  }
}
