import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api, formatApiError, type AttachmentUpload, type RunDetail, type Skill, type SubmittedRun } from "../api/client";

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
type SkillInstallCandidate = {
  fileName: string;
  skill: Skill;
  status: "scanned" | "enabled";
};
type ChatAttachmentDraft = {
  fileName: string;
  size: number;
  kind: "code_review" | "image" | "context";
  attachment?: AttachmentUpload;
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

function displayRoutingReason(reason: string) {
  const normalized = reason.trim();
  const labels: Record<string, string> = {
    "workflow selected explicitly": "按你选择的工作流执行",
    routing_requires_user_choice: "自动判断把握不足，需要确认模式",
    hermes_recommendation: "Hermes 根据历史经验推荐",
  };
  return labels[normalized] ?? normalized;
}

function displayAgentPool(selectedAgentIds: string | undefined, agentNames: Map<string, string>) {
  if (!selectedAgentIds) return null;
  const names = selectedAgentIds
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((id) => agentNames.get(id) ?? id);
  return names.length > 0 ? names.join("、") : null;
}

function displayEventTitle(event: RunDetail["events"][number], agentNames: Map<string, string>) {
  const actor = displayEventActor(event.actor, agentNames);
  const labels: Record<string, string> = {
    queued: "任务已入队",
    "run.queued": "任务已入队",
    "model.started": actor ? `${actor} 开始调用模型` : "开始调用模型",
    "runtime.started": "开始执行本次对话",
    "runtime.completed": "完成本次对话",
    "runtime.failed": "本次对话中断",
    "message.created": actor ? `${actor} 输出阶段消息` : "输出阶段消息",
    "artifact.created": actor ? `${actor} 生成了结果` : "生成了结果",
    "dispatch.started": "主 Agent 开始拆解并派单",
    "dispatch.completed": "主 Agent 完成派单汇总",
    "discussion.started": "多角色开始讨论",
    "discussion.completed": "多角色完成讨论",
    "decision.started": "主 Agent 开始裁决",
    "decision.completed": "主 Agent 完成裁决",
    "step.started": actor ? `${actor} 开始执行` : "开始执行一个步骤",
    "step.completed": actor ? `${actor} 完成执行` : "完成一个步骤",
    "step.failed": actor ? `${actor} 执行失败` : "一个步骤执行失败",
    "tool.started": event.tool_name ? `开始使用工具：${event.tool_name}` : "开始使用工具",
    "tool.completed": event.tool_name ? `工具执行完成：${event.tool_name}` : "工具执行完成",
    "tool.failed": event.tool_name ? `工具执行失败：${event.tool_name}` : "工具执行失败",
    "approval.requested": "等待你确认后继续",
    "approval.resolved": "确认已处理",
    "temporary_agent.proposed": "主 Agent 建议临时加入子 Agent",
  };
  return labels[event.kind] ?? "执行了一步操作";
}

function displayEventMessage(event: RunDetail["events"][number]) {
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : null;
  const messages: Record<string, string> = {
    queued: "任务已进入队列，等待 Worker 调度执行。",
    "run.queued": "任务已进入队列，等待 Worker 调度执行。",
    "model.started": "模型请求已开始。",
    "runtime.started": "运行时已启动，正在按模式执行。",
    "runtime.completed": "运行完成，已汇总结果。",
    "runtime.failed": readableMessage ?? "运行失败，请查看日志中心的模式运行错误。",
    "message.created": readableMessage ?? "运行过程中产生了一条可公开消息。",
    "artifact.created": "已生成一个可查看的结果或中间产物。",
    "dispatch.started": "主 Agent 正在拆解任务，并准备派给合适角色。",
    "dispatch.completed": "派单执行完成，主 Agent 正在汇总结论。",
    "discussion.started": "多个角色开始讨论方案、分歧和取舍。",
    "discussion.completed": readableMessage ?? "讨论完成，已形成阶段性结论。",
    "decision.started": "主 Agent 开始根据目标、证据和风险做裁决。",
    "decision.completed": readableMessage ?? "主 Agent 已完成裁决并整理最终结论。",
    "step.started": readableMessage ?? "一个执行步骤已开始。",
    "step.completed": readableMessage ?? "一个执行步骤已完成。",
    "step.failed": readableMessage ?? "一个执行步骤失败，已保留失败前的输出。",
    "tool.started": readableMessage ?? "工具调用已开始。",
    "tool.completed": readableMessage ?? "工具调用已完成。",
    "tool.failed": readableMessage ?? "工具调用失败，已记录错误上下文。",
    "approval.requested": "主 Agent 需要你确认后再继续。",
    "approval.resolved": "你的确认已处理，任务会继续推进。",
    "temporary_agent.proposed": "主 Agent 建议临时加入一个子 Agent。",
  };
  return messages[event.kind] ?? readableMessage ?? "系统记录了一步运行过程。";
}

function displayEventActor(actor: string | null | undefined, agentNames: Map<string, string>) {
  if (!actor) return null;
  return agentNames.get(actor) ?? actor;
}

function displayEventParticipants(participants: string[], agentNames: Map<string, string>) {
  const names = participants.map((id) => agentNames.get(id) ?? id).filter(Boolean);
  return names.length > 0 ? names.join("、") : null;
}

function formatEventPayloadValue(value: unknown): string {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function eventDetailRows(event: RunDetail["events"][number], agentNames: Map<string, string>) {
  const rows: Array<{ label: string; value: string }> = [];
  const actor = displayEventActor(event.actor, agentNames);
  const participants = displayEventParticipants(event.participants, agentNames);
  if (actor) rows.push({ label: "执行者", value: actor });
  if (participants) rows.push({ label: "参与者", value: participants });
  if (event.tool_name) rows.push({ label: "工具", value: event.tool_name });
  if (event.step_id) rows.push({ label: "步骤", value: event.step_id });
  if (event.action) rows.push({ label: "动作", value: event.action });
  if (event.decision) rows.push({ label: "决策", value: event.decision });
  Object.entries(event.payload).forEach(([key, value]) => {
    const formatted = formatEventPayloadValue(value);
    if (formatted) {
      rows.push({ label: `详情：${key}`, value: formatted });
    }
  });
  return rows;
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
  const textArtifacts = dedupeTextArtifacts(detail.artifacts);
  const replyArtifact = preferredReplyArtifact(textArtifacts);
  const failureReason = failureReasonFromEvents(detail.events);
  const artifactMessages = replyArtifact
    ? [
        {
          id: `artifact-${replyArtifact.id}`,
          role: "assistant",
          title: "回复",
          body:
            textArtifacts.length > 1
              ? `${replyArtifact.text?.trim() ?? ""}\n\n（另有 ${
                  textArtifacts.length - 1
                } 条角色产物，可点“查看运行详情”查看。）`
              : replyArtifact.text?.trim() ?? "",
        },
      ]
    : detail.artifacts
        .filter((artifact) => !artifact.text?.trim())
        .map((artifact) => ({
          id: `artifact-${artifact.id}`,
          role: "assistant",
          title: `附件：${artifact.title}`,
          body: artifact.kind,
        }));
  const failureMessages =
    detail.status === "failed"
      ? [
          {
            id: "failed",
            role: "assistant",
            title: artifactMessages.length > 0 ? "运行中断" : "运行失败",
            body:
              artifactMessages.length > 0
                ? `中断前输出已保留。错误原因：${failureReason ?? "后端没有记录具体失败原因，请打开运行详情或调试接口排查。"}`
                : `本次运行没有生成最终回复。错误原因：${
                    failureReason ?? "后端没有记录具体失败原因，请展开执行摘要或到日志中心查看。"
                  }`,
          },
        ]
      : [];
  return [
    {
      id: "request",
      role: "user",
      title: "你的任务",
      body: detail.request,
    },
    ...artifactMessages,
    ...failureMessages,
  ];
}

function failureReasonFromEvents(events: RunDetail["events"]) {
  const event = [...events]
    .sort((left, right) => right.sequence - left.sequence)
    .find((item) => ["runtime.failed", "step.failed", "tool.failed"].includes(item.kind) && item.message);
  return event?.message ?? null;
}

function dedupeTextArtifacts(artifacts: RunDetail["artifacts"]) {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    const text = artifact.text?.trim();
    if (!text || seen.has(text)) return false;
    seen.add(text);
    return true;
  });
}

