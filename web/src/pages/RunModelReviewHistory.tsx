import { useInfiniteQuery } from "@tanstack/react-query";
import { ShieldCheck, ShieldQuestion, ShieldX } from "lucide-react";

import { ApiError, api, type PublicModelReview, type RunDetail } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import "./RunModelReviewHistory.css";

const STATUS = { pending: "审查中", allow: "审查通过", deny: "审查未通过", needs_human: "需人工确认", unknown: "审查结果未知" };
const RISK = { low: "风险低", medium: "风险中等", high: "风险高", unknown: "风险未知" };
const REASON: Record<NonNullable<PublicModelReview["reason_code"]>, string> = {
  authorized_action: "符合已授权范围", outside_authorized_scope: "超出已授权范围", protected_resource: "涉及受保护资源",
  untrusted_content: "涉及不可信内容", unclear_impact: "操作影响尚不明确", missing_context: "缺少必要上下文", policy_requires_human: "策略要求人工确认",
  accounting_unknown: "用量记账状态未知", review_timeout: "模型审查超时", review_unavailable: "模型审查服务不可用",
  review_invalid_response: "模型审查响应无效", review_unknown: "模型审查状态未知", review_cancelled: "模型审查已取消",
};

export function RunModelReviewHistory({ run }: { run: RunDetail }) {
  const { user, hasPermission } = useAuth();
  const readable = Boolean(user && hasPermission("run:read"));
  const terminal = ["completed", "failed", "cancelled"].includes(run.status);
  const history = useInfiniteQuery({
    queryKey: ["model-review-history", user?.tenant_id, user?.user_id, user?.role, run.id, terminal],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => api.modelReviews(run.id, { cursor: pageParam, signal }),
    getNextPageParam: (lastPage, _pages, _lastParam, pageParams) =>
      lastPage.next_cursor && !pageParams.includes(lastPage.next_cursor) ? lastPage.next_cursor : undefined,
    enabled: readable, gcTime: 0, staleTime: 0, retry: false, placeholderData: undefined,
    refetchOnMount: "always", refetchInterval: terminal ? false : 2000,
  });
  if (!readable) return null;
  const denied = history.error instanceof ApiError && [401, 403, 404].includes(history.error.status);
  // A 404 is also how older servers report this optional, read-only endpoint.
  if (history.error instanceof ApiError && history.error.status === 404) return null;
  const records = denied ? [] : [...new Map(history.data?.pages.flatMap((page) => page.items).map((item) => [item.id, item])).values()];
  return <>
    {records.map((record) => <article key={record.id} className="run-approval-result" aria-label="模型自动审查" data-review-status={record.status}>
      {record.status === "allow" ? <ShieldCheck size={16} aria-hidden="true" />
        : record.status === "deny" ? <ShieldX size={16} aria-hidden="true" /> : <ShieldQuestion size={16} aria-hidden="true" />}
      <span className="approval-record-label">模型自动审查</span>
      <strong>{STATUS[record.status]}</strong>
      <span>工作区文本写入</span>
      <span>{record.risk ? RISK[record.risk] : "风险未评估"}</span>
      {record.reason_code ? <span>{REASON[record.reason_code]}</span> : null}
      {record.status === "allow" ? <span>仅表示审查通过，不代表动作执行成功。</span> : null}
      <time dateTime={record.created_at} title="模型审查记录时间">记录于 {new Date(record.created_at).toLocaleString()}</time>
    </article>)}
    {history.isError ? <p className="model-review-history-notice">模型审查记录暂时无法读取</p> : null}
    {records.length > 0 || history.isError ? <div className="model-review-history-controls">
      <button type="button" disabled={history.isFetching} onClick={() => void history.refetch()}>
        {history.isError ? "重试读取模型审查记录" : "重新加载模型审查记录"}
      </button>
      {history.hasNextPage && !denied ? <button type="button" disabled={history.isFetching}
        onClick={() => void history.fetchNextPage()}>继续加载模型审查记录</button> : null}
    </div> : null}
  </>;
}
