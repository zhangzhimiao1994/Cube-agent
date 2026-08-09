import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError, type HermesRecommendation } from "../api/client";

function parseList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function statusLabel(confirmedAt: string | null) {
  return confirmedAt ? "已确认" : "待确认";
}

export function HermesPage() {
  const { insightId } = useParams();
  if (insightId) return <HermesInsightDetail insightId={insightId} />;
  return <HermesLearningTable />;
}

function HermesLearningTable() {
  const queryClient = useQueryClient();
  const [task, setTask] = useState("需要多个角色讨论一个内容方案，并给出最终决策。");
  const [modeCandidates, setModeCandidates] = useState("dispatch,discuss,hybrid");
  const [modelCandidates, setModelCandidates] = useState("main,planner,critic");
  const [skillCandidates, setSkillCandidates] = useState("script_review,safe_search");
  const [conversationId, setConversationId] = useState("");
  const [lesson, setLesson] = useState("当任务存在明显分歧时，优先使用讨论模式，再由主 Agent 裁决。");
  const [tags, setTags] = useState("discussion,decision");
  const [outcome, setOutcome] = useState<"success" | "failure" | "neutral">("success");
  const [weight, setWeight] = useState("5");
  const [recommendation, setRecommendation] = useState<HermesRecommendation | null>(null);

  const insights = useQuery({
    queryKey: ["hermes"],
    queryFn: () => api.hermesInsights(),
  });
  const recommend = useMutation({
    mutationFn: () =>
      api.recommendWithHermes({
        task,
        mode_candidates: parseList(modeCandidates),
        model_candidates: parseList(modelCandidates),
        skill_candidates: parseList(skillCandidates),
      }),
    onSuccess: setRecommendation,
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
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["hermes"] }),
  });

  const sortedInsights = useMemo(
    () => [...(insights.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [insights.data],
  );

  function submitRecommendation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    recommend.mutate();
  }

  function submitFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    feedback.mutate();
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
        Hermes 学习是独立模块。它按时间和对话 ID 记录每次运行沉淀的经验，你可以在这里查看总结、进入详情并人工确认，
        不会把学习信息堆到对话页里。
      </p>

      <div className="two-column">
        <form onSubmit={submitRecommendation} aria-label="Hermes 推荐">
          <h3>请求 Hermes 推荐</h3>
          <label>
            任务描述
            <textarea value={task} onChange={(event) => setTask(event.currentTarget.value)} />
          </label>
          <label>
            候选模式，英文逗号分隔
            <input value={modeCandidates} onChange={(event) => setModeCandidates(event.target.value)} />
          </label>
          <label>
            候选模型，英文逗号分隔
            <input value={modelCandidates} onChange={(event) => setModelCandidates(event.target.value)} />
          </label>
          <label>
            候选 Skill，英文逗号分隔
            <input value={skillCandidates} onChange={(event) => setSkillCandidates(event.target.value)} />
          </label>
          <button type="submit" disabled={recommend.isPending}>
            {recommend.isPending ? "正在分析..." : "获取推荐"}
          </button>
          {recommend.isError ? <p role="alert">{formatApiError(recommend.error, "Hermes 推荐失败")}</p> : null}
        </form>

        <form onSubmit={submitFeedback} aria-label="记录 Hermes 经验">
          <h3>记录经验</h3>
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
            <input type="number" min="1" max="10" value={weight} onChange={(event) => setWeight(event.target.value)} />
          </label>
          <button type="submit" disabled={feedback.isPending}>
            {feedback.isPending ? "正在记录..." : "记录经验"}
          </button>
          {feedback.isError ? <p role="alert">{formatApiError(feedback.error, "Hermes 经验记录失败")}</p> : null}
        </form>
      </div>

      {recommendation ? (
        <article>
          <h3>推荐结果</h3>
          <p>模式：{recommendation.recommended_mode}</p>
          <p>模型：{recommendation.recommended_model ?? "默认"}</p>
          <p>Skill：{recommendation.recommended_skills.join(", ") || "无"}</p>
          <p>置信度：{Math.round(recommendation.confidence * 100)}%</p>
          <p>是否需要审批：{recommendation.requires_approval ? "需要" : "不需要"}</p>
          <ul>
            {recommendation.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </article>
      ) : null}

      <section aria-label="Hermes 学习台账">
        <h3>学习台账</h3>
        {sortedInsights.length === 0 ? (
          <article>
            <h4>还没有学习记录</h4>
            <p>运行完成或手动记录经验后，Hermes 会按时间和对话 ID 在这里建立台账。</p>
          </article>
        ) : (
          <div className="table-shell">
            <table aria-label="Hermes 学习台账">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>对话 ID</th>
                  <th>学习总结</th>
                  <th>结果</th>
                  <th>确认状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {sortedInsights.map((insight) => (
                  <tr key={insight.id}>
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
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  );
}

function HermesInsightDetail({ insightId }: { insightId: string }) {
  const queryClient = useQueryClient();
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
        <button type="button" disabled={confirm.isPending || item.confirmed_at !== null} onClick={() => confirm.mutate()}>
          {item.confirmed_at ? "已确认" : confirm.isPending ? "正在确认..." : "确认这条学习"}
        </button>
        {confirm.isError ? <p role="alert">{formatApiError(confirm.error, "Hermes 学习确认失败")}</p> : null}
      </article>
    </section>
  );
}
