import { useQuery } from "@tanstack/react-query";
import { RotateCw } from "lucide-react";
import { useRef } from "react";

import { ApiError, api, type CapabilityApprovalEvent, type RunDetail } from "../api/client";
import { useAuth } from "../auth/AuthProvider";

type ApprovalRecord = {
  id: string;
  sequence: number;
  source: number;
  decision: "approved" | "rejected";
  createdAt: string;
  label?: string;
};

function safeIdentifier(value: string | null | undefined) {
  return value && /^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$/.test(value) ? value : undefined;
}

function approvalRecords(run: RunDetail, history: CapabilityApprovalEvent[]): ApprovalRecord[] {
  const records = new Map<string, ApprovalRecord>();
  // These streams have independent sequences. Never merge them into a worker cursor.
  for (const [source, events] of [run.events, history.filter((event) => event.run_id === run.id)].entries()) {
    const requests = new Map<string, string | undefined>();
    for (const event of [...events].sort((left, right) => left.sequence - right.sequence)) {
      if (!event.approval_id) continue;
      if (event.kind === "approval.requested") {
        const capability = event.payload.capability;
        const operation = event.payload.operation;
        const capabilityLabel = capability === "file" && operation === "write" ? "写入文件"
          : capability === "file" && operation === "create" ? "创建文件"
          : capability === "command" && operation === "run" ? "执行命令" : undefined;
        requests.set(event.approval_id, safeIdentifier(event.tool_name) ?? capabilityLabel
          ?? (event.action === "capability_tool" ? "沙箱工具调用" : safeIdentifier(event.action)));
      } else if (event.kind === "approval.resolved"
        && (event.decision === "approved" || event.decision === "rejected")) {
        records.set(event.approval_id, {
          id: event.approval_id, sequence: event.sequence, source, decision: event.decision,
          createdAt: event.created_at, label: requests.get(event.approval_id),
        });
      }
    }
  }
  return [...records.values()].sort((left, right) => {
    const time = Date.parse(left.createdAt) - Date.parse(right.createdAt);
    return (Number.isFinite(time) ? time : 0)
      || (left.source === right.source ? left.sequence - right.sequence : 0)
      || left.id.localeCompare(right.id);
  });
}

export function RunApprovalHistory({ run }: { run: RunDetail }) {
  const { user, hasPermission } = useAuth();
  const actorScope = user ? JSON.stringify([user.tenant_id, user.user_id, user.role]) : null;
  const legacyActor = useRef(actorScope);
  if (legacyActor.current === null && actorScope !== null) legacyActor.current = actorScope;
  const terminal = ["completed", "failed", "cancelled"].includes(run.status);
  const history = useQuery({
    queryKey: ["capability-approval-history", user?.tenant_id, user?.user_id, user?.role, run.id, terminal],
    queryFn: ({ signal }) => api.capabilityApprovals(run.id, signal),
    enabled: Boolean(user && hasPermission("run:read")),
    gcTime: 0,
    staleTime: 0,
    retry: false,
    refetchOnMount: "always",
    refetchInterval: terminal ? false : 1000,
  });
  if (!user || !hasPermission("run:read")) return null;
  const scopeDenied = history.error instanceof ApiError && [401, 403, 404].includes(history.error.status);
  const records = scopeDenied ? [] : approvalRecords(
    // Legacy run caches are not actor-scoped. Confirm current access before using their events.
    legacyActor.current === actorScope && history.data !== undefined ? run : { ...run, events: [] }, history.data?.items ?? [],
  );
  return <>
    {records.map((record) => (
      <article key={record.id} className="run-approval-result" aria-label="权限审批结果">
        <strong>{record.decision === "approved" ? "已批准本次操作" : "已拒绝本次操作"}</strong>
        {record.label ? <span>{record.label}</span> : null}
        <code>{record.id}</code>
        {Number.isFinite(Date.parse(record.createdAt)) ? (
          <time dateTime={record.createdAt} title="审批记录时间">记录于 {new Date(record.createdAt).toLocaleString()}</time>
        ) : null}
      </article>
    ))}
    {history.isError ? <div className="approval-history-error">
      <span>审批记录暂时无法读取</span>
      <button type="button" aria-label="重试读取审批记录" title="重试读取审批记录"
        disabled={history.isFetching} onClick={() => void history.refetch()}>
        <RotateCw size={14} aria-hidden="true" />
      </button>
    </div> : null}
  </>;
}
