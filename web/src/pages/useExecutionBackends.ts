import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { api, type ExecutionBackend } from "../api/client";

export function isExecutionBackendPending(backend: ExecutionBackend | undefined): boolean {
  return Boolean(backend && !backend.available && backend.reason === `${backend.id}_probe_pending`);
}

export function executionBackendOptionLabel(backend: ExecutionBackend): string {
  return `${backend.name}${backend.available ? "" : isExecutionBackendPending(backend) ? "（准备中）" : "（不可用）"}`;
}

export function useExecutionBackends() {
  const pendingWindow = useRef<{ startedAt: number; firstUpdate: number } | null>(null);
  return useQuery({
    queryKey: ["execution-backends"],
    queryFn: () => api.executionBackends(),
    retry: false,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      if (query.state.status !== "success" || !query.state.data?.some(isExecutionBackendPending)) {
        pendingWindow.current = null;
        return false;
      }
      // Cover the server's bounded cold probe, without polling an indefinitely
      // pending backend forever. Shared query key retains request deduplication.
      pendingWindow.current ??= { startedAt: Date.now(), firstUpdate: query.state.dataUpdateCount };
      const window = pendingWindow.current;
      return Date.now() - window.startedAt < 35_000 && query.state.dataUpdateCount - window.firstUpdate < 30
        ? 1000 : false;
    },
  });
}
