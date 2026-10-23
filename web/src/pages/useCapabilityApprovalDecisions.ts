import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import { api, formatApiError, type SubmittedRun } from "../api/client";
import { useAuth } from "../auth/AuthProvider";

type Approval = { runId: string; approvalId: string; version: number };
type Decision = "approve" | "approve_session" | "reject";
type DecisionState = {
  status: "pending" | "success" | "error";
  decision: Decision;
  error?: string;
};
type DecisionStates = Readonly<Record<string, DecisionState>>;

const decisionQueryKey = ["capability-approval-decisions"] as const;
type ActorScope = readonly [typeof decisionQueryKey[0], string, string, string];
const emptyStates: DecisionStates = Object.freeze({});

const approvalKey = (approval: Approval) => JSON.stringify([approval.runId, approval.approvalId]);

export function useCapabilityApprovalDecisions(
  onSuccess: (run: SubmittedRun, approval: Approval, decision: Decision) => void,
) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const scope = useMemo<ActorScope | null>(() => user
    ? [decisionQueryKey[0], user.tenant_id, user.user_id, user.role]
    : null, [user?.tenant_id, user?.user_id, user?.role]);
  const subscribe = useCallback((notify: () => void) => queryClient.getQueryCache().subscribe((event) => {
    if (scope && scope.every((part, index) => event.query.queryKey[index] === part)) notify();
  }), [queryClient, scope]);
  const getSnapshot = useCallback(() => scope
    ? queryClient.getQueryData<DecisionStates>(scope) ?? emptyStates
    : emptyStates, [queryClient, scope]);
  const states = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const currentScopeRef = useRef<string | null>(null);
  currentScopeRef.current = scope ? JSON.stringify(scope) : null;
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  const onSuccessRef = useRef(onSuccess);
  onSuccessRef.current = onSuccess;
  function update(clickedScope: ActorScope, approval: Approval, state: DecisionState) {
    queryClient.setQueryData<DecisionStates>(clickedScope, (current) => ({
      ...current,
      [approvalKey(approval)]: state,
    }));
  }
  const mutation = useMutation({
    mutationFn: ({ approval, decision, clickedScope }: { approval: Approval; decision: Decision; clickedScope: ActorScope }) => {
      if (currentScopeRef.current !== JSON.stringify(clickedScope)) throw new Error("审批身份已变化，请重新确认");
      return api[decision !== "reject" ? "approveCapability" : "rejectCapability"](approval.runId, {
        approval_id: approval.approvalId,
        version: approval.version,
        ...(decision === "approve_session" ? { scope: "conversation" as const } : {}),
      });
    },
    onSuccess: (run, { approval, decision, clickedScope }) => {
      // A successful response settles this ID even if stale run data carries a different version.
      update(clickedScope, approval, { status: "success", decision });
      if (mountedRef.current && currentScopeRef.current === JSON.stringify(clickedScope)) {
        onSuccessRef.current(run, approval, decision);
      }
    },
    onError: (error, { approval, decision, clickedScope }) => {
      update(clickedScope, approval, {
        status: "error", decision,
        error: `${decision !== "reject" ? "批准" : "拒绝"}失败：${formatApiError(error, "沙箱权限确认失败")}。请重试。`,
      });
    },
  });
  function decide(approval: Approval, decision: Decision) {
    if (!scope) return;
    const clickedApproval = { ...approval };
    // Keep receipts for this client's lifetime, including periods with no mounted approval UI.
    queryClient.getQueryCache().build<DecisionStates>(queryClient, { queryKey: scope, gcTime: Infinity });
    let claimed = false;
    // The synchronous updater claims the ID before either page can start another decision.
    queryClient.setQueryData<DecisionStates>(scope, (current) => {
      const state = current?.[approvalKey(clickedApproval)];
      if (state?.status === "pending" || state?.status === "success") return current;
      claimed = true;
      return { ...current, [approvalKey(clickedApproval)]: { status: "pending", decision } };
    });
    if (claimed) mutation.mutate({ approval: clickedApproval, decision, clickedScope: scope });
  }
  return {
    stateFor: (approval: Approval | null) => approval ? states[approvalKey(approval)] : undefined,
    approve: (approval: Approval) => decide(approval, "approve"),
    approveConversation: (approval: Approval) => decide(approval, "approve_session"),
    reject: (approval: Approval) => decide(approval, "reject"),
  };
}
