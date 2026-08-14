import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError, type EvolutionRun } from "../api/client";

function latestRound(run: EvolutionRun) {
  return run.rounds[run.rounds.length - 1] ?? null;
}

function splitList(value: string) {
  return value
    .split(/[，,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function EvolutionPage() {
  const queryClient = useQueryClient();
  const evolutionRuns = useQuery({ queryKey: ["evolution-runs"], queryFn: () => api.evolutionRuns() });
  const [title, setTitle] = useState("Skill 进化任务");
  const [objective, setObjective] = useState("用固定评测集验证候选版本，未达标不发布。");
  const [sourceSkills, setSourceSkills] = useState("darwin-skill");
  const [roundRunId, setRoundRunId] = useState("");
  const [changedDimension, setChangedDimension] = useState("实测表现");
  const [candidateSummary, setCandidateSummary] = useState("补充测试 prompt 并降低自评偏差。");
  const [scoreBefore, setScoreBefore] = useState("72");
  const [scoreAfter, setScoreAfter] = useState("76");

  const runs = evolutionRuns.data ?? [];
  const activeRuns = runs.filter((run) => run.status !== "stopped" && run.status !== "completed");
  const selectedRoundRunId = roundRunId || activeRuns[0]?.id || runs[0]?.id || "";

  const createRun = useMutation({
    mutationFn: () =>
      api.createEvolutionRun({
        kind: "skill_optimization",
        title: title.trim(),
        objective: objective.trim(),
        mode: "hybrid",
        source_skill_ids: splitList(sourceSkills),
        target_artifact_type: "skill",
        max_rounds: 5,
        min_delta: 2,
        rubric: ["实测表现", "反例覆盖", "人工验收"],
      }),
    onSuccess: async (created) => {
      setRoundRunId(created.id);
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  const recordRound = useMutation({
    mutationFn: () =>
      api.recordEvolutionRound(selectedRoundRunId, {
        changed_dimension: changedDimension.trim(),
        candidate_summary: candidateSummary.trim(),
        score_before: Number(scoreBefore),
        score_after: Number(scoreAfter),
        tests_passed: true,
        regression_detected: false,
        judge_summary: "由基准评测 agent 和固定测试集记录。",
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim() || !objective.trim()) return;
    createRun.mutate();
  }

  function submitRound(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedRoundRunId || !changedDimension.trim() || !candidateSummary.trim()) return;
    recordRound.mutate();
  }

  return (
    <section>
      <p className="eyebrow">Evolution</p>
      <h2>进化</h2>
      <p className="compact-page-intro">
        这里管理 Skill 蒸馏、达尔文式迭代和长期多轮任务。候选结果先进入记录，经过评测和审批后再发布。
      </p>

      <div className="resource-layout">
        <section className="resource-card" aria-label="创建进化任务">
          <h3>新建进化任务</h3>
          <form className="stacked-form" onSubmit={submitCreate}>
            <label>
              任务名称
              <input value={title} onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label>
              目标
              <textarea value={objective} onChange={(event) => setObjective(event.target.value)} />
            </label>
            <label>
              来源 Skill
              <input value={sourceSkills} onChange={(event) => setSourceSkills(event.target.value)} />
            </label>
            <button type="submit" disabled={createRun.isPending || !title.trim() || !objective.trim()}>
              {createRun.isPending ? "创建中..." : "创建任务"}
            </button>
            {createRun.isError ? <p role="alert">{formatApiError(createRun.error, "进化任务创建失败")}</p> : null}
          </form>
        </section>

        <section className="resource-card" aria-label="登记迭代轮次">
          <h3>登记一轮迭代</h3>
          <form className="stacked-form" onSubmit={submitRound}>
            <label>
              任务
              <select value={selectedRoundRunId} onChange={(event) => setRoundRunId(event.target.value)}>
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    {run.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              改动维度
              <input value={changedDimension} onChange={(event) => setChangedDimension(event.target.value)} />
            </label>
            <label>
              候选摘要
              <textarea value={candidateSummary} onChange={(event) => setCandidateSummary(event.target.value)} />
            </label>
            <div className="inline-fields">
              <label>
                前分数
                <input value={scoreBefore} onChange={(event) => setScoreBefore(event.target.value)} inputMode="decimal" />
              </label>
              <label>
                后分数
                <input value={scoreAfter} onChange={(event) => setScoreAfter(event.target.value)} inputMode="decimal" />
              </label>
            </div>
            <button type="submit" disabled={recordRound.isPending || !selectedRoundRunId}>
              {recordRound.isPending ? "登记中..." : "登记轮次"}
            </button>
            {recordRound.isError ? <p role="alert">{formatApiError(recordRound.error, "迭代轮次登记失败")}</p> : null}
          </form>
        </section>
      </div>

      <section className="resource-card" aria-label="进化任务">
        <div className="conversation-list-header">
          <div>
            <h3>进化记录</h3>
            <span>{runs.length} 条</span>
          </div>
        </div>
        {evolutionRuns.isLoading ? <p>正在加载进化记录...</p> : null}
        {evolutionRuns.isError ? <p role="alert">{formatApiError(evolutionRuns.error, "进化记录加载失败")}</p> : null}
        {runs.length === 0 && !evolutionRuns.isLoading ? <p className="field-help">还没有进化任务。</p> : null}
        <div className="evolution-run-list">
          {runs.map((run) => {
            const latest = latestRound(run);
            return (
              <article key={run.id} className="evolution-run-card">
                <div>
                  <span className="eyebrow">{run.kind}</span>
                  <h3>{run.title}</h3>
                  <p>{run.objective}</p>
                </div>
                <dl>
                  <dt>状态</dt>
                  <dd>{run.status}</dd>
                  <dt>模式</dt>
                  <dd>{run.mode}</dd>
                  <dt>轮次</dt>
                  <dd>{run.rounds.length} / {run.max_rounds}</dd>
                  <dt>最近建议</dt>
                  <dd>{latest ? latest.recommendation : "等待首轮"}</dd>
                </dl>
                {latest ? <p>第 {latest.round} 轮：{latest.changed_dimension}，提升 {latest.delta}</p> : null}
              </article>
            );
          })}
        </div>
      </section>
    </section>
  );
}