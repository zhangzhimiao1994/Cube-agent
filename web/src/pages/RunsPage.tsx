import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api, formatApiError, type AttachmentUpload, type ModelDeployment, type RunDetail, type Skill, type SubmittedRun } from "../api/client";

const RUN_MODES = [
  { value: "auto", label: "自动", description: "主 Agent 判断应使用直连、派单、讨论或混合；不确定时向你确认。" },
  { value: "direct", label: "直连", description: "由你指定一个模型/API回答，主 Agent 负责控场、提示词和记录。" },
  { value: "dispatch", label: "派单", description: "适合拆成多个专业角色执行；派给谁由工作流或本次选择决定。" },
  { value: "discuss", label: "讨论", description: "适合多角色观点冲突、方案评审或需要裁决的任务。" },
  { value: "hybrid", label: "混合", description: "先讨论定方案，再派单执行，最后审查收口。" },
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
  kind: "archive" | "image" | "context";
  attachment?: AttachmentUpload;
};
type TemporaryAgentProposal = NonNullable<SubmittedRun["temporary_agent_proposal"]>;
type RunSubmissionOverride = {
  message?: string;
  directModel?: string;
  mode?: RunMode;
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MANUAL_RUN_MODES = RUN_MODES.filter((item) => item.value !== "auto");
const ARCHIVE_EXTENSIONS = [
  ".zip",
  ".rar",
  ".7z",
  ".tar",
  ".tar.gz",
  ".tgz",
  ".tar.bz2",
  ".tbz2",
  ".tar.xz",
  ".txz",
  ".tar.zst",
  ".gz",
  ".bz2",
  ".xz",
  ".zst",
  ".cab",
  ".iso",
  ".jar",
  ".war",
  ".ear",
  ".apk",
  ".ipa",
];
const ATTACHMENT_ACCEPT = [
  ...ARCHIVE_EXTENSIONS,
  ".txt",
  ".md",
  ".pdf",
  ".doc",
  ".docx",
  ".ppt",
  ".pptx",
  ".xls",
  ".xlsx",
  "image/*",
].join(",");

function isArchiveFileName(fileName: string) {
  const lower = fileName.toLowerCase();
  return ARCHIVE_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

function mayBeSkillArchive(fileName: string) {
  const lower = fileName.toLowerCase();
  return [".zip", ".tar.gz", ".tgz"].some((extension) => lower.endsWith(extension));
}

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
    main_agent_auto_resolved: "主 Agent 已根据任务现场自动裁决",
    router_unavailable: "主 Agent 暂时无法可靠判断，需要你确认运行方式",
    main_agent_local_fallback: "旧版本回退记录：需要重新提交后由主 Agent 判断",
    hermes_recommendation: "Hermes 根据历史经验推荐",
  };
  return labels[normalized] ?? normalized;
}

function parseChoiceText(
  text: string,
  options: Array<{ value: string; label: string; aliases?: string[] }>,
) {
  const raw = text.trim();
  if (!raw || options.length === 0) return null;
  const numbered = raw.match(/^([1-9])(?:[\s.、:：-]+)?([\s\S]*)$/);
  if (numbered) {
    const index = Number(numbered[1]) - 1;
    if (index >= 0 && index < options.length) {
      return { option: options[index], note: (numbered[2] ?? "").trim() };
    }
  }
  const lower = raw.toLowerCase();
  const candidates = options.flatMap((option) =>
    [option.label, option.value, ...(option.aliases ?? [])]
      .filter(Boolean)
      .map((alias) => ({ option, alias, lowerAlias: alias.toLowerCase() })),
  );
  const matched = candidates
    .sort((left, right) => right.lowerAlias.length - left.lowerAlias.length)
    .find((candidate) => lower === candidate.lowerAlias || lower.includes(candidate.lowerAlias));
  if (!matched) return null;
  const index = lower.indexOf(matched.lowerAlias);
  const note =
    index < 0
      ? raw
      : `${raw.slice(0, index)} ${raw.slice(index + matched.alias.length)}`
          .replace(/^[\s.、:：-]+|[\s.、:：-]+$/g, "")
          .trim();
  return { option: matched.option, note };
}

