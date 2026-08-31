import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useNavSection } from "../app/navSections";
import { api, formatApiError, type HermesInsight } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

type HermesSortKey = "created" | "category" | "conversation" | "summary" | "outcome" | "status";

type HermesColumnFilters = {
  category: "all" | "conversation" | "scheduler";
  conversation: string;
  created: string;
  outcome: string;
  status: string;
  summary: string;
};

const EMPTY_HERMES_FILTERS: HermesColumnFilters = {
  category: "all",
  conversation: "",
  created: "",
  outcome: "all",
  status: "all",
  summary: "",
};

function parseList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function statusLabel(confirmedAt: string | null) {
  return confirmedAt ? "已确认" : "待确认";
}

function categoryLabel(category: HermesInsight["category"]) {
  return category === "scheduler" ? "调度观察" : "对话记忆";
}

function hermesLearningSummary(insight: HermesInsight) {
  return insight.user_summary?.trim() || insight.summary;
}

function normalizeCategory(value: string | null): HermesColumnFilters["category"] {
  return value === "conversation" || value === "scheduler" ? value : "all";
}

function normalizeStatus(value: string | null): HermesColumnFilters["status"] {
  return value === "pending" || value === "confirmed" ? value : "all";
}

function toggle(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function hermesColumnValue(insight: HermesInsight, key: HermesSortKey) {
  if (key === "created") return insight.created_at;
  if (key === "category") return categoryLabel(insight.category);
  if (key === "conversation") return insight.conversation_id ?? "未关联";
  if (key === "summary") return hermesLearningSummary(insight);
  if (key === "outcome") return insight.outcome;
  return statusLabel(insight.confirmed_at);
}

function matchesHermesSearch(insight: HermesInsight, query: string) {
  return textContains(
    [
      insight.id,
      insight.run_id ?? "",
      insight.conversation_id ?? "",
      categoryLabel(insight.category),
      insight.outcome,
      hermesLearningSummary(insight),
      insight.summary,
      insight.lesson,
      statusLabel(insight.confirmed_at),
      insight.created_at,
      ...insight.tags,
    ].join(" "),
    query,
  );
}

function matchesHermesColumns(insight: HermesInsight, filters: HermesColumnFilters) {
  const status = insight.confirmed_at ? "confirmed" : "pending";
  return (
    textContains(insight.created_at, filters.created) &&
    (filters.category === "all" || insight.category === filters.category) &&
    textContains(insight.conversation_id ?? "未关联", filters.conversation) &&
    textContains(hermesLearningSummary(insight), filters.summary) &&
    (filters.outcome === "all" || insight.outcome === filters.outcome) &&
    (filters.status === "all" || status === filters.status)
  );
}

function sortedHermesInsights(items: HermesInsight[], sort: SortState<HermesSortKey>) {
  return [...items].sort((left, right) => compareText(hermesColumnValue(left, sort.key), hermesColumnValue(right, sort.key), sort.direction));
}

export function HermesPage() {
  const { insightId } = useParams();
  if (insightId) return <HermesInsightDetail insightId={insightId} />;
  return <HermesLearningTable />;
}

function HermesLearningTable() {
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const { activeSection, navTargetProps } = useNavSection(["category", "status"]);
  const searchParamKey = searchParams.toString();
  const [conversationId, setConversationId] = useState("");
  const [lesson, setLesson] = useState(
    "When agents disagree, ask the main agent to compare evidence, risk, and output quality before deciding.",
  );
  const [tags, setTags] = useState("decision,review");
  const [outcome, setOutcome] = useState<"success" | "failure" | "neutral">("success");
  const [feedbackCategory, setFeedbackCategory] = useState<HermesInsight["category"]>("conversation");
  const [weight, setWeight] = useState("5");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [columnFilters, setColumnFilters] = useState<HermesColumnFilters>({
    ...EMPTY_HERMES_FILTERS,
    category: normalizeCategory(searchParams.get("category")),
    status: normalizeStatus(searchParams.get("status")),
  });
  const [sort, setSort] = useState<SortState<HermesSortKey>>({ key: "created", direction: "desc" });

  useEffect(() => {
    setColumnFilters((current) => ({
      ...current,
      category: normalizeCategory(searchParams.get("category")),
      status: normalizeStatus(searchParams.get("status")),
    }));
    setSelectedIds([]);
  }, [searchParamKey, searchParams]);

  const insights = useQuery({
    queryKey: ["hermes"],
    queryFn: () => api.hermesInsights(),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });
  const feedback = useMutation({
    mutationFn: () =>
      api.recordHermesFeedback({
        conversation_id: conversationId.trim() || null,
        category: feedbackCategory,
        outcome,
        lesson,
        tags: parseList(tags),
        weight: Number(weight) || 1,
      }),
    onSuccess: async () => {
      setConversationId("");
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });
  const bulkConfirm = useMutation({
    mutationFn: (ids: string[]) => api.bulkConfirmHermesInsights(ids),
    onSuccess: async () => {
      setSelectedIds([]);
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });
  const bulkDelete = useMutation({
    mutationFn: (ids: string[]) => api.bulkDeleteHermesInsights(ids),
    onSuccess: async (_result, ids) => {
      setSelectedIds((current) => current.filter((item) => !ids.includes(item)));
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });
  const confirmInsight = useMutation({
    mutationFn: (id: string) => api.confirmHermesInsight(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });
  const deleteInsight = useMutation({
    mutationFn: (id: string) => api.deleteHermesInsight(id),
    onSuccess: async (_result, id) => {
      setSelectedIds((current) => current.filter((item) => item !== id));
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });

  function updateColumnFilter(key: keyof HermesColumnFilters, value: string) {
    setColumnFilters((current) => ({ ...current, [key]: value }));
  }

  function submitFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    feedback.mutate();
  }

  function toggleAll(ids: string[]) {
    setSelectedIds((current) => {
      const allSelected = ids.length > 0 && ids.every((id) => current.includes(id));
      if (allSelected) return current.filter((id) => !ids.includes(id));
      return Array.from(new Set([...current, ...ids]));
    });
  }

  function confirmSelectedInsights(ids: string[]) {
    if (ids.length === 0) return;
    bulkConfirm.mutate(ids);
  }

  function deleteSelectedInsights(ids: string[]) {
    if (ids.length === 0) return;
    if (!window.confirm(`确认删除当前结果中已选的 ${ids.length} 条学习记录？删除后不会再进入 Hermes 建议。`)) return;
    bulkDelete.mutate(ids);
  }

  if (insights.isLoading) return <p>正在加载 Hermes...</p>;
  if (insights.isError) {
    return <p role="alert">{formatApiError(insights.error, "Hermes 加载失败")}</p>;
  }

  const items = insights.data ?? [];
  const filteredInsights = items.filter((insight) => matchesHermesSearch(insight, searchTerm) && matchesHermesColumns(insight, columnFilters));
  const visibleInsights = sortedHermesInsights(filteredInsights, sort);
  const visibleIds = visibleInsights.map((insight) => insight.id);
  const visibleConfirmableIds = visibleInsights.filter((insight) => insight.confirmed_at === null).map((insight) => insight.id);
  const selectedVisibleIds = selectedIds.filter((id) => visibleIds.includes(id));
  const selectedVisibleConfirmableIds = selectedIds.filter((id) => visibleConfirmableIds.includes(id));
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.includes(id));
  const allVisibleConfirmableSelected =
    visibleConfirmableIds.length > 0 && visibleConfirmableIds.every((id) => selectedIds.includes(id));
  const busy = bulkConfirm.isPending || bulkDelete.isPending || confirmInsight.isPending || deleteInsight.isPending;
  const ledgerNavSection = activeSection ?? "ledger";
  const filtersHideSchedulerLearning =
    visibleInsights.length === 0 &&
    items.some((insight) => insight.category === "scheduler") &&
    columnFilters.category !== "scheduler";

  return (
    <section>
      <p className="eyebrow">Hermes learning</p>
      <h2>Hermes 学习</h2>
      <p>
        Hermes 是独立学习模块。它按时间和对话 ID 记录运行经验，外层以表格展示，
        点击后进入详情查看和确认。学习建议不会直接挤到对话界面，也不会绕过主 Agent 的审批策略。
      </p>

      <section aria-label="Hermes 学习台账" {...navTargetProps(ledgerNavSection)}>
        <h3>学习台账</h3>
        {items.length === 0 ? (
          <article>
            <h4>还没有学习记录</h4>
            <p>运行完成或手动记录经验后，Hermes 会按时间和对话 ID 在这里建立台账。</p>
          </article>
        ) : (
          <>
            <div className="list-toolbar">
              <label>
                快速搜索学习记录
                <input
                  type="search"
                  aria-label="快速搜索 Hermes 学习"
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.currentTarget.value)}
                  placeholder="跨对话 ID、摘要、标签、结果或状态搜索"
                />
              </label>
              <button type="button" className="secondary-action" onClick={() => { setSearchTerm(""); setColumnFilters(EMPTY_HERMES_FILTERS); }}>
                清空筛选
              </button>
              <small>
                显示 {visibleInsights.length} / {items.length}
              </small>
            </div>
            <div className="bulk-action-bar">
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="Select all visible Hermes learning records"
                  checked={allVisibleSelected}
                  disabled={visibleIds.length === 0 || busy}
                  onChange={() => toggleAll(visibleIds)}
                />
                全选当前结果
              </label>
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="Select all visible unconfirmed Hermes learning records"
                  checked={allVisibleConfirmableSelected}
                  disabled={visibleConfirmableIds.length === 0 || busy}
                  onChange={() => toggleAll(visibleConfirmableIds)}
                />
                全选待确认
              </label>
              <button
                type="button"
                className="secondary-action"
                disabled={selectedVisibleConfirmableIds.length === 0 || busy}
                onClick={() => confirmSelectedInsights(selectedVisibleConfirmableIds)}
              >
                {bulkConfirm.isPending ? "确认中..." : `批量确认待确认学习（${selectedVisibleConfirmableIds.length}）`}
              </button>
              <button
                type="button"
                className="danger-button"
                disabled={selectedVisibleIds.length === 0 || busy}
                onClick={() => deleteSelectedInsights(selectedVisibleIds)}
              >
                {bulkDelete.isPending ? "正在删除..." : `批量删除已选学习（${selectedVisibleIds.length}）`}
              </button>
              <small>当前结果已选 {selectedVisibleIds.length}</small>
            </div>
            {bulkConfirm.isError ? (
              <p role="alert">{formatApiError(bulkConfirm.error, "Hermes 批量确认失败")}</p>
            ) : null}
            {bulkDelete.isError ? (
              <p role="alert">{formatApiError(bulkDelete.error, "Hermes 批量删除失败")}</p>
            ) : null}
            {confirmInsight.isError ? (
              <p role="alert">{formatApiError(confirmInsight.error, "Hermes 确认失败")}</p>
            ) : null}
            {deleteInsight.isError ? (
              <p role="alert">{formatApiError(deleteInsight.error, "Hermes 删除失败")}</p>
            ) : null}
            {visibleInsights.length === 0 ? (
              <article>
                <h4>{filtersHideSchedulerLearning ? "当前筛选隐藏了调度观察记录" : "没有匹配的学习记录"}</h4>
                <p>
                  {filtersHideSchedulerLearning
                    ? "自动运行学习通常归类在“调度观察”。清空筛选或切到调度观察后，可以查看运行完成、失败或取消后写入的台账。"
                    : "调整列筛选或清空筛选查看全部 Hermes 学习记录。"}
                </p>
              </article>
            ) : (
              <div className="table-shell">
                <table aria-label="Hermes 学习台账">
                  <thead>
                    <tr>
                      <th>选择</th>
                      <th><SortHeader column="category" label="分类" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>分类</SortHeader></th>
                      <th><SortHeader column="created" label="时间" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>时间</SortHeader></th>
                      <th><SortHeader column="conversation" label="对话 ID" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>对话 ID</SortHeader></th>
                      <th><SortHeader column="summary" label="中文学习摘要" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>中文学习摘要</SortHeader></th>
                      <th><SortHeader column="outcome" label="结果" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>结果</SortHeader></th>
                      <th><SortHeader column="status" label="确认状态" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>确认状态</SortHeader></th>
                      <th>操作</th>
                    </tr>
                    <tr className="table-filter-row">
                      <th></th>
                      <th>
                        <select aria-label="按 Hermes 分类筛选" value={columnFilters.category} onChange={(event) => updateColumnFilter("category", event.currentTarget.value)}>
                          <option value="all">全部</option>
                          <option value="conversation">对话记忆</option>
                          <option value="scheduler">调度观察</option>
                        </select>
                      </th>
                      <th><input aria-label="按 Hermes 时间筛选" value={columnFilters.created} onChange={(event) => updateColumnFilter("created", event.currentTarget.value)} placeholder="时间" /></th>
                      <th><input aria-label="按 Hermes 对话 ID 筛选" value={columnFilters.conversation} onChange={(event) => updateColumnFilter("conversation", event.currentTarget.value)} placeholder="对话 ID" /></th>
                      <th><input aria-label="按 Hermes 中文学习摘要筛选" value={columnFilters.summary} onChange={(event) => updateColumnFilter("summary", event.currentTarget.value)} placeholder="摘要关键词" /></th>
                      <th>
                        <select aria-label="按 Hermes 结果筛选" value={columnFilters.outcome} onChange={(event) => updateColumnFilter("outcome", event.currentTarget.value)}>
                          <option value="all">全部</option>
                          <option value="success">success</option>
                          <option value="failure">failure</option>
                          <option value="neutral">neutral</option>
                        </select>
                      </th>
                      <th>
                        <select aria-label="按 Hermes 确认状态筛选" value={columnFilters.status} onChange={(event) => updateColumnFilter("status", event.currentTarget.value)}>
                          <option value="all">全部</option>
                          <option value="pending">待确认</option>
                          <option value="confirmed">已确认</option>
                        </select>
                      </th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleInsights.map((insight) => (
                      <tr key={insight.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select Hermes learning ${insight.id}`}
                            checked={selectedIds.includes(insight.id)}
                            disabled={busy}
                            onChange={() => setSelectedIds((current) => toggle(current, insight.id))}
                          />
                        </td>
                        <td>{categoryLabel(insight.category)}</td>
                        <td>
                          <time dateTime={insight.created_at}>{insight.created_at}</time>
                        </td>
                        <td>{insight.conversation_id ?? "未关联"}</td>
                        <td>{hermesLearningSummary(insight)}</td>
                        <td>{insight.outcome}</td>
                        <td>{statusLabel(insight.confirmed_at)}</td>
                        <td className="table-actions">
                          {insight.confirmed_at === null ? (
                            <button
                              type="button"
                              className="secondary-action"
                              aria-label={`确认 Hermes 学习 ${insight.id}`}
                              disabled={busy}
                              onClick={() => confirmInsight.mutate(insight.id)}
                            >
                              确认
                            </button>
                          ) : null}
                          <Link
                            to={`/hermes/${encodeURIComponent(insight.id)}`}
                            aria-label={`查看 ${insight.conversation_id ?? insight.id} 的 Hermes 学习详情`}
                          >
                            查看详情
                          </Link>
                          <button
                            type="button"
                            className="danger-action"
                            aria-label={`删除 Hermes 学习 ${insight.id}`}
                            disabled={busy}
                            onClick={() => {
                              if (window.confirm(`确认删除这条 Hermes 学习记录？删除后不会再进入 Hermes 建议。`)) {
                                deleteInsight.mutate(insight.id);
                              }
                            }}
                          >
                            删除
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      <details className="inline-guide">
        <summary>手动补充学习记录</summary>
        <form onSubmit={submitFeedback} aria-label="记录 Hermes 经验">
          <label>
            对话 ID，可选
            <input
              value={conversationId}
              onChange={(event) => setConversationId(event.target.value)}
              placeholder="例如 conv-architecture-1"
            />
          </label>
          <label>
            结果
            <select value={outcome} onChange={(event) => setOutcome(event.target.value as typeof outcome)}>
              <option value="success">成功</option>
              <option value="failure">失败</option>
              <option value="neutral">中性</option>
            </select>
          </label>
          <label>
            经验分类
            <select value={feedbackCategory} onChange={(event) => setFeedbackCategory(event.target.value as HermesInsight["category"])}>
              <option value="conversation">对话记忆</option>
              <option value="scheduler">调度观察</option>
            </select>
          </label>
          <label>
            经验内容
            <textarea value={lesson} onChange={(event) => setLesson(event.currentTarget.value)} />
          </label>
          <label>
            标签，英文逗号分隔
            <input value={tags} onChange={(event) => setTags(event.target.value)} />
          </label>
          <label>
            权重 1-10
            <input
              type="number"
              min="1"
              max="10"
              value={weight}
              onChange={(event) => setWeight(event.target.value)}
            />
          </label>
          <button type="submit" disabled={feedback.isPending}>
            {feedback.isPending ? "正在记录..." : "记录经验"}
          </button>
          {feedback.isError ? <p role="alert">{formatApiError(feedback.error, "Hermes 经验记录失败")}</p> : null}
        </form>
      </details>
    </section>
  );
}

function HermesInsightDetail({ insightId }: { insightId: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const insight = useQuery({
    queryKey: ["hermes", insightId],
    queryFn: () => api.hermesInsight(insightId),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });
  const confirm = useMutation({
    mutationFn: () => api.confirmHermesInsight(insightId),
    onSuccess: (updated) => {
      queryClient.setQueryData(["hermes", insightId], updated);
      void queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });
  const deleteInsight = useMutation({
    mutationFn: () => api.deleteHermesInsight(insightId),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ["hermes", insightId] });
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
      navigate("/hermes");
    },
  });

  if (insight.isLoading) return <p>正在加载 Hermes 学习详情...</p>;
  if (insight.isError) return <p role="alert">{formatApiError(insight.error, "Hermes 学习详情加载失败")}</p>;

  const item = confirm.data ?? insight.data;
  if (!item) return <p role="alert">Hermes 学习详情为空。</p>;
  return (
    <section>
      <Link to="/hermes" className="button-link">
        返回学习台账
      </Link>
      <p className="eyebrow">Hermes detail</p>
      <h2>学习详情</h2>
      <article>
        <span className="eyebrow">{statusLabel(item.confirmed_at)}</span>
        <h3>{hermesLearningSummary(item)}</h3>
        <p>{item.lesson}</p>
        <dl className="detail-list">
          {item.summary !== hermesLearningSummary(item) ? (
            <div>
              <dt>系统摘要</dt>
              <dd>{item.summary}</dd>
            </div>
          ) : null}
          <div>
            <dt>分类</dt>
            <dd>{categoryLabel(item.category)}</dd>
          </div>
          <div>
            <dt>对话 ID</dt>
            <dd>{item.conversation_id ?? "未关联"}</dd>
          </div>
          <div>
            <dt>运行 ID</dt>
            <dd>{item.run_id ?? "未关联"}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{item.created_at}</dd>
          </div>
          <div>
            <dt>确认时间</dt>
            <dd>{item.confirmed_at ?? "尚未确认"}</dd>
          </div>
          <div>
            <dt>标签</dt>
            <dd>{item.tags.join(", ") || "无"}</dd>
          </div>
          <div>
            <dt>权重</dt>
            <dd>{item.weight}</dd>
          </div>
        </dl>
        <div className="inline-actions">
          <button type="button" disabled={confirm.isPending || item.confirmed_at !== null} onClick={() => confirm.mutate()}>
            {item.confirmed_at ? "已确认" : confirm.isPending ? "正在确认..." : "确认这条学习"}
          </button>
          <button
            type="button"
            className="danger-action"
            disabled={deleteInsight.isPending}
            onClick={() => {
              if (window.confirm(`确认删除这条 Hermes 学习记录？删除后不会再进入 Hermes 建议。`)) {
                deleteInsight.mutate();
              }
            }}
          >
            {deleteInsight.isPending ? "正在删除..." : "删除"}
          </button>
        </div>
        {confirm.isError ? <p role="alert">{formatApiError(confirm.error, "Hermes 学习确认失败")}</p> : null}
        {deleteInsight.isError ? <p role="alert">{formatApiError(deleteInsight.error, "Hermes 学习删除失败")}</p> : null}
      </article>
    </section>
  );
}
