import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError, type EvolutionNextRoundExecution, type EvolutionNextRoundPlan, type EvolutionRun } from "../api/client";

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

type ApprovalDraft = {
  baselineAgentId: string;
  evaluatorAgentId: string;
  note: string;
};

type AcceptedChoice = "auto" | "accept" | "reject";

function defaultApprovalDraft(run: EvolutionRun): ApprovalDraft {
  return {
    baselineAgentId: run.baseline_agent_id ?? "",
    evaluatorAgentId: run.evaluator_agent_id ?? "",
    note: run.approval_note || "人工确认基准 agent 和评测口径。",
  };
}

function acceptedValue(choice: AcceptedChoice) {
  if (choice === "accept") return true;
  if (choice === "reject") return false;
  return null;
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
  const [approvalDrafts, setApprovalDrafts] = useState<Record<string, ApprovalDraft>>({});
  const [nextRoundPlans, setNextRoundPlans] = useState<Record<string, EvolutionNextRoundPlan>>({});
  const [nextRoundExecutions, setNextRoundExecutions] = useState<Record<string, EvolutionNextRoundExecution>>({});
  const [roundRunId, setRoundRunId] = useState("");
  const [changedDimension, setChangedDimension] = useState("实测表现");
  const [candidateSummary, setCandidateSummary] = useState("补充测试 prompt 并降低自评偏差。");
  const [scoreBefore, setScoreBefore] = useState("72");
  const [scoreAfter, setScoreAfter] = useState("76");
  const [testsPassed, setTestsPassed] = useState(true);
  const [regressionDetected, setRegressionDetected] = useState(false);
  const [acceptedChoice, setAcceptedChoice] = useState<AcceptedChoice>("auto");
  const [judgeSummary, setJudgeSummary] = useState("由基准评测 agent 和固定测试集记录。");
  const [artifactRefs, setArtifactRefs] = useState("");
  const [tokensUsed, setTokensUsed] = useState("0");
  const [elapsedSeconds, setElapsedSeconds] = useState("0");

  const runs = evolutionRuns.data ?? [];
  const activeRuns = runs.filter((run) => run.status === "running");
  const selectedRoundRunId = roundRunId || activeRuns[0]?.id || runs[0]?.id || "";
  const selectedRoundRun = runs.find((run) => run.id === selectedRoundRunId) ?? null;

  function draftFor(run: EvolutionRun) {
    return approvalDrafts[run.id] ?? defaultApprovalDraft(run);
  }

  function updateDraft(run: EvolutionRun, patch: Partial<ApprovalDraft>) {
    setApprovalDrafts((current) => ({
      ...current,
      [run.id]: { ...draftFor(run), ...patch },
    }));
  }

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
      setApprovalDrafts((current) => ({ ...current, [created.id]: defaultApprovalDraft(created) }));
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  const planNextRound = useMutation({
    mutationFn: (run: EvolutionRun) => api.evolutionNextRoundPlan(run.id),
    onSuccess: (plan) => {
      setNextRoundPlans((current) => ({ ...current, [plan.run_id]: plan }));
    },
  });
  const executeNextRound = useMutation({
    mutationFn: (run: EvolutionRun) => api.executeEvolutionNextRound(run.id),
    onSuccess: async (execution) => {
      setNextRoundExecutions((current) => ({ ...current, [execution.evolution_run_id]: execution }));
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const approveRun = useMutation({
    mutationFn: ({ run, approved }: { run: EvolutionRun; approved: boolean }) => {
      const draft = draftFor(run);
      return api.approveEvolutionRun(run.id, {
        approved,
        baseline_agent_id: draft.baselineAgentId.trim() || null,
        evaluator_agent_id: draft.evaluatorAgentId.trim() || null,
        note: draft.note.trim(),
      });
    },
    onSuccess: async (approved) => {
      setRoundRunId(approved.id);
      setApprovalDrafts((current) => ({ ...current, [approved.id]: defaultApprovalDraft(approved) }));
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
        tests_passed: testsPassed,
        regression_detected: regressionDetected,
        accepted: acceptedValue(acceptedChoice),
        judge_summary: judgeSummary.trim(),
        artifact_refs: splitList(artifactRefs),
        tokens_used: Number(tokensUsed) || 0,
        elapsed_seconds: Number(elapsedSeconds) || 0,
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
            <div className="inline-fields">
              <label className="inline-check compact-check">
                <input type="checkbox" checked={testsPassed} onChange={(event) => setTestsPassed(event.target.checked)} />
                测试通过
              </label>
              <label className="inline-check compact-check">
                <input type="checkbox" checked={regressionDetected} onChange={(event) => setRegressionDetected(event.target.checked)} />
                发现回归
              </label>
            </div>
            <label>
              候选接收
              <select value={acceptedChoice} onChange={(event) => setAcceptedChoice(event.target.value as AcceptedChoice)}>
                <option value="auto">按分数和测试自动判断</option>
                <option value="accept">人工接收</option>
                <option value="reject">人工拒绝</option>
              </select>
            </label>
            <label>
              评审说明
              <textarea value={judgeSummary} onChange={(event) => setJudgeSummary(event.target.value)} />
            </label>
            <label>
              产物引用
              <textarea value={artifactRefs} onChange={(event) => setArtifactRefs(event.target.value)} placeholder="每行一个产物或报告引用" />
            </label>
            <div className="inline-fields">
              <label>
                Token 消耗
                <input value={tokensUsed} onChange={(event) => setTokensUsed(event.target.value)} inputMode="numeric" />
              </label>
              <label>
                耗时秒
                <input value={elapsedSeconds} onChange={(event) => setElapsedSeconds(event.target.value)} inputMode="numeric" />
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
            const draft = draftFor(run);
            const nextRoundPlan = nextRoundPlans[run.id] ?? null;
            const nextRoundExecution = nextRoundExecutions[run.id] ?? null;
            const isPlanningThisRun = planNextRound.isPending && planNextRound.variables?.id === run.id;
            const planningFailedThisRun = planNextRound.isError && planNextRound.variables?.id === run.id;
            const isExecutingThisRun = executeNextRound.isPending && executeNextRound.variables?.id === run.id;
            const executionFailedThisRun = executeNextRound.isError && executeNextRound.variables?.id === run.id;
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
                  <dt>停止原因</dt>
                  <dd>{run.stop_reason ?? latest?.stop_reason ?? "无"}</dd>
                </dl>
                {run.candidate_agent_ids.length > 0 ? <p>候选 agent：{run.candidate_agent_ids.join("、")}</p> : null}
                {run.source_conversation_id ? <p>来源会话：{run.source_conversation_id}</p> : null}
                {latest ? <p>第 {latest.round} 轮：{latest.changed_dimension}，提升 {latest.delta}</p> : null}
                {run.approval_status === "pending" ? (
                  <div className="stacked-form evolution-approval-controls" aria-label={`${run.title} 审批设置`}>
                    <div className="inline-fields">
                      <label>
                        审批基准 agent
                        <input value={draft.baselineAgentId} onChange={(event) => updateDraft(run, { baselineAgentId: event.target.value })} />
                      </label>
                      <label>
                        审批评测 agent
                        <input value={draft.evaluatorAgentId} onChange={(event) => updateDraft(run, { evaluatorAgentId: event.target.value })} />
                      </label>
                    </div>
                    <label>
                      审批备注
                      <textarea value={draft.note} onChange={(event) => updateDraft(run, { note: event.target.value })} />
                    </label>
                    <div className="table-actions">
                      <button type="button" onClick={() => approveRun.mutate({ run, approved: true })} disabled={approveRun.isPending}>
                        {approveRun.isPending ? "审批中..." : "审批通过"}
                      </button>
                      <button type="button" className="danger-action" onClick={() => approveRun.mutate({ run, approved: false })} disabled={approveRun.isPending}>
                        拒绝进化
                      </button>
                    </div>
                  </div>
                ) : null}
                {run.status === "running" ? (
                  <div className="table-actions">
                    <button type="button" onClick={() => planNextRound.mutate(run)} disabled={isPlanningThisRun}>
                      {isPlanningThisRun ? "生成中..." : "生成执行包"}
                    </button>
                    <button type="button" onClick={() => executeNextRound.mutate(run)} disabled={isExecutingThisRun}>
                      {isExecutingThisRun ? "启动中..." : "启动执行"}
                    </button>
                  </div>
                ) : null}
                {planningFailedThisRun ? <p role="alert">{formatApiError(planNextRound.error, "执行包生成失败")}</p> : null}
                {executionFailedThisRun ? <p role="alert">{formatApiError(executeNextRound.error, "执行启动失败")}</p> : null}
                {nextRoundExecution ? (
                  <p className="field-help">
                    已启动第 {nextRoundExecution.round} 轮执行：{nextRoundExecution.execution_run_id}（{nextRoundExecution.status}）
                  </p>
                ) : null}
                {nextRoundPlan ? (
                  <div className="reference-preview">
                    <strong>{nextRoundPlan.task_title}</strong>
                    <p>
                      第 {nextRoundPlan.round} 轮 · {nextRoundPlan.action} · 记忆：{nextRoundPlan.memory_policy}
                    </p>
                    <pre className="code-block">{nextRoundPlan.task_prompt}</pre>
                  </div>
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