function parseLeadingKeywordChoiceText(
  text: string,
  options: Array<{ value: string; label: string; aliases?: string[] }>,
) {
  const raw = text.trim();
  if (!raw || options.length === 0) return null;
  const candidates = options
    .flatMap((option) =>
      [option.label, option.value, ...(option.aliases ?? [])]
        .filter(Boolean)
        .map((alias) => ({ option, alias, lowerAlias: alias.toLowerCase() })),
    )
    .sort((left, right) => right.lowerAlias.length - left.lowerAlias.length);
  const lower = raw.toLowerCase();
  const matched = candidates.find(
    (candidate) =>
      lower === candidate.lowerAlias ||
      lower.startsWith(`${candidate.lowerAlias} `) ||
      lower.startsWith(`${candidate.lowerAlias}：`) ||
      lower.startsWith(`${candidate.lowerAlias}:`) ||
      lower.startsWith(`${candidate.lowerAlias}，`) ||
      lower.startsWith(`${candidate.lowerAlias},`) ||
      lower.startsWith(`${candidate.lowerAlias}。`) ||
      lower.startsWith(`${candidate.lowerAlias}.`) ||
      lower.startsWith(`${candidate.lowerAlias}、`) ||
      lower.startsWith(`${candidate.lowerAlias}-`),
  );
  if (!matched) return null;
  const note = raw
    .slice(matched.alias.length)
    .replace(/^[\s.、:：,，-]+/, "")
    .trim();
  return { option: matched.option, note };
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
    "artifact.created": actor ? `${actor} 产出阶段内容` : "产出阶段内容",
    "dispatch.started": "主 Agent 开始拆解并派单",
    "dispatch.completed": "主 Agent 完成派单汇总",
    "discussion.started": "多角色开始讨论",
    "discussion.completed": "多角色完成讨论",
    "decision.started": "主 Agent 开始裁决",
    "decision.completed": "主 Agent 完成裁决",
    "step.started": actor ? `${actor} 开始执行` : "开始执行一个步骤",
    "step.completed": actor ? `${actor} 完成执行` : "完成一个步骤",
    "step.failed": actor ? `${actor} 执行失败` : "一个步骤执行失败",
    "step.retrying": actor ? `${actor} 重试执行` : "重试一个步骤",
    "review.completed": actor ? `${actor} 完成审查` : "完成审查",
    "tool.started": event.tool_name ? `开始使用工具：${event.tool_name}` : "开始使用工具",
    "tool.completed": event.tool_name ? `工具执行完成：${event.tool_name}` : "工具执行完成",
    "tool.failed": event.tool_name ? `工具执行失败：${event.tool_name}` : "工具执行失败",
    "approval.requested": "等待你确认后继续",
    "approval.resolved": "确认已处理",
    "temporary_agent.proposed": "主 Agent 建议临时加入子 Agent",
    "cost.recorded": "记录成本",
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
    "step.retrying": readableMessage ?? "步骤执行失败后正在重试。",
    "review.completed": readableMessage ?? "审查完成，已记录风险、证据或结论。",
    "tool.started": readableMessage ?? "工具调用已开始。",
    "tool.completed": readableMessage ?? "工具调用已完成。",
    "tool.failed": readableMessage ?? "工具调用失败，已记录错误上下文。",
    "approval.requested": "主 Agent 需要你确认后再继续。",
    "approval.resolved": "你的确认已处理，任务会继续推进。",
    "temporary_agent.proposed": "主 Agent 建议临时加入一个子 Agent。",
    "cost.recorded": readableMessage ?? "已记录本轮模型调用成本。",
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

type RunEvent = RunDetail["events"][number];
type RunArtifact = RunDetail["artifacts"][number];

function conciseProcessText(value: string, fallback: string) {
  const normalized = value
    .replace(/```[\s\S]*?```/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return fallback;
  const sentence = normalized.split(/(?<=[。！？.!?])\s+/)[0]?.trim() || normalized;
  return sentence.length > 42 ? `${sentence.slice(0, 42)}...` : sentence;
}

function isNoiseEvent(event: RunDetail["events"][number]) {
  return new Set([
    "queued",
    "run.queued",
    "runtime.started",
    "runtime.completed",
    "checkpoint.saved",
    "cost.recorded",
  ]).has(event.kind);
}

function hasUsefulPayload(event: RunDetail["events"][number]) {
  return Object.values(event.payload).some((value) => {
    if (value === null || typeof value === "undefined") return false;
    if (typeof value === "string") return value.trim().length > 0;
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === "object") return Object.keys(value).length > 0;
    return true;
  });
}

function isActionEvent(event: RunDetail["events"][number]) {
  if (isNoiseEvent(event)) return false;
  if (event.kind === "artifact.created") {
    return true;
  }
  if (["step.started", "step.completed"].includes(event.kind)) {
    return Boolean(event.actor || event.action || event.tool_name || event.decision || hasUsefulPayload(event));
  }
  return true;
}

function eventPayloadLabel(key: string) {
  const labels: Record<string, string> = {
    instruction: "下发指令",
    instructions: "下发指令",
    task: "下发指令",
    prompt: "提示词/指令",
    input: "输入内容",
    summary: "执行摘要",
    result: "得到结果",
    output: "输出内容",
    conclusion: "讨论结论",
    final_decision: "最终裁决",
    main_agent_judgement: "主 Agent 判断",
    main_agent_judgment: "主 Agent 判断",
    director_opinion: "导演意见",
    copywriter_opinion: "文案意见",
    editor_opinion: "剪辑师意见",
    researcher_opinion: "研究员意见",
    engineer_opinion: "工程师意见",
    critic_opinion: "审查员意见",
    model: "调用模型",
    logical_model: "逻辑模型",
    upstream_model: "上游模型",
    model_used: "调用模型",
    model_provider: "模型服务商",
    model_deployment: "模型部署",
    deployment: "模型部署",
    provider: "服务商",
    role: "角色",
    agent: "Agent",
  };
  if (labels[key]) return labels[key];
  if (key.endsWith("_opinion")) {
    return `${key.replace(/_opinion$/, "").replace(/_/g, " ")} 意见`;
  }
  return `详情：${key}`;
}

function orderedEventPayloadEntries(payload: Record<string, unknown>) {
  const priority = [
    "summary",
    "result",
    "output",
    "conclusion",
    "director_opinion",
    "copywriter_opinion",
    "editor_opinion",
    "researcher_opinion",
    "engineer_opinion",
    "critic_opinion",
    "main_agent_judgement",
    "main_agent_judgment",
    "final_decision",
  ];
  return Object.entries(payload).sort(([left], [right]) => {
    const leftIndex = priority.indexOf(left);
    const rightIndex = priority.indexOf(right);
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
}

function eventDetailRows(event: RunDetail["events"][number], agentNames: Map<string, string>) {
  const rows: Array<{ label: string; value: string }> = [];
  const actor = displayEventActor(event.actor, agentNames);
  const participants = displayEventParticipants(event.participants, agentNames);
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  if (actor) rows.push({ label: "执行者", value: actor });
  if (participants) rows.push({ label: "参与者", value: participants });
  if (event.tool_name) rows.push({ label: "工具", value: event.tool_name });
  if (event.step_id) rows.push({ label: "步骤", value: event.step_id });
  if (event.action) rows.push({ label: "动作", value: event.action });
  if (event.decision) rows.push({ label: "决策", value: event.decision });
  if (readableMessage) rows.push({ label: "事件内容", value: readableMessage });
  orderedEventPayloadEntries(event.payload).forEach(([key, value]) => {
    const formatted = formatEventPayloadValue(value);
    if (formatted) {
      rows.push({ label: eventPayloadLabel(key), value: formatted });
    }
  });
  return rows;
}

type ProcessDetailTarget = {
  id: string;
  title: string;
  message: string;
  rows: Array<{ label: string; value: string }>;
  createdAt: string | null;
};

function toggle(list: string[], value: string) {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function explainActualMode(run: { status: string; mode: string | null }) {
  if (run.status === "waiting_user_mode") {
    return "自动检测没有足够把握，这轮回复需要你确认运行模式。";
  }
  if (!run.mode) return "这轮回复尚未确定运行模式。";
  return `这轮回复使用：${displayMode(run.mode)}。你可以继续在当前会话里追问。`;
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

function temporaryApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.decision_token || !run.temporary_agent_proposal) {
    return null;
  }
  const parsedVersion = Number(run.explicit_details.version ?? "0");
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: Number.isInteger(parsedVersion) && parsedVersion > 0 ? parsedVersion : 0,
    proposal: run.temporary_agent_proposal,
    approved: false,
  };
}

function temporaryAgentApprovalBody(proposal: TemporaryAgentProposal) {
  const skills =
    proposal.suggested_skills.length > 0 ? `\n建议 Skill：${proposal.suggested_skills.join("、")}` : "";
  return [
    "主 Agent 判断当前角色池缺少一个临时子 Agent。主 Agent 已生成角色和提示词；模型/API 不需要你选，主 Agent 会按角色能力、任务要求和模型并发情况自动选择模型/API。",
    `拟加入：${proposal.name}（${proposal.id}）`,
    `缺少能力：${proposal.missing_capability}`,
    `加入原因：${proposal.reason}`,
    `角色边界：${proposal.prompt}${skills}`,
    "请直接回复：\n1 同意临时加入\n2 不加入，按现有角色继续\n3 你的修改意见\n4 保存为永久 Agent（需先同意并运行过）",
  ].join("\n\n");
}

function detailMessages(detail: RunDetail | undefined) {
  if (!detail) return [];
  const textArtifacts = dedupeTextArtifacts(detail.artifacts);
  const replyArtifact = preferredReplyArtifact(textArtifacts);
  const internalNotice = internalArtifactNotice(detail);
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
      title: "你",
      body: detail.request,
    },
    ...(detail.status === "waiting_approval" && detail.temporary_agent_proposal
      ? [
          {
            id: "temporary-agent-approval",
            role: "assistant",
            title: detail.temporary_agent_proposal.name,
            body: temporaryAgentApprovalBody(detail.temporary_agent_proposal),
          },
        ]
      : []),
    ...(internalNotice ? [internalNotice] : []),
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
  const preferredTitles = new Set(["main", "final_synthesizer", "domain_expert", "copywriter"]);
  const internalTitles = new Set(["decision_recorder", "quality_reviewer", "reviewer"]);
  return (
    [...artifacts].reverse().find((artifact) => preferredTitles.has(artifact.title)) ??
    [...artifacts].reverse().find((artifact) => !internalTitles.has(artifact.title)) ??
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

function sameRunSnapshot(left: RunDetail, right: RunDetail) {
  return (
    left.id === right.id &&
    left.status === right.status &&
    left.mode === right.mode &&
    left.request === right.request &&
    left.events.length === right.events.length &&
    left.artifacts.length === right.artifacts.length
  );
}

function mergeConversationRuns(previous: RunDetail[] | undefined, incoming: RunDetail[]) {
  if (!previous || previous.length === 0) return incoming;
  if (incoming.length === 0) return previous;
  const incomingById = new Map(incoming.map((run) => [run.id, run]));
  const previousIds = new Set(previous.map((run) => run.id));
  const merged = previous.map((run) => incomingById.get(run.id) ?? run);
  for (const run of incoming) {
    if (!previousIds.has(run.id)) merged.push(run);
  }
  if (
    merged.length === previous.length &&
    merged.every((run, index) => sameRunSnapshot(run, previous[index]))
  ) {
    return previous;
  }
  return merged;
}

function internalArtifactNotice(detail: RunDetail) {
  const textArtifacts = dedupeTextArtifacts(detail.artifacts);
  if (textArtifacts.length === 0) return null;
  if (preferredReplyArtifact(textArtifacts)) return null;
  return {
    id: "internal-artifacts",
    role: "assistant",
    title: "回复待生成",
    body: "这轮只生成了内部审查或裁决内容，没有生成可直接交付给你的正式回复。请点运行过程查看原因，或继续补充要求让主 Agent 重新生成。",
  };
}

function processRoutingRows(
  detail: RunDetail,
  agentNames: Map<string, string>,
  mainAgentModelName?: string,
) {
  const agentPool = displayAgentPool(detail.explicit_details.selected_agent_ids, agentNames);
  return [
    { label: "运行模式", value: displayMode(detail.mode) },
    mainAgentModelName && mainAgentModelName !== "未配置" ? { label: "主 Agent 模型", value: mainAgentModelName } : null,
    detail.explicit_details.direct_model ? { label: "直连模型", value: detail.explicit_details.direct_model } : null,
    detail.explicit_details.workflow_id ? { label: "工作流", value: detail.explicit_details.workflow_id } : null,
    detail.explicit_details.workflow_adjustment_policy
      ? {
          label: "工作流调整",
          value:
            detail.explicit_details.workflow_adjustment_policy === "ask_before_apply"
              ? "允许提出，执行前核对"
              : "严格按预设",
        }
      : null,
    agentPool ? { label: "参与角色", value: agentPool } : null,
    detail.explicit_details.routing_reason
      ? { label: "路由原因", value: displayRoutingReason(detail.explicit_details.routing_reason) }
      : null,
  ].filter((item): item is { label: string; value: string } => Boolean(item));
}

function eventArtifactText(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  return artifact?.text?.trim() || "";
}

function eventArtifactRows(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  const rows: Array<{ label: string; value: string }> = [];
  if (!artifact) return rows;
  if (artifact.title) rows.push({ label: "产出者", value: artifact.title });
  if (artifact.kind) rows.push({ label: "产物类型", value: artifact.kind });
  const text = eventArtifactText(artifact);
  if (text) rows.push({ label: "输出内容", value: text });
  return rows;
}

function fallbackArtifactForEvent(
  event: RunEvent,
  artifacts: RunArtifact[],
  consumedArtifactIds: Set<string>,
) {
  if (event.artifact) return event.artifact;
  const explicitArtifactId =
    formatEventPayloadValue(event.payload.artifact_id) ||
    formatEventPayloadValue(event.payload.artifactId) ||
    formatEventPayloadValue(event.payload.id);
  if (explicitArtifactId) {
    const matched = artifacts.find((artifact) => artifact.id === explicitArtifactId);
    if (matched) {
      consumedArtifactIds.add(matched.id);
      return matched;
    }
  }
  if (event.kind !== "artifact.created" && event.kind !== "message.created") return null;
  const byActor = event.actor
    ? artifacts.find((artifact) => artifact.title === event.actor && !consumedArtifactIds.has(artifact.id))
    : null;
  const byOrder = artifacts.find((artifact) => !consumedArtifactIds.has(artifact.id));
  const matched = byActor ?? byOrder ?? null;
  if (matched) consumedArtifactIds.add(matched.id);
  return matched;
}

function eventInstructionSignal(event: RunEvent) {
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  return (
    formatEventPayloadValue(event.payload.instruction) ||
    formatEventPayloadValue(event.payload.instructions) ||
    formatEventPayloadValue(event.payload.task) ||
    formatEventPayloadValue(event.payload.prompt) ||
    readableMessage
  );
}

function eventOutputSignal(event: RunEvent, artifact?: RunArtifact | NonNullable<RunEvent["artifact"]> | null) {
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  return (
    formatEventPayloadValue(event.payload.result) ||
    formatEventPayloadValue(event.payload.output) ||
    formatEventPayloadValue(event.payload.summary) ||
    eventArtifactText(artifact) ||
    readableMessage
  );
}

function eventDecisionSignal(event: RunEvent) {
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  return (
    formatEventPayloadValue(event.payload.final_decision) ||
    formatEventPayloadValue(event.payload.main_agent_judgement) ||
    formatEventPayloadValue(event.payload.main_agent_judgment) ||
    formatEventPayloadValue(event.payload.decision) ||
    event.decision ||
    readableMessage
  );
}

function humanizeEventIdentifier(value: string) {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function eventOpinionEntries(event: RunEvent, agentNames: Map<string, string>) {
  return Object.entries(event.payload)
    .filter(([key, value]) => key.endsWith("_opinion") && Boolean(formatEventPayloadValue(value)))
    .map(([key, value]) => {
      const actorId = key.replace(/_opinion$/, "");
      return {
        actor: agentNames.get(actorId) ?? humanizeEventIdentifier(actorId),
        label: eventPayloadLabel(key),
        value: formatEventPayloadValue(value),
      };
    });
}

function eventSummaryText(
  event: RunDetail["events"][number],
  agentNames: Map<string, string>,
  artifact?: RunArtifact | NonNullable<RunEvent["artifact"]> | null,
) {
  const actor = displayEventActor(event.actor, agentNames);
  const participants = displayEventParticipants(event.participants, agentNames);
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  const instructionSignal = eventInstructionSignal(event);
  const outputSignal = eventOutputSignal(event, artifact);
  const discussionSignal =
    formatEventPayloadValue(event.payload.conclusion) ||
    formatEventPayloadValue(event.payload.result) ||
    formatEventPayloadValue(event.payload.discussion) ||
    formatEventPayloadValue(event.payload.opinions) ||
    formatEventPayloadValue(event.payload.summary) ||
    readableMessage;
  const decisionSignal = eventDecisionSignal(event);
  const modelSignal =
    formatEventPayloadValue(event.payload.model) ||
    formatEventPayloadValue(event.payload.logical_model) ||
    formatEventPayloadValue(event.payload.model_used) ||
    formatEventPayloadValue(event.payload.upstream_model);
  const subject =
    event.kind === "discussion.completed"
      ? participants || "多角色"
      : event.kind === "dispatch.started" || event.kind === "dispatch.completed" || event.kind.startsWith("decision.")
        ? "主 Agent"
        : actor || (event.tool_name ? "工具" : "系统");

  if (event.kind === "model.started") {
    return `${subject} 调用模型${modelSignal ? `：${conciseProcessText(modelSignal, "模型")}` : ""}`;
  }
  if (event.kind === "step.started") {
    return `${subject} 接收任务：${conciseProcessText(instructionSignal, "开始执行")}`;
  }
  if (["step.completed", "artifact.created", "message.created", "review.completed"].includes(event.kind)) {
    return `${subject} 输出：${conciseProcessText(outputSignal || readableMessage, "完成阶段输出")}`;
  }
  if (event.kind === "discussion.started") {
    return `${participants || "多角色"} 开始讨论`;
  }
  if (event.kind === "discussion.completed") {
    return `讨论结论：${conciseProcessText(discussionSignal, "完成讨论")}`;
  }
  if (event.kind === "decision.started") {
    return `主 Agent 开始决策${instructionSignal ? `：${conciseProcessText(instructionSignal, "开始裁决")}` : ""}`;
  }
  if (event.kind === "decision.completed") {
    return `主 Agent 决策：${conciseProcessText(decisionSignal, "完成裁决")}`;
  }
  if (event.kind === "dispatch.started") {
    const assignees = participants ? `给${participants}` : "";
    return `主 Agent 派单${assignees}：${conciseProcessText(instructionSignal, "拆解任务并安排角色")}`;
  }
  if (event.kind === "dispatch.completed") {
    return `派单汇总：${conciseProcessText(outputSignal || discussionSignal, "完成派单汇总")}`;
  }
  if (event.kind === "tool.started") {
    return `${subject} 使用工具：${event.tool_name ?? "工具"}`;
  }
  if (event.kind === "tool.completed") {
    return `${subject} 工具结果：${conciseProcessText(outputSignal || readableMessage, event.tool_name ?? "工具完成")}`;
  }
  if (event.kind === "tool.failed" || event.kind === "step.failed" || event.kind === "runtime.failed") {
    return `${subject} 失败：${conciseProcessText(readableMessage || outputSignal, "执行失败")}`;
  }
  if (event.kind === "approval.requested") {
    return `等待确认：${conciseProcessText(instructionSignal, "需要你确认后继续")}`;
  }
  if (event.kind === "temporary_agent.proposed") {
    return `主 Agent 建议临时加入子 Agent：${conciseProcessText(instructionSignal, "补齐缺失能力")}`;
  }
  return `${subject} 执行：${conciseProcessText(readableMessage || outputSignal || instructionSignal, "记录了一步过程")}`;
}

function modelRowsForEvent(
  event: RunDetail["events"][number],
  events: RunDetail["events"],
  agentNames: Map<string, string>,
) {
  const rows: Array<{ label: string; value: string }> = [];
  const eventModel = formatEventPayloadValue(event.payload.model || event.payload.logical_model);
  if (eventModel) rows.push({ label: "调用模型", value: eventModel });
  const provider = formatEventPayloadValue(event.payload.provider);
  const deployment = formatEventPayloadValue(event.payload.deployment);
  if (provider) rows.push({ label: "模型服务商", value: provider });
  if (deployment) rows.push({ label: "模型部署", value: deployment });
  if (!eventModel && event.actor) {
    const modelEvent = [...events]
      .filter((candidate) => candidate.kind === "model.started" && candidate.actor === event.actor && candidate.sequence <= event.sequence)
      .sort((left, right) => right.sequence - left.sequence)
      .at(0);
    const model = modelEvent ? formatEventPayloadValue(modelEvent.payload.model || modelEvent.payload.logical_model) : "";
    if (model) rows.push({ label: "调用模型", value: model });
  }
  const actor = displayEventActor(event.actor, agentNames);
  if (actor && rows.length > 0) rows.unshift({ label: "模型使用者", value: actor });
  return rows;
}

function processItemsForEvent(
  detail: RunDetail,
  event: RunEvent,
  index: number,
  agentNames: Map<string, string>,
  artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null,
): ProcessDetailTarget[] {
  const baseRows = [
    ...modelRowsForEvent(event, detail.events, agentNames),
    ...eventDetailRows(event, agentNames),
    ...eventArtifactRows(artifact),
  ];
  const baseItem: ProcessDetailTarget = {
    id: `${detail.id}-event-${event.sequence}-${index}`,
    title: displayEventTitle(event, agentNames),
    message: eventSummaryText(event, agentNames, artifact),
    rows: baseRows,
    createdAt: event.created_at,
  };
  if (event.kind !== "discussion.completed") return [baseItem];

  const opinionItems = eventOpinionEntries(event, agentNames).map((opinion, opinionIndex) => ({
    id: `${detail.id}-event-${event.sequence}-${index}-opinion-${opinionIndex}`,
    title: `${opinion.actor} 给出讨论意见`,
    message: `${opinion.actor} 意见：${conciseProcessText(opinion.value, "给出意见")}`,
    rows: [
      ...modelRowsForEvent(event, detail.events, agentNames),
      { label: "发言角色", value: opinion.actor },
      { label: opinion.label, value: opinion.value },
    ],
    createdAt: event.created_at,
  }));

  const judgement = eventDecisionSignal(event);
  if (!judgement) return [baseItem, ...opinionItems];
  return [
    baseItem,
    ...opinionItems,
    {
      id: `${detail.id}-event-${event.sequence}-${index}-decision`,
      title: "主 Agent 完成裁决",
      message: `主 Agent 裁决：${conciseProcessText(judgement, "完成裁决")}`,
      rows: baseRows,
      createdAt: event.created_at,
    },
  ];
}

function runProcessItems(
  detail: RunDetail,
  agentNames: Map<string, string>,
  mainAgentModelName?: string,
): ProcessDetailTarget[] {
  const routingRows = processRoutingRows(detail, agentNames, mainAgentModelName);
  const routingAgentPool = displayAgentPool(detail.explicit_details.selected_agent_ids, agentNames);
  const routingItem =
    routingRows.length > 0
      ? [
          {
            id: `${detail.id}-routing`,
            title: "主 Agent 调度判断",
            message: `主 Agent 选择${displayMode(detail.mode)}${routingAgentPool ? `：${routingAgentPool}` : ""}`,
            rows: routingRows,
            createdAt: null,
          },
        ]
      : [];
  const consumedArtifactIds = new Set<string>();
  const eventItems = detail.events
    .filter(isActionEvent)
    .flatMap((event, index) => {
      const artifact = fallbackArtifactForEvent(event, detail.artifacts, consumedArtifactIds);
      if (event.kind === "artifact.created" && !artifact && !hasUsefulPayload(event)) return [];
      return processItemsForEvent(detail, event, index, agentNames, artifact);
    });
  return [...routingItem, ...eventItems];
}

function RunProcessSummary({
  detail,
  onOpen,
  agentNames,
  mainAgentModelName,
}: {
  detail: RunDetail;
  onOpen: (target: ProcessDetailTarget) => void;
  agentNames: Map<string, string>;
  mainAgentModelName?: string;
}) {
  const items = runProcessItems(detail, agentNames, mainAgentModelName);
  if (items.length === 0) return null;
  return (
    <section className="run-process-summary" aria-label="Agent 集群动作">
      <div className="agent-cluster-status" role="status" aria-label={`Agent 集群，${items.length} 个关键动作`}>
        <span aria-hidden="true">⌘</span>
        <strong>Agent 集群</strong>
        <small>{items.length} 个关键动作</small>
      </div>
      <div className="agent-cluster-actions">
        {items.map((item) => (
          <button key={item.id} type="button" className="run-process-toggle" onClick={() => onOpen(item)}>
            <span aria-hidden="true">›</span>
            <strong>{item.message}</strong>
          </button>
        ))}
      </div>
    </section>
  );
}

function RunProcessDrawer({
  target,
  onClose,
}: {
  target: ProcessDetailTarget;
  onClose: () => void;
}) {
  return (
    <div className="process-drawer-backdrop" role="presentation" onClick={onClose}>
      <section
        className="process-drawer"
        role="dialog"
        aria-label="运行过程详情"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="process-drawer-handle" aria-hidden="true" />
        <div className="process-drawer-header">
          <div>
            <span className="eyebrow">运行过程</span>
            <h3>{target.title}</h3>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="run-process-detail">
          <article>
            <p>{target.message}</p>
            {target.rows.length > 0 ? (
              <dl>
                {target.rows.map((row, index) => (
                  <Fragment key={`${target.id}-${row.label}-${index}`}>
                    <dt>{row.label}</dt>
                    <dd>{row.value}</dd>
                  </Fragment>
                ))}
              </dl>
            ) : null}
            {target.createdAt ? <small>{target.createdAt}</small> : null}
          </article>
        </div>
      </section>
    </div>
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
    { value: "auto", label: "自动", description: "主 Agent 判断该怎么回复；把握不足时才向你确认。" },
    { value: "direct", label: "直连", description: "指定一个模型/API直接回答，主 Agent 负责控场和提示词。" },
    { value: "dispatch", label: "派单", description: "把任务拆给合适角色执行，最后汇总成一条回复。" },
    { value: "discuss", label: "讨论", description: "多角色表达意见，主 Agent 说明取舍。" },
    { value: "hybrid", label: "混合", description: "先讨论定方案，再派单执行，适合复杂问题。" },
  ] as const;
  const selected = entryModes.find((item) => item.value === selectedMode) ?? entryModes[0];
  return (
    <article className="mode-entry-panel">
      <span className="mode-entry-logo" aria-hidden="true">
        ✦
      </span>
      <h3>新对话</h3>
      <p>先选一个运行方式，也可以保持自动直接发送。</p>
      <div className="mode-entry-tabs" role="list" aria-label="对话模式入口">
        {entryModes.map((item) => (
          <button
            key={item.value}
            type="button"
            aria-label={item.label}
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
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.settings() });
  const mainAgent = useQuery({ queryKey: ["main-agent"], queryFn: () => api.mainAgent() });
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
  const [directModel, setDirectModel] = useState("");
  const [showModeEntry, setShowModeEntry] = useState(true);
  const [processDetailTarget, setProcessDetailTarget] = useState<ProcessDetailTarget | null>(null);
  const [modeSelection, setModeSelection] = useState<ModeSelection | null>(null);
  const [skillInstallCandidate, setSkillInstallCandidate] = useState<SkillInstallCandidate | null>(null);
  const [attachmentDraft, setAttachmentDraft] = useState<ChatAttachmentDraft | null>(null);
  const [conversationRunCache, setConversationRunCache] = useState<Record<string, RunDetail[]>>({});
  const [temporaryApproval, setTemporaryApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: NonNullable<SubmittedRun["temporary_agent_proposal"]>;
    approved: boolean;
  } | null>(null);
  const [temporaryFeedback, setTemporaryFeedback] = useState("");
  const userSelectedMode = useRef(false);
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
    if (!userSelectedMode.current) {
      setMode(settings.data.default_mode);
    }
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
      if (
        !modeSelection ||
        modeSelection.runId !== selection.runId ||
        modeSelection.version !== selection.version ||
        modeSelection.decisionToken !== selection.decisionToken
      ) {
        setModeSelection(selection);
      }
    } else if (
      selectedRun.data &&
      selectedRun.data.status !== "waiting_user_mode" &&
      modeSelection &&
      modeSelection.runId !== selectedRun.data.id
    ) {
      setModeSelection(null);
    }
    const selectedConversationId = runConversationId(selectedRun.data);
    if (selectedConversationId) {
      setConversationId(selectedConversationId);
    }
    const approval = temporaryApprovalFromRunDetail(selectedRun.data);
    if (approval) {
      setModeSelection(null);
      setTemporaryApproval((current) =>
        current &&
        current.runId === approval.runId &&
        current.version === approval.version &&
        current.decisionToken === approval.decisionToken
          ? current
          : approval,
      );
    }
  }, [modeSelection, selectedRun.data, temporaryApproval]);

  useEffect(() => {
    setProcessDetailTarget(null);
  }, [selectedRunId]);

  useEffect(() => {
    if (!activeConversation.data) return;
    setConversationRunCache((current) => {
      const conversationRuns = mergeConversationRuns(
        current[activeConversation.data.conversation_id],
        activeConversation.data.runs,
      );
      if (conversationRuns === current[activeConversation.data.conversation_id]) return current;
      return {
        ...current,
        [activeConversation.data.conversation_id]: conversationRuns,
      };
    });
  }, [activeConversation.data]);

  useEffect(() => {
    const selectedConversationId = runConversationId(selectedRun.data);
    if (!selectedRun.data || !selectedConversationId) return;
    setConversationRunCache((current) => {
      const conversationRuns = mergeConversationRuns(current[selectedConversationId], [selectedRun.data]);
      if (conversationRuns === current[selectedConversationId]) return current;
      return {
        ...current,
        [selectedConversationId]: conversationRuns,
      };
    });
  }, [selectedRun.data]);

  const createRun = useMutation({
    mutationFn: (override?: RunSubmissionOverride) => {
      const runMessage = (override?.message ?? message).trim();
      const runMode = override?.mode ?? mode;
      const selectedDirectModel = (override?.directModel ?? directModel).trim();
      return api.createRun({
        message: runMessage,
        mode: runMode,
        workflow_id: workflowId || null,
        allow_workflow_adjustment: runMode !== "direct" && (settings.data?.allow_main_agent_override ?? false),
        agent_ids: runMode === "direct" ? [] : agentIds,
        direct_model: runMode === "direct" ? selectedDirectModel : null,
        conversation_id: conversationId,
        reference_conversation_id: referenceConversationId.trim() || null,
        attachment_ids: attachmentDraft?.attachment ? [attachmentDraft.attachment.id] : [],
      });
    },
    onSuccess: async (run) => {
      setSelectedRunId(run.id);
      setShowModeEntry(false);
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
        setSubmitNotice("主 Agent 对这轮回复的模式判断不够确定，请直接在输入框回复编号或关键词继续。");
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
    mutationFn: ({ chosenMode, operatorNote }: { chosenMode: ManualRunMode; operatorNote?: string }) => {
      if (!modeSelection) throw new Error("mode selection is unavailable");
      return api.chooseMode(modeSelection.runId, {
        mode: chosenMode,
        decision_token: modeSelection.decisionToken,
        version: modeSelection.version,
        operator_note: operatorNote,
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
      setSubmitNotice("已确认临时子 Agent，这轮对话已继续推进。完成后你可以决定是否永久保存该 Agent。");
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
        model: temporaryApproval.proposal.model ?? (savedModels.find((model) => model.logical_model === "main")?.logical_model ?? savedModels[0]?.logical_model ?? "main"),
        skills: temporaryApproval.proposal.suggested_skills,
      });
    },
    onSuccess: async () => {
      setSubmitNotice("临时子 Agent 已保存为永久 Agent；后续运行仍由主 Agent 按任务自动匹配模型。");
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  const reviseTemporaryAgent = useMutation({
    mutationFn: (feedbackOverride?: string) => {
      if (!temporaryApproval) throw new Error("temporary approval is unavailable");
      return api.reviseTemporaryAgent(temporaryApproval.runId, {
        decision_token: temporaryApproval.decisionToken,
        version: temporaryApproval.version,
        feedback: (feedbackOverride ?? temporaryFeedback).trim(),
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
      setConversationRunCache((current) => {
        let changed = false;
        const next = Object.fromEntries(
          Object.entries(current).map(([conversationKey, runs]) => {
            const filteredRuns = runs.filter((run) => run.id !== result.id);
            if (filteredRuns.length !== runs.length) changed = true;
            return [conversationKey, filteredRuns];
          }),
        );
        return changed ? next : current;
      });
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
      setConversationRunCache((current) => {
        let changed = false;
        const next = Object.fromEntries(
          Object.entries(current).map(([conversationKey, runs]) => {
            const filteredRuns = runs.filter((run) => !deletedIds.has(run.id));
            if (filteredRuns.length !== runs.length) changed = true;
            return [conversationKey, filteredRuns];
          }),
        );
        return changed ? next : current;
      });
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
      if (isArchiveFileName(file.name) && error instanceof ApiError && error.code === "invalid_skill_package") {
        setSubmitNotice(
          "这个压缩包不是有效 Skill 包，正在按普通压缩包附件上传。是否用于代码审查，由你在对话中说明。",
        );
        uploadAttachment.mutate(file);
      } else {
        setAttachmentDraft({
          fileName: file.name,
          size: file.size,
          kind: isArchiveFileName(file.name) ? "archive" : "context",
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
      const kind =
        attachment.kind === "image"
          ? "image"
          : attachment.kind === "archive" || attachment.kind === "code_archive" || isArchiveFileName(attachment.filename || file.name)
            ? "archive"
            : "context";
      setSkillInstallCandidate(null);
      setAttachmentDraft({ fileName: attachment.filename || file.name, size: attachment.size_bytes, kind, attachment });
      setSubmitNotice(
        kind === "archive"
          ? "压缩包已上传。请在输入框说明它是 Skill、代码审查材料，还是普通任务附件。"
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
    if (mayBeSkillArchive(file.name)) {
      uploadSkillArchive.mutate(file);
      return;
    }
    uploadAttachment.mutate(file);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitNotice(null);
    const trimmed = message.trim();
    if (!trimmed) return;
    if (temporaryApproval) {
      const choice = parseChoiceText(trimmed, [
        { value: "approve", label: "同意临时加入", aliases: ["同意", "接受", "加入", "approve", "yes"] },
        { value: "reject", label: "不加入，按现有角色继续", aliases: ["不加入", "拒绝", "不要", "reject", "no"] },
        { value: "revise", label: "提出新的意见", aliases: ["意见", "修改", "重规", "调整", "revise", "feedback"] },
        { value: "persist", label: "保存为永久 Agent", aliases: ["保存", "永久", "persist", "permanent"] },
      ]);
      if (!choice) {
        setSubmitNotice("请回复 1/同意、2/不加入、3 加上你的修改意见，或 4/保存为永久 Agent。");
        return;
      }
      setMessage("");
      if (choice.option.value === "approve") {
        if (temporaryApproval.approved) {
          setSubmitNotice("这个临时 Agent 已经加入。本轮完成后可回复“4”保存为永久 Agent。");
          return;
        }
        setSubmitNotice("已选择同意临时加入，正在继续这轮对话。");
        approveTemporaryAgent.mutate();
        return;
      }
      if (choice.option.value === "persist") {
        if (!temporaryApproval.approved) {
          setSubmitNotice("保存为永久 Agent 前，需要先回复 1 同意临时加入并完成本轮运行。");
          return;
        }
        setSubmitNotice("正在把这个临时 Agent 保存为永久 Agent。");
        promoteTemporaryAgent.mutate();
        return;
      }
      const feedback =
        choice.option.value === "reject"
          ? choice.note || "不加入临时子 Agent，按现有角色继续。"
          : choice.note;
      if (!feedback) {
        setSubmitNotice("选择“提出新的意见”时，请在编号后写清楚你的意见，例如：3 不要加工程师，先让产品经理重拆。");
        return;
      }
      setTemporaryFeedback(feedback);
      setSubmitNotice("已收到你的反馈，正在让主 Agent 重新规划。");
      reviseTemporaryAgent.mutate(feedback);
      return;
    }
    if (modeSelection) {
      const choice = parseChoiceText(
        trimmed,
        MANUAL_RUN_MODES.map((item) => ({
          value: item.value,
          label: item.label,
          aliases: [item.value, item.description],
        })),
      );
      if (!choice) {
        setSubmitNotice("请回复 1-4 的编号，或回复“直连 / 派单 / 讨论 / 混合”这类关键词；后面可以继续补充你的想法。");
        return;
      }
      setMessage("");
      setSubmitNotice(`已选择“${choice.option.label}”，正在按你的选择继续。`);
      setMode(choice.option.value as RunMode);
      chooseMode.mutate({
        chosenMode: choice.option.value as ManualRunMode,
        operatorNote: choice.note || undefined,
      });
      return;
    }
    const initialModeChoice =
      showModeEntry && !selectedRunId
        ? parseLeadingKeywordChoiceText(
            trimmed,
            RUN_MODES.map((item) => ({
              value: item.value,
              label: item.label,
              aliases: [item.value],
            })),
          )
        : null;
    const effectiveMode = (initialModeChoice?.option.value as RunMode | undefined) ?? mode;
    const effectiveMessage = initialModeChoice?.note || trimmed;
    if (initialModeChoice) {
      userSelectedMode.current = true;
      setMode(effectiveMode);
      if (!effectiveMessage) {
        setMessage("");
        setSubmitNotice(`已切换到“${initialModeChoice.option.label}”。现在输入你的问题即可继续。`);
        return;
      }
    }
    if (effectiveMode === "direct") {
      if (savedModels.length === 0) {
        setSubmitNotice("还没有可用于直连的已测试模型。请先到“模型与 API”页面保存并通过可用性测试。");
        return;
      }
      const choice = parseChoiceText(
        effectiveMessage,
        savedModels.map((model) => ({
          value: model.logical_model,
          label: model.logical_model,
          aliases: [model.upstream_model, model.provider],
        })),
      );
      const selectedModel = choice?.option.value ?? directModel;
      if (!selectedModel) {
        setSubmitNotice("请先回复模型编号或模型关键词，例如“1”或“qwen-max”；也可以写成“2 帮我写一段口播”。");
        return;
      }
      if (!registeredModelIds.has(selectedModel)) {
        setSubmitNotice("所选直连模型/API 未注册或未通过配置，请先到模型页面修正。");
        return;
      }
      setDirectModel(selectedModel);
      const nextMessage = (choice?.note || (!choice ? effectiveMessage : "")).trim();
      if (!nextMessage) {
        setMessage("");
        setSubmitNotice(`已选择直连模型/API：${selectedModel}。现在输入你的问题即可发送。`);
        return;
      }
      createRun.mutate({ message: nextMessage, directModel: selectedModel, mode: effectiveMode });
      return;
    }
    createRun.mutate({ message: effectiveMessage, mode: effectiveMode });
  }

  function startNewConversation() {
    setSelectedRunId(null);
    setShowModeEntry(true);
    setConversationId(newConversationId());
    setReferenceConversationId("");
    setMessage("");
    userSelectedMode.current = false;
    setMode(settings.data?.default_mode ?? "auto");
    setWorkflowId(settings.data?.default_workflow_id ?? "");
    setAgentIds(settings.data?.default_agent_ids ?? []);
    setDirectModel("");
    setTemporaryApproval(null);
    setModeSelection(null);
    setProcessDetailTarget(null);
    setSubmitNotice("已新建空白对话。选一个模式或直接发送，主 Agent 会按当前设置处理。");
  }

  function startHandoffConversation(sourceRun?: RunDetail | null) {
    const sourceConversationId = runConversationId(sourceRun ?? selectedRun.data) ?? conversationId;
    if (!sourceConversationId) {
      setSubmitNotice("当前没有可 Handoff 的会话。");
      return;
    }
    setSelectedRunId(null);
    setShowModeEntry(false);
    setReferenceConversationId(sourceConversationId);
    setConversationId(newConversationId());
    setMessage("");
    setDirectModel("");
    setTemporaryApproval(null);
    setModeSelection(null);
    setProcessDetailTarget(null);
    setSubmitNotice(`已按原思路开启新对话：新对话会读取 ${sourceConversationId} 作为参考上下文。`);
  }

  function loadReferenceConversation() {
    if (!trimmedReferenceConversationId) return;
    void referenceConversation.refetch();
  }

  if (runs.isLoading) {
    return <p>正在加载对话...</p>;
  }
  if (runs.isError) return <p role="alert">{formatApiError(runs.error, "会话列表加载失败")}</p>;

  const items = runs.data ?? [];
  const selectedMode = RUN_MODES.find((item) => item.value === mode) ?? RUN_MODES[0];
  const savedAgents = agents.data ?? [];
  const savedModels = models.data ?? [];
  const enabledAgents = savedAgents.filter((agent) => agent.enabled);
  const savedWorkflows = workflows.data ?? [];
  const agentNameMap = new Map(savedAgents.map((agent) => [agent.id, agent.name]));
  const cachedConversationRuns = selectedRunConversationId ? conversationRunCache[selectedRunConversationId] : undefined;
  const visibleRuns = cachedConversationRuns ?? activeConversation.data?.runs ?? (selectedRun.data ? [selectedRun.data] : []);
  const messages = conversationMessages(visibleRuns);
  const temporaryApprovalVisibleInMessages =
    !!temporaryApproval &&
    messages.some((item) => item.id === `${temporaryApproval.runId}-temporary-agent-approval`);
  const latestVisibleRun = visibleRuns.at(-1) ?? selectedRun.data;
  const registeredModelIds = new Set(savedModels.map((model) => model.logical_model));
  const directModelDeployment = savedModels.find((model) => model.logical_model === directModel) ?? null;
  const directModelName = directModelDeployment?.logical_model ?? (directModel || "未指定");
  const mainAgentModelName = mainAgent.data?.model
    ? `${mainAgent.data.model.provider}/${mainAgent.data.model.upstream_model}`
    : "未配置";
  const directSendBlockedReason =
    mode !== "direct"
      ? null
      : savedModels.length === 0
        ? "还没有可用于直连的已测试模型。请先到“模型与 API”页面保存并通过可用性测试。"
        : directModel && !registeredModelIds.has(directModel)
            ? "所选直连模型/API 未注册或未通过配置，请先到模型页面修正。"
          : null;
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
      <h2>对话</h2>
      <p className="compact-page-intro">
        这里是连续对话窗口。只要不新建对话，后续消息都会沿用当前会话上下文；工作流配置请到“工作流配置”页面维护。
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
            <p className="field-help">还没有对话。可以从右侧输入框发起第一次交流。</p>
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
                    onClick={() => {
                      setShowModeEntry(false);
                      setSelectedRunId(run.id);
                    }}
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
                <p>未选择工作流时，主 Agent 会按消息内容和你勾选的角色进行调度。</p>
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

          <details className="inline-guide" open={mode !== "direct"}>
            <summary>{mode === "direct" ? "直连说明" : "选择本次参与角色池"}</summary>
            {mode === "direct" ? (
              <>
                <p className="field-help">
                  直连模型不在这里下拉选择。请回到主对话，按编号或模型关键词选择本次对话使用的模型/API。
                </p>
                {models.isLoading ? <p className="field-help">正在加载已测试模型...</p> : null}
                {models.isError ? (
                  <p className="field-help" role="alert">
                    {formatApiError(models.error, "模型列表加载失败")}
                  </p>
                ) : null}
                {savedModels.length === 0 ? (
                  <p className="field-help">还没有可用于直连的已测试模型，请先到“模型与 API”页面配置。</p>
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
            <div className="chat-session-toolbar" aria-label="当前对话操作">
              <p className="chat-conversation-status">当前会话：{conversationId}</p>
              <div>
                <button type="button" className="secondary-action" onClick={startNewConversation}>
                  新建对话
                </button>
              </div>
            </div>
            {showModeEntry ? (
              <ModeEntryPanel selectedMode={mode} onSelect={chooseRunMode} />
            ) : null}
            {mode === "direct" && messages.length === 0 ? (
              <article className="chat-message assistant" aria-label="直连模型选择">
                <span className="eyebrow">Agent Hub</span>
                <h3>直连准备</h3>
                {savedModels.length > 0 ? (
                  <>
                    <p>
                      直连会由主 Agent 控场、组织提示词和记录过程；实际生成由你选择的模型/API完成。请回复编号或模型关键词，
                      后面可以直接补充任务内容。
                    </p>
                    <ol className="choice-list">
                      {savedModels.map((model, index) => (
                        <li key={model.id}>
                          {index + 1}. {model.logical_model}（{model.provider} / {model.upstream_model}）
                        </li>
                      ))}
                    </ol>
                    <p>
                      {directModel
                        ? `已选：${directModelName}。现在直接输入任务即可发送。`
                        : "直连需要先选择本次对话使用的模型/API。例如：1 帮我写一段口播。"}
                    </p>
                  </>
                ) : (
                  <p>还没有可用于直连的已测试模型。请先到“模型与 API”页面保存并通过可用性测试。</p>
                )}
              </article>
            ) : null}
            {modeSelection ? (
              <article className="chat-message assistant" aria-label="运行模式确认">
                <span className="eyebrow">Agent Hub</span>
                <h3>主 Agent 需要你确认运行方式</h3>
                <p>
                  自动检测没有足够把握，原因：{modeSelection.reason ?? "routing_requires_user_choice"}。
                  请在当前输入框回复编号或关键词；后面可以继续补充你的想法。
                </p>
                <ol className="choice-list">
                  {MANUAL_RUN_MODES.map((item, index) => (
                    <li key={item.value}>
                      {index + 1}. {item.label}：{item.description}
                    </li>
                  ))}
                </ol>
              </article>
            ) : null}
            {temporaryApproval && !temporaryApprovalVisibleInMessages ? (
              <article className="chat-message assistant" aria-label="临时 Agent 文字确认">
                <span className="eyebrow">Agent Hub</span>
                <h3>{temporaryApproval.proposal.name}</h3>
                <p>{temporaryAgentApprovalBody(temporaryApproval.proposal)}</p>
              </article>
            ) : null}
            {messages.map((item, index) => (
              <Fragment key={item.id}>
                <article className={`chat-message ${item.role}`}>
                  <span className="eyebrow">{item.role === "user" ? "你" : "Agent Hub"}</span>
                  <h3>{item.title}</h3>
                  <p>{item.body}</p>
                </article>
                {item.id.endsWith("-request") && item.run ? (
                  <RunProcessSummary
                    detail={item.run}
                    onOpen={setProcessDetailTarget}
                    agentNames={agentNameMap}
                    mainAgentModelName={mainAgentModelName}
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
          {processDetailTarget ? (
            <RunProcessDrawer
              target={processDetailTarget}
              onClose={() => setProcessDetailTarget(null)}
            />
          ) : null}

          <form onSubmit={submit} aria-label="发送消息" className="chat-composer">
            {chooseMode.isError ? (
              <p role="alert">{formatApiError(chooseMode.error, "运行模式确认失败")}</p>
            ) : null}
            {approveTemporaryAgent.isError ? (
              <p role="alert">{formatApiError(approveTemporaryAgent.error, "临时 Agent 确认失败")}</p>
            ) : null}
            {reviseTemporaryAgent.isError ? (
              <p role="alert">{formatApiError(reviseTemporaryAgent.error, "临时 Agent 重规失败")}</p>
            ) : null}
            {promoteTemporaryAgent.isError ? (
              <p role="alert">{formatApiError(promoteTemporaryAgent.error, "永久化 Agent 失败")}</p>
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
                    {attachmentDraft.kind === "archive"
                      ? "压缩包附件"
                      : attachmentDraft.kind === "image"
                        ? "图片附件"
                        : "上下文附件"}
                  </span>
                  <strong>{attachmentDraft.fileName}</strong>
                  <small>{Math.max(1, Math.ceil(attachmentDraft.size / 1024))} KB</small>
                </div>
                <p>
                  {attachmentDraft.kind === "archive"
                    ? "压缩包已作为附件保存。请在对话里说明它是 Skill、代码审查材料，还是普通任务文件。"
                    : attachmentDraft.kind === "image"
                      ? "图片已选中。当前先记录附件，启用多模态链路后可交给视觉模型识别。"
                      : "附件已选中。当前先记录附件名称，完整内容读取会走后端附件存储。"}
                </p>
              </aside>
            ) : null}
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入消息，继续当前对话。例如：这个方案继续往更玄幻一点改。"
              required
            />
            <div className="composer-actions">
              <label className="composer-upload-button">
                <span>附件</span>
                <input
                  aria-label="上传文件或 Skill ZIP"
                  type="file"
                  accept={ATTACHMENT_ACCEPT}
                  disabled={uploadSkillArchive.isPending || uploadAttachment.isPending}
                  onChange={(event) => handleAttachmentUpload(event.currentTarget.files)}
                />
              </label>
              <button
                type="button"
                className="composer-handoff-button"
                aria-label="按照原思路"
                title="按照原思路开启新对话"
                disabled={!latestVisibleRun}
                onClick={() => startHandoffConversation(latestVisibleRun)}
              >
                按照原思路
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
                {mode === "auto"
                  ? "自动 · 主 Agent 判断"
                  : mode === "direct"
                    ? `直连 · 模型 ${directModelName}`
                    : `${displayMode(mode)} · 本会话倾向`}
                {mode !== "direct" && agentIds.length > 0 ? ` · 角色 ${agentIds.length} 个` : ""}
                {mainAgent.data?.model ? ` · 主 Agent ${mainAgent.data.model.upstream_model}` : " · 主 Agent 未配置"}
                {referenceConversationId.trim() ? " · 已引用会话" : ""}
              </span>
              <button
                type="submit"
                disabled={createRun.isPending || message.trim().length === 0 || Boolean(directSendBlockedReason)}
              >
                {createRun.isPending ? "发送中..." : "发送"}
              </button>
            </div>
            {directSendBlockedReason && !(mode === "direct" && savedModels.length === 0) ? (
              <p className="field-help" role="status">{directSendBlockedReason}</p>
            ) : null}
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
            {createRun.isError ? <p role="alert">{formatApiError(createRun.error, "消息发送失败")}</p> : null}
          </form>
        </div>
      </div>

    </section>
  );
}