function preferredReplyArtifact(artifacts: RunDetail["artifacts"]) {
  const preferredTitles = new Set(["main", "final_synthesizer", "decision_recorder", "domain_expert"]);
  return (
    [...artifacts].reverse().find((artifact) => preferredTitles.has(artifact.title)) ??
    artifacts.at(-1) ??
    null
  );
}

function runConversationId(detail: RunDetail | undefined) {
  return detail?.explicit_details.conversation_id?.trim() || null;
}

function conversationMessages(runs: RunDetail[]) {
  return runs.flatMap((run) =>
    detailMessages(run).map((message) => ({
      ...message,
      id: `${run.id}-${message.id}`,
      run,
    })),
  );
}

function runProcessSummary(detail: RunDetail, agentNames: Map<string, string>) {
  const agentPool = displayAgentPool(detail.explicit_details.selected_agent_ids, agentNames);
  const routing = [
    `运行模式：${displayMode(detail.mode)}`,
    detail.explicit_details.workflow_id ? `工作流：${detail.explicit_details.workflow_id}` : null,
    detail.explicit_details.workflow_adjustment_policy
      ? `工作流调整：${
          detail.explicit_details.workflow_adjustment_policy === "ask_before_apply"
            ? "允许提出，执行前核对"
            : "严格按预设"
        }`
      : null,
    agentPool ? `参与角色：${agentPool}` : null,
    detail.explicit_details.routing_reason
      ? `路由原因：${displayRoutingReason(detail.explicit_details.routing_reason)}`
      : null,
  ].filter(Boolean);
  return { routing, events: detail.events };
}

