import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, formatApiError } from "../api/client";

function parseList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function statusLabel(confirmedAt: string | null) {
  return confirmedAt ? "已确认" : "待确认";
}

function toggle(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export function HermesPage() {
  const { insightId } = useParams();
  if (insightId) return <HermesInsightDetail insightId={insightId} />;
  return <HermesLearningTable />;
}

function HermesLearningTable() {
  const queryClient = useQueryClient();
  const [conversationId, setConversationId] = useState("");
  const [lesson, setLesson] = useState(
    "When agents disagree, ask the main agent to compare evidence, risk, and output quality before deciding.",
  );
  const [tags, setTags] = useState("decision,review");
  const [outcome, setOutcome] = useState<"success" | "failure" | "neutral">("success");
  const [weight, setWeight] = useState("5");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const insights = useQuery({
    queryKey: ["hermes"],
    queryFn: () => api.hermesInsights(),
  });
  const feedback = useMutation({
    mutationFn: () =>
      api.recordHermesFeedback({
        conversation_id: conversationId.trim() || null,
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
  const deleteInsight = useMutation({
    mutationFn: (id: string) => api.deleteHermesInsight(id),
    onSuccess: async (_result, id) => {
      setSelectedIds((current) => current.filter((item) => item !== id));
      await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    },
  });

  const sortedInsights = useMemo(
    () => [...(insights.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [insights.data],
  );
  const confirmableIds = sortedInsights
    .filter((insight) => insight.confirmed_at === null)
    .map((insight) => insight.id);
  const selectedConfirmableIds = selectedIds.filter((id) => confirmableIds.includes(id));
  const allConfirmableSelected =
    confirmableIds.length > 0 && confirmableIds.every((id) => selectedIds.includes(id));

  function submitFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    feedback.mutate();
  }

  function toggleAllInsights() {
    setSelectedIds((current) => {
      if (allConfirmableSelected) return current.filter((id) => !confirmableIds.includes(id));
      return Array.from(new Set([...current, ...confirmableIds]));
    });
  }

  function confirmSelectedInsights() {
    if (selectedConfirmableIds.length === 0) return;
    bulkConfirm.mutate(selectedConfirmableIds);
  }

  if (insights.isLoading) return <p>正在加载 Hermes...</p>;
  if (insights.isError) {
    return <p role="alert">{formatApiError(insights.error, "Hermes 加载失败")}</p>;
  }

  return (
    <section>
      <p className="eyebrow">Hermes learning</p>
      <h2>Hermes 学习</h2>
      <p>
        Hermes 是独立学习模块。它按时间和对话 ID 记录运行经验，外层以表格展示，
        点击后进入详情查看和确认。学习建议不会直接挤到对话界面，也不会绕过主 Agent 的审批策略。
      </p>

      <section aria-label="Hermes 学习台账">
        <h3>学习台账</h3>
        {sortedInsights.length === 0 ? (
          <article>
            <h4>还没有学习记录</h4>
            <p>运行完成或手动记录经验后，Hermes 会按时间和对话 ID 在这里建立台账。</p>
          </article>
        ) : (
          <>
            <div className="bulk-action-bar">
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="Select all Hermes learning records"
                  checked={allConfirmableSelected}
                  disabled={confirmableIds.length === 0 || bulkConfirm.isPending}
                  onChange={toggleAllInsights}
                />
                全选待确认
              </label>
              <button
                type="button"
                className="secondary-action"
                disabled={selectedConfirmableIds.length === 0 || bulkConfirm.isPending}
                onClick={confirmSelectedInsights}
              >
                {bulkConfirm.isPending ? "确认中..." : "批量确认已选学习"}
              </button>
              <small>已选 {selectedConfirmableIds.length}</small>
            </div>
            {bulkConfirm.isError ? (
              <p role="alert">{formatApiError(bulkConfirm.error, "Hermes 批量确认失败")}</p>
            ) : null}
            {deleteInsight.isError ? (
              <p role="alert">{formatApiError(deleteInsight.error, "Hermes 删除失败")}</p>
            ) : null}
            <div className="table-shell">
              <table aria-label="Hermes 学习台账">
                <thead>
                  <tr>
                    <th>选择</th>
                    <th>时间</th>
                    <th>对话 ID</th>
                    <th>学习总结</th>
                    <th>结果</th>
                    <th>确认状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedInsights.map((insight) => {
                    const canConfirm = insight.confirmed_at === null;
                    return (
                      <tr key={insight.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select Hermes learning ${insight.id}`}
                            checked={selectedIds.includes(insight.id)}
                            disabled={!canConfirm || bulkConfirm.isPending}
                            onChange={() => setSelectedIds((current) => toggle(current, insight.id))}
                          />
                        </td>
                        <td>
                          <time dateTime={insight.created_at}>{insight.created_at}</time>
                        </td>
                        <td>{insight.conversation_id ?? "未关联"}</td>
                        <td>{insight.summary}</td>
                        <td>{insight.outcome}</td>
                        <td>{statusLabel(insight.confirmed_at)}</td>
                        <td>
                          <Link
                            to={`/hermes/${encodeURIComponent(insight.id)}`}
                            aria-label={`查看 ${insight.conversation_id ?? insight.id} 的 Hermes 学习详情`}
                          >
                            查看详情
                          </Link>
                          <button
                            type="button"
                            className="secondary-action"
                            aria-label={`删除 Hermes 学习 ${insight.id}`}
                            disabled={deleteInsight.isPending}
                            onClick={() => deleteInsight.mutate(insight.id)}
                          >
                            删除
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
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
        <h3>{item.summary}</h3>
        <p>{item.lesson}</p>
        <dl className="detail-list">
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
            className="secondary-action"
            disabled={deleteInsight.isPending}
            onClick={() => deleteInsight.mutate()}
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
