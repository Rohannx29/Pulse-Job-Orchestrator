"use client";

import { useEffect, useRef, useState } from "react";
import { API_URL } from "./api";

export interface PulseEvent {
  event: string;
  org_id: string;
  timestamp: string;
  data: any;
}

export type ConnectionStatus = "connected" | "connecting" | "disconnected";

export function useEvents(onEvent?: (event: PulseEvent) => void) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let es: EventSource | null = null;
    let reconnectTimer: NodeJS.Timeout | null = null;
    let retryDelay = 2000;
    let isMounted = true;

    function connect() {
      if (!isMounted) return;
      setStatus("connecting");

      try {
        es = new EventSource(`${API_URL}/events`, { withCredentials: true });

        es.addEventListener("connected", () => {
          if (!isMounted) return;
          setStatus("connected");
          retryDelay = 2000; // Reset backoff on successful connection
        });

        es.addEventListener("update", (messageEvent) => {
          if (!isMounted) return;
          try {
            const parsed: PulseEvent = JSON.parse(messageEvent.data);
            if (onEventRef.current) {
              onEventRef.current(parsed);
            }
          } catch (e) {
            console.error("Failed to parse SSE event data:", e);
          }
        });

        es.onerror = () => {
          if (!isMounted) return;
          setStatus("disconnected");
          if (es) {
            es.close();
            es = null;
          }
          reconnectTimer = setTimeout(() => {
            retryDelay = Math.min(retryDelay * 1.5, 15000);
            connect();
          }, retryDelay);
        };
      } catch {
        if (!isMounted) return;
        setStatus("disconnected");
        reconnectTimer = setTimeout(connect, retryDelay);
      }
    }

    connect();

    return () => {
      isMounted = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (es) es.close();
    };
  }, []);

  return { status, isConnected: status === "connected" };
}
