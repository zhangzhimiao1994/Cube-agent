import { useQuery } from "@tanstack/react-query";

import { api, type RunDetail } from "../api/client";
import { useAuth } from "../auth/AuthProvider";

export function useRunCapabilityApprovals(run: RunDetail | undefined) {
  const { user, hasPermission } = useAuth();
  const readable = Boolean(user && hasPermission("run:read"));
  const canDecide = Boolean(user && hasPermission("run:create"));
  const terminal = Boolean(run && ["completed", "failed", "cancelled"].includes(run.status));
  const approvalId = run?.explicit_details.approval_kind === "capability_tool"
    ? run.explicit_details.approval_id?.trim() || null : null;
  const topVersion = run?.version;
  const fallbackVersion = Number(run?.explicit_details.version);
  const approvalVersion = typeof topVersion === "number" && Number.isSafeInteger(topVersion) && topVersion > 0
    ? topVersion : Number.isSafeInteger(fallbackVersion) && fallbackVersion > 0 ? fallbackVersion : null;
  const history = useQuery({
    queryKey: ["capability-approval-history", user?.tenant_id, user?.user_id, user?.role,
      run?.id, terminal, approvalId, approvalVersion],
    queryFn: ({ signal }) => api.capabilityApprovals(run!.id, signal),
    enabled: Boolean(run && readable),
    gcTime: 0,
    staleTime: 0,
    retry: false,
    placeholderData: undefined,
    refetchOnMount: "always",
    refetchInterval: terminal ? false : 1000,
  });
  const sessionUnavailableReason = !readable ? "当前身份无法确认会话批准支持"
    : !canDecide ? "当前身份没有操作审批权限"
    : !run || run.status !== "waiting_approval" ? "审批尚未就绪"
    : history.isError ? "暂时无法确认会话批准支持"
    : history.isFetching || !history.isFetchedAfterMount || !history.data ? "正在确认会话批准支持"
    : !history.data.ready ? "审批尚未就绪"
    : history.data.conversation_approval_available !== true ? "当前审批不支持会话批准"
    : !approvalId || approvalVersion === null || history.data.conversation_approval_id !== approvalId
      || history.data.conversation_approval_version !== approvalVersion ? "审批信息已变化，请等待刷新"
    : undefined;
  return { ...history, sessionAvailable: sessionUnavailableReason === undefined, sessionUnavailableReason };
}
