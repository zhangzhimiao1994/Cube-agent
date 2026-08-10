import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, formatApiError, type RunDetail, type SubmittedRun } from "../api/client";

const RUN_MODES = [
  { value: "auto", label: "自动检测", description: "主 Agent 会判断应使用直接、派单、讨论或混合模式；不确定时应询问用户。" },
  { value: "direct", label: "直接执行", description: "适合简单问答或单角色、单步骤任务。" },
  { value: "dispatch", label: "派单式", description: "适合拆成多个专业角色执行的任务；派给谁由工作流或本次选择决定。" },
  { value: "discuss", label: "讨论式", description: "适合多角色观点冲突、方案评审或需要裁决的任务。" },
  { value: "hybrid", label: "混合式", description: "先讨论定方案，再派单执行，最后审查收口。" },
] as const;

type RunMode = (typeof RUN_MODES)[number]["value"];
type ManualRunMode = Exclude<RunMode, "auto">;
type ModeSelection = {
  runId: string;
  decisionToken: string;
  version: number;
  reason: string | null;
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MANUAL_RUN_MODES = RUN_MODES.filter((item) => item.value !== "auto");

function newConversationId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `conv-${crypto.randomUUID()}`;
  }
  return `conv-${Date.now().toString(36)}`;
}

function displayMode(mode: string | null | undefined) {
  return RUN_MODES.find((item) => item.value === mode)?.label ?? mode ?? "等待选择";
}

function toggle(list: string[], value: string) {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function explainActualMode(run: { status: string; mode: string | null }) {
  if (run.status === "waiting_user_mode") {
    return "自动检测没有足够把握，本次任务正在等待你确认运行模式。";
  }
  if (!run.mode) return "本次任务尚未确定运行模式。";
  return `本次任务实际使用：${displayMode(run.mode)}。`;
}

function modeSelectionFromSubmittedRun(run: SubmittedRun): ModeSelection | null {
  if (run.status !== "waiting_user_mode" || !run.decision_token) return null;
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: run.version,
    reason: run.clarification_reason,
  };
}

function modeSelectionFromRunDetail(run: RunDetail | undefined): ModeSelection | null {
  if (!run || run.status !== "waiting_user_mode" || !run.decision_token) return null;
  const parsedVersion = Number(run.explicit_details.version ?? "0");
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: Number.isInteger(parsedVersion) && parsedVersion > 0 ? parsedVersion : 0,
    reason: run.explicit_details.routing_reason ?? null,
  };
}

function detailMessages(detail: RunDetail | undefined) {
  if (!detail) return [];
  return [
    {
      id: "request",
      role: "user",
      title: "你的任务",
      body: detail.request,
    },
    {
      id: "routing",
      role: "assistant",
      title: "模式与角色",
      body: [
        `运行模式：${displayMode(detail.mode)}`,
        detail.explicit_details.workflow_id ? `工作流：${detail.explicit_details.workflow_id}` : null,
        detail.explicit_details.workflow_adjustment_policy
          ? `工作流调整：${
              detail.explicit_details.workflow_adjustment_policy === "ask_before_apply"
                ? "允许提出，执行前核对"
                : "严格按预设"
            }`
          : null,
        detail.explicit_details.selected_agent_ids ? `参与角色：${detail.explicit_details.selected_agent_ids}` : null,
        detail.explicit_details.routing_reason ? `路由原因：${detail.explicit_details.routing_reason}` : null,
      ]
        .filter(Boolean)
        .join("\n"),
    },
    ...detail.events.map((event) => ({
      id: `event-${event.sequence}`,
      role: "assistant",
      title: event.kind,
      body: event.message,
    })),
    ...detail.artifacts.map((artifact) => ({
      id: `artifact-${artifact.id}`,
      role: "assistant",
      title: `产物：${artifact.title}`,
      body: artifact.kind,
    })),
  ];
}

