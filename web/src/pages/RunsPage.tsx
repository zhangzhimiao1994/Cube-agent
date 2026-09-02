import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";

import { ApiError, api, formatApiError, type AttachmentUpload, type ModelDeployment, type RunDetail, type RunListItem, type Skill, type SkillArchiveUpload, type SubmittedRun } from "../api/client";
import { APP_BRAND_NAME } from "../app/brand";
import { ArtifactFileCard, artifactFileName, formatFileSize, hasArtifactDownload } from "../components/ArtifactFileCard";

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
  skills: Skill[];
  skipped: SkillArchiveUpload["skipped"];
  status: "scanned" | "enabled";
};
type ChatAttachmentDraft = {
  fileName: string;
  size: number;
  kind: "archive" | "image" | "context";
  attachment?: AttachmentUpload;
};
type TemporaryAgentProposal = NonNullable<SubmittedRun["temporary_agent_proposal"]>;
type ScheduleProposal = NonNullable<SubmittedRun["schedule_proposal"]>;
type EvolutionProposal = NonNullable<SubmittedRun["evolution_proposal"]>;
type OpenClawProposal = NonNullable<SubmittedRun["openclaw_proposal"]>;
type RepairProposal = NonNullable<SubmittedRun["repair_proposal"]>;
type RunSubmissionOverride = {
  message?: string;
  directModel?: string;
  mode?: RunMode;
  skipEvolutionProposal?: boolean;
  successNotice?: string;
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
    "model.reasoning_delta": "思考过程",
    "model.text_delta": "输出进度",
    "runtime.started": "开始执行本次对话",
    "runtime.completed": "完成本次对话",
    "runtime.failed": "本次对话中断",
    "harness.started": "Harness 已启动",
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
    "tool.requested": "工具请求已记录",
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
  const isRestrictedIntent = isIntentEventWithRestrictedPayload(event);
  const readableMessage =
    !isRestrictedIntent &&
    event.message &&
    event.message !== event.kind &&
    !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : null;
  const messages: Record<string, string> = {
    queued: "任务已进入队列，等待 Worker 调度执行。",
    "run.queued": "任务已进入队列，等待 Worker 调度执行。",
    "model.started": "模型请求已开始。",
    "model.reasoning_delta": "模型正在分析，公开日志只记录进度元数据。",
    "model.text_delta": "模型正在生成回复，公开日志只记录进度元数据。",
    "runtime.started": "运行时已启动，正在按模式执行。",
    "runtime.completed": "运行完成，已汇总结果。",
    "runtime.failed": readableMessage ?? "运行失败，请查看日志中心的模式运行错误。",
    "harness.started": "Harness 已完成模型、能力和策略选择，运行进入工程执行面。",
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
  if (actor === "main_agent" || actor === "main") return "主 Agent";
  return agentNames.get(actor) ?? actor;
}

function displayEventParticipants(participants: string[], agentNames: Map<string, string>) {
  const names = participants.map((id) => agentNames.get(id) ?? id).filter(Boolean);
  return names.length > 0 ? names.join("、") : null;
}

function displayPayloadParticipants(payload: Record<string, unknown>, agentNames: Map<string, string>) {
  const participants = payload.participants;
  if (!Array.isArray(participants)) return null;
  const names = participants
    .filter((item): item is string => typeof item === "string" && item.length > 0)
    .map((id) => agentNames.get(id) ?? id);
  return names.length > 0 ? names.join("、") : null;
}

function displayPayloadParticipantModels(payload: Record<string, unknown>, agentNames: Map<string, string>) {
  const participantModels = payload.participant_models;
  if (!participantModels || typeof participantModels !== "object" || Array.isArray(participantModels)) return null;
  const rows = Object.entries(participantModels)
    .filter((entry): entry is [string, string] => typeof entry[1] === "string" && entry[1].length > 0)
    .map(([agentId, model]) => `${agentNames.get(agentId) ?? agentId}：${model}`);
  return rows.length > 0 ? rows.join("；") : null;
}

function formatEventPayloadValue(value: unknown): string {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((item) => ["string", "number", "boolean"].includes(typeof item))) {
    return value.map((item) => String(item)).join("、");
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

type RunEvent = RunDetail["events"][number];
type RunArtifact = RunDetail["artifacts"][number];
type DownloadableArtifact = (RunArtifact | NonNullable<RunEvent["artifact"]>) & {
  download_url: string;
};
type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  title: string;
  body: string;
  artifact?: DownloadableArtifact;
  temporaryAgent?: TemporaryAgentProposal;
  run?: RunDetail;
};
type EventGroupItem = {
  event: RunEvent;
  index: number;
};

function isFinalDownloadableArtifact(
  artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined,
): artifact is DownloadableArtifact {
  return hasArtifactDownload(artifact) && artifact.presentation === "final_attachment";
}

function downloadArtifactMessage(artifact: DownloadableArtifact): ChatMessage {
  const filename = artifactFileName(artifact);
  return {
    id: `download-${artifact.id}`,
    role: "assistant",
    title: `附件：${filename}`,
    body: [artifact.kind, artifact.mime_type].filter(Boolean).join(" · ") || artifact.kind,
    artifact,
  };
}

function artifactMessage(artifact: RunArtifact): ChatMessage {
  if (isFinalDownloadableArtifact(artifact)) {
    return downloadArtifactMessage(artifact);
  }
  return {
    id: `artifact-${artifact.id}`,
    role: "assistant",
    title: `附件：${artifact.title}`,
    body: artifact.kind,
  };
}

function artifactDisplayName(artifact: RunArtifact | NonNullable<RunEvent["artifact"]>) {
  return artifactFileName(artifact);
}

function artifactDetailDownload(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  return hasArtifactDownload(artifact) ? artifact : undefined;
}

function isGenericArtifactText(value: string | null | undefined) {
  const normalized = value?.replace(/\s+/g, " ").trim();
  if (!normalized) return false;
  return new Set([
    "已生成一个可查看的结果或中间产物。",
    "已生成一个可查看的结果或中间产物",
    "artifact.created",
    "message.created",
  ]).has(normalized);
}

function conciseProcessText(value: string, fallback: string) {
  const normalized = value
    .replace(/```[\s\S]*?```/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return fallback;
  const sentence = normalized.split(/(?<=[。！？.!?])\s+/)[0]?.trim() || normalized;
  return sentence.length > 34 ? `${sentence.slice(0, 34)}...` : sentence;
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
    return Boolean(
      event.actor ||
        event.step_id ||
        event.tool_name ||
        event.artifact ||
        formatEventPayloadValue(event.payload.output) ||
        formatEventPayloadValue(event.payload.result),
    );
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
    assigned_task: "下发任务",
    prompt: "提示词/指令",
    input: "输入内容",
    role_message: "角色发言",
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
    model_used: "调用模型",
    model_provider: "模型服务商",
    model_deployment: "模型部署",
    deployment: "模型部署",
    provider: "服务商",
    id: "调用 ID",
    name: "工具",
    argument_keys: "参数字段",
    argument_key_count: "参数字段数",
    redacted_argument_key_count: "已隐藏字段数",
    argument_bytes: "参数字节数",
    arguments_sha256: "参数摘要",
    status: "状态",
    exit_code: "退出码",
    command_bytes: "命令字节数",
    output_bytes: "输出字节数",
    stdout_bytes: "标准输出字节数",
    stderr_bytes: "标准错误字节数",
    result_bytes: "结果字节数",
    content_bytes: "内容字节数",
    operation_kind: "操作类别",
    sandbox: "沙箱",
    replay_safe: "可重放",
    failure_kind: "失败类型",
    delta_kind: "Delta 类型",
    text_bytes: "内容字节数",
    chunk_index: "分片序号",
    phase: "阶段",
    capabilities: "工程能力",
    policy: "策略原因",
    context: "上下文信号",
    fallbacks: "备选路径",
    requires_approval: "审批要求",
    role: "角色",
    agent: "Agent",
    artifact_id: "产物 ID",
    tools: "可用工具",
    attempts: "执行次数",
    attempt: "第几次尝试",
    missing_capability: "缺少能力",
    reason: "原因",
    approval_id: "审批 ID",
    repair_action: "修复动作",
    repair_kind: "修复类型",
    remediation_action: "修复动作",
    self_repair: "自修复",
    upstream_model: "上游模型",
  };
  if (labels[key]) return labels[key];
  if (key.endsWith("_opinion")) {
    return `${key.replace(/_opinion$/, "").replace(/_/g, " ")} 意见`;
  }
  return `详情：${key}`;
}

function orderedEventPayloadEntries(payload: Record<string, unknown>) {
  const priority = [
    "logical_model",
    "model",
    "upstream_model",
    "provider",
    "deployment",
    "name",
    "id",
    "argument_keys",
    "argument_key_count",
    "redacted_argument_key_count",
    "argument_bytes",
    "arguments_sha256",
    "delta_kind",
    "text_bytes",
    "chunk_index",
    "phase",
    "role",
    "agent",
    "task",
    "assigned_task",
    "instruction",
    "instructions",
    "prompt",
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

function isModelDeltaEvent(event: RunDetail["events"][number]) {
  return event.kind === "model.reasoning_delta" || event.kind === "model.text_delta";
}

function modelDeltaGroupKey(event: RunEvent) {
  const phase = formatEventPayloadValue(event.payload.phase);
  const deltaKind = formatEventPayloadValue(event.payload.delta_kind);
  return [event.kind, event.actor ?? "", event.step_id ?? "", phase, deltaKind].join("|");
}

function modelDeltaEventsCanMerge(left: RunEvent, right: RunEvent) {
  return modelDeltaGroupKey(left) === modelDeltaGroupKey(right);
}

function numericPayloadValue(event: RunEvent, key: string) {
  const value = event.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function isToolEvent(event: RunDetail["events"][number]) {
  return event.kind === "tool.requested" || event.kind === "tool.started" || event.kind === "tool.completed" || event.kind === "tool.failed";
}

const RAW_TOOL_PAYLOAD_KEYS = new Set([
  "arguments",
  "arguments_sha256",
  "body",
  "command",
  "content",
  "input",
  "output",
  "prompt",
  "result",
  "role_message",
  "stderr",
  "stdout",
  "summary",
  "text",
]);

const INTENT_RAW_PAYLOAD_KEYS = new Set([
  ...RAW_TOOL_PAYLOAD_KEYS,
  "assigned_task",
  "details",
  "error",
  "feedback",
  "instructions",
  "reason",
  "task",
  "traceback",
]);

const SUMMARY_BACKED_RAW_PAYLOAD_KEYS = new Set([
  ...RAW_TOOL_PAYLOAD_KEYS,
  "assigned_task",
  "command",
  "details",
  "error",
  "feedback",
  "input",
  "instructions",
  "output",
  "prompt",
  "result",
  "role_message",
  "task",
  "traceback",
]);

const DISCUSSION_MINUTES_PAYLOAD_KEYS = new Set([
  "conclusion",
  "result",
  "discussion",
  "opinions",
  "summary",
  "disagreement",
  "conflict",
  "risks",
  "concerns",
  "main_agent_judgement",
  "main_agent_judgment",
  "final_decision",
]);

function isIntentEventWithRestrictedPayload(event: RunDetail["events"][number]) {
  return event.kind === "step.retrying" || event.kind.startsWith("approval.") || isRepairIntentEvent(event);
}

function eventSafeSummary(event: RunDetail["events"][number]) {
  return formatEventPayloadValue(event.summary);
}

function toolEventName(event: RunDetail["events"][number]) {
  return event.tool_name || formatEventPayloadValue(event.payload.name) || "工具";
}

function toolOperationLabel(toolName: string) {
  const normalized = toolName.toLowerCase().replace(/[.\s-]+/g, "_");
  if (
    normalized.includes("run_safe_command") ||
    normalized.includes("command") ||
    normalized.includes("terminal") ||
    normalized.includes("shell") ||
    normalized.includes("exec")
  ) {
    return "运行终端";
  }
  if (normalized.includes("edit") || normalized.includes("write") || normalized.includes("patch")) {
    return "编辑文件";
  }
  if (normalized.includes("read") || normalized.includes("context") || normalized.includes("workspace")) {
    return "读取文件";
  }
  if (normalized.includes("browser") || normalized.includes("click") || normalized.includes("screen")) {
    return "浏览操作";
  }
  return "使用工具";
}

function toolStatusLabel(event: RunDetail["events"][number]) {
  if (event.kind === "tool.requested") return "请求";
  if (event.kind === "tool.started") return "开始";
  if (event.kind === "tool.completed") return "完成";
  if (event.kind === "tool.failed") return "失败";
  return "记录";
}

function eventDetailRows(event: RunDetail["events"][number], agentNames: Map<string, string>) {
  const rows: Array<{ label: string; value: string }> = [];
  const actor = displayEventActor(event.actor, agentNames);
  const participants = displayEventParticipants(event.participants, agentNames);
  const isModelDelta = isModelDeltaEvent(event);
  const isTool = isToolEvent(event);
  const isRestrictedIntent = isIntentEventWithRestrictedPayload(event);
  const readableMessage =
    !isRestrictedIntent &&
    !isModelDelta &&
    !isTool &&
    event.message &&
    event.message !== event.kind &&
    !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  if (actor) rows.push({ label: "执行者", value: actor });
  if (isTool) {
    rows.push({ label: "操作类型", value: toolOperationLabel(toolEventName(event)) });
    rows.push({ label: "工具状态", value: toolStatusLabel(event) });
  }
  const payloadParticipants = displayPayloadParticipants(event.payload, agentNames);
  if (participants || payloadParticipants) rows.push({ label: "参与者", value: participants ?? payloadParticipants ?? "" });
  const participantModels = displayPayloadParticipantModels(event.payload, agentNames);
  if (participantModels) rows.push({ label: "模型分配", value: participantModels });
  if (event.tool_name) rows.push({ label: "工具", value: event.tool_name });
  if (event.tool_call_id) rows.push({ label: "调用 ID", value: event.tool_call_id });
  if (event.step_id) rows.push({ label: "步骤", value: event.step_id });
  if (event.approval_id) rows.push({ label: "审批 ID", value: event.approval_id });
  if (event.action) rows.push({ label: "动作", value: event.action });
  if (event.decision) rows.push({ label: "决策", value: event.decision });
  const safeSummary = eventSafeSummary(event);
  if (safeSummary) rows.push({ label: "安全摘要", value: safeSummary });
  if (readableMessage) rows.push({ label: "事件内容", value: readableMessage });
  orderedEventPayloadEntries(event.payload).forEach(([key, value]) => {
    if (key === "participants" || key === "participant_models") return;
    if (event.kind === "discussion.completed" && (DISCUSSION_MINUTES_PAYLOAD_KEYS.has(key) || key.endsWith("_opinion"))) return;
    if (safeSummary && key === "summary") {
      const formatted = formatEventPayloadValue(value);
      if (formatted && formatted !== safeSummary) rows.push({ label: eventPayloadLabel(key), value: formatted });
      return;
    }
    if (safeSummary && SUMMARY_BACKED_RAW_PAYLOAD_KEYS.has(key)) return;
    if (isTool && RAW_TOOL_PAYLOAD_KEYS.has(key)) return;
    if (isIntentEventWithRestrictedPayload(event) && INTENT_RAW_PAYLOAD_KEYS.has(key)) return;
    if (isModelDelta && key === "text") return;
    const formatted = formatEventPayloadValue(value);
    if (formatted) {
      rows.push({ label: eventPayloadLabel(key), value: formatted });
    }
  });
  return rows;
}

type ProcessDetailTarget = {
  id: string;
  runId: string;
  conversationId: string | null;
  title: string;
  message: string;
  badge: string;
  rows: Array<{ label: string; value: string }>;
  createdAt: string | null;
  artifact?: DownloadableArtifact;
  sourceKind?: string;
  sourceStepId?: string | null;
  sourceActor?: string | null;
};

type AgentDispatchCard = {
  id: string;
  name: string;
  role: string;
  model: string;
  summary: string;
  status: "异常" | "已完成" | "工作中" | "已安排";
};

type ProcessDetailGroup = {
  key: string;
  label: string;
  rows: Array<{ label: string; value: string }>;
};

type TaskChainStep = {
  id: string;
  agentId: string;
  agentName: string;
  status: "等待确认" | "异常" | "已完成" | "进行中" | "已安排" | "等待上游";
  summary: string;
  dependsOn: string[];
};

type RunExecutionIntent = {
  id: string;
  label: "审批意图" | "重试意图" | "回放意图" | "修复意图";
  title: string;
  detail: string;
  meta: string[];
  tone: "pending" | "retry" | "replay" | "repair" | "done";
};

type RunFailureDiagnostic = {
  id: string;
  label: "工具执行失败" | "模型链路失败" | "运行阶段失败" | "等待人工确认";
  title: string;
  detail: string;
  recommendation: string;
  meta: string[];
  tone: "tool" | "model" | "runtime" | "approval";
};

type ApprovalState = {
  pending: RunEvent[];
  resolved: RunEvent[];
};

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function stringArrayValue(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function mainAgentPlanEvent(detail: RunDetail): RunEvent | null {
  return (
    [...detail.events].reverse().find((event) => {
      if (event.kind !== "step.started" || event.actor !== "main_agent" || event.step_id !== "main_agent_plan") return false;
      return Array.isArray(event.payload.roles);
    }) ?? null
  );
}

function agentStatusForPlan(detail: RunDetail, agentId: string, stepIds: Set<string>): AgentDispatchCard["status"] {
  const events = detail.events.filter((event) => event.actor === agentId || (event.step_id ? stepIds.has(event.step_id) : false));
  if (events.some((event) => event.kind.endsWith(".failed") || event.kind === "runtime.failed")) return "异常";
  if (events.some((event) => event.kind === "step.completed")) return "已完成";
  if (events.some((event) => ["step.started", "model.started", "tool.requested", "tool.started"].includes(event.kind))) {
    return "工作中";
  }
  return "已安排";
}

function dispatchAgentCards(detail: RunDetail, agentNames: Map<string, string>): AgentDispatchCard[] {
  const plan = mainAgentPlanEvent(detail);
  if (!plan) return [];
  const roles = Array.isArray(plan.payload.roles) ? plan.payload.roles.filter(isObjectRecord) : [];
  const steps = Array.isArray(plan.payload.steps) ? plan.payload.steps.filter(isObjectRecord) : [];
  const stepOwners = new Map<string, Set<string>>();
  steps.forEach((step) => {
    const stepId = stringValue(step.id);
    const agentId = stringValue(step.agent);
    if (!stepId || !agentId) return;
    const owners = stepOwners.get(stepId) ?? new Set<string>();
    owners.add(agentId);
    stepOwners.set(stepId, owners);
  });
  return roles
    .map((role) => {
      const id = stringValue(role.id);
      const purpose = stringValue(role.purpose);
      if (!id || id === "final_synthesizer" || purpose === "synthesize") return null;
      const stepIds = new Set(
        steps.flatMap((step) => {
          const stepId = stringValue(step.id);
          if (!stepId || stringValue(step.agent) !== id || (stepOwners.get(stepId)?.size ?? 0) !== 1) return [];
          return [stepId];
        }),
      );
      const roleLabel = stringValue(role.role) ?? id;
      const model = stringValue(role.logical_model) ?? "默认模型";
      const roleSummary =
        stringValue(role.summary) ??
        stringValue(role.goal) ??
        stringValue(role.description) ??
        stringValue(role.backstory) ??
        "等待执行分配任务";
      return {
        id,
        name: agentNames.get(id) ?? id,
        role: roleLabel,
        model,
        summary: conciseProcessText(roleSummary, "等待执行分配任务"),
        status: agentStatusForPlan(detail, id, stepIds),
      };
    })
    .filter((card): card is AgentDispatchCard => card !== null);
}

function plannedTaskChain(detail: RunDetail, agentNames: Map<string, string>): TaskChainStep[] {
  const plan = mainAgentPlanEvent(detail);
  if (!plan) return [];
  const roles = Array.isArray(plan.payload.roles) ? plan.payload.roles.filter(isObjectRecord) : [];
  const steps = Array.isArray(plan.payload.steps) ? plan.payload.steps.filter(isObjectRecord) : [];
  const roleNames = new Map<string, string>();
  roles.forEach((role) => {
    const id = stringValue(role.id);
    const label = stringValue(role.role) || stringValue(role.name);
    if (id && label) roleNames.set(id, label);
  });
  const completedSteps = new Set(
    detail.events.filter((event) => event.kind === "step.completed" && event.step_id).map((event) => event.step_id as string),
  );
  const approvalState = approvalStateFromEvents(detail.events);

  return steps.flatMap((step) => {
    const stepId = stringValue(step.id);
    const agentId = stringValue(step.agent);
    if (!stepId || !agentId) return [];
    const dependsOn = stringArrayValue(step.depends_on);
    const stepEvents = detail.events
      .filter((event) => event.step_id === stepId)
      .sort((left, right) => left.sequence - right.sequence);
    const pendingApproval = approvalState.pending.some((event) => event.step_id === stepId);
    const hasFailure = stepEvents.some((event) => event.kind.endsWith(".failed") || event.kind === "runtime.failed");
    const hasCompletion = stepEvents.some((event) => event.kind === "step.completed");
    const hasStarted = stepEvents.some((event) =>
      ["step.started", "model.started", "model.reasoning_delta", "model.text_delta", "tool.requested", "tool.started", "tool.completed"].includes(event.kind),
    );
    const dependenciesComplete = dependsOn.every((dependency) => completedSteps.has(dependency));
    const status = pendingApproval
      ? "等待确认"
      : hasFailure
        ? "异常"
        : hasCompletion
          ? "已完成"
          : hasStarted
            ? "进行中"
            : dependenciesComplete
              ? "已安排"
              : "等待上游";
    const latestProgressEvent = [...stepEvents]
      .reverse()
      .find((event) => !event.kind.startsWith("approval.") && !isWrappedToolFailureEvent(event, detail.events));
    const fallbackSummary = stringValue(step.task) || stringValue(step.summary) || stringValue(step.description) || "等待执行";
    const summary = conciseProcessText(latestProgressEvent ? eventSummaryText(latestProgressEvent, agentNames) : fallbackSummary, "等待执行");
    return [
      {
        id: stepId,
        agentId,
        agentName: roleNames.get(agentId) ?? agentNames.get(agentId) ?? humanizeEventIdentifier(agentId),
        status,
        summary,
        dependsOn,
      },
    ];
  });
}

function approvalStateFromEvents(events: RunDetail["events"]): ApprovalState {
  const pending: RunEvent[] = [];
  const resolved: RunEvent[] = [];
  events.forEach((event) => {
    if (event.kind === "approval.resolved" && event.approval_id) {
      let resolvedIndex = -1;
      for (let index = pending.length - 1; index >= 0; index -= 1) {
        if (pending[index].approval_id === event.approval_id) {
          resolvedIndex = index;
          break;
        }
      }
      if (resolvedIndex >= 0) pending.splice(resolvedIndex, 1);
      resolved.push(event);
      return;
    }
    if (event.kind === "approval.requested") pending.push(event);
  });
  return { pending, resolved };
}

const SAFE_INTENT_PAYLOAD_KEYS = [
  "repair_action",
  "repair_kind",
  "failure_kind",
  "attempt",
  "status",
  "operation_kind",
  "decision",
];

function safeIntentValue(event: RunEvent, keys: string[]) {
  for (const key of keys) {
    if (RAW_TOOL_PAYLOAD_KEYS.has(key)) continue;
    const formatted = formatEventPayloadValue(event.payload[key]);
    if (formatted) return formatted;
  }
  return "";
}

function replaySafetyLabel(value: unknown) {
  if (value === false || value === "false") return "不可回放";
  if (value === true || value === "true") return "可回放";
  return "";
}

function repairAttemptLabel(event: RunEvent) {
  const attempt = safeIntentValue(event, ["attempt"]);
  const maxAttempts = safeIntentValue(event, ["max_attempts"]);
  if (attempt && maxAttempts) return `第 ${attempt}/${maxAttempts} 次`;
  return attempt ? `第 ${attempt} 次` : "";
}

function repairStatusLabel(event: RunEvent) {
  if (event.kind === "repair.started") return "修复已开始";
  if (event.kind === "repair.completed") return "修复已完成";
  if (event.kind === "repair.failed") return "修复未完成";
  return safeIntentValue(event, ["status"]) || "等待执行";
}

function isPayloadFlagTrue(value: unknown) {
  if (value === true || value === 1) return true;
  if (typeof value !== "string") return false;
  return ["1", "true", "yes", "y", "需要", "需要确认"].includes(value.trim().toLowerCase());
}

function hasPositiveIntentSignal(value: unknown) {
  if (value === false || value === null || typeof value === "undefined") return false;
  if (typeof value === "string") return !["", "0", "false", "no", "off"].includes(value.trim().toLowerCase());
  return typeof value === "number" ? value !== 0 : true;
}

function isRepairIntentEvent(event: RunEvent) {
  const kind = event.kind.toLowerCase();
  if (kind.includes("repair") || kind.includes("remediation")) return true;
  return ["repair_action", "repair_kind", "self_repair", "remediation_action"].some((key) =>
    hasPositiveIntentSignal(event.payload[key]),
  );
}

function eventIntentDetail(event: RunEvent, fallback: string) {
  return event.action || safeIntentValue(event, SAFE_INTENT_PAYLOAD_KEYS) || fallback;
}

function eventFailureStatus(event: RunEvent) {
  const candidates = [
    formatEventPayloadValue(event.payload.status_code),
    formatEventPayloadValue(event.payload.http_status),
    event.message,
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const statusMatch = candidate.match(/\bstatus=(\d{3})\b/i) ?? candidate.match(/\bhttp\s*(\d{3})\b/i);
    if (statusMatch?.[1]) return `status=${statusMatch[1]}`;
    if (/^\d{3}$/.test(candidate)) return `status=${candidate}`;
  }
  return "";
}

function eventModelName(event: RunEvent) {
  return (
    formatEventPayloadValue(event.payload.logical_model) ||
    formatEventPayloadValue(event.payload.model) ||
    formatEventPayloadValue(event.payload.upstream_model)
  );
}

function isModelFailureEvent(event: RunEvent) {
  const text = [
    event.message,
    formatEventPayloadValue(event.payload.failure_kind),
    formatEventPayloadValue(event.payload.provider),
    eventModelName(event),
  ]
    .join(" ")
    .toLowerCase();
  return (
    text.includes("model") ||
    text.includes("gateway") ||
    text.includes("transport") ||
    text.includes("provider") ||
    text.includes("litellm") ||
    Boolean(eventFailureStatus(event) && eventModelName(event))
  );
}

function pushUniqueDiagnostic(diagnostics: RunFailureDiagnostic[], diagnostic: RunFailureDiagnostic) {
  const key = `${diagnostic.label}:${diagnostic.title}:${diagnostic.detail}:${diagnostic.meta.join("|")}`;
  if (diagnostics.some((existing) => `${existing.label}:${existing.title}:${existing.detail}:${existing.meta.join("|")}` === key)) return;
  diagnostics.push(diagnostic);
}

function pendingApprovalDiagnostics(
  detail: RunDetail,
  agentNames: Map<string, string>,
): RunFailureDiagnostic[] {
  return approvalStateFromEvents(detail.events).pending.map((event) => ({
      id: `${detail.id}-diagnostic-approval-${event.approval_id ?? event.sequence}`,
      label: "等待人工确认" as const,
      title: event.action || "需要确认",
      detail: eventIntentDetail(event, "需要确认后继续"),
      recommendation: "处理审批或拒绝高风险动作，再继续执行。",
      meta: [
        event.approval_id ? `审批 ${event.approval_id}` : "",
        displayEventActor(event.actor, agentNames) ?? "",
        replaySafetyLabel(event.payload.replay_safe),
      ].filter(Boolean),
      tone: "approval" as const,
    }));
}

function failureDiagnosticsForRun(detail: RunDetail, agentNames: Map<string, string>): RunFailureDiagnostic[] {
  if (detail.failure_diagnostics.length > 0) {
    return detail.failure_diagnostics.map((diagnostic, index) =>
      runFailureDiagnosticFromApi(detail.id, diagnostic, index, agentNames),
    );
  }

  const diagnostics: RunFailureDiagnostic[] = [];

  detail.events.forEach((event) => {
    if (event.kind === "tool.failed") {
      const toolName = toolEventName(event);
      const failureKind = formatEventPayloadValue(event.payload.failure_kind);
      const exitCode = formatEventPayloadValue(event.payload.exit_code);
      const outputBytes = formatEventPayloadValue(event.payload.output_bytes);
      pushUniqueDiagnostic(diagnostics, {
        id: `${detail.id}-diagnostic-tool-${toolLifecycleKey(event)}`,
        label: "工具执行失败",
        title: toolName,
        detail: [
          failureKind ? `失败类型 ${failureKind}` : "工具调用未完成",
          exitCode ? `退出码 ${exitCode}` : "",
          outputBytes ? `输出 ${outputBytes} 字节` : "",
        ]
          .filter(Boolean)
          .join("；"),
        recommendation: "检查工具权限、参数和运行环境，再决定是否重试或改派。",
        meta: [
          displayEventActor(event.actor, agentNames) ?? "",
          event.step_id ? `步骤 ${event.step_id}` : "",
          `#${event.sequence}`,
        ].filter(Boolean),
        tone: "tool",
      });
      return;
    }

    if (!["runtime.failed", "step.failed"].includes(event.kind) || isWrappedToolFailureEvent(event, detail.events)) return;

    const actor = displayEventActor(event.actor, agentNames) ?? "运行时";
    const status = eventFailureStatus(event);
    const model = eventModelName(event);
    const failureKind = formatEventPayloadValue(event.payload.failure_kind);
    const label = isModelFailureEvent(event) ? "模型链路失败" : "运行阶段失败";
    pushUniqueDiagnostic(diagnostics, {
      id: `${detail.id}-diagnostic-${event.sequence}`,
      label,
      title: label === "模型链路失败" ? model || actor : actor,
      detail: [failureKind, status, model && label !== "模型链路失败" ? model : ""]
        .filter(Boolean)
        .join("；") || (label === "模型链路失败" ? "模型调用失败" : "运行失败，已记录安全摘要"),
      recommendation:
        label === "模型链路失败"
          ? "检查模型配置、API Key、上游状态码和限流，再重试或切换模型。"
          : "按失败阶段查看上下文，优先保留已有产物并缩小重试范围。",
      meta: [actor, event.step_id ? `步骤 ${event.step_id}` : "", `#${event.sequence}`].filter(Boolean),
      tone: label === "模型链路失败" ? "model" : "runtime",
    });
  });

  pendingApprovalDiagnostics(detail, agentNames).forEach((diagnostic) => pushUniqueDiagnostic(diagnostics, diagnostic));
  return diagnostics;
}

function runFailureDiagnosticFromApi(
  runId: string,
  diagnostic: RunDetail["failure_diagnostics"][number],
  index: number,
  agentNames: Map<string, string>,
): RunFailureDiagnostic {
  const label = diagnosticLabel(diagnostic.category);
  const actor = displayEventActor(diagnostic.actor, agentNames) ?? diagnostic.actor ?? "";
  const title =
    diagnostic.tool_name ||
    diagnostic.logical_model ||
    diagnostic.action ||
    actor ||
    label;
  const statusCode = diagnostic.status_code ? `status=${diagnostic.status_code}` : "";
  const reason = diagnosticDisplayReason(diagnostic);
  const detail = [
    diagnostic.failure_kind,
    statusCode,
    reason,
  ]
    .filter(Boolean)
    .filter((value, position, list) => list.indexOf(value) === position)
    .join("；");
  return {
    id: `${runId}-api-diagnostic-${diagnostic.sequence}-${index}`,
    label,
    title,
    detail: detail || "已记录结构化故障摘要",
    recommendation: diagnosticRecommendation(diagnostic.category, diagnostic.recommendation),
    meta: [
      actor,
      diagnostic.error_stage ? `位置 ${diagnostic.error_stage}` : "",
      diagnostic.error_code ? `错误码 ${diagnostic.error_code}` : "",
      typeof diagnostic.retryable === "boolean" ? `可重试 ${diagnostic.retryable ? "是" : "否"}` : "",
      diagnostic.error_category ? `类型 ${diagnostic.error_category}` : "",
      diagnostic.step_id ? `步骤 ${diagnostic.step_id}` : "",
      diagnostic.approval_id ? `审批 ${diagnostic.approval_id}` : "",
      diagnostic.wrapped_by ? `包装于 #${diagnostic.wrapped_by}` : "",
      `#${diagnostic.sequence}`,
    ].filter(Boolean),
    tone: diagnosticTone(diagnostic.category),
  };
}

function diagnosticLabel(category: string): RunFailureDiagnostic["label"] {
  if (category === "tool") return "工具执行失败";
  if (category === "model") return "模型链路失败";
  if (category === "approval") return "等待人工确认";
  return "运行阶段失败";
}

function diagnosticTone(category: string): RunFailureDiagnostic["tone"] {
  if (category === "tool") return "tool";
  if (category === "model") return "model";
  if (category === "approval") return "approval";
  return "runtime";
}

function diagnosticRecommendation(category: string, fallback: string) {
  if (category === "tool") return "检查工具权限、参数和运行环境，再决定是否重试或改派。";
  if (category === "model") return "检查模型配置、API Key、上游状态码和限流，再重试或切换模型。";
  if (category === "approval") return "处理审批或拒绝高风险动作，再继续执行。";
  return fallback || "按失败阶段查看上下文，优先保留已有产物并缩小重试范围。";
}

function pushUniqueIntent(intents: RunExecutionIntent[], intent: RunExecutionIntent) {
  const key = `${intent.label}:${intent.title}:${intent.detail}:${intent.meta.join("|")}`;
  if (intents.some((existing) => `${existing.label}:${existing.title}:${existing.detail}:${existing.meta.join("|")}` === key)) return;
  intents.push(intent);
}

function executionIntentsForRun(detail: RunDetail, agentNames: Map<string, string>): RunExecutionIntent[] {
  const intents: RunExecutionIntent[] = [];
  const toolGroups = new Map<string, RunEvent[]>();
  detail.events.forEach((event) => {
    if (!isToolEvent(event)) return;
    const key = toolLifecycleKey(event);
    toolGroups.set(key, [...(toolGroups.get(key) ?? []), event]);
  });

  toolGroups.forEach((events, key) => {
    const finalEvent = events.at(-1);
    if (!finalEvent) return;
    const replaySafe = events.map((event) => event.payload.replay_safe).find((value) => value !== null && typeof value !== "undefined");
    const replayLabel = replaySafetyLabel(replaySafe);
    if (!replayLabel) return;
    pushUniqueIntent(intents, {
      id: `${detail.id}-intent-replay-${key}`,
      label: "回放意图",
      title: replayLabel,
      detail: toolEventName(finalEvent),
      meta: [toolOperationLabel(toolEventName(finalEvent)), toolLifecycleStatusText(finalEvent)].filter(Boolean),
      tone: replayLabel === "不可回放" ? "replay" : "done",
    });
  });

  const approvalState = approvalStateFromEvents(detail.events);
  approvalState.pending.forEach((event) => {
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-approval-${event.approval_id ?? event.sequence}`,
        label: "审批意图",
        title: "等待确认",
        detail: eventIntentDetail(event, "需要确认后继续"),
        meta: [
          event.approval_id ? `审批 ${event.approval_id}` : "",
          replaySafetyLabel(event.payload.replay_safe),
          isPayloadFlagTrue(event.payload.requires_approval) ? "需要确认" : "",
        ].filter(Boolean),
        tone: "pending",
      });
  });
  approvalState.resolved.forEach((event) => {
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-approval-resolved-${event.approval_id ?? event.sequence}`,
        label: "审批意图",
        title: "确认已处理",
        detail: event.decision || safeIntentValue(event, ["decision", "status"]) || "已处理",
        meta: [event.approval_id ? `审批 ${event.approval_id}` : ""].filter(Boolean),
        tone: "done",
      });
  });

  detail.events.forEach((event) => {
    if (event.kind.startsWith("approval.")) {
      return;
    }
    if (event.kind === "step.retrying") {
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-retry-${event.step_id ?? event.sequence}`,
        label: "重试意图",
        title: "准备重试",
        detail: eventIntentDetail(event, "失败后重试"),
        meta: [
          safeIntentValue(event, ["attempt"]) ? `第 ${safeIntentValue(event, ["attempt"])} 次` : "",
          safeIntentValue(event, ["failure_kind", "status"]),
          replaySafetyLabel(event.payload.replay_safe),
        ].filter(Boolean),
        tone: "retry",
      });
      return;
    }
    if (isRepairIntentEvent(event)) {
      const repairAction = safeIntentValue(event, ["repair_action", "repair_kind", "remediation_action"]) || event.action || "修复方案";
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-repair-${event.step_id ?? event.sequence}`,
        label: "修复意图",
        title: repairAction,
        detail: event.action && event.action !== repairAction ? event.action : repairStatusLabel(event),
        meta: [
          repairAttemptLabel(event),
          isPayloadFlagTrue(event.payload.requires_approval) ? "需要确认" : "",
          safeIntentValue(event, ["failure_kind"]),
          replaySafetyLabel(event.payload.replay_safe),
          displayEventParticipants(event.participants, agentNames) ?? "",
        ].filter(Boolean),
        tone: "repair",
      });
    }
  });
  return intents;
}

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

function scheduleApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.schedule_proposal) return null;
  return {
    runId: run.id,
    proposal: run.schedule_proposal,
    createdScheduleId: null,
  };
}

function evolutionApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.evolution_proposal) return null;
  return {
    runId: run.id,
    proposal: run.evolution_proposal,
    createdEvolutionId: null,
  };
}

function openClawApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.openclaw_proposal) return null;
  return {
    runId: run.id,
    proposal: run.openclaw_proposal,
    createdOperationId: null,
  };
}

function repairApprovalFromSubmittedRun(run: SubmittedRun) {
  if (run.status !== "failed" || !run.decision_token || !run.repair_proposal) return null;
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: run.version,
    proposal: run.repair_proposal,
  };
}

function repairApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "failed" || !run.decision_token || !run.repair_proposal) return null;
  const parsedVersion = Number(run.explicit_details.version ?? "0");
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: Number.isInteger(parsedVersion) && parsedVersion > 0 ? parsedVersion : 0,
    proposal: run.repair_proposal,
  };
}

function repairProposalBody(proposal: RepairProposal) {
  return [
    proposal.summary,
    `失败类型：${proposal.failure_kind}`,
    `修复动作：${proposal.repair_action}`,
    `修复次数：第 ${proposal.attempt}/${proposal.max_attempts} 次`,
    proposal.instruction ? `受控指令：${proposal.instruction}` : "",
    proposal.automatic_execution
      ? "该修复提案标记为自动执行。"
      : "不会自动执行；只有确认后才会重新排队一次。",
  ].filter(Boolean).join("\n\n");
}

