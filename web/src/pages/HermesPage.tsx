import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { api, formatApiError, type HermesRecommendation } from "../api/client";

function parseList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function HermesPage() {
  const queryClient = useQueryClient();
  const [task, setTask] = useState("需要多个角色讨论一个内容方案，并给出最终决策。");
  const [modeCandidates, setModeCandidates] = useState("dispatch,discuss,hybrid");
  const [modelCandidates, setModelCandidates] = useState("main,planner,critic");
  const [skillCandidates, setSkillCandidates] = useState("script_review,safe_search");
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
        Hermes 用来沉淀安全经验，辅助主 Agent 选择模式、模型和 Skill。它只给建议，
        不会绕过审批、权限和安全边界。
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
          <h3>记录安全经验</h3>
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

      <section aria-label="Hermes 经验列表">
        <h3>经验记忆</h3>
        {sortedInsights.length === 0 ? (
          <article>
            <h4>还没有经验</h4>
            <p>记录一次成功、失败或中性经验后，Hermes 会在后续推荐时参考。</p>
          </article>
        ) : (
          <div className="card-grid">
            {sortedInsights.map((insight) => (
              <article key={insight.id}>
                <span className="eyebrow">{insight.outcome}</span>
                <h3>{insight.lesson}</h3>
                <p>标签：{insight.tags.join(", ") || "无"}</p>
                <p>权重：{insight.weight}</p>
                <time dateTime={insight.created_at}>{insight.created_at}</time>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
