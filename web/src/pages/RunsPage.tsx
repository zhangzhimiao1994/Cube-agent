import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, formatApiError, type RunDetail } from "../api/client";

const RUN_MODES = [
  { value: "auto", label: "自动检测", description: "主 Agent 会判断应使用直接、派单、讨论或混合模式；不确定时应询问用户。" },
  { value: "direct", label: "直接执行", description: "适合简单问答或单角色、单步骤任务。" },
  { value: "dispatch", label: "派单式", description: "适合拆成多个专业角色执行的任务；派给谁由工作流或本次选择决定。" },
  { value: "discuss", label: "讨论式", description: "适合多角色观点冲突、方案评审或需要裁决的任务。" },
  { value: "hybrid", label: "混合式", description: "先讨论定方案，再派单执行，最后审查收口。" },
] as const;

type RunMode = (typeof RUN_MODES)[number]["value"];

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

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
      return data && !TERMINAL_STATUSES.has(data.status) ? 2000 : false;
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

  const createRun = useMutation({
    mutationFn: () =>
      api.createRun({
        message: message.trim(),
        mode,
        workflow_id: workflowId || null,
        agent_ids: agentIds,
        conversation_id: conversationId,
        reference_conversation_id: referenceConversationId.trim() || null,
      }),
    onSuccess: async (run) => {
      setSelectedRunId(run.id);
      if (run.conversation_id) setConversationId(run.conversation_id);
      setSubmitNotice(explainActualMode(run));
      setMessage("");
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

        <div className="chat-panel">
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
                <p>
                  当前工作流：{selectedWorkflow.name}
                  {selectedWorkflow.task_type ? `；适用场景：${selectedWorkflow.task_type}` : ""}
                </p>
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

          <details className="inline-guide">
            <summary>选择本次参与角色</summary>
            <p className="field-help">
              同一个模式可以派给不同对象。选择工作流会自动带出默认角色；你也可以为本次任务临时增删。
            </p>
            <fieldset>
              <legend>角色</legend>
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
          </details>

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
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入任务，例如：让导演、文案和剪辑师讨论一个短视频脚本方案。"
              required
            />
            <div className="composer-actions">
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