function openClawProposalBody(proposal: OpenClawProposal) {
  return [
    proposal.summary,
    `操作类型：${proposal.kind}`,
    `目标平台：${proposal.platform}`,
    `目标范围：${proposal.target_type} / ${proposal.target}`,
    `请求内容：${proposal.operation_text}`,
    "系统不会在对话页直接执行。请到 OpenClaw 管理页确认目标、权限、审批策略和执行边界。",
  ].join("\n\n");
}
function evolutionProposalBody(proposal: EvolutionProposal) {
  const skills = proposal.source_skill_ids.length > 0 ? proposal.source_skill_ids.join("、") : "由主 Agent 在确认后补齐";
  const candidates = proposal.candidate_agent_ids.length > 0 ? proposal.candidate_agent_ids.join("、") : "由主 Agent 调度";
  return [
    proposal.summary,
    `任务目标：${proposal.objective}`,
    `任务类型：${proposal.kind}`,
    `来源 Skill：${skills}`,
    `基准 agent：${proposal.baseline_agent_id ?? "主 Agent 判断"}`,
    `候选 agent：${candidates}`,
    `评测 agent：${proposal.evaluator_agent_id ?? "主 Agent 判断"}`,
    `迭代策略：${proposal.iteration_policy}；记忆策略：${proposal.memory_policy}`,
  ].join("\n\n");
}

function evolutionProposalCreatePayload(proposal: EvolutionProposal) {
  return {
    kind: proposal.kind,
    title: proposal.title,
    objective: proposal.objective,
    mode: proposal.mode,
    source_skill_ids: proposal.source_skill_ids,
    source_conversation_id: proposal.source_conversation_id ?? null,
    source_run_id: proposal.source_run_id ?? null,
    target_artifact_type: proposal.target_artifact_type,
    baseline_agent_id: proposal.baseline_agent_id ?? null,
    candidate_agent_ids: proposal.candidate_agent_ids,
    evaluator_agent_id: proposal.evaluator_agent_id ?? null,
    approval_policy: proposal.approval_policy,
    iteration_policy: proposal.iteration_policy,
    memory_policy: proposal.memory_policy,
    max_rounds: proposal.max_rounds,
    min_delta: proposal.min_delta,
    budget_tokens: proposal.budget_tokens,
    budget_minutes: proposal.budget_minutes,
    rubric: proposal.rubric,
  };
}

function scheduleProposalBody(proposal: ScheduleProposal) {
  return [
    "主 Agent 判断这条消息更像计划任务。请先确认计划，再加入日程；加入后由系统计划任务按时间提交普通运行。",
    `执行安排：${proposal.summary}`,
    `执行模式：${displayMode(proposal.mode)}`,
    `工作流：${proposal.workflow_id}`,
    `任务内容：${proposal.message}`,
  ].join("\n\n");
}

function scheduleProposalCreatePayload(proposal: ScheduleProposal) {
  return {
    name: proposal.name,
    message: proposal.message,
    mode: proposal.mode,
    workflow_id: proposal.workflow_id,
    kind: proposal.kind,
    run_at: proposal.run_at ?? null,
    cron: proposal.cron ?? null,
    timezone: proposal.timezone,
    misfire_policy: proposal.misfire_policy,
    budget: proposal.budget,
    metadata: proposal.metadata,
  };
}
function temporaryAgentApprovalBody(proposal: TemporaryAgentProposal) {
  const model = proposal.model ? `模型：${proposal.model}` : "模型：主 Agent 自动选择";
  return [
    `主 Agent 建议临时加入子 Agent：补齐 ${proposal.missing_capability} 能力。`,
    `职责：${proposal.role}；${model}。`,
    "回复：1 同意临时加入；2 不加入；3 给修改意见；4 保存为永久 Agent（需先同意并运行过）。",
  ].join("\n\n");
}

function temporaryAgentDetailRows(proposal: TemporaryAgentProposal) {
  const skills =
    proposal.suggested_skills.length > 0 ? proposal.suggested_skills.join("、") : "无";
  return [
    { label: "Agent ID", value: proposal.id },
    { label: "名称", value: proposal.name },
    { label: "职责", value: proposal.role },
    { label: "缺少能力", value: proposal.missing_capability },
    { label: "加入原因", value: proposal.reason },
    { label: "角色边界", value: proposal.prompt },
    { label: "建议 Skill", value: skills },
    { label: "可保存为永久 Agent", value: proposal.permanentizable ? "是" : "否" },
  ];
}