function RunProcessSummary({
  detail,
  open,
  onToggle,
  agentNames,
}: {
  detail: RunDetail;
  open: boolean;
  onToggle: () => void;
  agentNames: Map<string, string>;
}) {
  const summary = runProcessSummary(detail, agentNames);
  const eventCount = summary.events.length;
  const routingCount = summary.routing.length > 0 ? 1 : 0;
  const total = eventCount + routingCount;
  if (total === 0) return null;
  return (
    <section className="run-process-summary" aria-label="折叠的运行过程">
      <button type="button" className="run-process-toggle" aria-expanded={open} onClick={onToggle}>
        <span aria-hidden="true">‹/›</span>
        <strong>已运行 {total} 个动作</strong>
        <small>{open ? "点击收起" : "展开执行轨迹"}</small>
      </button>
      {open ? (
        <div className="run-process-detail">
          {summary.routing.length > 0 ? (
            <article>
              <span className="eyebrow">调度概况</span>
              {summary.routing.map((item) => (
                <p key={item}>{item}</p>
              ))}
            </article>
          ) : null}
          {summary.events.map((event) => {
            const detailRows = eventDetailRows(event, agentNames);
            return (
              <article key={event.sequence}>
                <span className="eyebrow">
                  动作 {event.sequence} · {displayEventTitle(event, agentNames)}
                </span>
                <p>{displayEventMessage(event)}</p>
                {detailRows.length > 0 ? (
                  <dl>
                    {detailRows.map((row) => (
                      <Fragment key={`${event.sequence}-${row.label}`}>
                        <dt>{row.label}</dt>
                        <dd>{row.value}</dd>
                      </Fragment>
                    ))}
                  </dl>
                ) : null}
                <small>{event.created_at}</small>
              </article>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function ModeEntryPanel({
  selectedMode,
  onSelect,
}: {
  selectedMode: RunMode;
  onSelect: (mode: RunMode) => void;
}) {
  const entryModes = [
    { value: "auto", label: "快速模式", description: "主 Agent 自动判断直接、派单、讨论或混合。" },
    { value: "direct", label: "直连模式", description: "一个回答者直接回复，适合短问答。" },
    { value: "dispatch", label: "派单模式", description: "按角色拆分任务，再由主 Agent 汇总。" },
    { value: "discuss", label: "讨论模式", description: "多角色先讨论分歧，再给结论。" },
    { value: "hybrid", label: "混合模式", description: "先讨论方案，再分工执行。" },
  ] as const;
  const selected = entryModes.find((item) => item.value === selectedMode) ?? entryModes[0];
  return (
    <article className="mode-entry-panel">
      <span className="mode-entry-logo" aria-hidden="true">
        ✦
      </span>
      <h3>选择模式开始对话</h3>
      <div className="mode-entry-tabs" role="list" aria-label="对话模式入口">
        {entryModes.map((item) => (
          <button
            key={item.value}
            type="button"
            aria-label={`进入${item.label}`}
            aria-pressed={selectedMode === item.value}
            className={selectedMode === item.value ? "mode-entry-active" : ""}
            onClick={() => onSelect(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <p>{selected.description}</p>
    </article>
  );
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
  const [selectedConversationIds, setSelectedConversationIds] = useState<string[]>([]);
  const [submitNotice, setSubmitNotice] = useState<string | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [processOpen, setProcessOpen] = useState(false);
  const [modeSelection, setModeSelection] = useState<ModeSelection | null>(null);
  const [skillInstallCandidate, setSkillInstallCandidate] = useState<SkillInstallCandidate | null>(null);
  const [attachmentDraft, setAttachmentDraft] = useState<ChatAttachmentDraft | null>(null);
  const [temporaryApproval, setTemporaryApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: NonNullable<SubmittedRun["temporary_agent_proposal"]>;
    approved: boolean;
  } | null>(null);
  const [temporaryFeedback, setTemporaryFeedback] = useState("");
  const userSelectedMode = useRef(false);
  const autoModeChoiceKey = useRef<string | null>(null);
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

  const selectedRunConversationId = runConversationId(selectedRun.data);
  const activeConversation = useQuery({
    queryKey: ["conversation", selectedRunConversationId],
    queryFn: () => api.conversation(selectedRunConversationId ?? ""),
    enabled: Boolean(selectedRunConversationId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.runs.some((run) => !TERMINAL_STATUSES.has(run.status)) ? 1000 : false;
    },
  });

  useEffect(() => {
    if (!settings.data) return;
    if (!userSelectedMode.current) setMode(settings.data.default_mode);
    setWorkflowId(settings.data.default_workflow_id ?? "");
    setAgentIds(settings.data.default_agent_ids);
  }, [settings.data]);

  useEffect(() => {
    if (!selectedWorkflow) return;
    if (selectedWorkflow.mode) {
      userSelectedMode.current = true;
      setMode(selectedWorkflow.mode);
    }
    setAgentIds(selectedWorkflow.agent_ids ?? []);
  }, [selectedWorkflow]);

  useEffect(() => {
    const selection = modeSelectionFromRunDetail(selectedRun.data);
    if (selection) {
      setModeSelection(selection);
    } else if (selectedRun.data && selectedRun.data.status !== "waiting_user_mode") {
      setModeSelection(null);
    }
    const selectedConversationId = runConversationId(selectedRun.data);
    if (selectedConversationId) {
      setConversationId(selectedConversationId);
    }
  }, [selectedRun.data]);

  useEffect(() => {
    setProcessOpen(false);
  }, [selectedRunId]);

  const createRun = useMutation({
    mutationFn: () =>
      api.createRun({
        message: message.trim(),
        mode,
        workflow_id: workflowId || null,
        allow_workflow_adjustment: mode !== "direct" && (settings.data?.allow_main_agent_override ?? false),
        agent_ids: mode === "direct" ? agentIds.slice(0, 1) : agentIds,
        conversation_id: conversationId,
        reference_conversation_id: referenceConversationId.trim() || null,
        attachment_ids: attachmentDraft?.attachment ? [attachmentDraft.attachment.id] : [],
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
      setAttachmentDraft(null);
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
      if (run.conversation_id) {
        await queryClient.invalidateQueries({ queryKey: ["conversation", run.conversation_id] });
      }
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

  useEffect(() => {
    if (!modeSelection || mode === "auto" || chooseMode.isPending) return;
    const key = `${modeSelection.runId}:${modeSelection.version}:${mode}`;
    if (autoModeChoiceKey.current === key) return;
    autoModeChoiceKey.current = key;
    setSubmitNotice(`已按你选择的“${displayMode(mode)}”继续执行，不再重复确认模式。`);
    chooseMode.mutate(mode as ManualRunMode);
  }, [modeSelection, mode, chooseMode]);

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

  const deleteRun = useMutation({
    mutationFn: (runId: string) => api.deleteRun(runId),
    onSuccess: async (result) => {
      if (selectedRunId === result.id) {
        setSelectedRunId(null);
      }
      setSelectedConversationIds((current) => current.filter((id) => id !== result.id));
      queryClient.removeQueries({ queryKey: ["run", result.id] });
      setSubmitNotice("已删除对话。");
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  const bulkDeleteRuns = useMutation({
    mutationFn: (ids: string[]) => api.bulkDeleteRuns(ids),
    onSuccess: async (result) => {
      const deletedIds = new Set(result.deleted.map((item) => item.id));
      if (selectedRunId && deletedIds.has(selectedRunId)) {
        setSelectedRunId(null);
      }
      for (const id of deletedIds) {
        queryClient.removeQueries({ queryKey: ["run", id] });
      }
      setSelectedConversationIds((current) => current.filter((id) => !deletedIds.has(id)));
      setSubmitNotice(
        result.failed.length > 0
          ? `Deleted ${result.deleted.length} conversations; ${result.failed.length} failed.`
          : `Deleted ${result.deleted.length} conversations.`,
      );
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  const uploadSkillArchive = useMutation({
    mutationFn: (file: File) => api.uploadSkillArchive(file),
    onSuccess: (skill, file) => {
      setAttachmentDraft(null);
      setSkillInstallCandidate({ fileName: file.name, skill, status: "scanned" });
      setSubmitNotice("Skill 包已完成安全扫描，请确认权限后再安装。");
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (error, file) => {
      setSkillInstallCandidate(null);
      if (file.name.toLowerCase().endsWith(".zip") && error instanceof ApiError && error.code === "invalid_skill_package") {
        setSubmitNotice(
          "这个 ZIP 不是有效 Skill 包，正在按代码审查附件上传。",
        );
        uploadAttachment.mutate(file);
      } else {
        setAttachmentDraft({
          fileName: file.name,
          size: file.size,
          kind: file.name.toLowerCase().endsWith(".zip") ? "code_review" : "context",
        });
        setSubmitNotice("Skill 扫描失败。请查看错误详情，确认是否为有效 Skill 包。");
      }
    },
  });

  const approveUploadedSkill = useMutation({
    mutationFn: () => {
      if (!skillInstallCandidate) throw new Error("skill install candidate is unavailable");
      return api.approveSkill(skillInstallCandidate.skill.id);
    },
    onSuccess: async (skill) => {
      setSkillInstallCandidate((current) => (current ? { ...current, skill, status: "enabled" } : current));
      setSubmitNotice("Skill 已安装并启用。后续 Agent 可以在权限边界内引用它。");
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });

  const uploadAttachment = useMutation({
    mutationFn: (file: File) => api.uploadAttachment(file),
    onSuccess: (attachment, file) => {
      const kind = attachment.kind === "image" ? "image" : attachment.kind === "code_archive" ? "code_review" : "context";
      setSkillInstallCandidate(null);
      setAttachmentDraft({ fileName: attachment.filename || file.name, size: attachment.size_bytes, kind, attachment });
      setSubmitNotice(
        kind === "code_review"
          ? "代码压缩包已上传。请在输入框说明审查目标，提交后主 Agent 会把附件 ID 带入任务。"
          : kind === "image"
            ? "图片已上传。提交任务后会作为附件引用进入运行上下文。"
            : "附件已上传。提交任务后会作为附件引用进入运行上下文。",
      );
    },
  });

  function handleAttachmentUpload(fileList: FileList | null) {
    const file = fileList?.item(0);
    if (!file) return;
    setSubmitNotice(null);
    setAttachmentDraft(null);
    setSkillInstallCandidate(null);
    if (file.name.toLowerCase().endsWith(".zip")) {
      uploadSkillArchive.mutate(file);
      return;
    }
    uploadAttachment.mutate(file);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitNotice(null);
    createRun.mutate();
  }

  function startNewConversation() {
    setSelectedRunId(null);
    setConversationId(newConversationId());
    setReferenceConversationId("");
    setMessage("");
    setTemporaryApproval(null);
    setModeSelection(null);
    setProcessOpen(false);
    setSubmitNotice("已新建空白对话。请选择模式后开始新的会话。");
  }

  function startHandoffConversation() {
    const sourceConversationId = runConversationId(selectedRun.data) ?? conversationId;
    if (!sourceConversationId) {
      setSubmitNotice("当前没有可 Handoff 的会话。");
      return;
    }
    setSelectedRunId(null);
    setReferenceConversationId(sourceConversationId);
    setConversationId(newConversationId());
    setMessage("");
    setTemporaryApproval(null);
    setModeSelection(null);
    setProcessOpen(false);
    setSubmitNotice(`已按原思路开启新对话：新对话会读取 ${sourceConversationId} 作为参考上下文。`);
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
  const savedAgents = agents.data ?? [];
  const savedWorkflows = workflows.data ?? [];
  const agentNameMap = new Map(savedAgents.map((agent) => [agent.id, agent.name]));
  const visibleRuns = activeConversation.data?.runs ?? (selectedRun.data ? [selectedRun.data] : []);
  const messages = conversationMessages(visibleRuns);
  const latestVisibleRun = visibleRuns.at(-1) ?? selectedRun.data;
  const directAnswerer = mode === "direct" && agentIds.length > 0 ? agentIds[0] : "main_agent";
  const deletableConversationIds = items
    .filter((run) => TERMINAL_STATUSES.has(run.status))
    .map((run) => run.id);
  const selectedDeletableConversationIds = selectedConversationIds.filter((id) =>
    deletableConversationIds.includes(id),
  );
  const allDeletableSelected =
    deletableConversationIds.length > 0 &&
    deletableConversationIds.every((id) => selectedConversationIds.includes(id));

  function deleteConversation(run: (typeof items)[number]) {
    if (!TERMINAL_STATUSES.has(run.status)) {
      setSubmitNotice("这条对话仍在运行或等待处理，请先取消后再删除。");
      return;
    }
    if (!window.confirm(`确认删除对话 ${run.id.slice(0, 8)}？删除后运行详情和产物记录也会移除。`)) {
      return;
    }
    deleteRun.mutate(run.id);
  }

  function toggleAllConversations() {
    setSelectedConversationIds((current) => {
      if (allDeletableSelected) return current.filter((id) => !deletableConversationIds.includes(id));
      return Array.from(new Set([...current, ...deletableConversationIds]));
    });
  }

  function toggleConversation(runId: string) {
    setSelectedConversationIds((current) => toggle(current, runId));
  }

  function chooseRunMode(nextMode: RunMode) {
    userSelectedMode.current = true;
    setMode(nextMode);
  }

  function deleteSelectedConversations() {
    if (selectedDeletableConversationIds.length === 0) {
      setSubmitNotice("Please select completed, failed, or cancelled conversations first.");
      return;
    }
    if (!window.confirm(`Delete ${selectedDeletableConversationIds.length} selected conversations?`)) {
      return;
    }
    bulkDeleteRuns.mutate(selectedDeletableConversationIds);
  }

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
          <button type="button" className="secondary-action conversation-new-button" onClick={startNewConversation}>
            新建对话
          </button>
          {items.length > 0 ? (
            <div className="bulk-action-bar conversation-bulk-actions">
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="Select all deletable conversations"
                  checked={allDeletableSelected}
                  disabled={deletableConversationIds.length === 0 || bulkDeleteRuns.isPending}
                  onChange={toggleAllConversations}
                />
                全选可删
              </label>
              <button
                type="button"
                className="secondary-action"
                disabled={selectedDeletableConversationIds.length === 0 || bulkDeleteRuns.isPending}
                onClick={deleteSelectedConversations}
              >
                {bulkDeleteRuns.isPending ? "删除中..." : "批量删除已选会话"}
              </button>
              <small>已选 {selectedDeletableConversationIds.length}</small>
            </div>
          ) : null}
          {items.length === 0 ? (
            <p className="field-help">还没有任务。可以从右侧输入框发起第一次对话。</p>
          ) : (
            items.map((run) => {
              const canDelete = TERMINAL_STATUSES.has(run.status);
              return (
                <div
                  key={run.id}
                  className={`conversation-row${selectedRunId === run.id ? " conversation-row-active" : ""}`}
                >
                  <input
                    type="checkbox"
                    className="conversation-select"
                    aria-label={`Select conversation ${run.id.slice(0, 8)}`}
                    checked={selectedConversationIds.includes(run.id)}
                    disabled={!canDelete || bulkDeleteRuns.isPending}
                    onChange={() => toggleConversation(run.id)}
                  />
                  <button
                    type="button"
                    className="conversation-item"
                    aria-label={`进入会话 ${run.id.slice(0, 8)}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <span>{displayMode(run.mode)}</span>
                    <strong>{run.id.slice(0, 8)}</strong>
                    <small>{run.status}</small>
                  </button>
                  <button
                    type="button"
                    className="conversation-delete-button"
                    aria-label={`Delete conversation ${run.id.slice(0, 8)}`}
                    title={canDelete ? "删除对话" : "运行中先取消"}
                    disabled={!canDelete || deleteRun.isPending}
                    onClick={() => deleteConversation(run)}
                  >
                    ×
                  </button>
                </div>
              );
            })
          )}
          {deleteRun.isError ? (
            <p className="form-error" role="alert">
              {formatApiError(deleteRun.error, "对话删除失败")}
            </p>
          ) : null}
        </nav>

        <div className={`chat-panel${configOpen ? " chat-panel-config-open" : ""}`}>
          {configOpen ? (
              <div className="composer-config-sheet" role="region" aria-label="本次运行更多设置">
          <details className="run-settings-panel" aria-label="本次运行设置" open>
            <summary aria-label="展开或收起本次运行设置">本次运行设置</summary>
            <div className="chat-config-strip" aria-label="本次对话运行设置">
            <label htmlFor="run-mode">
              模式
              <select id="run-mode" value={mode} onChange={(event) => chooseRunMode(event.target.value as RunMode)}>
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
                    全局临场策略：
                    {settings.data?.allow_main_agent_override
                      ? "全局临场策略已开启；主 Agent 可以提出改步骤、换角色或加交付物，但执行前必须向你核对。"
                      : "关闭；主 Agent 会按预设执行，只提示明显不匹配风险。"}
                  </p>
                  <p>
                    临时子 Agent：
                    {settings.data?.allow_temporary_agents
                      ? "允许在能力不足时提出申请，用户确认后才加入。"
                      : "关闭；不会临时扩充角色池。"}
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
            {activeConversation.isLoading ? <p>正在读取当前会话...</p> : null}
            {activeConversation.isError ? (
              <p role="alert">{formatApiError(activeConversation.error, "当前会话读取失败")}</p>
            ) : null}
            <p className="chat-conversation-status">当前会话：{conversationId}</p>
            {!selectedRunId ? (
              <ModeEntryPanel selectedMode={mode} onSelect={chooseRunMode} />
            ) : null}
            {messages.map((item, index) => (
              <Fragment key={item.id}>
                <article className={`chat-message ${item.role}`}>
                  <span className="eyebrow">{item.role === "user" ? "你" : "Agent Hub"}</span>
                  <h3>{item.title}</h3>
                  <p>{item.body}</p>
                </article>
                {index === 0 && item.run ? (
                  <RunProcessSummary
                    detail={item.run}
                    open={processOpen}
                    onToggle={() => setProcessOpen((current) => !current)}
                    agentNames={agentNameMap}
                  />
                ) : null}
              </Fragment>
            ))}
            {latestVisibleRun ? (
              <div className="chat-detail-action">
                <Link to={`/runs/${latestVisibleRun.id}`} className="secondary-action">
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
            {modeSelection && mode === "auto" ? (
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
            {skillInstallCandidate ? (
              <aside className="composer-attachment-card" role="status" aria-label="Skill 安装确认">
                <div>
                  <span className="eyebrow">
                    {skillInstallCandidate.status === "enabled" ? "Skill 已安装并启用" : "Skill 包已扫描，等待确认"}
                  </span>
                  <strong>{skillInstallCandidate.skill.name}</strong>
                  <small>{skillInstallCandidate.fileName}</small>
                </div>
                {skillInstallCandidate.skill.requested_permissions.length > 0 ? (
                  <ul>
                    {skillInstallCandidate.skill.requested_permissions.map((permission) => (
                      <li key={permission}>{permission}</li>
                    ))}
                  </ul>
                ) : (
                  <p>未请求额外权限。</p>
                )}
                {skillInstallCandidate.status === "scanned" ? (
                  <button type="button" disabled={approveUploadedSkill.isPending} onClick={() => approveUploadedSkill.mutate()}>
                    {approveUploadedSkill.isPending ? "安装中..." : "确认安装 Skill"}
                  </button>
                ) : null}
                {approveUploadedSkill.isError ? (
                  <p className="form-error" role="alert">
                    {formatApiError(approveUploadedSkill.error, "Skill 安装失败")}
                  </p>
                ) : null}
              </aside>
            ) : null}
            {attachmentDraft ? (
              <aside className="composer-attachment-card" role="status" aria-label="附件草稿">
                <div>
                  <span className="eyebrow">
                    {attachmentDraft.kind === "code_review"
                      ? "代码审查附件"
                      : attachmentDraft.kind === "image"
                        ? "图片附件"
                        : "上下文附件"}
                  </span>
                  <strong>{attachmentDraft.fileName}</strong>
                  <small>{Math.max(1, Math.ceil(attachmentDraft.size / 1024))} KB</small>
                </div>
                <p>
                  {attachmentDraft.kind === "code_review"
                    ? "这个 ZIP 不是 Skill 包。请在输入框说明审查目标；后续会接入后端附件存储后让审查 Agent 读取压缩包内容。"
                    : attachmentDraft.kind === "image"
                      ? "图片已选中。当前先记录附件，启用多模态链路后可交给视觉模型识别。"
                      : "附件已选中。当前先记录附件名称，完整内容读取会走后端附件存储。"}
                </p>
              </aside>
            ) : null}
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入消息，继续当前对话；也可以输入任务，例如：让导演、文案和剪辑师讨论一个短视频脚本方案。"
              required
            />
            <div className="composer-actions">
              <label className="composer-upload-button">
                <span>附件</span>
                <input
                  aria-label="上传文件或 Skill ZIP"
                  type="file"
                  accept=".zip,.txt,.md,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,image/*"
                  disabled={uploadSkillArchive.isPending || uploadAttachment.isPending}
                  onChange={(event) => handleAttachmentUpload(event.currentTarget.files)}
                />
              </label>
              <button
                type="button"
                className="composer-handoff-button"
                disabled={!selectedRun.data}
                onClick={startHandoffConversation}
              >
                按照原思路开启新对话
              </button>
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
            {uploadSkillArchive.isPending ? <p role="status">正在扫描 Skill 包...</p> : null}
            {uploadAttachment.isPending ? <p role="status">正在上传附件...</p> : null}
            {uploadSkillArchive.isError ? (
              <p className="field-help" role="status">
                {formatApiError(uploadSkillArchive.error, "Skill 扫描失败")}
              </p>
            ) : null}
            {uploadAttachment.isError ? (
              <p className="form-error" role="alert">
                {formatApiError(uploadAttachment.error, "附件上传失败")}
              </p>
            ) : null}
            {createRun.isError ? <p role="alert">{formatApiError(createRun.error, "任务提交失败")}</p> : null}
          </form>
        </div>
      </div>

    </section>
  );
}