export function RunsPage() {
  const queryClient = useQueryClient();
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.runs() });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.agents() });
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.settings() });
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState<RunMode>("auto");
  const [workflowId, setWorkflowId] = useState("");
  const [agentIds, setAgentIds] = useState<string[]>([]);
  const [conversationId, setConversationId] = useState(newConversationId);
  const [referenceConversationId, setReferenceConversationId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [submitNotice, setSubmitNotice] = useState<string | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [modeSelection, setModeSelection] = useState<ModeSelection | null>(null);
  const [temporaryApproval, setTemporaryApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: NonNullable<SubmittedRun["temporary_agent_proposal"]>;
    approved: boolean;
  } | null>(null);
  const [temporaryFeedback, setTemporaryFeedback] = useState("");
  const trimmedReferenceConversationId = referenceConversationId.trim();

  const selectedWorkflow = useMemo(
    () => (workflows.data ?? []).find((workflow) => workflow.id === workflowId),
    [workflowId, workflows.data],
  );

  const selectedRun = useQuery({
    queryKey: ["run", selectedRunId],
    queryFn: () => api.run(selectedRunId ?? ""),
    enabled: Boolean(selectedRunId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && !TERMINAL_STATUSES.has(data.status) ? 1000 : false;
    },
  });

  const referenceConversation = useQuery({
    queryKey: ["conversation", trimmedReferenceConversationId],
    queryFn: () => api.conversation(trimmedReferenceConversationId),
    enabled: false,
  });

  useEffect(() => {
    if (!settings.data) return;
    setMode(settings.data.default_mode);
    setWorkflowId(settings.data.default_workflow_id ?? "");
    setAgentIds(settings.data.default_agent_ids);
  }, [settings.data]);

  useEffect(() => {
    if (!selectedWorkflow) return;
    if (selectedWorkflow.mode) setMode(selectedWorkflow.mode);
    setAgentIds(selectedWorkflow.agent_ids ?? []);
  }, [selectedWorkflow]);

  useEffect(() => {
    const selection = modeSelectionFromRunDetail(selectedRun.data);
    if (selection) {
      setModeSelection(selection);
    } else if (selectedRun.data && selectedRun.data.status !== "waiting_user_mode") {
      setModeSelection(null);
    }
  }, [selectedRun.data]);

  const createRun = useMutation({
    mutationFn: () =>
      api.createRun({
        message: message.trim(),
        mode,
        workflow_id: workflowId || null,
        allow_workflow_adjustment: selectedWorkflow?.allow_main_agent_override ?? false,
        agent_ids: mode === "direct" ? agentIds.slice(0, 1) : agentIds,
        conversation_id: conversationId,
        reference_conversation_id: referenceConversationId.trim() || null,
      }),
    onSuccess: async (run) => {
      setSelectedRunId(run.id);
      if (run.conversation_id) setConversationId(run.conversation_id);
      const selection = modeSelectionFromSubmittedRun(run);
      if (run.temporary_agent_proposal && run.decision_token) {
        setModeSelection(null);
        setTemporaryApproval({
          runId: run.id,
          decisionToken: run.decision_token,
          version: run.version,
          proposal: run.temporary_agent_proposal,
          approved: false,
        });
        setTemporaryFeedback("");
        setSubmitNotice("主 Agent 发现当前角色池能力不足，已暂停并等待你确认是否临时加入新子 Agent。");
      } else if (selection) {
        setTemporaryApproval(null);
        setModeSelection(selection);
        setSubmitNotice("主 Agent 对本次任务的模式判断不够确定，请在输入框上方选择运行模式后继续。");
      } else {
        setTemporaryApproval(null);
        setModeSelection(null);
        setSubmitNotice(explainActualMode(run));
      }
      setMessage("");
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    },
  });

  const chooseMode = useMutation({
    mutationFn: (chosenMode: ManualRunMode) => {
      if (!modeSelection) throw new Error("mode selection is unavailable");
      return api.chooseMode(modeSelection.runId, {
        mode: chosenMode,
        decision_token: modeSelection.decisionToken,
        version: modeSelection.version,
      });
    },
    onSuccess: async (run) => {
      setModeSelection(null);
      if (run.mode) setMode(run.mode as RunMode);
      setSubmitNotice(explainActualMode(run));
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    },
  });

  const approveTemporaryAgent = useMutation({
    mutationFn: () => {
      if (!temporaryApproval) throw new Error("temporary approval is unavailable");
      return api.approveTemporaryAgent(temporaryApproval.runId, {
        decision_token: temporaryApproval.decisionToken,
        version: temporaryApproval.version,
      });
    },
    onSuccess: async (run) => {
      setTemporaryApproval((current) => (current ? { ...current, approved: true } : current));
      setSubmitNotice("已确认临时子 Agent，本次任务已重新进入执行队列。任务完成后你可以决定是否永久保存该 Agent。");
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    },
  });

  const promoteTemporaryAgent = useMutation({
    mutationFn: () => {
      if (!temporaryApproval) throw new Error("temporary approval is unavailable");
      return api.createAgent({
        id: temporaryApproval.proposal.id,
        name: temporaryApproval.proposal.name,
        enabled: true,
        role: temporaryApproval.proposal.role,
        prompt: temporaryApproval.proposal.prompt,
        model: null,
        skills: temporaryApproval.proposal.suggested_skills,
      });
    },
    onSuccess: async () => {
      setSubmitNotice("临时子 Agent 已保存为永久 Agent。请到 Agent 页面为它绑定已测试模型后投入常规工作流。");
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  const reviseTemporaryAgent = useMutation({
    mutationFn: () => {
      if (!temporaryApproval) throw new Error("temporary approval is unavailable");
      return api.reviseTemporaryAgent(temporaryApproval.runId, {
        decision_token: temporaryApproval.decisionToken,
        version: temporaryApproval.version,
        feedback: temporaryFeedback.trim(),
      });
    },
    onSuccess: async (run) => {
      setTemporaryApproval(null);
      setTemporaryFeedback("");
      setSubmitNotice("已收到你的新意见，主 Agent 会按反馈重新规划本次任务。");
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitNotice(null);
    createRun.mutate();
  }

  function loadReferenceConversation() {
    if (!trimmedReferenceConversationId) return;
    void referenceConversation.refetch();
  }

  if (runs.isLoading) {
    return <p>正在加载对话任务...</p>;
  }
  if (runs.isError) return <p role="alert">{formatApiError(runs.error, "任务列表加载失败")}</p>;

  const items = runs.data ?? [];
  const selectedMode = RUN_MODES.find((item) => item.value === mode) ?? RUN_MODES[0];
  const messages = detailMessages(selectedRun.data);
  const savedAgents = agents.data ?? [];
  const savedWorkflows = workflows.data ?? [];
  const directAnswerer = mode === "direct" && agentIds.length > 0 ? agentIds[0] : "main_agent";

  return (
    <section>
      <p className="eyebrow">Conversation</p>
      <h2>对话任务</h2>
      <p className="compact-page-intro">
        工作流配置和工作流使用是分开的：这里负责选择本次对话怎么运行，配置请到“工作流配置”页面维护。
      </p>

      <div className="mobile-chat-hierarchy" aria-label="移动端对话层级">
        <span>1 · 会话</span>
        <span>2 · 对话</span>
        <span>3 · 设置 / 详情</span>
      </div>

      <div className="chat-console">
        <nav className="conversation-list" aria-label="手机版会话导航">
          <div className="conversation-list-header">
            <h3>会话</h3>
            <span>{items.length}</span>
          </div>
          {items.length === 0 ? (
            <p className="field-help">还没有任务。可以从右侧输入框发起第一次对话。</p>
          ) : (
            items.map((run) => (
              <button
                type="button"
                key={run.id}
                className={`conversation-item${selectedRunId === run.id ? " conversation-item-active" : ""}`}
                onClick={() => setSelectedRunId(run.id)}
              >
                <span>{displayMode(run.mode)}</span>
                <strong>{run.id.slice(0, 8)}</strong>
                <small>{run.status}</small>
              </button>
            ))
          )}
        </nav>

        <div className={`chat-panel${configOpen ? " chat-panel-config-open" : ""}`}>
          {configOpen ? (
              <div className="composer-config-sheet" role="region" aria-label="本次运行更多设置">
          <details className="run-settings-panel" aria-label="本次运行设置" open>
            <summary aria-label="展开或收起本次运行设置">本次运行设置</summary>
            <div className="chat-config-strip" aria-label="本次对话运行设置">
            <label htmlFor="run-mode">
              模式
              <select id="run-mode" value={mode} onChange={(event) => setMode(event.target.value as RunMode)}>
                {RUN_MODES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="run-workflow">
              使用工作流
              <select id="run-workflow" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
                <option value="">不使用固定工作流</option>
                {savedWorkflows
                  .filter((workflow) => workflow.enabled)
                  .map((workflow) => (
                    <option key={workflow.id} value={workflow.id}>
                      {workflow.name}
                    </option>
                  ))}
              </select>
            </label>
            <label htmlFor="conversation-id">
              本次会话 ID
              <input
                id="conversation-id"
                value={conversationId}
                onChange={(event) => setConversationId(event.target.value)}
              />
            </label>
            <label htmlFor="reference-conversation-id">
              参考会话 ID
              <input
                id="reference-conversation-id"
                value={referenceConversationId}
                onChange={(event) => setReferenceConversationId(event.target.value)}
                placeholder="可选：粘贴其他会话 ID"
              />
            </label>
            <button
              className="secondary-action inline-action"
              type="button"
              disabled={!trimmedReferenceConversationId || referenceConversation.isFetching}
              onClick={loadReferenceConversation}
            >
              {referenceConversation.isFetching ? "读取中..." : "读取参考会话"}
            </button>
            <div className="mode-help">
              <span className="eyebrow">{selectedMode.label}</span>
              <p>{selectedMode.description}</p>
              {settings.isLoading ? <p>正在加载默认运行设置...</p> : null}
              {settings.isError ? (
                <p role="alert">{formatApiError(settings.error, "系统设置加载失败")}</p>
              ) : null}
              {workflows.isError ? (
                <p role="alert">{formatApiError(workflows.error, "工作流列表加载失败")}</p>
              ) : null}
              {selectedWorkflow ? (
                <>
                  <p>
                    当前工作流：{selectedWorkflow.name}
                    {selectedWorkflow.task_type ? `；适用场景：${selectedWorkflow.task_type}` : ""}
                  </p>
                  <p>
                    工作流临场调整：
                    {selectedWorkflow.allow_main_agent_override
                      ? "主 Agent 可以提出改步骤、换角色或加交付物，但执行前必须向你核对。"
                      : "关闭；主 Agent 会按该工作流预设执行，只提示明显不匹配风险。"}
                  </p>
                </>
              ) : (
                <p>未选择工作流时，主 Agent 会按任务内容和你勾选的角色进行调度。</p>
              )}
            </div>
            {referenceConversation.data ? (
              <div className="reference-preview">
                <span className="eyebrow">{referenceConversation.data.conversation_id}</span>
                <strong>已读取 {referenceConversation.data.runs.length} 条运行</strong>
                {referenceConversation.data.runs.slice(0, 3).map((run) => (
                  <p key={run.id}>{run.request}</p>
                ))}
              </div>
            ) : null}
            {referenceConversation.isError ? (
              <p className="form-error" role="alert">
                {formatApiError(referenceConversation.error, "参考会话读取失败")}
              </p>
            ) : null}
            </div>
          </details>

          <details className="inline-guide" open={mode === "direct"}>
            <summary>{mode === "direct" ? "选择直连回答者" : "选择本次参与角色池"}</summary>
            {mode === "direct" ? (
              <>
                <p className="field-help">
                  直接执行只会让一个对象回答。选择“主 Agent 自己回答”会提交空角色列表；选择某个角色会提交该角色 ID。
                </p>
                <label htmlFor="direct-answerer">
                  直连回答者
                  <select
                    id="direct-answerer"
                    value={directAnswerer}
                    onChange={(event) =>
                      setAgentIds(event.target.value === "main_agent" ? [] : [event.target.value])
                    }
                  >
                    <option value="main_agent">主 Agent 自己回答</option>
                    {savedAgents
                      .filter((agent) => agent.enabled)
                      .map((agent) => (
                        <option key={agent.id} value={agent.id}>
                          {agent.name}（{agent.id}）
                        </option>
                      ))}
                  </select>
                </label>
                {agents.isLoading ? <p className="field-help">正在加载 Agent 角色...</p> : null}
                {agents.isError ? (
                  <p className="field-help" role="alert">
                    {formatApiError(agents.error, "Agent 列表加载失败")}
                  </p>
                ) : null}
              </>
            ) : (
              <>
                <p className="field-help">
                  同一个模式可以派给不同对象。选择工作流会自动带出默认角色；你也可以为本次任务临时增删。
                </p>
                <fieldset>
                  <legend>角色池</legend>
                  {agents.isLoading ? (
                    <p className="field-help">正在加载 Agent 角色...</p>
                  ) : agents.isError ? (
                    <p className="field-help" role="alert">
                      {formatApiError(agents.error, "Agent 列表加载失败")}
                    </p>
                  ) : savedAgents.length === 0 ? (
                    <p className="field-help">还没有 Agent。请先到 Agent 页面创建角色。</p>
                  ) : (
                    savedAgents.map((agent) => (
                      <label key={agent.id} className="inline-check">
                        <input
                          type="checkbox"
                          checked={agentIds.includes(agent.id)}
                          onChange={() => setAgentIds((current) => toggle(current, agent.id))}
                        />
                        {agent.name}（{agent.id}）
                      </label>
                    ))
                  )}
                </fieldset>
              </>
            )}
          </details>
            </div>
          ) : null}

          <div className="chat-stream" role="region" aria-label="主对话内容" aria-live="polite">
            {selectedRun.isLoading ? <p>正在加载会话...</p> : null}
            {selectedRun.isError ? <p role="alert">{formatApiError(selectedRun.error, "会话加载失败")}</p> : null}
            {!selectedRunId ? (
              <article className="chat-message assistant">
                <span className="eyebrow">主 Agent</span>
                <h3>选择模式、工作流和角色，然后发送任务。</h3>
                <p>如果选择“自动检测”，提交后这里会显示主 Agent 最终采用的模式；如果无法判断，会等待你确认。</p>
              </article>
            ) : null}
            {messages.map((item) => (
              <article key={item.id} className={`chat-message ${item.role}`}>
                <span className="eyebrow">{item.role === "user" ? "你" : "Agent Hub"}</span>
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </article>
            ))}
            {selectedRun.data && !TERMINAL_STATUSES.has(selectedRun.data.status) ? (
              <article className="chat-message assistant streaming-status">
                <span className="eyebrow">LIVE</span>
                <h3>
                  {selectedRun.data.status === "waiting_user_mode"
                    ? "等待你选择运行模式"
                    : "正在实时刷新运行状态"}
                </h3>
                <p>
                  {selectedRun.data.status === "waiting_user_mode"
                    ? "主 Agent 判断不够确定，请在下方确认本次走直接、派单、讨论或混合模式。"
                    : "后端事件会持续同步到这里；生成、派单、讨论和产物状态会按时间追加。"}
                </p>
              </article>
            ) : null}
            {selectedRunId ? (
              <div className="chat-detail-action">
                <Link to={`/runs/${selectedRunId}`} className="secondary-action">
                  查看运行详情
                </Link>
                <span>打开完整事件、产物、错误和运行控制。</span>
              </div>
            ) : null}
          </div>

          <form onSubmit={submit} aria-label="发送对话任务" className="chat-composer">
            {temporaryApproval ? (
              <aside className="composer-approval-popover" role="dialog" aria-label="临时 Agent 确认提醒">
                <div>
                  <span className="eyebrow">主 Agent 请求确认</span>
                  <h3>{temporaryApproval.proposal.name}</h3>
                  <p>{temporaryApproval.proposal.reason}</p>
                  <p>
                    缺少能力：{temporaryApproval.proposal.missing_capability}；角色边界：
                    {temporaryApproval.proposal.prompt}
                  </p>
                </div>
                <label htmlFor="temporary-agent-feedback">
                  提出新的意见
                  <textarea
                    id="temporary-agent-feedback"
                    value={temporaryFeedback}
                    onChange={(event) => setTemporaryFeedback(event.target.value)}
                    placeholder="例如：不要加工程师，先让产品经理重新拆需求。"
                  />
                </label>
                <div className="composer-actions">
                  <button
                    type="button"
                    disabled={temporaryApproval.approved || approveTemporaryAgent.isPending}
                    onClick={() => approveTemporaryAgent.mutate()}
                  >
                    {temporaryApproval.approved ? "已临时加入" : "接受并临时加入"}
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={temporaryFeedback.trim().length === 0 || reviseTemporaryAgent.isPending}
                    onClick={() => reviseTemporaryAgent.mutate()}
                  >
                    按我的意见重规
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={!temporaryApproval.approved || promoteTemporaryAgent.isPending}
                    onClick={() => promoteTemporaryAgent.mutate()}
                  >
                    保存为永久 Agent
                  </button>
                </div>
                {approveTemporaryAgent.isError ? (
                  <p role="alert">{formatApiError(approveTemporaryAgent.error, "临时 Agent 确认失败")}</p>
                ) : null}
                {reviseTemporaryAgent.isError ? (
                  <p role="alert">{formatApiError(reviseTemporaryAgent.error, "临时 Agent 重规失败")}</p>
                ) : null}
                {promoteTemporaryAgent.isError ? (
                  <p role="alert">{formatApiError(promoteTemporaryAgent.error, "永久化 Agent 失败")}</p>
                ) : null}
              </aside>
            ) : null}
            {modeSelection ? (
              <aside className="composer-approval-popover mode-choice-popover" role="dialog" aria-label="运行模式确认">
                <div>
                  <span className="eyebrow">主 Agent 需要你确认</span>
                  <h3>本次任务应该怎么运行？</h3>
                  <p>
                    自动检测没有足够把握，原因：{modeSelection.reason ?? "routing_requires_user_choice"}。
                    选择后任务会继续进入队列，并按对应模式派给角色池。
                  </p>
                </div>
                <div className="mode-choice-grid">
                  {MANUAL_RUN_MODES.map((item) => (
                    <button
                      type="button"
                      key={item.value}
                      disabled={chooseMode.isPending}
                      onClick={() => chooseMode.mutate(item.value as ManualRunMode)}
                    >
                      <strong>{item.label}</strong>
                      <small>{item.description}</small>
                    </button>
                  ))}
                </div>
                {chooseMode.isError ? (
                  <p role="alert">{formatApiError(chooseMode.error, "运行模式确认失败")}</p>
                ) : null}
              </aside>
            ) : null}
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入任务，例如：让导演、文案和剪辑师讨论一个短视频脚本方案。"
              required
            />
            <div className="composer-actions">
              <button
                type="button"
                className="composer-plus-button"
                aria-label={configOpen ? "收起本次运行配置" : "打开本次运行配置"}
                aria-pressed={configOpen}
                onClick={() => setConfigOpen((current) => !current)}
              >
                +
              </button>
              <span>
                {mode === "auto" ? "自动检测模式" : `手动模式：${displayMode(mode)}`}
                {agentIds.length > 0 ? ` · 角色 ${agentIds.length} 个` : " · 未固定角色"}
                {referenceConversationId.trim() ? " · 已引用会话" : ""}
              </span>
              <button type="submit" disabled={createRun.isPending || message.trim().length === 0}>
                {createRun.isPending ? "发送中..." : "发送"}
              </button>
            </div>
            {submitNotice ? <p role="status">{submitNotice}</p> : null}
            {createRun.isError ? <p role="alert">{formatApiError(createRun.error, "任务提交失败")}</p> : null}
          </form>
        </div>
      </div>

    </section>
  );
}
