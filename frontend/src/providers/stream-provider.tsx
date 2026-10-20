"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { handleUnauthorized, session } from "@/lib/api";
import { StreamClient, type StreamListener, type StreamStatus } from "@/lib/stream";
import { createStreamDispatcher } from "@/lib/stream-dispatch";
import type { StreamEvent, StreamEventType } from "@/lib/types";
import { useAuth } from "./auth";

interface StreamContextValue {
  status: StreamStatus;
  /** Subscribe to every stream event (after the cache has been updated). */
  subscribe: (listener: StreamListener) => () => void;
  reconnect: () => void;
}

const StreamContext = createContext<StreamContextValue | null>(null);

/**
 * Owns the single SSE connection for this tab. Reconnects when the token or
 * active workspace changes, and refetches active views after a reconnect
 * (events are not replayed by the server).
 */
export function StreamProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { token, activeOrgId } = useAuth();
  const listeners = useRef(new Set<StreamListener>());
  const clientRef = useRef<StreamClient | null>(null);
  const [status, setStatus] = useState<StreamStatus>("idle");

  useEffect(() => {
    if (!token || !activeOrgId) return;
    const dispatcher = createStreamDispatcher(qc);
    const client = new StreamClient({
      getToken: () => session.getToken(),
      getOrg: () => session.getOrg(),
      onOpen: ({ isReconnect }) => {
        if (!isReconnect) return;
        // Events aren't replayed: drop transient live state and refetch what's on screen.
        qc.removeQueries({ queryKey: ["conversation-status"] });
        void qc.invalidateQueries();
      },
      onUnauthorized: () => handleUnauthorized("/stream"),
    });
    const offEvents = client.subscribe((event) => {
      dispatcher.handle(event);
      listeners.current.forEach((l) => {
        try {
          l(event);
        } catch (err) {
          console.error("[stream] subscriber failed", err);
        }
      });
    });
    const offStatus = client.subscribeStatus(setStatus);
    clientRef.current = client;
    client.start();
    return () => {
      offEvents();
      offStatus();
      client.stop();
      dispatcher.dispose();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [qc, token, activeOrgId]);

  const subscribe = useCallback((listener: StreamListener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const reconnect = useCallback(() => clientRef.current?.reconnectNow(), []);

  const value = useMemo(() => ({ status, subscribe, reconnect }), [status, subscribe, reconnect]);
  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
}

export function useStream() {
  const ctx = useContext(StreamContext);
  if (!ctx) throw new Error("useStream must be used inside StreamProvider");
  return ctx;
}

export function useStreamStatus(): StreamStatus {
  return useContext(StreamContext)?.status ?? "idle";
}

/** Run `handler` for stream events of the given type(s) (or all events with "*"). */
export function useStreamEvent<T extends StreamEventType>(
  types: T | T[] | "*",
  handler: (event: Extract<StreamEvent, { type: T }>) => void,
) {
  const subscribe = useContext(StreamContext)?.subscribe;
  const handlerRef = useRef(handler);
  useEffect(() => {
    handlerRef.current = handler;
  });
  const key = Array.isArray(types) ? types.join(",") : types;
  useEffect(() => {
    if (!subscribe) return;
    const wanted = key === "*" ? null : new Set(key.split(","));
    return subscribe((event) => {
      if (wanted && !wanted.has(event.type)) return;
      handlerRef.current(event as Extract<StreamEvent, { type: T }>);
    });
  }, [subscribe, key]);
}
