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

function compactValue(value: string | null | undefined, fallback = "未设置") {
  return value && value.trim() ? value : fallback;
}

export function EvolutionPage() {
  const queryClient = useQueryClient();
  const evolutionRuns = useQuery({ queryKey: ["evolution-runs"], queryFn: () => api.evolutionRuns() });
  const [title, setTitle] = useState("Skill 进化任务");
  const [objective, setObjective] = useState("用固定评测集验证候选版本，未达标不发布。");
  const [sourceSkills, setSourceSkills] = useState("darwin-skill");
  const [baselineAgentId, setBaselineAgentId] = useState("agent-main-m3");
  const [candidateAgentIds, setCandidateAgentIds] = useState("agent-coder, agent-reviewer");
  const [evaluatorAgentId, setEvaluatorAgentId] = useState("agent-evaluator");
  const [approvalPolicy, setApprovalPolicy] = useState<"ask" | "auto" | "manual">("ask");
  const [iterationPolicy, setIterationPolicy] = useState<"score_gated" | "fixed_rounds" | "manual_review">("score_gated");
  const [memoryPolicy, setMemoryPolicy] = useState<"none" | "summarize_between_rounds" | "full_ledger">("summarize_between_rounds");
  const [roundRunId, setRoundRunId] = useState("");
  const [changedDimension, setChangedDimension] = useState("实测表现");
  const [candidateSummary, setCandidateSummary] = useState("补充测试 prompt 并降低自评偏差。");
  const [scoreBefore, setScoreBefore] = useState("72");
  const [scoreAfter, setScoreAfter] = useState("76");

  const runs = evolutionRuns.data ?? [];
  const activeRuns = runs.filter((run) => run.status === "running");
  const selectedRoundRunId = roundRunId || activeRuns[0]?.id || runs[0]?.id || "";
  const selectedRoundRun = runs.find((run) => run.id === selectedRoundRunId) ?? null;

  const createRun = useMutation({
    mutationFn: () =>
      api.createEvolutionRun({
        kind: "skill_optimization",
        title: title.trim(),
        objective: objective.trim(),
        mode: "hybrid",
        source_skill_ids: splitList(sourceSkills),
        target_artifact_type: "skill",
        baseline_agent_id: baselineAgentId.trim() || null,
        candidate_agent_ids: splitList(candidateAgentIds),
        evaluator_agent_id: evaluatorAgentId.trim() || null,
        approval_policy: approvalPolicy,
        iteration_policy: iterationPolicy,
        memory_policy: memoryPolicy,
        max_rounds: 5,
        min_delta: 2,
        rubric: ["实测表现", "反例覆盖", "人工验收"],
      }),
    onSuccess: async (created) => {
      setRoundRunId(created.id);
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  const approveRun = useMutation({
    mutationFn: (run: EvolutionRun) =>
      api.approveEvolutionRun(run.id, {
        approved: true,
        baseline_agent_id: run.baseline_agent_id,
        evaluator_agent_id: run.evaluator_agent_id,
        note: "人工确认基准 agent 和评测口径。",
      }),
    onSuccess: async (approved) => {
      setRoundRunId(approved.id);
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
    if (!selectedRoundRunId || selectedRoundRun?.status !== "running" || !changedDimension.trim() || !candidateSummary.trim()) return;
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
            <div className="inline-fields">
              <label>
                基准 agent
                <input value={baselineAgentId} onChange={(event) => setBaselineAgentId(event.target.value)} />
              </label>
              <label>
                评测 agent
                <input value={evaluatorAgentId} onChange={(event) => setEvaluatorAgentId(event.target.value)} />
              </label>
            </div>
            <label>
              候选 agent
              <input value={candidateAgentIds} onChange={(event) => setCandidateAgentIds(event.target.value)} />
            </label>
            <div className="inline-fields">
              <label>
                审批
                <select value={approvalPolicy} onChange={(event) => setApprovalPolicy(event.target.value as typeof approvalPolicy)}>
                  <option value="ask">需要确认</option>
                  <option value="manual">手动推进</option>
                  <option value="auto">自动推进</option>
                </select>
              </label>
              <label>
                迭代
                <select value={iterationPolicy} onChange={(event) => setIterationPolicy(event.target.value as typeof iterationPolicy)}>
                  <option value="score_gated">按评分门控</option>
                  <option value="fixed_rounds">固定轮次</option>
                  <option value="manual_review">人工复核</option>
                </select>
              </label>
            </div>
            <label>
              记忆
              <select value={memoryPolicy} onChange={(event) => setMemoryPolicy(event.target.value as typeof memoryPolicy)}>
                <option value="summarize_between_rounds">轮次间压缩</option>
                <option value="full_ledger">完整台账</option>
                <option value="none">不启用</option>
              </select>
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
            {selectedRoundRun && selectedRoundRun.status !== "running" ? <p className="field-help">该任务需要审批后才能登记轮次。</p> : null}
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
            <button type="submit" disabled={recordRound.isPending || !selectedRoundRunId || selectedRoundRun?.status !== "running"}>
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
                  <dt>审批</dt>
                  <dd>{run.approval_status}</dd>
                  <dt>基准 agent</dt>
                  <dd>{compactValue(run.baseline_agent_id)}</dd>
                  <dt>评测 agent</dt>
                  <dd>{compactValue(run.evaluator_agent_id)}</dd>
                  <dt>下一步</dt>
                  <dd>{run.next_action}</dd>
                  <dt>轮次</dt>
                  <dd>{run.rounds.length} / {run.max_rounds}</dd>
                  <dt>最近建议</dt>
                  <dd>{latest ? latest.recommendation : "等待首轮"}</dd>
                </dl>
                {run.candidate_agent_ids.length > 0 ? <p>候选 agent：{run.candidate_agent_ids.join("、")}</p> : null}
                {latest ? <p>第 {latest.round} 轮：{latest.changed_dimension}，提升 {latest.delta}</p> : null}
                {run.approval_status === "pending" ? (
                  <button type="button" onClick={() => approveRun.mutate(run)} disabled={approveRun.isPending}>
                    {approveRun.isPending ? "审批中..." : "审批通过"}
                  </button>
                ) : null}
                {approveRun.isError ? <p role="alert">{formatApiError(approveRun.error, "进化任务审批失败")}</p> : null}
              </article>
            );
          })}
        </div>
      </section>
    </section>
  );
}