function TemporaryAgentApprovalMessage({ proposal }: { proposal: TemporaryAgentProposal }) {
  const [detailOpen, setDetailOpen] = useState(false);
  return (
    <>
      <MessageBody text={temporaryAgentApprovalBody(proposal)} title={proposal.name} />
      <div className="temporary-agent-summary-card">
        <button
          type="button"
          className="secondary-action"
          aria-expanded={detailOpen}
          onClick={() => setDetailOpen((open) => !open)}
        >
          {detailOpen ? "收起临时 Agent 详情" : "展开临时 Agent 详情"}
        </button>
        {detailOpen ? (
          <dl className="temporary-agent-detail-list" aria-label="临时 Agent 详情">
            {temporaryAgentDetailRows(proposal).map((row) => (
              <div key={row.label}>
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
    </>
  );
}

function detailMessages(detail: RunDetail | undefined): ChatMessage[] {
  if (!detail) return [];
  const textArtifacts = dedupeTextArtifacts(detail.artifacts);
  const replyArtifact = preferredReplyArtifact(textArtifacts);
  const internalNotice = internalArtifactNotice(detail);
  const failureReason = failureSummaryForChat(detail);
  const downloadableArtifacts = detail.artifacts.filter(isFinalDownloadableArtifact);
  const artifactMessages = replyArtifact
    ? [
        {
          id: `artifact-${replyArtifact.id}`,
          role: "assistant" as const,
          title: "回复",
          body:
            textArtifacts.length > 1
              ? `${replyArtifact.text?.trim() ?? ""}\n\n（另有 ${
                  textArtifacts.length - 1
                } 条角色产物，可在对应 Agent 动作卡片中展开查看。）`
              : replyArtifact.text?.trim() ?? "",
          artifact: isFinalDownloadableArtifact(replyArtifact) ? replyArtifact : undefined,
        },
        ...downloadableArtifacts
          .filter((artifact) => artifact.id !== replyArtifact.id)
          .map(downloadArtifactMessage),
      ]
    : detail.artifacts
        .filter((artifact) => !artifact.text?.trim())
        .map(artifactMessage);
  const failureMessages: ChatMessage[] =
    detail.status === "failed"
      ? [
          {
            id: "failed",
            role: "assistant",
            title: artifactMessages.length > 0 ? "运行中断" : "运行失败",
            body:
              artifactMessages.length > 0
                ? `中断前输出已保留。错误原因：${failureReason ?? "后端没有记录具体失败原因，请展开对应 Agent 动作或到日志中心排查。"}`
                : `本次运行没有生成最终回复。错误原因：${
                    failureReason ?? "后端没有记录具体失败原因，请展开执行摘要或到日志中心查看。"
                  }`,
          },
        ]
      : [];
  return [
    {
      id: "request",
      role: "user" as const,
      title: "你",
      body: detail.request,
    },
    ...(detail.status === "waiting_approval" && detail.temporary_agent_proposal
      ? [
          {
            id: `${detail.id}-temporary-agent-approval`,
            role: "assistant" as const,
            title: detail.temporary_agent_proposal.name,
            body: temporaryAgentApprovalBody(detail.temporary_agent_proposal),
            temporaryAgent: detail.temporary_agent_proposal,
          },
        ]
      : []),
    ...(detail.status === "waiting_approval" && detail.schedule_proposal
      ? [
          {
            id: `${detail.id}-schedule-approval`,
            role: "assistant" as const,
            title: "计划任务确认",
            body: scheduleProposalBody(detail.schedule_proposal),
          },
        ]
      : []),
    ...(detail.status === "waiting_approval" && detail.evolution_proposal
      ? [
          {
            id: `${detail.id}-evolution-approval`,
            role: "assistant" as const,
            title: "进化任务确认",
            body: evolutionProposalBody(detail.evolution_proposal),
          },
        ]
      : []),
    ...(detail.status === "waiting_approval" && detail.openclaw_proposal
      ? [
          {
            id: `${detail.id}-openclaw-approval`,
            role: "assistant" as const,
            title: "OpenClaw 操作确认",
            body: openClawProposalBody(detail.openclaw_proposal),
          },
        ]
      : []),
    ...(detail.status === "failed" && detail.repair_proposal
      ? [
          {
            id: `${detail.id}-repair-approval`,
            role: "assistant" as const,
            title: detail.repair_proposal.title,
            body: repairProposalBody(detail.repair_proposal),
          },
        ]
      : []),
    ...(internalNotice ? [internalNotice] : []),
    ...artifactMessages,
    ...failureMessages,
  ];
}

function failureSummaryForChat(detail: RunDetail) {
  const diagnostic = detail.failure_diagnostics[0];
  if (diagnostic) {
    const parts = [
      `原因：${diagnosticChatReason(diagnostic)}`,
      diagnostic.error_code ? `错误码：${diagnostic.error_code}` : null,
      diagnostic.error_stage ? `位置：${diagnostic.error_stage}` : null,
      typeof diagnostic.retryable === "boolean" ? `可重试：${diagnostic.retryable ? "是" : "否"}` : null,
      diagnostic.recommendation ? `建议：${diagnostic.recommendation}` : null,
    ].filter(Boolean);
    return parts.join("\n");
  }
  return failureReasonFromEvents(detail.events);
}

function diagnosticChatReason(diagnostic: RunDetail["failure_diagnostics"][number]) {
  return diagnosticDisplayReason(diagnostic);
}

function diagnosticDisplayReason(diagnostic: RunDetail["failure_diagnostics"][number]) {
  if (diagnostic.error_code === "model.empty_response" || diagnostic.error_category === "empty_response") {
    return "模型返回了空内容";
  }
  return diagnostic.reason;
}

function failureReasonFromEvents(events: RunDetail["events"]) {
  const event = [...events]
    .sort((left, right) => right.sequence - left.sequence)
    .find((item) => ["runtime.failed", "step.failed", "tool.failed"].includes(item.kind) && item.message);
  if (!event) return null;
  const toolFailure = latestToolFailureEvent(events);
  if (event.kind !== "tool.failed" && toolFailure && isWrappedToolFailureEvent(event, events)) {
    return toolFailureSummary(toolFailure);
  }
  if (event.kind !== "tool.failed") return event.message ?? null;
  return toolFailureSummary(event);
}

function latestToolFailureEvent(events: RunDetail["events"]) {
  return [...events]
    .filter((event) => event.kind === "tool.failed")
    .sort((left, right) => right.sequence - left.sequence)
    .at(0);
}

function isWrappedToolFailureEvent(event: RunDetail["events"][number], events: RunDetail["events"]) {
  if (event.kind !== "runtime.failed" && event.kind !== "step.failed") return false;
  if (isModelFailureEvent(event)) return false;
  return events.some((candidate) => {
    if (candidate.kind !== "tool.failed" || candidate.sequence > event.sequence) return false;
    if (candidate.step_id && event.step_id && candidate.step_id === event.step_id) return true;
    return !events.some((between) => between.sequence > candidate.sequence && between.sequence < event.sequence && isActionEvent(between));
  });
}

function toolFailureSummary(event: RunDetail["events"][number]) {
  const toolName = toolEventName(event);
  const failureKind = formatEventPayloadValue(event.payload.failure_kind);
  const exitCode = formatEventPayloadValue(event.payload.exit_code);
  const outputBytes = formatEventPayloadValue(event.payload.output_bytes);
  return [
    `${toolOperationLabel(toolName)}失败：${toolName}`,
    failureKind ? `失败类型 ${failureKind}` : "",
    exitCode ? `退出码 ${exitCode}` : "",
    outputBytes ? `输出 ${outputBytes} 字节` : "",
    "原始命令和输出已隐藏，可在运行过程查看安全摘要。",
  ]
    .filter(Boolean)
    .join("；");
}

function dedupeTextArtifacts(artifacts: RunDetail["artifacts"]) {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    const text = artifact.text?.trim();
    if (!text || isGenericArtifactText(text) || seen.has(text)) return false;
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

function normalizeConversationQuestion(value: string | undefined, fallback: string) {
  const normalized = (value ?? "").replace(/\s+/g, " ").trim();
  if (!normalized) return fallback;
  return normalized.length > 32 ? `${normalized.slice(0, 31)}...` : normalized;
}

function conversationTimestamp(value: string | null | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => part.toString().padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function conversationTitle(run: RunListItem, items: RunListItem[]) {
  const fallback = run.id.slice(0, 8);
  const conversationKey = run.conversation_id?.trim();
  const sameConversation = conversationKey ? items.filter((item) => item.conversation_id === conversationKey) : [];
  const firstRun = sameConversation.length > 0 ? sameConversation.at(-1) : run;
  const question = normalizeConversationQuestion(firstRun?.request, fallback);
  const timestamp = conversationTimestamp(firstRun?.created_at);
  return timestamp ? `${question} · ${timestamp}` : question;
}

function conversationMessages(runs: RunDetail[]): ChatMessage[] {
  return runs.flatMap((run) =>
    detailMessages(run).map((message) => ({
      ...message,
      id: `${run.id}-${message.id}`,
      run,
    })),
  );
}

function sameRunSnapshot(left: RunDetail, right: RunDetail) {
  return runSnapshotSignature(left) === runSnapshotSignature(right);
}

function runSnapshotSignature(run: RunDetail) {
  const lastArtifact = run.artifacts.at(-1);
  return JSON.stringify({
    id: run.id,
    status: run.status,
    mode: run.mode,
    request: run.request,
    decision_token: run.decision_token,
    explicit_details: {
      conversation_id: run.explicit_details.conversation_id,
      version: run.explicit_details.version,
      harness_provider: run.explicit_details.harness_provider,
      harness_logical_model: run.explicit_details.harness_logical_model,
      harness_capabilities: run.explicit_details.harness_capabilities,
    },
    temporary_agent_proposal: run.temporary_agent_proposal,
    schedule_proposal: run.schedule_proposal,
    evolution_proposal: run.evolution_proposal,
    openclaw_proposal: run.openclaw_proposal,
    repair_proposal: run.repair_proposal,
    failure_diagnostics: run.failure_diagnostics,
    tool_lifecycle: run.tool_lifecycle,
    events: run.events.map((event) => ({
      sequence: event.sequence,
      kind: event.kind,
      message: event.message,
      summary: event.summary,
      created_at: event.created_at,
      actor: event.actor,
      participants: event.participants,
      step_id: event.step_id,
      tool_name: event.tool_name,
      tool_call_id: event.tool_call_id,
      action: event.action,
      decision: event.decision,
      payload: event.payload,
      artifact: event.artifact,
    })),
    artifacts: run.artifacts.length,
    last_artifact: lastArtifact
      ? {
          id: lastArtifact.id,
          kind: lastArtifact.kind,
          title: lastArtifact.title,
          filename: lastArtifact.filename,
          mime_type: lastArtifact.mime_type,
          size_bytes: lastArtifact.size_bytes,
          sha256: lastArtifact.sha256,
          download_url: lastArtifact.download_url,
          text: lastArtifact.text,
        }
      : null,
  });
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

function internalArtifactNotice(detail: RunDetail): ChatMessage | null {
  const textArtifacts = dedupeTextArtifacts(detail.artifacts);
  if (textArtifacts.length === 0) return null;
  if (preferredReplyArtifact(textArtifacts)) return null;
  return {
    id: "internal-artifacts",
    role: "assistant",
    title: "回复待生成",
    body: "这轮只生成了内部审查或裁决内容，没有生成可直接交付给你的正式回复。可展开对应 Agent 动作查看来源，或继续补充要求让主 Agent 重新生成。",
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
    detail.explicit_details.harness_provider
      ? { label: "Harness 服务商", value: detail.explicit_details.harness_provider }
      : null,
    detail.explicit_details.harness_model
      ? { label: "Harness 模型", value: detail.explicit_details.harness_model }
      : null,
    detail.explicit_details.harness_logical_model
      ? { label: "逻辑模型", value: detail.explicit_details.harness_logical_model }
      : null,
    detail.explicit_details.harness_requires_approval
      ? {
          label: "审批要求",
          value: detail.explicit_details.harness_requires_approval === "true" ? "需要审批" : "无需审批",
        }
      : null,
    detail.explicit_details.harness_capabilities
      ? { label: "工程能力", value: detail.explicit_details.harness_capabilities }
      : null,
    detail.explicit_details.harness_policy
      ? { label: "策略原因", value: detail.explicit_details.harness_policy }
      : null,
    detail.explicit_details.harness_context
      ? { label: "上下文信号", value: detail.explicit_details.harness_context }
      : null,
    detail.explicit_details.harness_fallbacks
      ? { label: "备选路径", value: detail.explicit_details.harness_fallbacks }
      : null,
  ].filter((item): item is { label: string; value: string } => Boolean(item));
}

function eventArtifactText(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  const text = artifact?.text?.trim() || "";
  return isGenericArtifactText(text) ? "" : text;
}

function eventArtifactRows(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  const rows: Array<{ label: string; value: string }> = [];
  if (!artifact) return rows;
  if (artifact.title) rows.push({ label: "产物标题", value: artifact.title });
  if (artifact.kind) rows.push({ label: "产物类型", value: artifact.kind });
  if (artifact.filename) rows.push({ label: "文件名", value: artifact.filename });
  if (artifact.mime_type) rows.push({ label: "文件类型", value: artifact.mime_type });
  const size = formatFileSize(artifact.size_bytes);
  if (size) rows.push({ label: "文件大小", value: size });
  if (artifact.sha256) rows.push({ label: "SHA-256", value: artifact.sha256 });
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
  const canUseOrderedFallback = Boolean(event.actor || event.step_id || event.tool_name);
  const byOrder = canUseOrderedFallback ? artifacts.find((artifact) => !consumedArtifactIds.has(artifact.id)) : null;
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
    [
      formatEventPayloadValue(event.payload.result),
      formatEventPayloadValue(event.payload.output),
      formatEventPayloadValue(event.payload.summary),
      eventArtifactText(artifact),
      artifact?.title ?? "",
      readableMessage,
    ]
      .map((item) => item.trim())
      .find((item) => item && !isGenericArtifactText(item)) ?? ""
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

function discussionConsensusSignal(event: RunEvent) {
  return (
    formatEventPayloadValue(event.payload.conclusion) ||
    formatEventPayloadValue(event.payload.result) ||
    formatEventPayloadValue(event.payload.discussion) ||
    formatEventPayloadValue(event.payload.opinions) ||
    formatEventPayloadValue(event.payload.summary) ||
    ""
  );
}

function discussionDisagreementSignal(event: RunEvent) {
  return (
    formatEventPayloadValue(event.payload.disagreement) ||
    formatEventPayloadValue(event.payload.conflict) ||
    formatEventPayloadValue(event.payload.risks) ||
    formatEventPayloadValue(event.payload.concerns)
  );
}

function discussionMinutesSummary(event: RunEvent, agentNames: Map<string, string>) {
  const consensus = discussionConsensusSignal(event);
  const disagreement = discussionDisagreementSignal(event);
  const judgement = eventDecisionSignal(event);
  const participants = displayEventParticipants(event.participants, agentNames) ?? displayPayloadParticipants(event.payload, agentNames);
  const conciseDiscussionText = (value: string, fallback: string) => conciseProcessText(value, fallback).replace(/[。.!?！？]+$/u, "");
  const parts = [
    consensus ? `共识 ${conciseDiscussionText(consensus, "已形成阶段共识")}` : "",
    disagreement ? `分歧 ${conciseDiscussionText(disagreement, "存在待裁决分歧")}` : "",
    !consensus && judgement ? `结论 ${conciseDiscussionText(judgement, "已完成裁决")}` : "",
  ].filter(Boolean);
  if (parts.length > 0) return `讨论纪要：${parts.join("；")}`;
  return `讨论纪要：${participants || "多角色"}已完成讨论`;
}

function discussionMinutesRows(event: RunEvent, agentNames: Map<string, string>) {
  const rows: Array<{ label: string; value: string }> = [];
  const consensus = discussionConsensusSignal(event);
  const disagreement = discussionDisagreementSignal(event);
  const judgement = eventDecisionSignal(event);
  const minutes = [
    consensus ? `共识：${consensus}` : "",
    disagreement ? `分歧：${disagreement}` : "",
    judgement ? `结论：${judgement}` : "",
  ].filter(Boolean);
  if (minutes.length > 0) rows.push({ label: "会议纪要", value: minutes.join("；") });
  eventOpinionEntries(event, agentNames).forEach((opinion) => {
    rows.push({ label: `${opinion.actor}意见`, value: opinion.value });
  });
  if (judgement) rows.push({ label: "主 Agent 裁决", value: judgement });
  return rows;
}

function eventSummaryText(
  event: RunDetail["events"][number],
  agentNames: Map<string, string>,
  artifact?: RunArtifact | NonNullable<RunEvent["artifact"]> | null,
) {
  const safeSummary = eventSafeSummary(event);
  if (safeSummary) return conciseProcessText(safeSummary, "记录了一步过程");
  const actor = displayEventActor(event.actor, agentNames);
  const participants = displayEventParticipants(event.participants, agentNames) ?? displayPayloadParticipants(event.payload, agentNames);
  const readableMessage =
    event.message && event.message !== event.kind && !/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message)
      ? event.message
      : "";
  const instructionSignal = eventInstructionSignal(event);
  const outputSignal = eventOutputSignal(event, artifact);
  const discussionSignal = discussionConsensusSignal(event) || readableMessage;
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
  if (event.kind === "model.reasoning_delta") {
    return "思考过程：模型正在分析";
  }
  if (event.kind === "model.text_delta") {
    return "输出进度：模型正在生成";
  }
  if (event.kind === "harness.started") {
    const logicalModel = formatEventPayloadValue(event.payload.logical_model) || "harness";
    const provider = formatEventPayloadValue(event.payload.provider);
    return `Harness 启动：${conciseProcessText(logicalModel, "模型")}${provider ? ` / ${provider}` : ""}`;
  }
  if (event.kind === "tool.requested") {
    const requestedTool = formatEventPayloadValue(event.payload.name) || event.tool_name || "工具";
    return `工具请求：${conciseProcessText(requestedTool, "工具")}`;
  }
  if (event.kind === "tool.started") {
    const toolName = toolEventName(event);
    return `${subject} ${toolOperationLabel(toolName)}：${toolName}`;
  }
  if (event.kind === "tool.completed") {
    const toolName = toolEventName(event);
    return `${subject} ${toolOperationLabel(toolName)}完成：${toolName}`;
  }
  if (event.kind === "tool.failed") {
    const toolName = toolEventName(event);
    return `${subject} ${toolOperationLabel(toolName)}失败：${toolName}`;
  }
  if (event.kind === "step.started") {
    return `${subject} 接收任务：${conciseProcessText(instructionSignal, "开始执行")}`;
  }
  if (event.kind === "artifact.created") {
    const producer = actor || artifact?.title || subject;
    return `${producer} 输出：${conciseProcessText(outputSignal || readableMessage, "阶段结果")}`;
  }
  if (["step.completed", "message.created", "review.completed"].includes(event.kind)) {
    return `${subject} 输出：${conciseProcessText(outputSignal || readableMessage, "完成阶段输出")}`;
  }
  if (event.kind === "discussion.started") {
    return `${participants || "多角色"} 开始讨论`;
  }
  if (event.kind === "discussion.completed") {
    return discussionMinutesSummary(event, agentNames);
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
  if (event.kind === "step.failed" || event.kind === "runtime.failed") {
    return `${subject} 失败：${conciseProcessText(readableMessage || outputSignal, "执行失败")}`;
  }
  if (event.kind === "approval.requested") {
    return `等待确认：${conciseProcessText(event.action || safeIntentValue(event, ["operation_kind", "status"]), "需要你确认后继续")}`;
  }
  if (event.kind === "step.retrying") {
    const retrySignal = event.action || safeIntentValue(event, ["attempt", "failure_kind", "status"]) || "失败后重试";
    return `${subject} 重试：${conciseProcessText(retrySignal, "失败后重试")}`;
  }
  if (isRepairIntentEvent(event)) {
    const repairSignal = safeIntentValue(event, ["repair_action", "repair_kind", "failure_kind", "status"]) || event.action || "准备修复";
    const status = repairStatusLabel(event);
    return `修复意图：${conciseProcessText(`${repairSignal} ${status}`, "准备修复")}`;
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
  const upstreamModel = formatEventPayloadValue(event.payload.upstream_model);
  const provider = formatEventPayloadValue(event.payload.provider);
  const deployment = formatEventPayloadValue(event.payload.deployment);
  if (upstreamModel && upstreamModel !== eventModel) rows.push({ label: "上游模型", value: upstreamModel });
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

function processBadgeForEvent(event: RunEvent) {
  if (event.kind.startsWith("approval.")) return "审批意图";
  if (event.kind === "step.retrying") return "重试意图";
  if (isRepairIntentEvent(event)) return "修复意图";
  if (event.kind === "artifact.created" || event.kind === "message.created" || event.kind === "step.completed") {
    return "中间产物";
  }
  if (event.kind === "review.completed" || event.kind.startsWith("decision.")) return "裁决过程";
  if (event.kind.startsWith("discussion.")) return "讨论过程";
  if (event.kind === "model.reasoning_delta") return "思考过程";
  if (event.kind === "model.text_delta") return "输出进度";
  if (event.kind === "tool.started" || event.kind === "tool.completed" || event.kind === "tool.failed") {
    return toolOperationLabel(toolEventName(event));
  }
  if (event.kind.startsWith("model.")) return "模型调用";
  if (event.kind.startsWith("tool.")) return "工具过程";
  if (event.kind.startsWith("harness.")) return "Harness";
  if (event.kind.startsWith("dispatch.")) return "调度过程";
  if (event.kind === "step.started") return "任务分解";
  return "过程记录";
}

function toolLifecycleKey(event: RunEvent) {
  return event.tool_call_id || formatEventPayloadValue(event.payload.id) || `${event.kind}:${toolEventName(event)}:${event.sequence}`;
}

function toolLifecycleStatusText(event: RunEvent) {
  if (event.kind === "tool.requested") return "请求";
  if (event.kind === "tool.started") return "开始";
  if (event.kind === "tool.completed") return "完成";
  if (event.kind === "tool.failed") return "失败";
  return "记录";
}

function durationBetween(first: string | null | undefined, last: string | null | undefined) {
  if (!first || !last) return "";
  const start = Date.parse(first);
  const end = Date.parse(last);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const milliseconds = end - start;
  if (milliseconds < 1000) return `${milliseconds}ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)}s`;
}

function modelDeltaActivityLabel(event: RunEvent) {
  return event.kind === "model.reasoning_delta" ? "模型正在分析" : "模型正在生成";
}

function modelDeltaSummaryText(events: RunEvent[]) {
  const lastEvent = events.at(-1);
  if (!lastEvent) return "模型流式进度已记录";
  return modelDeltaActivityLabel(lastEvent);
}

function modelDeltaPhaseLabel(events: RunEvent[]) {
  const phases = [
    ...new Set(
      events
        .map((event) => formatEventPayloadValue(event.payload.phase))
        .filter((value) => value.trim().length > 0),
    ),
  ];
  return phases.join("、");
}

function processItemsForModelDeltaGroup(
  detail: RunDetail,
  events: RunEvent[],
  index: number,
  agentNames: Map<string, string>,
): ProcessDetailTarget[] {
  const lastEvent = events.at(-1);
  if (!lastEvent) return [];
  const totalBytes = events.reduce((total, event) => total + numericPayloadValue(event, "text_bytes"), 0);
  const duration = durationBetween(events[0]?.created_at, lastEvent.created_at);
  const phase = modelDeltaPhaseLabel(events);
  const actor = displayEventActor(lastEvent.actor, agentNames);
  const deltaKind = formatEventPayloadValue(lastEvent.payload.delta_kind);
  const rows = [
    ...modelRowsForEvent(lastEvent, detail.events, agentNames),
    actor ? { label: "执行者", value: actor } : null,
    lastEvent.step_id ? { label: "步骤", value: lastEvent.step_id } : null,
    deltaKind ? { label: "Delta 类型", value: deltaKind } : null,
    { label: "分片数", value: String(events.length) },
    totalBytes > 0 ? { label: "内容字节数", value: String(totalBytes) } : null,
    duration ? { label: "耗时", value: duration } : null,
    phase ? { label: "阶段", value: phase } : null,
    { label: "事件范围", value: `#${events[0]?.sequence}-${lastEvent.sequence}` },
  ].filter((row): row is { label: string; value: string } => Boolean(row));
  return [
    {
      id: `${detail.id}-model-delta-${modelDeltaGroupKey(lastEvent)}-${index}`,
      runId: detail.id,
      conversationId: runConversationId(detail),
      title: displayEventTitle(lastEvent, agentNames),
      message: `${displayEventTitle(lastEvent, agentNames)}：${modelDeltaSummaryText(events)}`,
      badge: processBadgeForEvent(lastEvent),
      rows,
      createdAt: lastEvent.created_at,
      sourceKind: lastEvent.kind,
      sourceStepId: lastEvent.step_id,
      sourceActor: lastEvent.actor,
    },
  ];
}

function safeLifecyclePayloadRows(events: RunEvent[], existingLabels: Set<string>) {
  const rows: Array<{ label: string; value: string }> = [];
  const safeKeys = [
    "argument_keys",
    "argument_key_count",
    "redacted_argument_key_count",
    "argument_bytes",
    "command_bytes",
    "operation_kind",
    "sandbox",
    "replay_safe",
    "status",
    "exit_code",
    "stdout_bytes",
    "stderr_bytes",
    "output_bytes",
    "result_bytes",
    "failure_kind",
    "artifact_id",
  ];
  events.forEach((event) => {
    safeKeys.forEach((key) => {
      if (RAW_TOOL_PAYLOAD_KEYS.has(key)) return;
      const value = event.payload[key];
      const formatted = formatEventPayloadValue(value);
      const label = eventPayloadLabel(key);
      if (!formatted || existingLabels.has(label)) return;
      existingLabels.add(label);
      rows.push({ label, value: formatted });
    });
  });
  return rows;
}

function processItemsForToolLifecycle(
  detail: RunDetail,
  events: RunEvent[],
  index: number,
  agentNames: Map<string, string>,
  artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null,
): ProcessDetailTarget[] {
  const lastEvent = events.at(-1);
  if (!lastEvent) return [];
  const firstEvent = events[0];
  const statusFlow = events.map(toolLifecycleStatusText).filter((value, position, list) => position === 0 || value !== list[position - 1]);
  const lifecycleRows = [
    { label: "状态流", value: statusFlow.join("，") },
    durationBetween(firstEvent.created_at, lastEvent.created_at)
      ? { label: "耗时", value: durationBetween(firstEvent.created_at, lastEvent.created_at) }
      : null,
  ].filter((row): row is { label: string; value: string } => Boolean(row));
  const baseRows = [
    ...modelRowsForEvent(lastEvent, detail.events, agentNames),
    ...eventDetailRows(lastEvent, agentNames),
    ...eventArtifactRows(artifact),
  ];
  const labels = new Set([...lifecycleRows, ...baseRows].map((row) => row.label));
  const rows = [...lifecycleRows, ...baseRows, ...safeLifecyclePayloadRows(events, labels)];
  return [
    {
      id: `${detail.id}-tool-${toolLifecycleKey(lastEvent)}-${index}`,
      runId: detail.id,
      conversationId: runConversationId(detail),
      title: displayEventTitle(lastEvent, agentNames),
      message: eventSummaryText(lastEvent, agentNames, artifact),
      badge: processBadgeForEvent(lastEvent),
      rows,
      createdAt: lastEvent.created_at,
      artifact: artifactDetailDownload(artifact),
      sourceKind: lastEvent.kind,
      sourceStepId: lastEvent.step_id,
      sourceActor: lastEvent.actor,
    },
  ];
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
    ...(event.kind === "discussion.completed" ? discussionMinutesRows(event, agentNames) : []),
    ...eventDetailRows(event, agentNames),
    ...eventArtifactRows(artifact),
  ];
  if (baseRows.length === 0 && !event.message) return [];
  const baseItem: ProcessDetailTarget = {
    id: `${detail.id}-event-${event.sequence}-${index}`,
    runId: detail.id,
    conversationId: runConversationId(detail),
    title: displayEventTitle(event, agentNames),
    message: eventSummaryText(event, agentNames, artifact),
    badge: processBadgeForEvent(event),
    rows: baseRows,
    createdAt: event.created_at,
    artifact: artifactDetailDownload(artifact),
    sourceKind: event.kind,
    sourceStepId: event.step_id,
    sourceActor: event.actor,
  };
  return [baseItem];
}

function refreshedProcessTarget(
  currentTarget: ProcessDetailTarget,
  candidates: ProcessDetailTarget[],
): ProcessDetailTarget {
  const matchedByStableSource =
    currentTarget.sourceKind || currentTarget.sourceStepId || currentTarget.sourceActor
      ? candidates.filter(
          (item) =>
            item.runId === currentTarget.runId &&
            item.sourceKind === currentTarget.sourceKind &&
            item.sourceStepId === currentTarget.sourceStepId &&
            item.sourceActor === currentTarget.sourceActor,
        )
      : [];
  return matchedByStableSource.at(-1) ?? candidates.find((item) => item.id === currentTarget.id) ?? currentTarget;
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
            runId: detail.id,
            conversationId: runConversationId(detail),
            title: "主 Agent 调度判断",
            message: `主 Agent 选择${displayMode(detail.mode)}${routingAgentPool ? `：${routingAgentPool}` : ""}`,
            badge: "调度判断",
            rows: routingRows,
            createdAt: null,
          },
        ]
      : [];
  const consumedArtifactIds = new Set<string>();
  const toolGroups = new Map<string, EventGroupItem[]>();
  detail.events.forEach((event, index) => {
    if (!isToolEvent(event)) return;
    const key = toolLifecycleKey(event);
    toolGroups.set(key, [...(toolGroups.get(key) ?? []), { event, index }]);
  });
  const finalToolEvents = new Map<RunEvent, EventGroupItem[]>();
  toolGroups.forEach((group) => {
    const finalEvent = group.at(-1)?.event;
    if (finalEvent) finalToolEvents.set(finalEvent, group);
  });
  const actionEvents = detail.events.flatMap((event, index) => (isActionEvent(event) ? [{ event, index }] : []));
  const eventItems: ProcessDetailTarget[] = [];
  for (let index = 0; index < actionEvents.length; index += 1) {
    const { event, index: eventIndex } = actionEvents[index];
    if (isModelDeltaEvent(event)) {
      const group: EventGroupItem[] = [{ event, index: eventIndex }];
      while (index + 1 < actionEvents.length) {
        const next = actionEvents[index + 1];
        if (!isModelDeltaEvent(next.event) || !modelDeltaEventsCanMerge(group.at(-1)?.event ?? event, next.event)) break;
        group.push(next);
        index += 1;
      }
      if (group.length === 1) {
        const artifact = fallbackArtifactForEvent(event, detail.artifacts, consumedArtifactIds);
        eventItems.push(...processItemsForEvent(detail, event, eventIndex, agentNames, artifact));
        continue;
      }
      eventItems.push(
        ...processItemsForModelDeltaGroup(
          detail,
          group.map((item) => item.event),
          group[0]?.index ?? eventIndex,
          agentNames,
        ),
      );
      continue;
    }
    if (isToolEvent(event)) {
      const group = finalToolEvents.get(event);
      if (!group) continue;
      const finalEvent = group.at(-1)?.event ?? event;
      const artifact = fallbackArtifactForEvent(finalEvent, detail.artifacts, consumedArtifactIds);
      eventItems.push(
        ...processItemsForToolLifecycle(
          detail,
          group.map((item) => item.event),
          group[0]?.index ?? eventIndex,
          agentNames,
          artifact,
        ),
      );
      continue;
    }
    if (isWrappedToolFailureEvent(event, detail.events)) continue;
    const artifact = fallbackArtifactForEvent(event, detail.artifacts, consumedArtifactIds);
    if (event.kind === "artifact.created" && !artifact && !hasUsefulPayload(event)) continue;
    eventItems.push(...processItemsForEvent(detail, event, eventIndex, agentNames, artifact));
  }
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
  const dispatchCards = dispatchAgentCards(detail, agentNames);
  const taskChain = plannedTaskChain(detail, agentNames);
  const failureDiagnostics = failureDiagnosticsForRun(detail, agentNames);
  const executionIntents = executionIntentsForRun(detail, agentNames);
  const shouldShowSummary =
    items.length > 0 ||
    dispatchCards.length > 0 ||
    taskChain.length > 0 ||
    failureDiagnostics.length > 0 ||
    executionIntents.length > 0;
  if (!shouldShowSummary) return null;
  return (
    <section className="run-process-summary" aria-label="Agent 集群动作">
      {items.length > 0 ? (
        <div className="agent-cluster-status" role="status" aria-label={`Agent 集群，${items.length} 个关键动作`}>
          <span aria-hidden="true">⌘</span>
          <strong>Agent 集群</strong>
          <small>{items.length} 个关键动作</small>
        </div>
      ) : null}
      {taskChain.length > 0 ? (
        <section className="run-task-chain" aria-label="任务链路">
          <div className="run-task-chain-header">
            <span aria-hidden="true">⌁</span>
            <strong>任务链路</strong>
            <small>{taskChain.length} 个步骤</small>
          </div>
          <div className="run-task-chain-list">
            {taskChain.map((step, index) => (
              <article key={`${step.id}-${step.agentId}-${index}`} className={`run-task-chain-step step-${step.status}`}>
                <small>第 {index + 1} 步</small>
                <div>
                  <strong>{step.agentName}</strong>
                  <span>{step.status}</span>
                </div>
                <p>{step.summary || "等待执行"}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {failureDiagnostics.length > 0 ? (
        <section className="run-failure-diagnostics" aria-label="故障诊断">
          <div className="run-failure-diagnostics-header">
            <span aria-hidden="true">!</span>
            <strong>故障诊断</strong>
            <small>{failureDiagnostics.length} 个待处理信号</small>
          </div>
          <div className="run-failure-diagnostic-list">
            {failureDiagnostics.map((diagnostic) => (
              <article key={diagnostic.id} className={`run-failure-diagnostic diagnostic-${diagnostic.tone}`}>
                <small>{diagnostic.label}</small>
                <strong>{diagnostic.title}</strong>
                <span>{diagnostic.detail}</span>
                <p>{diagnostic.recommendation}</p>
                {diagnostic.meta.length > 0 ? (
                  <div aria-label={`${diagnostic.label}元数据`}>
                    {diagnostic.meta.map((meta) => (
                      <em key={meta}>{meta}</em>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {executionIntents.length > 0 ? (
        <section className="run-execution-intents" aria-label="执行意图">
          <div className="run-execution-intents-header">
            <span aria-hidden="true">◇</span>
            <strong>执行意图</strong>
            <small>{executionIntents.length} 个关键意图</small>
          </div>
          <div className="run-execution-intent-list">
            {executionIntents.map((intent) => (
              <article key={intent.id} className={`run-execution-intent intent-${intent.tone}`}>
                <small>{intent.label}</small>
                <strong>{intent.title}</strong>
                <span>{intent.detail}</span>
                {intent.meta.length > 0 ? (
                  <div aria-label={`${intent.label}元数据`}>
                    {intent.meta.map((meta) => (
                      <em key={meta}>{meta}</em>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {dispatchCards.length > 0 ? (
        <section className="agent-recruitment" aria-label="助手派单状态">
          <div className="agent-recruitment-header" role="status" aria-label={`已招募 ${dispatchCards.length} 个助手`}>
            <span aria-hidden="true">⌁</span>
            <strong>助手招募</strong>
            <small>已招募 {dispatchCards.length} 个助手</small>
          </div>
          <div className="agent-recruitment-list">
            {dispatchCards.map((card) => (
              <article key={card.id} className="agent-recruitment-card">
                <div className="agent-recruitment-avatar" aria-hidden="true">
                  {card.name.slice(0, 1)}
                </div>
                <div>
                  <strong>{card.name}</strong>
                  <small>
                    {card.role} · {card.model}
                  </small>
                  <p>{card.summary}</p>
                </div>
                <span>{card.status}</span>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {items.length > 0 ? (
        <div className="agent-cluster-actions">
          {items.map((item) => (
            <button key={item.id} type="button" className="run-process-toggle process-intermediate-card" onClick={() => onOpen(item)}>
              <span aria-hidden="true">›</span>
              <small className="process-card-badge">{item.badge}</small>
              <strong>{item.message}</strong>
              {item.artifact ? <small>{artifactDisplayName(item.artifact)}</small> : null}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}

const PROCESS_DETAIL_GROUPS: Array<{
  key: string;
  label: string;
  match: (row: { label: string; value: string }) => boolean;
}> = [
  {
    key: "conclusion",
    label: "结论",
    match: (row) => /结论|纪要|共识|得到结果|执行摘要|审查完成/.test(row.label),
  },
  {
    key: "artifact",
    label: "产物",
    match: (row) => /产物|文件|SHA|输出内容/.test(row.label),
  },
  {
    key: "blocker",
    label: "阻塞",
    match: (row) => /错误|失败|异常|超时|阻塞|退出|stderr|故障/.test(`${row.label} ${row.value}`),
  },
  {
    key: "decision",
    label: "决策",
    match: (row) => /决策|裁决|判断|审批|策略|模式|工作流|路由|修复/.test(row.label),
  },
  {
    key: "evidence",
    label: "证据",
    match: (row) => /执行者|参与者|模型|服务商|能力|步骤|工具|事件|耗时|字节|字段|分片|状态流|参数|类型/.test(row.label),
  },
];

function processDetailGroups(rows: Array<{ label: string; value: string }>): ProcessDetailGroup[] {
  const groups = new Map<string, ProcessDetailGroup>();
  PROCESS_DETAIL_GROUPS.forEach((group) => groups.set(group.key, { key: group.key, label: group.label, rows: [] }));
  groups.set("activity", { key: "activity", label: "活动", rows: [] });

  rows.forEach((row) => {
    const group = PROCESS_DETAIL_GROUPS.find((candidate) => candidate.match(row));
    groups.get(group?.key ?? "activity")?.rows.push(row);
  });

  return [...groups.values()].filter((group) => group.rows.length > 0);
}

function processDetailGroupSummary(group: ProcessDetailGroup) {
  const first = group.rows.find((row) => row.value.trim().length > 0);
  return conciseProcessText(first?.value ?? "", `${group.rows.length} 项摘要`);
}

function ProcessDetailCards({ target }: { target: ProcessDetailTarget }) {
  const [openGroupKey, setOpenGroupKey] = useState<string | null>(null);
  const groups = processDetailGroups(target.rows);
  if (groups.length === 0) return null;
  const openGroup = groups.find((group) => group.key === openGroupKey) ?? null;
  return (
    <>
      <div className="process-detail-card-grid" role="group" aria-label="运行详情摘要">
        {groups.map((group) => (
          <button
            key={`${target.id}-${group.key}`}
            type="button"
            className={`process-detail-card process-detail-card-${group.key}`}
            onClick={() => setOpenGroupKey(group.key)}
            aria-label={`${group.label}：${processDetailGroupSummary(group)}`}
          >
            <span>{group.label}</span>
            <small>{group.rows.length} 项</small>
            <strong>{processDetailGroupSummary(group)}</strong>
          </button>
        ))}
      </div>
      {openGroup ? (
        <div className="process-detail-modal-backdrop" role="presentation" onClick={() => setOpenGroupKey(null)}>
          <section
            className="process-detail-modal"
            role="dialog"
            aria-label={`${openGroup.label}详情`}
            aria-modal="true"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="process-detail-modal-header">
              <div>
                <span className="eyebrow">{target.badge}</span>
                <h4>{openGroup.label}</h4>
              </div>
              <button type="button" className="secondary-action" onClick={() => setOpenGroupKey(null)}>
                关闭
              </button>
            </div>
            <dl>
              {openGroup.rows.map((row, index) => (
                <Fragment key={`${target.id}-${openGroup.key}-${row.label}-${index}`}>
                  <dt>{row.label}</dt>
                  <dd>{row.value}</dd>
                </Fragment>
              ))}
            </dl>
          </section>
        </div>
      ) : null}
    </>
  );
}

function RunProcessDrawer({
  target,
  onClose,
}: {
  target: ProcessDetailTarget;
  onClose: () => void;
}) {
  return createPortal(
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
            <span className="eyebrow">{target.badge}</span>
            <h3>{target.title}</h3>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="run-process-detail">
          <article>
            <p>{target.message}</p>
            {target.artifact ? (
              <div className="artifact-download-list" aria-label="中间产物">
                <ArtifactFileCard artifact={target.artifact} compact />
              </div>
            ) : null}
            <ProcessDetailCards target={target} />
            {target.createdAt ? <small>{target.createdAt}</small> : null}
          </article>
        </div>
      </section>
    </div>,
    document.body,
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

type MessageBodyBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "table"; headers: string[]; rows: string[][] };

function markdownMessageBlocks(text: string): MessageBodyBlock[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: MessageBodyBlock[] = [];
  let paragraph: string[] = [];
  let index = 0;

  function flushParagraph() {
    const value = paragraph.join("\n").trim();
    if (value) blocks.push({ kind: "paragraph", text: value });
    paragraph = [];
  }

  while (index < lines.length) {
    if (isMarkdownTableStart(lines, index)) {
      flushParagraph();
      const headers = markdownTableCells(lines[index]);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && markdownTableCells(lines[index]).length >= headers.length && lines[index].includes("|")) {
        rows.push(markdownTableCells(lines[index]).slice(0, headers.length));
        index += 1;
      }
      if (headers.length > 0 && rows.length > 0) {
        blocks.push({ kind: "table", headers, rows });
        continue;
      }
    }
    paragraph.push(lines[index]);
    index += 1;
  }
  flushParagraph();
  return blocks;
}

function isMarkdownTableStart(lines: string[], index: number) {
  if (index + 1 >= lines.length) return false;
  const header = markdownTableCells(lines[index]);
  const separator = markdownTableCells(lines[index + 1]);
  if (header.length < 2 || separator.length !== header.length) return false;
  return separator.every((cell) => /^:?-{2,}:?$/.test(cell.trim()));
}

function markdownTableCells(line: string) {
  const trimmed = line.trim();
  if (!trimmed.includes("|")) return [];
  const body = trimmed.startsWith("|") ? trimmed.slice(1) : trimmed;
  const normalized = body.endsWith("|") ? body.slice(0, -1) : body;
  return normalized.split("|").map((cell) => cell.replace(/\\\|/g, "|").trim());
}

function MessageBody({ text, title }: { text: string; title: string }) {
  const blocks = markdownMessageBlocks(text);
  if (blocks.length === 0) return null;
  let tableIndex = 0;
  return (
    <div className="message-body">
      {blocks.map((block, index) => {
        if (block.kind === "paragraph") {
          return <p key={`paragraph-${index}`}>{block.text}</p>;
        }
        tableIndex += 1;
        return (
          <div className="message-table-wrap" key={`table-${index}`}>
            <table aria-label={`${title}表格 ${tableIndex}`} className="message-table">
              <thead>
                <tr>
                  {block.headers.map((header, headerIndex) => (
                    <th key={`${header}-${headerIndex}`} scope="col">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={`row-${rowIndex}`}>
                    {block.headers.map((_header, cellIndex) => (
                      <td key={`cell-${rowIndex}-${cellIndex}`}>{row[cellIndex] ?? ""}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}
export function RunsPage() {
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.runs(),
    refetchInterval: (query) =>
      query.state.data?.some((run) => !TERMINAL_STATUSES.has(run.status)) ? 1000 : 5000,
    refetchIntervalInBackground: true,
  });
  const runListItems = runs.data ?? [];
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents(),
    refetchInterval: 1000,
    refetchIntervalInBackground: true,
  });
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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [processDetailTarget, setProcessDetailTarget] = useState<ProcessDetailTarget | null>(null);
  const [modeSelection, setModeSelection] = useState<ModeSelection | null>(null);
  const [skillInstallCandidate, setSkillInstallCandidate] = useState<SkillInstallCandidate | null>(null);
  const [attachmentDraft, setAttachmentDraft] = useState<ChatAttachmentDraft | null>(null);
  const [archiveInstallFile, setArchiveInstallFile] = useState<File | null>(null);
  const [conversationRunCache, setConversationRunCache] = useState<Record<string, RunDetail[]>>({});
  const [temporaryApproval, setTemporaryApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: NonNullable<SubmittedRun["temporary_agent_proposal"]>;
    approved: boolean;
  } | null>(null);
  const [temporaryFeedback, setTemporaryFeedback] = useState("");
  const [scheduleApproval, setScheduleApproval] = useState<{
    runId: string;
    proposal: ScheduleProposal;
    createdScheduleId: string | null;
  } | null>(null);
  const [dismissedScheduleApprovalRunIds, setDismissedScheduleApprovalRunIds] = useState<string[]>([]);
  const [dismissedEvolutionApprovalRunIds, setDismissedEvolutionApprovalRunIds] = useState<string[]>([]);
  const [evolutionApproval, setEvolutionApproval] = useState<{
    runId: string;
    proposal: EvolutionProposal;
    createdEvolutionId: string | null;
  } | null>(null);
  const [openClawApproval, setOpenClawApproval] = useState<{
    runId: string;
    proposal: OpenClawProposal;
    createdOperationId: string | null;
  } | null>(null);
  const [repairApproval, setRepairApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: RepairProposal;
  } | null>(null);
  const userSelectedMode = useRef(false);
  const trimmedReferenceConversationId = referenceConversationId.trim();
  const handoffActive = Boolean(trimmedReferenceConversationId);

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
      if (!data) return false;
      if (processDetailTarget?.runId === data.id) return 1000;
      return !TERMINAL_STATUSES.has(data.status) ? 1000 : false;
    },
    refetchIntervalInBackground: true,
  });

  const referenceConversation = useQuery({
    queryKey: ["conversation", trimmedReferenceConversationId],
    queryFn: () => api.conversation(trimmedReferenceConversationId),
    enabled: false,
  });

  const selectedRunConversationId = runConversationId(selectedRun.data);
  const activeConversationId = conversationId.trim();
  const activeConversationKnown =
    Boolean(selectedRun.data) ||
    Boolean(conversationRunCache[activeConversationId]) ||
    runListItems.some((run) => run.conversation_id === activeConversationId);
  const activeConversation = useQuery({
    queryKey: ["conversation", activeConversationId],
    queryFn: () => api.conversation(activeConversationId),
    enabled: Boolean(activeConversationId && activeConversationKnown),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && processDetailTarget?.conversationId === data.conversation_id) return 1000;
      if (data && activeConversationId === data.conversation_id) return 1000;
      return data?.runs.some((run) => !TERMINAL_STATUSES.has(run.status)) ? 1000 : false;
    },
    refetchIntervalInBackground: true,
  });

  async function refreshRunSurfaces(run: { id: string; conversation_id?: string | null }) {
    await queryClient.invalidateQueries({ queryKey: ["runs"] });
    await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    const surfaceConversationId = run.conversation_id?.trim() || activeConversationId;
    if (surfaceConversationId) {
      await queryClient.invalidateQueries({ queryKey: ["conversation", surfaceConversationId] });
    }
  }

  useEffect(() => {
    const closeHistoryDrawer = () => setHistoryOpen(false);
    window.addEventListener("agent-hub:close-history-drawer", closeHistoryDrawer);
    return () => window.removeEventListener("agent-hub:close-history-drawer", closeHistoryDrawer);
  }, []);
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
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setRepairApproval(null);
      setTemporaryApproval((current) =>
        current &&
        current.runId === approval.runId &&
        current.version === approval.version &&
        current.decisionToken === approval.decisionToken
          ? current
          : approval,
      );
    }
    const proposedSchedule = scheduleApprovalFromRunDetail(selectedRun.data);
    if (proposedSchedule && !dismissedScheduleApprovalRunIds.includes(proposedSchedule.runId)) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setRepairApproval(null);
      setScheduleApproval((current) =>
        current && current.runId === proposedSchedule.runId ? current : proposedSchedule,
      );
    }
    const proposedEvolution = evolutionApprovalFromRunDetail(selectedRun.data);
    if (proposedEvolution && !dismissedEvolutionApprovalRunIds.includes(proposedEvolution.runId)) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setOpenClawApproval(null);
      setRepairApproval(null);
      setEvolutionApproval((current) =>
        current && current.runId === proposedEvolution.runId ? current : proposedEvolution,
      );
    }
    const proposedOpenClaw = openClawApprovalFromRunDetail(selectedRun.data);
    if (proposedOpenClaw) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setRepairApproval(null);
      setOpenClawApproval((current) =>
        current && current.runId === proposedOpenClaw.runId ? current : proposedOpenClaw,
      );
    }
    const proposedRepair = repairApprovalFromRunDetail(selectedRun.data);
    if (proposedRepair) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setRepairApproval((current) =>
        current &&
        current.runId === proposedRepair.runId &&
        current.version === proposedRepair.version &&
        current.decisionToken === proposedRepair.decisionToken
          ? current
          : proposedRepair,
      );
    }
  }, [dismissedEvolutionApprovalRunIds, dismissedScheduleApprovalRunIds, modeSelection, selectedRun.data, temporaryApproval]);

  useEffect(() => {
    setProcessDetailTarget(null);
  }, [selectedRunId]);

  const pageOverlayOpen = Boolean(processDetailTarget) || historyOpen;
  useEffect(() => {
    if (!pageOverlayOpen) return undefined;
    const previousBodyOverflow = document.body.style.overflow;
    const previousBodyTouchAction = document.body.style.touchAction;
    const previousDocumentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.body.style.touchAction = "none";
    document.documentElement.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow || "";
      document.body.style.touchAction = previousBodyTouchAction || "";
      document.documentElement.style.overflow = previousDocumentOverflow || "";
    };
  }, [pageOverlayOpen]);

  useEffect(() => {
    if (!processDetailTarget) return undefined;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setProcessDetailTarget(null);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [processDetailTarget]);

  useEffect(() => {
    if (!processDetailTarget) return undefined;
    const refreshOpenProcess = () => {
      if (selectedRunId === processDetailTarget.runId) {
        void selectedRun.refetch();
      } else {
        void queryClient.invalidateQueries({ queryKey: ["run", processDetailTarget.runId] });
      }
      if (processDetailTarget.conversationId) {
        if (activeConversationId === processDetailTarget.conversationId) {
          void activeConversation.refetch();
        } else {
          void queryClient.invalidateQueries({ queryKey: ["conversation", processDetailTarget.conversationId] });
        }
      }
    };
    refreshOpenProcess();
    const interval = window.setInterval(refreshOpenProcess, 1000);
    return () => window.clearInterval(interval);
  }, [activeConversation, activeConversationId, processDetailTarget, queryClient, selectedRun, selectedRunId]);

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
        skip_evolution_proposal: override?.skipEvolutionProposal === true ? true : undefined,
      });
    },
    onSuccess: async (run, override) => {
      setSelectedRunId(run.id);
      setShowModeEntry(false);
      if (run.conversation_id) setConversationId(run.conversation_id);
      const selection = modeSelectionFromSubmittedRun(run);
      const submittedMode = override?.mode ?? mode;
      if (selection && submittedMode !== "auto") {
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setModeSelection(null);
        setSubmitNotice(`已按你选择的“${displayMode(submittedMode)}”继续，不再重复确认模式。`);
        const continued = await api.chooseMode(run.id, {
          mode: submittedMode as ManualRunMode,
          decision_token: selection.decisionToken,
          version: selection.version,
          operator_note: "用户已在新对话入口明确选择该模式。",
        });
        if (continued.conversation_id) setConversationId(continued.conversation_id);
        await refreshRunSurfaces({ id: run.id, conversation_id: continued.conversation_id ?? run.conversation_id });
        setMessage("");
        setAttachmentDraft(null);
        setArchiveInstallFile(null);
        return;
      }
      const repair = repairApprovalFromSubmittedRun(run);
      if (repair) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(repair);
        setSubmitNotice("运行失败已生成受控自修复建议，需要确认后才会重新排队。");
      } else if (run.openclaw_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setRepairApproval(null);
        setOpenClawApproval({ runId: run.id, proposal: run.openclaw_proposal, createdOperationId: null });
        setSubmitNotice("主 Agent 已识别为 OpenClaw 操作请求，请到 OpenClaw 管理页确认权限和执行边界。");
      } else if (run.schedule_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
        setDismissedScheduleApprovalRunIds((current) => current.filter((id) => id !== run.id));
        setScheduleApproval({ runId: run.id, proposal: run.schedule_proposal, createdScheduleId: null });
        setSubmitNotice("主 Agent 已识别为计划任务，确认后会加入计划任务列表。");
      } else if (run.evolution_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
        setDismissedEvolutionApprovalRunIds((current) => current.filter((id) => id !== run.id));
        setEvolutionApproval({ runId: run.id, proposal: run.evolution_proposal, createdEvolutionId: null });
        setSubmitNotice("主 Agent 已识别为进化任务，确认后会加入进化记录。");
      } else if (run.temporary_agent_proposal && run.decision_token) {
        setModeSelection(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
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
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
        setModeSelection(selection);
        setSubmitNotice("主 Agent 对这轮回复的模式判断不够确定，请直接在输入框回复编号或关键词继续。");
      } else {
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
        setModeSelection(null);
        setSubmitNotice(override?.successNotice ?? explainActualMode(run));
      }
      setMessage("");
      setAttachmentDraft(null);
      setArchiveInstallFile(null);
      await refreshRunSurfaces(run);
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
      await refreshRunSurfaces(run);
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
      await refreshRunSurfaces(run);
    },
  });

  const acceptSelfRepair = useMutation({
    mutationFn: () => {
      if (!repairApproval) throw new Error("repair approval is unavailable");
      return api.acceptSelfRepair(repairApproval.runId, {
        decision_token: repairApproval.decisionToken,
        version: repairApproval.version,
      });
    },
    onSuccess: async (run) => {
      setRepairApproval(null);
      setSubmitNotice("已接受受控自修复，这次运行已重新排队。");
      await refreshRunSurfaces(run);
    },
  });

  const cancelScheduleApproval = () => {
    if (!scheduleApproval) return;
    setDismissedScheduleApprovalRunIds((current) =>
      current.includes(scheduleApproval.runId) ? current : [...current, scheduleApproval.runId],
    );
    setScheduleApproval(null);
    setSubmitNotice("已取消计划任务创建，后续消息会继续作为普通对话处理。");
  };
  const createScheduleFromProposal = useMutation({
    mutationFn: () => {
      if (!scheduleApproval) throw new Error("schedule approval is unavailable");
      return api.createSchedule(scheduleProposalCreatePayload(scheduleApproval.proposal));
    },
    onSuccess: async (schedule) => {
      setScheduleApproval((current) =>
        current ? { ...current, createdScheduleId: schedule.id } : current,
      );
      setSubmitNotice(`已加入计划：${schedule.name}。到计划任务页面可以查看、删除或等待系统自动触发。`);
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });
  const cancelEvolutionApproval = () => {
    const approval = evolutionApproval;
    if (!approval) return;
    setDismissedEvolutionApprovalRunIds((current) =>
      current.includes(approval.runId) ? current : [...current, approval.runId],
    );
    setEvolutionApproval(null);
    setSubmitNotice("已取消进化任务创建，正在按普通对话继续执行原消息。");
    createRun.mutate({
      message: approval.proposal.objective,
      mode: "auto",
      skipEvolutionProposal: true,
      successNotice: "已取消进化任务创建，已按普通对话继续执行原消息。",
    });
  };
  const createEvolutionFromProposal = useMutation({
    mutationFn: () => {
      if (!evolutionApproval) throw new Error("evolution approval is unavailable");
      return api.createEvolutionRun(evolutionProposalCreatePayload(evolutionApproval.proposal));
    },
    onSuccess: async (run) => {
      setEvolutionApproval((current) =>
        current ? { ...current, createdEvolutionId: run.id } : current,
      );
      setSubmitNotice(`已加入进化：${run.title}。到进化页面可以审批、登记轮次和查看结果。`);
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  const createOpenClawFromProposal = useMutation({
    mutationFn: () => {
      if (!openClawApproval) throw new Error("openclaw approval is unavailable");
      return api.createOpenClawOperationFromRun(openClawApproval.runId);
    },
    onSuccess: (operation) => {
      setOpenClawApproval((current) =>
        current ? { ...current, createdOperationId: operation.id } : current,
      );
      setSubmitNotice(`已创建 OpenClaw 待审批操作：${operation.id}。请到 OpenClaw 控制页审批和执行。`);
    },
  });

  const stopCurrentRun = useMutation({
    mutationFn: (runId: string) => api.cancelRun(runId),
    onSuccess: async (run) => {
      setSubmitNotice("已停止当前运行。你可以继续发送新消息。");
      await refreshRunSurfaces({ id: run.id, conversation_id: runConversationId(run) ?? run.conversation_id });
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
      await refreshRunSurfaces(run);
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
    onSuccess: (result, file) => {
      setArchiveInstallFile(null);
      setSkillInstallCandidate({ fileName: file.name, skills: result.items, skipped: result.skipped, status: "scanned" });
      setSubmitNotice("Skill 压缩包已完成安全扫描，请确认权限后再安装。");
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (error, file) => {
      setSkillInstallCandidate(null);
      setAttachmentDraft((current) =>
        current ?? {
          fileName: file.name,
          size: file.size,
          kind: isArchiveFileName(file.name) ? "archive" : "context",
        },
      );
      setSubmitNotice(
        error instanceof ApiError && error.code === "invalid_skill_package"
          ? "这个压缩包不是有效 Skill 压缩包，已保留为普通附件；如果它用于代码审查或普通任务，请直接在对话里说明。"
          : "Skill 扫描失败。压缩包仍保留为附件，请查看错误详情后决定是否重新上传。",
      );
    },
  });

  const approveUploadedSkill = useMutation({
    mutationFn: () => {
      if (!skillInstallCandidate) throw new Error("skill install candidate is unavailable");
      return Promise.all(skillInstallCandidate.skills.map((skill) => api.approveSkill(skill.id)));
    },
    onSuccess: async (skills) => {
      setSkillInstallCandidate((current) => (current ? { ...current, skills, status: "enabled" } : current));
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
      setArchiveInstallFile(kind === "archive" ? file : null);
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
    uploadAttachment.reset();
    uploadSkillArchive.reset();
    setSubmitNotice(null);
    setAttachmentDraft(null);
    setSkillInstallCandidate(null);
    setArchiveInstallFile(isArchiveFileName(file.name) ? file : null);
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
    if (repairApproval) {
      const choice = parseChoiceText(trimmed, [
        { value: "accept", label: "接受修复", aliases: ["接受", "修复", "重试", "approve", "yes", "fix"] },
      ]);
      if (!choice) {
        setSubmitNotice("请回复 1/接受/修复，或点击自修复确认卡里的按钮。");
        return;
      }
      setMessage("");
      setSubmitNotice("已选择接受受控自修复，正在重新排队。");
      acceptSelfRepair.mutate();
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
    setScheduleApproval(null);
    setEvolutionApproval(null);
    setOpenClawApproval(null);
    setModeSelection(null);
    setProcessDetailTarget(null);
    setSubmitNotice("已新建空白对话。选一个模式或直接发送，主 Agent 会按当前设置处理。");
  }

  function startBranchConversation(sourceConversationId?: string | null) {
    const trimmedSourceConversationId = sourceConversationId?.trim() || runConversationId(selectedRun.data) || conversationId.trim();
    if (!trimmedSourceConversationId) {
      setSubmitNotice("当前没有可引用的会话。");
      return;
    }
    setSelectedRunId(null);
    setShowModeEntry(false);
    setHistoryOpen(false);
    setReferenceConversationId(trimmedSourceConversationId);
    setConversationId(newConversationId());
    setMessage("");
    setDirectModel("");
    setTemporaryApproval(null);
    setScheduleApproval(null);
    setEvolutionApproval(null);
    setOpenClawApproval(null);
    setModeSelection(null);
    setProcessDetailTarget(null);
    setSubmitNotice(`已按原思路新建分支：新对话会读取 ${trimmedSourceConversationId} 作为参考上下文。`);
  }

  function cancelBranchReference() {
    setReferenceConversationId("");
    setSubmitNotice("已取消引用会话。");
  }

  function loadReferenceConversation() {
    if (!trimmedReferenceConversationId) return;
    void referenceConversation.refetch();
  }

  if (runs.isLoading) {
    return <p>正在加载对话...</p>;
  }
  if (runs.isError) return <p role="alert">{formatApiError(runs.error, "会话列表加载失败")}</p>;

  const items = runListItems;
  const selectedMode = RUN_MODES.find((item) => item.value === mode) ?? RUN_MODES[0];
  const savedAgents = agents.data ?? [];
  const savedModels = models.data ?? [];
  const enabledAgents = savedAgents.filter((agent) => agent.enabled);
  const savedWorkflows = workflows.data ?? [];
  const agentNameMap = new Map(savedAgents.map((agent) => [agent.id, agent.name]));
  const cachedConversationRuns = activeConversationId ? conversationRunCache[activeConversationId] : undefined;
  const activeConversationRuns =
    activeConversation.data?.conversation_id === activeConversationId ? activeConversation.data.runs : undefined;
  const conversationVisibleRuns = activeConversationRuns
    ? mergeConversationRuns(cachedConversationRuns, activeConversationRuns)
    : cachedConversationRuns;
  const visibleRuns =
    selectedRun.data && runConversationId(selectedRun.data) === activeConversationId
      ? mergeConversationRuns(conversationVisibleRuns, [selectedRun.data])
      : conversationVisibleRuns ?? activeConversationRuns ?? (selectedRun.data ? [selectedRun.data] : []);
  const messages = conversationMessages(visibleRuns);
  const temporaryApprovalVisibleInMessages =
    !!temporaryApproval &&
    messages.some((item) => item.id === `${temporaryApproval.runId}-temporary-agent-approval`);
  const scheduleApprovalVisibleInMessages =
    !!scheduleApproval && messages.some((item) => item.id === `${scheduleApproval.runId}-schedule-approval`);
  const openClawApprovalVisibleInMessages =
    !!openClawApproval && messages.some((item) => item.id === `${openClawApproval.runId}-openclaw-approval`);
  const repairApprovalVisibleInMessages =
    !!repairApproval && messages.some((item) => item.id === `${repairApproval.runId}-repair-approval`);
  const latestVisibleRun = visibleRuns.at(-1) ?? selectedRun.data;
  const canStopLatestRun = Boolean(latestVisibleRun && !TERMINAL_STATUSES.has(latestVisibleRun.status));
  const registeredModelIds = new Set(savedModels.map((model) => model.logical_model));
  const directModelDeployment = savedModels.find((model) => model.logical_model === directModel) ?? null;
  const directModelName = directModelDeployment?.logical_model ?? (directModel || "未指定");
  const mainAgentModelName = mainAgent.data?.model
    ? `${mainAgent.data.model.provider}/${mainAgent.data.model.upstream_model}`
    : "未配置";
  const refreshedRunForProcessDetail = processDetailTarget
    ? visibleRuns.find((run) => run.id === processDetailTarget.runId) ??
      (selectedRun.data?.id === processDetailTarget.runId ? selectedRun.data : null)
    : null;
  const refreshedProcessDetailTarget =
    processDetailTarget && refreshedRunForProcessDetail
      ? refreshedProcessTarget(
          processDetailTarget,
          runProcessItems(refreshedRunForProcessDetail, agentNameMap, mainAgentModelName),
        )
      : processDetailTarget;
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
    const title = conversationTitle(run, items);
    if (!window.confirm(`确认删除对话「${title}」？删除后运行详情和产物记录也会移除。`)) {
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
      setSubmitNotice("请先选择已完成、失败或已取消的会话。");
      return;
    }
    if (!window.confirm(`确认删除 ${selectedDeletableConversationIds.length} 条已选会话？删除后运行详情和产物记录也会移除。`)) {
      return;
    }
    bulkDeleteRuns.mutate(selectedDeletableConversationIds);
  }

  return (
    <section>
      <p className="eyebrow">Conversation</p>
      <h2>对话与进化</h2>
      <p className="compact-page-intro">
        这里是连续对话窗口，也会承接长期多轮任务、上下文压缩和后续 Skill 进化。历史会话从右侧抽屉打开。
      </p>

      <div className="mobile-chat-hierarchy" aria-label="移动端对话层级">
        <span>1 · 会话</span>
        <span>2 · 对话</span>
        <span>3 · 设置 / 详情</span>
      </div>

      <button
        type="button"
        className="mobile-nav-trigger conversation-drawer-trigger"
        aria-label={historyOpen ? "关闭历史对话" : "打开历史对话"}
        aria-expanded={historyOpen}
        onClick={() => {
          const next = !historyOpen;
          if (next) window.dispatchEvent(new Event("agent-hub:close-mobile-nav"));
          setHistoryOpen(next);
        }}
      >
        <span className="mobile-nav-trigger-icon" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      </button>

      <div className={`chat-console${historyOpen ? " history-drawer-open" : ""}`}>
        <button
          type="button"
          className="conversation-drawer-backdrop"
          aria-label="关闭历史对话"
          onClick={() => setHistoryOpen(false)}
        />
        <nav className="conversation-list" aria-label="会话导航">
          <div className="conversation-list-header">
            <div>
              <h3>会话</h3>
              <span>{items.length} 条</span>
            </div>
            <div className="conversation-list-actions">
              <button type="button" className="secondary-action conversation-new-button" aria-label="新建对话" onClick={startNewConversation}>
                新建
              </button>
              <button type="button" className="conversation-close-button" aria-label="关闭历史对话" onClick={() => setHistoryOpen(false)}>
                ×
              </button>
            </div>
          </div>
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
                aria-label={`批量删除已选会话 ${selectedDeletableConversationIds.length} 条`}
                disabled={selectedDeletableConversationIds.length === 0 || bulkDeleteRuns.isPending}
                onClick={deleteSelectedConversations}
              >
                {bulkDeleteRuns.isPending ? "删除中..." : `删除已选（${selectedDeletableConversationIds.length}）`}
              </button>
              <small>已选 {selectedDeletableConversationIds.length}</small>
            </div>
          ) : null}
          {items.length === 0 ? (
            <p className="field-help">还没有会话。直接发送消息即可开始。</p>
          ) : (
            items.map((run) => {
              const canDelete = TERMINAL_STATUSES.has(run.status);
              const title = conversationTitle(run, items);
              return (
                <div
                  key={run.id}
                  className={`conversation-row${selectedRunId === run.id ? " conversation-row-active" : ""}`}
                >
                  <input
                    type="checkbox"
                    className="conversation-select"
                    aria-label={`选择会话 ${title}`}
                    checked={selectedConversationIds.includes(run.id)}
                    disabled={!canDelete || bulkDeleteRuns.isPending}
                    onChange={() => toggleConversation(run.id)}
                  />
                  <button
                    type="button"
                    className="conversation-item"
                    aria-label={`进入会话 ${title}`}
                    onClick={() => {
                      setShowModeEntry(false);
                      if (run.conversation_id) setConversationId(run.conversation_id);
                      setSelectedRunId(run.id);
                      setHistoryOpen(false);
                    }}
                  >
                    <span className="conversation-mode-chip">{displayMode(run.mode)}</span>
                    <strong className="conversation-title-text">{title}</strong>
                    <small className="conversation-meta-line">{conversationTimestamp(run.created_at) || "最近会话"}</small>
                  </button>
                  <button
                    type="button"
                    className="conversation-branch-button"
                    aria-label={`按原思路新建分支 ${title}`}
                    title={run.conversation_id ? "引用这段会话新建分支" : "这条运行没有会话 ID"}
                    disabled={!run.conversation_id}
                    onClick={() => startBranchConversation(run.conversation_id)}
                  >
                    分支
                  </button>
                  <button
                    type="button"
                    className="conversation-delete-button"
                    aria-label={`删除会话 ${title}`}
                    title={canDelete ? "删除对话" : "运行中先取消"}
                    disabled={!canDelete || deleteRun.isPending}
                    onClick={() => deleteConversation(run)}
                  >
                    删除
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
              <p className="chat-conversation-status">会话：{conversationId}</p>
              <div>
                <button type="button" className="secondary-action" aria-label="新建对话" onClick={startNewConversation}>
                  新建
                </button>
              </div>
            </div>
            {showModeEntry ? (
              <ModeEntryPanel selectedMode={mode} onSelect={chooseRunMode} />
            ) : null}
            {mode === "direct" && messages.length === 0 ? (
              <article className="chat-message assistant" aria-label="直连模型选择">
                <span className="eyebrow">{APP_BRAND_NAME}</span>
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
                <span className="eyebrow">{APP_BRAND_NAME}</span>
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
                <span className="eyebrow">{APP_BRAND_NAME}</span>
                <h3>{temporaryApproval.proposal.name}</h3>
                <TemporaryAgentApprovalMessage proposal={temporaryApproval.proposal} />
              </article>
            ) : null}
            {scheduleApproval && !scheduleApprovalVisibleInMessages ? (
              <article className="chat-message assistant" aria-label="计划任务文字确认">
                <span className="eyebrow">{APP_BRAND_NAME}</span>
                <h3>计划任务确认</h3>
                <p>{scheduleProposalBody(scheduleApproval.proposal)}</p>
              </article>
            ) : null}
            {openClawApproval && !openClawApprovalVisibleInMessages ? (
              <article className="chat-message assistant" aria-label="OpenClaw 文字确认">
                <span className="eyebrow">{APP_BRAND_NAME}</span>
                <h3>OpenClaw 操作确认</h3>
                <p>{openClawProposalBody(openClawApproval.proposal)}</p>
              </article>
            ) : null}
            {repairApproval && !repairApprovalVisibleInMessages ? (
              <article className="chat-message assistant" aria-label="自修复文字确认">
                <span className="eyebrow">{APP_BRAND_NAME}</span>
                <h3>{repairApproval.proposal.title}</h3>
                <p>{repairProposalBody(repairApproval.proposal)}</p>
              </article>
            ) : null}
            {messages.map((item, index) => (
              <Fragment key={item.id}>
                <article className={`chat-message ${item.role}`}>
                  <span className="eyebrow">{item.role === "user" ? "你" : APP_BRAND_NAME}</span>
                  <h3>{item.title}</h3>
                  {item.temporaryAgent ? (
                    <TemporaryAgentApprovalMessage proposal={item.temporaryAgent} />
                  ) : (
                    <MessageBody text={item.body} title={item.title} />
                  )}
                  {item.artifact ? (
                    <div className="artifact-download-list" aria-label="附件">
                      <ArtifactFileCard artifact={item.artifact} />
                    </div>
                  ) : null}
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
          </div>
          {refreshedProcessDetailTarget ? (
            <RunProcessDrawer
              target={refreshedProcessDetailTarget}
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
            {createScheduleFromProposal.isError ? (
              <p role="alert">{formatApiError(createScheduleFromProposal.error, "计划任务创建失败")}</p>
            ) : null}
            {evolutionApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="进化任务确认">
                <div>
                  <span className="eyebrow">{evolutionApproval.createdEvolutionId ? "进化任务已加入" : "进化任务待确认"}</span>
                  <strong>{evolutionApproval.proposal.title}</strong>
                  <small>{evolutionApproval.proposal.summary}</small>
                </div>
                <p>{evolutionApproval.proposal.objective}</p>
                {evolutionApproval.createdEvolutionId ? (
                  <Link to="/evolution" className="secondary-action">
                    查看进化任务
                  </Link>
                ) : (
                  <div className="composer-card-actions">
                    <button type="button" onClick={() => createEvolutionFromProposal.mutate()} disabled={createEvolutionFromProposal.isPending}>
                      {createEvolutionFromProposal.isPending ? "加入中..." : "加入进化"}
                    </button>
                    <button type="button" className="secondary-action" disabled={createEvolutionFromProposal.isPending} onClick={cancelEvolutionApproval}>
                      取消进化
                    </button>
                  </div>
                )}
              </aside>
            ) : null}
            {createEvolutionFromProposal.isError ? (
              <p role="alert">{formatApiError(createEvolutionFromProposal.error, "进化任务创建失败")}</p>
            ) : null}
            {openClawApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="OpenClaw 操作确认">
                <div>
                  <span className="eyebrow">
                    {openClawApproval.createdOperationId ? "OpenClaw 操作已创建" : "OpenClaw 待确认"}
                  </span>
                  <strong>{openClawApproval.proposal.target}</strong>
                  <small>{openClawApproval.proposal.summary}</small>
                </div>
                <p>{openClawApproval.proposal.operation_text}</p>
                {openClawApproval.createdOperationId ? (
                  <small>待审批操作：{openClawApproval.createdOperationId}</small>
                ) : (
                  <button type="button" onClick={() => createOpenClawFromProposal.mutate()} disabled={createOpenClawFromProposal.isPending}>
                    {createOpenClawFromProposal.isPending ? "创建中..." : "创建待审批操作"}
                  </button>
                )}
                <Link to="/openclaw" className="secondary-action">
                  打开 OpenClaw
                </Link>
              </aside>
            ) : null}
            {createOpenClawFromProposal.isError ? (
              <p role="alert">{formatApiError(createOpenClawFromProposal.error, "OpenClaw 操作创建失败")}</p>
            ) : null}
            {acceptSelfRepair.isError ? (
              <p role="alert">{formatApiError(acceptSelfRepair.error, "自修复确认失败")}</p>
            ) : null}
            {repairApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="自修复确认">
                <div>
                  <span className="eyebrow">自修复待确认</span>
                  <strong>{repairApproval.proposal.title}</strong>
                  <small>{repairApproval.proposal.summary}</small>
                </div>
                <p>{repairProposalBody(repairApproval.proposal)}</p>
                <button type="button" disabled={acceptSelfRepair.isPending} onClick={() => acceptSelfRepair.mutate()}>
                  {acceptSelfRepair.isPending ? "排队中..." : "接受修复"}
                </button>
              </aside>
            ) : null}
            {scheduleApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="计划任务确认">
                <div>
                  <span className="eyebrow">{scheduleApproval.createdScheduleId ? "计划任务已加入" : "计划任务待确认"}</span>
                  <strong>{scheduleApproval.proposal.name}</strong>
                  <small>{scheduleApproval.proposal.summary}</small>
                </div>
                <p>{scheduleApproval.proposal.message}</p>
                {scheduleApproval.createdScheduleId ? (
                  <Link to="/schedules" className="secondary-action">
                    查看计划任务
                  </Link>
                ) : (
                  <div className="composer-card-actions">
                    <button type="button" disabled={createScheduleFromProposal.isPending} onClick={() => createScheduleFromProposal.mutate()}>
                      {createScheduleFromProposal.isPending ? "加入中..." : "加入计划"}
                    </button>
                    <button type="button" className="secondary-action" disabled={createScheduleFromProposal.isPending} onClick={cancelScheduleApproval}>
                      取消计划
                    </button>
                  </div>
                )}
              </aside>
            ) : null}
            {skillInstallCandidate ? (
              <aside className="composer-attachment-card" role="status" aria-label="Skill 安装确认">
                <div>
                  <span className="eyebrow">
                    {skillInstallCandidate.status === "enabled" ? "Skill 已安装并启用" : "Skill 压缩包已扫描，等待确认"}
                  </span>
                  <strong>{skillInstallCandidate.skills.map((skill) => skill.name).join(", ")}</strong>
                  <small>
                    {skillInstallCandidate.fileName} · {skillInstallCandidate.skills.length} Skill
                  </small>
                </div>
                {skillInstallCandidate.skipped.length > 0 ? (
                  <p className="field-help">
                    已跳过 {skillInstallCandidate.skipped.length} 项：
                    {skillInstallCandidate.skipped.map((item) => `${item.path}（${item.reason}）`).join("；")}
                  </p>
                ) : null}
                {skillInstallCandidate.skills.some((skill) => skill.requested_permissions.length > 0) ? (
                  <ul>
                    {skillInstallCandidate.skills.flatMap((skill) =>
                      skill.requested_permissions.map((permission) => (
                        <li key={`${skill.id}-${permission}`}>
                          {skill.name}: {permission}
                        </li>
                      )),
                    )}
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
                {attachmentDraft.kind === "archive" && archiveInstallFile ? (
                  <button type="button" disabled={uploadSkillArchive.isPending} onClick={() => uploadSkillArchive.mutate(archiveInstallFile)}>
                    {uploadSkillArchive.isPending ? "扫描中..." : "作为 Skill 安装"}
                  </button>
                ) : null}
              </aside>
            ) : null}
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入消息，继续当前对话。例如：这个方案继续往更玄幻一点改。"
              required
            />
            <div className="composer-actions">
              <div className={`composer-tool-row${handoffActive ? " composer-tool-row-reference" : ""}`} aria-label="消息工具">
                <label className="composer-upload-button">
                  <span>附件</span>
                  <input
                    aria-label="上传文件或 Skill 压缩包"
                    type="file"
                    accept={ATTACHMENT_ACCEPT}
                    disabled={uploadSkillArchive.isPending || uploadAttachment.isPending}
                    onChange={(event) => {
                      handleAttachmentUpload(event.currentTarget.files);
                      event.currentTarget.value = "";
                    }}
                  />
                </label>
                {handoffActive ? (
                  <button
                    type="button"
                    className="composer-reference-button composer-toggle-active"
                    aria-label="取消引用会话"
                    title="取消本次新分支的参考会话"
                    onClick={cancelBranchReference}
                  >
                    取消引用
                  </button>
                ) : null}
                <button
                  type="button"
                  className="composer-plus-button"
                  aria-label={configOpen ? "收起本次运行配置" : "打开本次运行配置"}
                  aria-pressed={configOpen}
                  onClick={() => setConfigOpen((current) => !current)}
                >
                  +
                </button>
              </div>
              <div className="composer-status-line" role="status">
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
              </div>
              <div className="composer-send-row">
                {canStopLatestRun && latestVisibleRun ? (
                  <button
                    type="button"
                    className="secondary-action composer-stop-button"
                    disabled={stopCurrentRun.isPending}
                    onClick={() => stopCurrentRun.mutate(latestVisibleRun.id)}
                  >
                    {stopCurrentRun.isPending ? "停止中..." : "停止生成"}
                  </button>
                ) : null}
                <button
                  type="submit"
                  disabled={createRun.isPending || message.trim().length === 0 || Boolean(directSendBlockedReason)}
                >
                  {createRun.isPending ? "发送中..." : "发送"}
                </button>
              </div>
            </div>
            {directSendBlockedReason && !(mode === "direct" && savedModels.length === 0) ? (
              <p className="field-help" role="status">{directSendBlockedReason}</p>
            ) : null}
            {submitNotice ? <p role="status">{submitNotice}</p> : null}
            {uploadSkillArchive.isPending ? <p role="status">正在扫描 Skill 压缩包...</p> : null}
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
            {stopCurrentRun.isError ? <p role="alert">{formatApiError(stopCurrentRun.error, "停止运行失败")}</p> : null}
          </form>
        </div>
      </div>

    </section>
  );
}
