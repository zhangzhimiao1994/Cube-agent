import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, FormEvent, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError, api, formatApiError, type AttachmentUpload, type Conversation, type ConversationMetadata, type ConversationQueueItem, type ModelDeployment, type ProjectWorkspace, type RunDetail, type RunListItem, type Skill, type SkillArchiveUpload, type SubmittedRun, type WorkspaceFileList } from "../api/client";
import { APP_BRAND_NAME } from "../app/brand";
import {
  ArtifactFileCard,
  artifactFileName,
  formatFileSize,
  hasArtifactDownload,
  type DownloadableFile,
} from "../components/ArtifactFileCard";
import { repairActionLabel, repairErrorCodeLabel, repairFailureKindLabel, repairRecoveryStrategyLabel } from "./repairLabels";

const RUN_MODES = [
  { value: "auto", label: "自动", description: "主 Agent 判断应使用直连、派单、讨论或混合；不确定时向你确认。" },
  { value: "direct", label: "直连", description: "由你指定一个模型/API回答，主 Agent 负责控场、提示词和记录。" },
  { value: "dispatch", label: "派单", description: "适合拆成多个专业角色执行；派给谁由工作流或本次选择决定。" },
  { value: "discuss", label: "讨论", description: "适合多角色观点冲突、方案评审或需要裁决的任务。" },
  { value: "hybrid", label: "混合", description: "先讨论定方案，再派单执行，最后审查收口。" },
] as const;

type RunMode = (typeof RUN_MODES)[number]["value"];
type ManualRunMode = Exclude<RunMode, "auto">;
const SANDBOX_OPTIONS = [
  { value: "none", label: "无沙箱", summary: "不预授权工具工作区" },
  { value: "read_only", label: "只读", summary: "可读取项目上下文" },
  { value: "restricted", label: "受限", summary: "读取和命令需受控" },
  { value: "workspace_write", label: "项目写入", summary: "允许在项目工作区读写和执行命令" },
] as const;
type SandboxProfile = (typeof SANDBOX_OPTIONS)[number]["value"];
type ExecutionBackendId = "systemd" | "docker";
export function requestedPermissionsForSandbox(profile: SandboxProfile): string[] {
  if (profile === "none") return [];
  if (profile === "read_only") return ["workspace.read"];
  if (profile === "restricted") return ["workspace.read", "command.run"];
  return ["workspace.read", "workspace.write", "command.run"];
}
export function workspacePreviewPath(projectId: string, sessionId: string): string {
  return `projects/${safeWorkspaceSegment(projectId, "default")}/sessions/${safeWorkspaceSegment(sessionId, "session-default")}`;
}
function safeWorkspaceSegment(value: string, fallback: string): string {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/[-_]{2,}/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
  return normalized || fallback;
}
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
type ProjectPreflightProposal = NonNullable<SubmittedRun["project_preflight_proposal"]>;
type RepairProposal = NonNullable<SubmittedRun["repair_proposal"]>;
type CapabilityApproval = {
  runId: string;
  approvalId: string;
  version: number;
  summary: string;
};
type RunSubmissionOverride = {
  message?: string;
  directModel?: string;
  mode?: RunMode;
  skipEvolutionProposal?: boolean;
  successNotice?: string;
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const TOOL_STATUS_LABELS: Record<string, string> = {
  requested: "已请求",
  running: "进行中",
  started: "进行中",
  waiting_approval: "待确认",
  completed: "已完成",
  succeeded: "已完成",
  failed: "异常",
};
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

function displayChatRunStatus(status: string) {
  const labels: Record<string, string> = {
    queued: "已排队",
    running: "运行中",
    paused: "已暂停",
    completed: "已完成",
    failed: "执行异常",
    cancelled: "已取消",
    waiting_approval: "等待确认",
    waiting_user_mode: "等待模式确认",
  };
  return labels[status] ?? status;
}

function displaySandboxProfile(profile: SandboxProfile) {
  return SANDBOX_OPTIONS.find((item) => item.value === profile)?.label ?? profile;
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
    "runtime.recovered": "恢复完成",
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
    "runtime.recovered": "已从检查点恢复并继续执行。",
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

function localizedRecordedEventLabel(kind: string) {
  const labels: Record<string, string> = {
    "step.started": "开始执行步骤",
    "step.completed": "完成阶段输出",
    "model.started": "开始调用模型",
    "tool.started": "开始使用工具",
    "tool.completed": "完成工具操作",
    "tool.failed": "工具操作失败",
    "decision.started": "开始决策",
    "decision.completed": "完成决策",
    "dispatch.started": "开始派单",
    "dispatch.completed": "完成派单",
  };
  return labels[kind] ?? `记录 ${kind}`;
}

function localizedEventSummaryText(
  summary: string,
  event: RunDetail["events"][number],
  agentNames: Map<string, string>,
) {
  const trimmed = summary.trim();
  if (/^main agent selected the runtime mode, roles, and models\.?$/i.test(trimmed)) {
    return "主 Agent 已选择运行模式、角色和模型";
  }
  const recordedMatch = trimmed.match(/^(.+?)\s+recorded\s+([a-z][a-z0-9_.-]*)\.?$/i);
  if (recordedMatch) {
    const actor = displayEventActor(event.actor || recordedMatch[1], agentNames) ?? recordedMatch[1];
    return `${actor} ${localizedRecordedEventLabel(recordedMatch[2])}`;
  }
  return trimmed;
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
type ConversationWorkspaceFileBuckets = {
  final: DownloadableFile[];
  intermediate: DownloadableFile[];
  total: number;
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
type ConversationCheckpoint = {
  anchorId: string;
  artifactCount: number;
  artifacts: Array<{
    href: string;
    label: string;
  }>;
  href: string;
  id: string;
  label: string;
  index: number;
};
export type SlashCommand = {
  aliases: string[];
  description: string;
  id: "new" | "mode" | "memory" | "skills" | "usage";
  label: string;
};
type EventGroupItem = {
  event: RunEvent;
  index: number;
};

const SLASH_COMMANDS: SlashCommand[] = [
  {
    id: "new",
    label: "/new 新建对话",
    description: "清空当前输入并开启一个新的连续对话。",
    aliases: ["new", "新建", "新对话"],
  },
  {
    id: "memory",
    label: "/memory 记忆",
    description: "打开记忆管理，查看长期偏好、项目事实和摘要。",
    aliases: ["memory", "mem", "记忆", "回忆"],
  },
  {
    id: "mode",
    label: "/mode 模式",
    description: "展开本轮运行设置，切换直连、派单、讨论或混合模式。",
    aliases: ["mode", "模式", "运行"],
  },
  {
    id: "skills",
    label: "/skills 技能",
    description: "进入 Skill 页面查看已安装技能和待审批权限。",
    aliases: ["skills", "skill", "技能", "工具"],
  },
  {
    id: "usage",
    label: "/usage 日志",
    description: "打开日志中心，排查模型调用、失败和运行记录。",
    aliases: ["usage", "logs", "log", "用量", "日志"],
  },
];

export function slashCommandsForQuery(value: string): SlashCommand[] {
  const trimmed = value.trim();
  if (!trimmed.startsWith("/")) return [];
  const query = trimmed.slice(1).trim().toLowerCase();
  if (!query) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter((command) =>
    [command.id, command.label, command.description, ...command.aliases]
      .join(" ")
      .toLowerCase()
      .includes(query),
  );
}

function isFinalDownloadableArtifact(
  artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined,
): artifact is DownloadableArtifact {
  return hasArtifactDownload(artifact) && artifact.presentation === "final_attachment";
}

function dedupeDownloadableArtifacts<T extends { download_url: string }>(artifacts: T[]) {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    const key = artifact.download_url.trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
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

function shouldShowStandaloneDownloadMessage(artifact: DownloadableArtifact) {
  const filename = artifactFileName(artifact);
  const normalizedTitle = artifact.title.trim().toLowerCase();
  const normalizedKind = artifact.kind.trim().toLowerCase();
  if (artifact.presentation !== "final_attachment") return true;
  if (artifact.text?.trim()) return false;
  if (normalizedTitle === "main" || normalizedTitle === "final_synthesizer") return false;
  return !(normalizedKind === "workspace_bundle" || filename === "workspace.zip");
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

function artifactDownloadKey(artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) {
  return hasArtifactDownload(artifact) ? artifact.download_url.trim() : "";
}

function isWorkspaceDownloadArtifact(
  artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined,
): artifact is DownloadableArtifact {
  return hasArtifactDownload(artifact) && artifact.download_url.trim().startsWith("/api/v1/workspaces/");
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

function hasWorkspaceFilePayload(event: RunDetail["events"][number]) {
  return Array.isArray(event.payload.workspace_files) && event.payload.workspace_files.length > 0;
}

function isActionEvent(event: RunDetail["events"][number]) {
  if (isNoiseEvent(event)) return false;
  if (event.kind.startsWith("model.")) {
    if (["model.started", "model.reasoning_delta", "model.text_delta", "model.failed"].includes(event.kind)) {
      return Boolean(event.actor || event.step_id || hasUsefulPayload(event));
    }
    return false;
  }
  if (event.kind.startsWith("harness.")) {
    return Boolean(hasUsefulPayload(event) || event.message);
  }
  if (event.kind === "message.created") return Boolean(event.artifact || hasWorkspaceFilePayload(event));
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
  if (
    event.kind.startsWith("tool.") ||
    event.kind.startsWith("approval.") ||
    event.kind.startsWith("dispatch.") ||
    event.kind.startsWith("discussion.") ||
    event.kind.startsWith("decision.") ||
    event.kind.startsWith("review.") ||
    event.kind.startsWith("runtime.") ||
    event.kind === "temporary_agent.proposed" ||
    event.kind === "observer.notice"
  ) {
    return Boolean(event.actor || event.action || event.tool_name || event.decision || event.artifact || hasUsefulPayload(event));
  }
  return false;
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
    recovery_count: "续跑次数",
    completed_steps: "已完成步骤",
    total_steps: "总步骤",
    model_status_counts: "模型状态",
    tool_status_counts: "工具状态",
    review_artifacts: "审查产物",
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
    "recovery_count",
    "completed_steps",
    "total_steps",
    "model_status_counts",
    "tool_status_counts",
    "review_artifacts",
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

const RUNTIME_RECOVERY_STATUS_LABELS: Record<string, string> = {
  completed: "已完成",
  failed: "异常",
  running: "进行中",
  started: "进行中",
  succeeded: "已完成",
};

function numericPayloadRecordValue(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function runtimeRecoveryPayloadStatusLabel(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return Object.entries(value as Record<string, unknown>)
    .flatMap(([status, count]) => {
      if (typeof count !== "number" || !Number.isFinite(count) || count <= 0) return [];
      return [`${RUNTIME_RECOVERY_STATUS_LABELS[status] ?? status} ${count}`];
    })
    .join("，");
}

function runtimeRecoveryEventSummary(event: RunEvent) {
  const completedSteps =
    numericPayloadRecordValue(event.payload, "completed_steps") ||
    numericPayloadRecordValue(event.payload, "last_completed_steps");
  const totalSteps =
    numericPayloadRecordValue(event.payload, "total_steps") ||
    numericPayloadRecordValue(event.payload, "last_total_steps");
  const modelStatus = runtimeRecoveryPayloadStatusLabel(event.payload.model_status_counts);
  const toolStatus = runtimeRecoveryPayloadStatusLabel(event.payload.tool_status_counts);
  const reviewArtifacts = numericPayloadRecordValue(event.payload, "review_artifacts");
  const headline = totalSteps > 0 ? `恢复完成：${completedSteps}/${totalSteps} 步` : "恢复完成";
  const detailParts = [
    modelStatus ? `模型状态：${modelStatus}` : "",
    toolStatus ? `工具状态：${toolStatus}` : "",
    reviewArtifacts > 0 ? `审查产物 ${reviewArtifacts}` : "",
  ].filter(Boolean);
  return detailParts.length > 0 ? `${headline}。${detailParts.join("；")}` : headline;
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

function isSensitiveEventPayloadKey(key: string) {
  const normalized = key.trim().toLowerCase();
  if (
    [
      "api_base",
      "checkpoint",
      "checkpoint_id",
      "checkpoint_state",
      "lease_id",
      "quota_scope_id",
      "capacity_scope_id",
      "model_execution_plan",
      "reservation_id",
    ].includes(normalized)
  ) {
    return true;
  }
  return /api[_-]?key|secret|token|password|credential/i.test(key);
}

function isSensitiveActionTargetText(value: string) {
  return /api[_-]?key|authorization|bearer|credential|password|private[-_ ]?token|secret|token/i.test(value);
}

function safeActionTargetText(value: unknown) {
  const text = formatEventPayloadValue(value);
  if (!text || isSensitiveActionTargetText(text)) return "";
  return text;
}

function workspaceFilesActionTarget(value: unknown) {
  if (!Array.isArray(value)) return "";
  const targets = value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const target = safeActionTargetText(record.path) || safeActionTargetText(record.filename);
    return target ? [target] : [];
  });
  return targets.slice(0, 3).join("、");
}

function safeActionTargetRows(event: RunDetail["events"][number]) {
  const rows: Array<{ label: string; value: string }> = [];
  const command = safeActionTargetText(event.payload.command);
  if (command) rows.push({ label: "命令", value: command });
  const workspaceFiles = workspaceFilesActionTarget(event.payload.workspace_files);
  if (workspaceFiles) rows.push({ label: "工作区文件", value: workspaceFiles });
  const directTarget =
    safeActionTargetText(event.payload.path) ||
    safeActionTargetText(event.payload.file_path) ||
    safeActionTargetText(event.payload.filename) ||
    safeActionTargetText(event.payload.target);
  if (directTarget && !rows.some((row) => row.value === directTarget)) rows.push({ label: "目标", value: directTarget });
  return rows;
}

function isIntentEventWithRestrictedPayload(event: RunDetail["events"][number]) {
  return event.kind === "step.retrying" || event.kind.startsWith("approval.") || isRepairIntentEvent(event);
}

function eventSafeSummary(event: RunDetail["events"][number]) {
  return formatEventPayloadValue(event.summary);
}

function toolEventName(event: RunDetail["events"][number]) {
  return event.tool_name || formatEventPayloadValue(event.payload.name) || "工具";
}

function toolOperationKindLabel(operationKind: string) {
  const normalized = operationKind.toLowerCase().replace(/[.\s-]+/g, "_");
  if (normalized === "terminal" || normalized === "server_command") return "运行终端";
  if (normalized === "file_create") return "创建文件";
  if (normalized === "file_edit" || normalized === "file_write") return "编辑文件";
  if (normalized === "file_read") return "读取文件";
  if (normalized === "browser" || normalized === "screen_read" || normalized === "desktop_action") return "浏览操作";
  return "";
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

function toolOperationLabelForEvent(event: RunDetail["events"][number]) {
  const operationKindLabel = toolOperationKindLabel(formatEventPayloadValue(event.payload.operation_kind));
  return operationKindLabel || toolOperationLabel(toolEventName(event));
}

function toolDisplayName(event: RunDetail["events"][number]) {
  const toolName = toolEventName(event);
  const operation = toolOperationLabelForEvent(event);
  return operation === "使用工具" ? toolName : operation;
}

function toolSummaryWithDisplay(event: RunDetail["events"][number], suffix = "") {
  const toolName = toolEventName(event);
  const operation = toolOperationLabelForEvent(event);
  const displayName = toolDisplayName(event);
  const detail = displayName === operation ? "" : `：${displayName}`;
  return `${operation}${suffix}${detail}`;
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
  if (event.tool_name) rows.push({ label: "工具", value: toolDisplayName(event) });
  if (event.tool_call_id) rows.push({ label: "调用 ID", value: event.tool_call_id });
  if (event.step_id) rows.push({ label: "步骤", value: event.step_id });
  if (event.approval_id) rows.push({ label: "审批 ID", value: event.approval_id });
  if (event.action) rows.push({ label: "动作", value: repairScopedActionLabel(event, event.action) });
  if (event.decision) rows.push({ label: "决策", value: event.decision });
  const safeSummary = eventSafeSummary(event);
  if (safeSummary) rows.push({ label: "安全摘要", value: safeSummary });
  if (readableMessage) rows.push({ label: "事件内容", value: readableMessage });
  safeActionTargetRows(event).forEach((row) => rows.push(row));
  orderedEventPayloadEntries(event.payload).forEach(([key, value]) => {
    if (isSensitiveEventPayloadKey(key)) return;
    if (key === "participants" || key === "participant_models") return;
    if (key === "discussion_trace" || key === "dispatch_discussion_trace" || key === "coordination_trace") return;
    if (key === "workspace_files") return;
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
    const formatted = formatEventPayloadDisplayValue(key, value);
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
  sourceSequence?: number;
  sourceStepId?: string | null;
  sourceActor?: string | null;
};

type ProcessDetailValuePresentation = {
  kind: "plain" | "json" | "code";
  label: string;
  copyLabel: string;
  text: string;
  shouldCollapse: boolean;
};

function prettyJsonValue(value: string) {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return null;
  }
}

function looksLikeCommandLabel(label: string) {
  return /命令|终端|shell|bash|command/i.test(label);
}

function looksLikeCodeValue(value: string) {
  if (!value.includes("\n")) return false;
  return /\b(import|export|const|let|function|class|return|def|from|SELECT|POST|GET|npm|pnpm|ssh|curl)\b/.test(value);
}

function detailBlockLanguage(label: string, value: string) {
  if (looksLikeCommandLabel(label)) return "bash";
  if (/tsx?|jsx?|typescript|javascript/i.test(`${label} ${value.slice(0, 80)}`)) return "ts";
  if (/python|\.py\b|def\s+\w+/i.test(`${label} ${value.slice(0, 120)}`)) return "python";
  return "text";
}

export function processDetailValuePresentation(row: { label: string; value: string }): ProcessDetailValuePresentation {
  const trimmed = row.value.trim();
  const jsonText = prettyJsonValue(trimmed);
  if (jsonText) {
    return {
      kind: "json",
      label: "json",
      copyLabel: "复制 json 内容",
      text: jsonText,
      shouldCollapse: jsonText.length > 1200 || jsonText.split("\n").length > 18,
    };
  }
  if (looksLikeCommandLabel(row.label) || looksLikeCodeValue(trimmed)) {
    const label = detailBlockLanguage(row.label, trimmed);
    return {
      kind: "code",
      label,
      copyLabel: `复制 ${label} 内容`,
      text: trimmed,
      shouldCollapse: trimmed.length > 1200 || trimmed.split("\n").length > 18,
    };
  }
  return {
    kind: "plain",
    label: "text",
    copyLabel: "复制文本",
    text: row.value,
    shouldCollapse: row.value.length > 900 || row.value.split("\n").length > 12,
  };
}

type WorkbenchFileItem = {
  id: string;
  title: string;
  filename: string;
  path: string | null;
  kind: string;
  operation: "创建文件" | "编辑文件" | "读取文件" | "产物" | "文件夹";
  mimeType: string | null;
  size: string;
  sha256: string | null;
  text: string | null;
  download?: DownloadableFile;
  source: ProcessDetailTarget | null;
};

type WorkbenchActionDescriptor = {
  operation: string;
  target: string;
  summary: string;
  meta: string[];
};

type AgentDispatchCard = {
  id: string;
  name: string;
  role: string;
  model: string;
  summary: string;
  purpose: string;
  taskInputs: string[];
  dependencyInputs: string[];
  toolInputs: string[];
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

const WORKBENCH_ACTION_PREVIEW_LIMIT = 12;
const WORKBENCH_AGENT_ACTIVITY_PREVIEW_LIMIT = 5;
const WORKBENCH_COORDINATION_SECTION_LIMIT = 4;
const AGENT_NICKNAMES = [
  "费曼",
  "陆思聆",
  "沈括",
  "顾准",
  "林衡",
  "苏澈",
  "程予",
  "许砚",
  "白芷",
  "周晏",
  "叶岚",
  "秦越",
  "陶然",
  "闻舟",
  "韩序",
  "江宁",
  "夏衡",
  "洛川",
  "宁远",
  "方知",
  "黎初",
  "孟珩",
  "楚越",
  "谢安",
];

function recentPreview<T>(items: T[], limit: number, expanded = false) {
  if (expanded || items.length <= limit) {
    return { visible: items, hiddenCount: 0 };
  }
  return { visible: items.slice(-limit), hiddenCount: items.length - limit };
}

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

function isWorkbenchAgentId(agentId: string) {
  return agentId.length > 0 && agentId !== "main_agent" && agentId !== "main" && agentId !== "system";
}

function fallbackAgentCards(detail: RunDetail, agentNames: Map<string, string>): AgentDispatchCard[] {
  const ids = new Set<string>();
  detail.explicit_details.selected_agent_ids
    ?.split(",")
    .map((item) => item.trim())
    .filter(isWorkbenchAgentId)
    .forEach((id) => ids.add(id));
  detail.events.forEach((event) => {
    if (event.actor && isWorkbenchAgentId(event.actor)) ids.add(event.actor);
    event.participants.filter(isWorkbenchAgentId).forEach((id) => ids.add(id));
  });
  return [...ids].map((id) => ({
    id,
    name: stableAgentNickname(id),
    role: agentFunctionLabel({ id, name: agentNames.get(id), role: "Agent" }),
    model:
      detail.events
        .map((event) => (event.actor === id ? eventModelName(event) : ""))
        .find((model) => model.length > 0) ?? "默认模型",
    summary: "参与本轮调度或讨论，可查看该成员的动作轨迹。",
    purpose: "",
    taskInputs: [],
    dependencyInputs: [],
    toolInputs: [],
    status: agentStatusForPlan(detail, id, new Set()),
  }));
}

function planStepInputSummary(step: Record<string, unknown>) {
  return (
    stringValue(step.task) ??
    stringValue(step.summary) ??
    stringValue(step.objective) ??
    stringValue(step.instruction) ??
    stringValue(step.instructions) ??
    stringValue(step.title) ??
    (stringValue(step.id) ? `步骤 ${stringValue(step.id)}` : null)
  );
}

function uniqLimited(values: string[], limit = 3) {
  const unique = [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  if (unique.length <= limit) return unique;
  return [...unique.slice(0, limit), `另 ${unique.length - limit} 项`];
}

function purposeLabel(value: string | null) {
  const labels: Record<string, string> = {
    discuss: "讨论",
    execute: "执行",
    plan: "规划",
    review: "审核",
    repair: "修复",
    synthesize: "汇总",
    verify: "验证",
  };
  if (!value) return "";
  return labels[value] ?? value;
}

function stableAgentNickname(agentId: string) {
  let hash = 0;
  for (const character of agentId) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return AGENT_NICKNAMES[hash % AGENT_NICKNAMES.length];
}

function agentFunctionLabel({
  id,
  name,
  purpose,
  role,
}: {
  id: string;
  name?: string | null;
  purpose?: string | null;
  role?: string | null;
}) {
  const combined = [id, name, role, purpose].filter(Boolean).join(" ").toLowerCase();
  if (/context|loader|上下文/.test(combined)) return "上下文";
  if (/implement|coder|engineer|build|代码|实现|工程/.test(combined)) return "实现";
  if (/review|critic|security|审查|审核|安全/.test(combined)) return "审查";
  if (/verify|verifier|test|qa|acceptance|验证|测试/.test(combined)) return "验证";
  if (/plan|architect|planner|架构|规划|计划/.test(combined)) return "规划";
  if (/repair|fix|self.?repair|修复/.test(combined)) return "修复";
  if (/summar|synth|final|汇总|总结/.test(combined)) return "汇总";
  if (/copy|writer|文案|写作/.test(combined)) return "文案";
  if (/director|导演/.test(combined)) return "导演";
  if (/editor|剪辑/.test(combined)) return "剪辑";
  const purposeText = purposeLabel(purpose ?? null);
  if (purposeText) return purposeText;
  const savedName = name?.trim();
  if (savedName && savedName !== id) return savedName;
  const roleText = role?.trim();
  if (roleText && roleText !== "Agent") return roleText;
  return "协作";
}

function agentDisplayTitle(card: Pick<AgentDispatchCard, "name" | "role">) {
  return card.role && card.role !== "Agent" ? `${card.name} · ${card.role}` : card.name;
}

function dispatchAgentCards(detail: RunDetail, agentNames: Map<string, string>): AgentDispatchCard[] {
  const plan = mainAgentPlanEvent(detail);
  if (!plan) return fallbackAgentCards(detail, agentNames);
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
  const cards = roles
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
      const savedName = agentNames.get(id);
      const model = stringValue(role.logical_model) ?? "默认模型";
      const roleSummary =
        stringValue(role.summary) ??
        stringValue(role.goal) ??
        stringValue(role.description) ??
        stringValue(role.backstory) ??
        "等待执行分配任务";
      const ownedSteps = steps.filter((step) => stringValue(step.agent) === id);
      const taskInputs = uniqLimited(ownedSteps.flatMap((step) => (planStepInputSummary(step) ? [planStepInputSummary(step) as string] : [])));
      const dependencyInputs = uniqLimited(ownedSteps.flatMap((step) => stringArrayValue(step.depends_on)));
      const toolInputs = uniqLimited([...stringArrayValue(role.tools), ...ownedSteps.flatMap((step) => stringArrayValue(step.tools))]);
      return {
        id,
        name: stableAgentNickname(id),
        role: agentFunctionLabel({ id, name: savedName, purpose, role: roleLabel }),
        model,
        summary: conciseProcessText(roleSummary, "等待执行分配任务"),
        purpose: purposeLabel(purpose),
        taskInputs,
        dependencyInputs,
        toolInputs,
        status: agentStatusForPlan(detail, id, stepIds),
      };
    })
    .filter((card): card is AgentDispatchCard => card !== null);
  return cards.length > 0 ? cards : fallbackAgentCards(detail, agentNames);
}

function agentWorkbenchStatusCounts(cards: AgentDispatchCard[]) {
  const statuses: AgentDispatchCard["status"][] = ["异常", "工作中", "已完成", "已安排"];
  return statuses.flatMap((status) => {
    const count = cards.filter((card) => card.status === status).length;
    return count > 0 ? [`${count} ${status}`] : [];
  });
}

function agentWorkbenchMeta({
  cards,
  diagnostics,
  files,
  intents,
  items,
  taskChain,
}: {
  cards: AgentDispatchCard[];
  diagnostics: RunFailureDiagnostic[];
  files?: WorkbenchFileItem[];
  intents: RunExecutionIntent[];
  items: ProcessDetailTarget[];
  taskChain: TaskChainStep[];
}) {
  const filePart = files && files.length > 0 ? `${files.length} 个文件` : "";
  if (cards.length > 0) return [`${cards.length} 个 Agent`, filePart, ...agentWorkbenchStatusCounts(cards)].filter(Boolean).join(" · ");
  const parts = [
    items.length > 0 ? `${items.length} 条证据` : "",
    filePart,
    taskChain.length > 0 ? `${taskChain.length} 个步骤` : "",
    diagnostics.length > 0 ? `${diagnostics.length} 个诊断` : "",
    intents.length > 0 ? `${intents.length} 个意图` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

function agentActivityItems(card: AgentDispatchCard, items: ProcessDetailTarget[]) {
  return items.filter((item) => item.sourceActor === card.id || item.rows.some((row) => row.value.includes(card.name) || row.value.includes(card.id)));
}

function agentRoleActionSummary(card: AgentDispatchCard) {
  const key = `${card.name} ${card.role} ${card.id}`.toLowerCase();
  if (/context|上下文/.test(key)) return "加载上下文与约束";
  if (/plan|architect|规划|计划|架构/.test(key)) return "拆解计划与方案";
  if (/implement|coder|build|实现|工程|代码/.test(key)) return "实现代码与产物";
  if (/review|critic|审查|审核|安全/.test(key)) return "审查风险与质量";
  if (/verify|test|qa|验证|测试/.test(key)) return "验证功能与结果";
  if (/repair|fix|修复/.test(key)) return "定位问题并修复";
  if (/summar|synth|汇总|总结/.test(key)) return "汇总结果与交付";
  if (/copy|writer|文案|写手/.test(key)) return "撰写内容与交付文案";
  if (/director|导演/.test(key)) return "把控流程与现场节奏";
  if (/editor|剪辑/.test(key)) return "整理素材与剪辑结构";
  return "";
}

export function agentInlineSummary(card: AgentDispatchCard) {
  const concreteTask = card.taskInputs.find((item) => !/^步骤\s+[-_\w]+$/i.test(item));
  if (concreteTask) return concreteTask;
  if (card.summary && card.summary !== "等待执行分配任务") return card.summary;
  const roleAction = agentRoleActionSummary(card);
  if (roleAction) return roleAction;
  if (card.purpose && card.role && card.role !== "Agent") return `${card.purpose}：${card.role}`;
  if (card.role && card.role !== "Agent") return `负责 ${card.role}`;
  return "参与本轮调度";
}

function processTargetRowValue(item: ProcessDetailTarget, labelPattern: RegExp) {
  return item.rows.find((row) => labelPattern.test(row.label))?.value.trim() ?? "";
}

function processTargetActionOperation(item: ProcessDetailTarget) {
  const operationKind = processTargetRowValue(item, /操作类别/);
  const operationLabel = operationKind ? toolOperationKindLabel(operationKind) : "";
  if (operationLabel) return operationLabel;
  if (/运行终端|创建文件|编辑文件|读取文件|浏览操作/.test(item.badge)) return item.badge;
  if (/运行终端|创建文件|编辑文件|读取文件|浏览操作/.test(item.title)) return item.title;
  if (item.sourceKind?.startsWith("tool.")) return "工具动作";
  if (item.sourceKind?.startsWith("model.")) return "模型过程";
  return "";
}

function processTargetActionTarget(item: ProcessDetailTarget) {
  const primaryTarget =
    processTargetRowValue(item, /^(关联文件|文件|路径|工作区文件|命令|目标)$/) ||
    processTargetRowValue(item, /command|path|file|target/i);
  if (primaryTarget) return conciseProcessText(primaryTarget, "");
  if (item.artifact) return artifactFileName(item.artifact);
  if (item.sourceStepId) return `步骤 ${item.sourceStepId}`;
  const fallbackTarget = processTargetRowValue(item, /^工具$/) || processTargetRowValue(item, /^步骤$/);
  if (fallbackTarget) return conciseProcessText(fallbackTarget, "");
  return "";
}

function processTargetActionMeta(item: ProcessDetailTarget) {
  const chips = [
    processTargetRowValue(item, /^执行者$/) || (item.sourceActor === "main_agent" || item.sourceActor === "main" ? "主 Agent" : item.sourceActor ?? ""),
    processTargetActionOperation(item),
    processTargetActionTarget(item),
  ];
  const seen = new Set<string>();
  return chips
    .map((chip) => conciseProcessText(chip, "").trim())
    .filter((chip) => {
      if (!chip || seen.has(chip)) return false;
      seen.add(chip);
      return true;
    })
    .slice(0, 3);
}

export function workbenchActionDescriptor(
  item: ProcessDetailTarget,
  files: Pick<WorkbenchFileItem, "path" | "filename" | "operation">[] = [],
): WorkbenchActionDescriptor {
  const concreteFile = files.find((file) => file.operation !== "产物" && file.operation !== "文件夹");
  const operation = concreteFile?.operation || processTargetActionOperation(item) || item.badge;
  const fileTarget = files.map((file) => file.path || file.filename).find((value) => value.trim().length > 0);
  const target = fileTarget || item.message || processTargetActionTarget(item) || item.sourceStepId || item.title;
  const summary = item.message === target ? item.title : item.message;
  const actor = processTargetRowValue(item, /^执行者$/) || item.sourceActor || "";
  const meta = fileTarget ? [actor, operation, target, ...processTargetActionMeta(item)] : processTargetActionMeta(item);
  const seen = new Set<string>();
  return {
    operation,
    target,
    summary,
    meta: meta
      .map((value) => conciseProcessText(value, "").trim())
      .filter((value) => {
        if (!value || seen.has(value)) return false;
        seen.add(value);
        return true;
      })
      .slice(0, 4),
  };
}

function isWorkbenchExecutionItem(item: ProcessDetailTarget) {
  const kind = item.sourceKind ?? "";
  if ((item.sourceActor === "main_agent" || item.sourceActor === "main") && item.sourceStepId === "main_agent_plan") return false;
  if (item.artifact) return true;
  if (item.rows.some((row) => row.label === "关联文件")) return true;
  if (processTargetActionOperation(item)) return true;
  if (kind.startsWith("tool.") || kind.startsWith("model.") || kind.startsWith("artifact.") || kind.startsWith("message.")) return true;
  if (kind.startsWith("step.") || kind.startsWith("review.")) return !/调度|讨论|决策/.test(`${item.badge} ${item.title} ${item.message}`);
  return false;
}

function isWorkbenchCoordinationItem(item: ProcessDetailTarget) {
  if (isWorkbenchExecutionItem(item)) return false;
  return (
    item.badge === "调度判断" ||
    item.badge === "调度过程" ||
    item.badge === "讨论过程" ||
    item.badge === "决策过程" ||
    (item.sourceActor === "main_agent" && item.badge !== "断点续跑")
  );
}

type WorkbenchCoordinationSnippet = {
  id: string;
  label: string;
  text: string;
  target: ProcessDetailTarget;
};

type WorkbenchCoordinationSection = {
  key: string;
  title: string;
  empty: string;
  snippets: WorkbenchCoordinationSnippet[];
};

function minutePart(value: string, label: string) {
  const match = value.match(new RegExp(`${label}[:：]([^；;]+)`));
  return match?.[1]?.trim() ?? "";
}

function addCoordinationSnippet(
  sections: Map<string, WorkbenchCoordinationSnippet[]>,
  key: string,
  item: ProcessDetailTarget,
  label: string,
  text: string,
) {
  const normalized = conciseProcessText(text, "").trim();
  if (!normalized) return;
  const bucket = sections.get(key) ?? [];
  if (bucket.some((snippet) => snippet.text === normalized && snippet.label === label)) return;
  bucket.push({
    id: `${item.id}-${key}-${bucket.length}`,
    label,
    text: normalized,
    target: item,
  });
  sections.set(key, bucket);
}

function coordinationEvidenceSections(items: ProcessDetailTarget[]): WorkbenchCoordinationSection[] {
  const sections = new Map<string, WorkbenchCoordinationSnippet[]>();
  items.forEach((item) => {
    if (item.badge === "调度判断" || item.badge === "调度过程") {
      addCoordinationSnippet(sections, "dispatch", item, item.badge, item.message);
    }
    if (item.badge === "决策过程") {
      addCoordinationSnippet(sections, "decision", item, item.badge, item.message);
    }
    item.rows.forEach((row) => {
      if (row.label.endsWith("意见")) {
        addCoordinationSnippet(sections, "statements", item, row.label, row.value);
      }
      if (/分歧|风险|冲突|concern/i.test(row.label)) {
        addCoordinationSnippet(sections, "disagreement", item, row.label, row.value);
      }
      if (/验证|求证|证据|检查|测试|安全摘要/.test(row.label)) {
        addCoordinationSnippet(sections, "verification", item, row.label, row.value);
      }
      if (/裁决|决策|结论/.test(row.label)) {
        addCoordinationSnippet(sections, "decision", item, row.label, row.value);
      }
      if (row.label === "会议纪要") {
        addCoordinationSnippet(sections, "disagreement", item, "分歧", minutePart(row.value, "分歧"));
        addCoordinationSnippet(sections, "decision", item, "结论", minutePart(row.value, "结论"));
      }
    });
  });
  const descriptors = [
    { key: "dispatch", title: "派工依据", empty: "暂无派工依据" },
    { key: "statements", title: "成员发言", empty: "暂无成员发言" },
    { key: "disagreement", title: "分歧与风险", empty: "暂无分歧记录" },
    { key: "verification", title: "求证与验证", empty: "暂无求证记录" },
    { key: "decision", title: "最终决策", empty: "暂无最终决策" },
  ];
  return descriptors.map((descriptor) => ({
    ...descriptor,
    snippets: (sections.get(descriptor.key) ?? []).slice(0, WORKBENCH_COORDINATION_SECTION_LIMIT),
  }));
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
        agentName: agentDisplayTitle({
          name: stableAgentNickname(agentId),
          role: agentFunctionLabel({
            id: agentId,
            name: agentNames.get(agentId),
            role: roleNames.get(agentId),
          }),
        }),
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

function safeActionLabel(value: unknown) {
  const text = formatEventPayloadValue(value);
  if (!text || text.length > 80) return "";
  return /^[A-Za-z0-9_.:/@-]+$/.test(text) ? repairActionLabel(text) : "";
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
  return safeActionLabel(event.action) || safeIntentValue(event, SAFE_INTENT_PAYLOAD_KEYS) || fallback;
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
      title: safeActionLabel(event.action) || "需要确认",
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
      const failureKind = repairFailureKindLabel(formatEventPayloadValue(event.payload.failure_kind));
      const exitCode = formatEventPayloadValue(event.payload.exit_code);
      const outputBytes = formatEventPayloadValue(event.payload.output_bytes);
      pushUniqueDiagnostic(diagnostics, {
        id: `${detail.id}-diagnostic-tool-${toolLifecycleKey(event)}`,
        label: "工具执行失败",
        title: toolDisplayName(event),
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
    repairFailureKindLabel(diagnostic.failure_kind),
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
      diagnostic.error_code ? `错误码 ${repairErrorCodeLabel(diagnostic.error_code)}` : "",
      typeof diagnostic.retryable === "boolean" ? `可重试 ${diagnostic.retryable ? "是" : "否"}` : "",
      diagnostic.error_category ? `类型 ${repairFailureKindLabel(diagnostic.error_category)}` : "",
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
      detail: toolDisplayName(finalEvent),
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
          repairFailureKindLabel(safeIntentValue(event, ["failure_kind"])) || safeIntentValue(event, ["status"]),
          replaySafetyLabel(event.payload.replay_safe),
        ].filter(Boolean),
        tone: "retry",
      });
      return;
    }
    if (isRepairIntentEvent(event)) {
      const rawRepairAction = safeIntentValue(event, ["repair_action", "repair_kind", "remediation_action"]);
      const repairAction = repairActionLabel(rawRepairAction) || safeActionLabel(event.action) || "修复方案";
      const failureKind = repairFailureKindLabel(safeIntentValue(event, ["failure_kind"]));
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-repair-${event.step_id ?? event.sequence}`,
        label: "修复意图",
        title: repairAction,
        detail: event.action && event.action !== rawRepairAction ? event.action : repairStatusLabel(event),
        meta: [
          repairAttemptLabel(event),
          isPayloadFlagTrue(event.payload.requires_approval) ? "需要确认" : "",
          failureKind,
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

export function runDetailVersion(run: RunDetail): number {
  if (typeof run.version === "number" && Number.isInteger(run.version) && run.version > 0) {
    return run.version;
  }
  const parsedVersion = Number(run.explicit_details.version ?? "0");
  return Number.isInteger(parsedVersion) && parsedVersion > 0 ? parsedVersion : 0;
}

function runMaxEventSequence(run: RunDetail): number {
  return run.events.reduce((max, event) => (event.sequence > max ? event.sequence : max), 0);
}

function runHasNewerProgress(candidate: RunDetail, current: RunDetail): boolean {
  const candidateVersion = runDetailVersion(candidate);
  const currentVersion = runDetailVersion(current);
  if (candidateVersion !== currentVersion) return candidateVersion > currentVersion;
  const candidateSequence = runMaxEventSequence(candidate);
  const currentSequence = runMaxEventSequence(current);
  if (candidateSequence !== currentSequence) return candidateSequence > currentSequence;
  return candidate.artifacts.length > current.artifacts.length;
}

function preferredRunSnapshot(current: RunDetail, candidate: RunDetail): RunDetail {
  if (runHasNewerProgress(candidate, current)) return candidate;
  if (runHasNewerProgress(current, candidate)) return current;
  return candidate;
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
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: runDetailVersion(run),
    reason: run.explicit_details.routing_reason ?? null,
  };
}

function temporaryApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.decision_token || !run.temporary_agent_proposal) {
    return null;
  }
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: runDetailVersion(run),
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
    confirmed: false,
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

function projectPreflightApprovalFromRunDetail(run: RunDetail | undefined) {
  if (!run || run.status !== "waiting_approval" || !run.decision_token || !run.project_preflight_proposal) {
    return null;
  }
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: runDetailVersion(run),
    proposal: run.project_preflight_proposal,
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
  return {
    runId: run.id,
    decisionToken: run.decision_token,
    version: runDetailVersion(run),
    proposal: run.repair_proposal,
  };
}

function capabilityApprovalFromRunDetail(run: RunDetail | undefined): CapabilityApproval | null {
  if (!run || run.status !== "waiting_approval") return null;
  if (run.explicit_details.approval_kind !== "capability_tool") return null;
  const approvalId = run.explicit_details.approval_id?.trim();
  if (!approvalId) return null;
  const pending = approvalStateFromEvents(run.events).pending.at(-1);
  const diagnostic = run.failure_diagnostics.find((item) => item.approval_id === approvalId);
  const summary =
    diagnostic?.reason ||
    diagnostic?.recommendation ||
    pending?.action ||
    pending?.summary ||
    "当前工具调用需要沙箱权限确认";
  return {
    runId: run.id,
    approvalId,
    version: runDetailVersion(run),
    summary,
  };
}

function repairScopedActionLabel(event: RunEvent, action: string) {
  if (!isRepairIntentEvent(event)) return action;
  return repairActionLabel(action);
}

function formatEventPayloadDisplayValue(key: string, value: unknown) {
  if (key === "repair_action" || key === "repair_kind" || key === "remediation_action") {
    return repairActionLabel(formatEventPayloadValue(value));
  }
  if (key === "failure_kind") {
    return repairFailureKindLabel(formatEventPayloadValue(value));
  }
  if (key === "recovery_strategy" || key === "orchestration_recovery_hint") {
    return repairRecoveryStrategyLabel(formatEventPayloadValue(value));
  }
  if (key === "model_status_counts" || key === "tool_status_counts") {
    return runtimeRecoveryPayloadStatusLabel(value);
  }
  if (key === "requires_approval") {
    const formatted = formatEventPayloadValue(value);
    if (!formatted) return "";
    return isPayloadFlagTrue(value) ? "需要确认" : "不需要确认";
  }
  if (key === "automatic_execution") {
    const formatted = formatEventPayloadValue(value);
    if (!formatted) return "";
    return isPayloadFlagTrue(value) ? "自动执行" : "等待确认";
  }
  return formatEventPayloadValue(value);
}

function repairProposalBody(proposal: RepairProposal) {
  return [
    proposal.summary,
    `失败类型：${repairFailureKindLabel(proposal.failure_kind)}`,
    `修复动作：${repairActionLabel(proposal.repair_action)}`,
    `修复次数：第 ${proposal.attempt}/${proposal.max_attempts} 次`,
    proposal.instruction ? `受控指令：${proposal.instruction}` : "",
    proposal.recovery_strategy ? `恢复策略：${repairRecoveryStrategyLabel(proposal.recovery_strategy)}` : "",
    proposal.orchestration_recovery_hint
      ? `角色交接恢复：${repairRecoveryStrategyLabel(proposal.orchestration_recovery_hint)}`
      : "",
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

function projectPreflightProposalBody(proposal: ProjectPreflightProposal) {
  return [
    proposal.summary,
    `预检能力：${proposal.capability}`,
    `计划文件：${proposal.plan_path}；图谱：${proposal.graph_path}。`,
    "批准后主 Agent 会按该预检方向继续执行，不会跳过约束和技能规则读取。",
  ].join("\n\n");
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
  const downloadableArtifacts = dedupeDownloadableArtifacts(detail.artifacts.filter(isFinalDownloadableArtifact));
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
          .filter(shouldShowStandaloneDownloadMessage)
          .map(downloadArtifactMessage),
      ]
    : detail.artifacts
        .filter((artifact) => !artifact.text?.trim())
        .filter((artifact) => !isFinalDownloadableArtifact(artifact) || shouldShowStandaloneDownloadMessage(artifact))
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
    ...(detail.status === "waiting_approval" && detail.project_preflight_proposal
      ? [
          {
            id: `${detail.id}-project-preflight-approval`,
            role: "assistant" as const,
            title: detail.project_preflight_proposal.title,
            body: projectPreflightProposalBody(detail.project_preflight_proposal),
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
      diagnostic.error_code ? `错误码：${repairErrorCodeLabel(diagnostic.error_code)}` : null,
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
  const failureKind = repairFailureKindLabel(formatEventPayloadValue(event.payload.failure_kind));
  const exitCode = formatEventPayloadValue(event.payload.exit_code);
  const outputBytes = formatEventPayloadValue(event.payload.output_bytes);
  return [
    toolSummaryWithDisplay(event, "失败"),
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

export function runConversationId(detail: RunDetail | undefined) {
  return detail?.explicit_details.conversation_id?.trim() || detail?.conversation_id?.trim() || null;
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

function timestampValue(value: string | null | undefined) {
  if (!value) return Number.MAX_SAFE_INTEGER;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? Number.MAX_SAFE_INTEGER : timestamp;
}

function compareTimestamp(left: string | null | undefined, right: string | null | undefined) {
  return timestampValue(left) - timestampValue(right);
}

function orderedConversationRuns(runs: RunDetail[]) {
  return [...runs].sort((left, right) => compareTimestamp(left.created_at, right.created_at) || left.id.localeCompare(right.id));
}

function orderedRunEvents(events: RunDetail["events"]) {
  return [...events].sort(
    (left, right) =>
      left.sequence - right.sequence ||
      compareTimestamp(left.created_at, right.created_at) ||
      left.kind.localeCompare(right.kind),
  );
}

function conversationTitle(run: RunListItem, items: RunListItem[]) {
  const persistedTitle = run.conversation_title?.trim();
  if (persistedTitle) return persistedTitle;
  const fallback = run.id.slice(0, 8);
  const conversationKey = run.conversation_id?.trim();
  const sameConversation = conversationKey ? items.filter((item) => item.conversation_id === conversationKey) : [];
  const firstRun = sameConversation.length > 0 ? sameConversation.at(-1) : run;
  const question = normalizeConversationQuestion(firstRun?.request, fallback);
  const timestamp = conversationTimestamp(firstRun?.created_at);
  return timestamp ? `${question} · ${timestamp}` : question;
}

export function conversationMatchesSearch(run: RunListItem, query: string, items: RunListItem[] = [run]) {
  const tokens = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return true;
  const conversationRuns = run.conversation_id
    ? items.filter((item) => item.conversation_id === run.conversation_id)
    : [run];
  const haystack = [
    ...conversationRuns.flatMap((item) => [item.request ?? "", item.id]),
    run.conversation_id ?? "",
    run.mode,
    displayMode(run.mode),
    run.status,
    displayChatRunStatus(run.status),
  ]
    .join(" ")
    .toLocaleLowerCase();
  return tokens.every((token) => haystack.includes(token));
}

export function conversationSelectionIds(items: RunListItem[], query: string) {
  return items
    .filter((item) => conversationMatchesSearch(item, query, items) && TERMINAL_STATUSES.has(item.status))
    .map((item) => item.id);
}

export function conversationIdFromSearch(search: string) {
  return new URLSearchParams(search).get("conversation")?.trim() || null;
}

export function shouldShowModeEntry(search: string) {
  return conversationIdFromSearch(search) === null;
}

export function conversationMessages(runs: RunDetail[]): ChatMessage[] {
  const seenDownloadMessages = new Set<string>();
  return orderedConversationRuns(runs).flatMap((run) =>
    detailMessages(run)
      .filter((message) => {
        const downloadKey = artifactDownloadKey(message.artifact);
        if (!downloadKey) return true;
        const messageKey = `${run.id}:${downloadKey}`;
        if (seenDownloadMessages.has(messageKey)) return false;
        seenDownloadMessages.add(messageKey);
        return true;
      })
      .map((message) => ({
        ...message,
        id: `${run.id}-${message.id}`,
        run,
      })),
  );
}

export function conversationCheckpoints(messages: ChatMessage[]): ConversationCheckpoint[] {
  return messages
    .filter((message) => message.role === "user" && message.id.endsWith("-request"))
    .map((message, index) => {
      const artifacts = messages
        .filter((candidate) => candidate.run?.id === message.run?.id && candidate.artifact)
        .map((candidate) => ({
          href: `#${chatMessageAnchorId(candidate.id)}`,
          label: artifactDisplayName(candidate.artifact!),
        }));
      return {
        anchorId: chatMessageAnchorId(message.id),
        artifactCount: artifacts.length,
        artifacts,
        href: `#${chatMessageAnchorId(message.id)}`,
        id: message.id,
        label: normalizeConversationQuestion(message.body, `第 ${index + 1} 轮`),
        index: index + 1,
      };
    });
}

function chatMessageAnchorId(messageId: string) {
  return `chat-message-${messageId.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

export function scrollToConversationHash(behavior: ScrollBehavior = "smooth") {
  if (!window.location.hash.startsWith("#chat-message-")) return false;
  let targetId: string;
  try {
    targetId = decodeURIComponent(window.location.hash.slice(1));
  } catch {
    return false;
  }
  const target = document.getElementById(targetId);
  if (!target) return false;
  target.scrollIntoView({ block: "start", behavior });
  target.tabIndex = -1;
  target.focus({ preventScroll: true });
  return true;
}

function navigateToConversationHash(href: string) {
  window.history.pushState(null, "", href);
  scrollToConversationHash();
}

export function ConversationCheckpointNav({
  checkpoints,
  conversationId,
}: {
  checkpoints: ConversationCheckpoint[];
  conversationId?: string;
}) {
  const [query, setQuery] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [hashNotice, setHashNotice] = useState<string | null>(null);
  const checkpointHashKey = checkpoints.map((checkpoint) => checkpoint.anchorId).join("|");
  useEffect(() => {
    let attempts = 0;
    let retryTimer: number | undefined;
    const restore = () => {
      if (!window.location.hash.startsWith("#chat-message-")) {
        setHashNotice(null);
        return;
      }
      if (scrollToConversationHash(attempts === 0 ? "auto" : "smooth")) {
        setHashNotice(null);
        return;
      }
      attempts += 1;
      if (attempts < 10) {
        retryTimer = window.setTimeout(restore, 100);
      } else {
        setHashNotice("未找到这个检查点，可能已被删除或尚未加载。");
      }
    };
    const restoreFromHistory = () => {
      attempts = 0;
      if (retryTimer) window.clearTimeout(retryTimer);
      restore();
    };
    restore();
    window.addEventListener("hashchange", restoreFromHistory);
    window.addEventListener("popstate", restoreFromHistory);
    return () => {
      if (retryTimer) window.clearTimeout(retryTimer);
      window.removeEventListener("hashchange", restoreFromHistory);
      window.removeEventListener("popstate", restoreFromHistory);
    };
  }, [checkpointHashKey]);
  if (checkpoints.length < 2) return null;
  const visibleCheckpoints = checkpoints.filter((checkpoint) =>
    `${checkpoint.index} ${checkpoint.label}`.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const expandedCheckpoint = checkpoints.find((checkpoint) => checkpoint.id === expandedId) ?? null;
  return (
    <nav className="conversation-checkpoints" aria-label="对话检查点" aria-live="off">
      <div className="conversation-checkpoints-header">
        <span>检查点</span>
        <label>
          <span>搜索对话检查点</span>
          <input
            type="search"
            aria-label="搜索对话检查点"
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
            placeholder="搜问题"
          />
        </label>
      </div>
      <div className="conversation-checkpoint-list">
        {visibleCheckpoints.length === 0 ? (
          <small className="conversation-checkpoints-empty">没有匹配的检查点</small>
        ) : (
          visibleCheckpoints.map((checkpoint) => (
            <article key={checkpoint.id} className="conversation-checkpoint-item">
              <button
                type="button"
                onClick={() => {
                  navigateToConversationHash(checkpoint.href);
                }}
              >
                <small>{checkpoint.index}</small>
                <strong>{checkpoint.label}</strong>
              </button>
              {checkpoint.artifactCount > 0 ? (
                <button
                  type="button"
                  className="conversation-checkpoint-artifact-toggle"
                  aria-expanded={expandedId === checkpoint.id}
                  onClick={() => setExpandedId((current) => current === checkpoint.id ? null : checkpoint.id)}
                >
                  产物 {checkpoint.artifactCount}
                </button>
              ) : null}
              <button
                type="button"
                className="conversation-checkpoint-copy"
                aria-label={`复制检查点链接：${checkpoint.label}`}
                onClick={() => {
                  const link = new URL(window.location.href);
                  if (conversationId?.trim()) link.searchParams.set("conversation", conversationId.trim());
                  link.hash = checkpoint.href;
                  void copyTextToClipboard(link.toString())
                    .then(() => {
                      setCopiedId(checkpoint.id);
                      window.setTimeout(() => setCopiedId(null), 1600);
                    })
                    .catch(() => undefined);
                }}
              >
                {copiedId === checkpoint.id ? "已复制" : "复制"}
              </button>
            </article>
          ))
        )}
      </div>
      {expandedCheckpoint ? (
        <section className="conversation-checkpoint-artifacts" aria-label={`${expandedCheckpoint.label}的关联产物`}>
          <strong>关联产物</strong>
          <ul>
            {expandedCheckpoint.artifacts.map((artifact) => (
              <li key={`${expandedCheckpoint.id}-${artifact.href}`}>
                <a
                  href={artifact.href}
                  onClick={(event) => {
                    event.preventDefault();
                    navigateToConversationHash(artifact.href);
                  }}
                >
                  {artifact.label}
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {hashNotice ? <small role="status" className="conversation-checkpoints-empty">{hashNotice}</small> : null}
    </nav>
  );
}

export function conversationWorkspaceFiles(runs: RunDetail[]) {
  const final: DownloadableFile[] = [];
  const intermediate: DownloadableFile[] = [];
  const seen = new Set<string>();
  const append = (artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | null | undefined) => {
    if (!isWorkspaceDownloadArtifact(artifact)) return;
    const key = artifact.download_url.trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    if (artifact.presentation === "final_attachment") {
      final.push(artifact);
      return;
    }
    intermediate.push(artifact);
  };

  orderedConversationRuns(runs).forEach((run) => {
    run.artifacts.forEach(append);
    orderedRunEvents(run.events).forEach((event) => append(event.artifact));
  });
  return { final, intermediate, total: final.length + intermediate.length };
}

function mergeWorkspaceFileList(
  current: ConversationWorkspaceFileBuckets,
  workspace: WorkspaceFileList | undefined,
): ConversationWorkspaceFileBuckets {
  if (!workspace) return current;
  const seen = new Set(
    [...current.final, ...current.intermediate].map((artifact) => artifact.download_url.trim()),
  );
  const final: DownloadableFile[] = [...current.final];
  const intermediate: DownloadableFile[] = [...current.intermediate];
  if (workspace.bundle_download_url.trim() && workspace.items.length > 0 && !seen.has(workspace.bundle_download_url)) {
    seen.add(workspace.bundle_download_url);
    final.push({
      id: "workspace-bundle",
      kind: "workspace_bundle",
      title: "当前会话文件夹",
      filename: "workspace.zip",
      mime_type: "application/zip",
      size_bytes: workspace.items.reduce((total, item) => total + item.size_bytes, 0),
      sha256: null,
      download_url: workspace.bundle_download_url,
      presentation: "final_attachment",
    });
  }
  workspace.items.forEach((item) => {
    const key = item.download_url.trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    intermediate.push({
      id: `workspace-file-${item.path}`,
      kind: "workspace_file",
      title: item.path,
      filename: item.filename,
      mime_type: item.mime_type,
      size_bytes: item.size_bytes,
      sha256: item.sha256,
      download_url: item.download_url,
      presentation: "step_detail",
      path: item.path,
    });
  });
  return { final, intermediate, total: final.length + intermediate.length };
}

function ConversationWorkspaceFiles({
  files,
  previewFiles = [],
  onPreviewFile,
  title = "当前会话文件",
  eyebrow = "Files",
  ariaLabel = title,
  showIntermediateInline = false,
}: {
  files: ConversationWorkspaceFileBuckets;
  previewFiles?: WorkbenchFileItem[];
  onPreviewFile?: (file: WorkbenchFileItem) => void;
  title?: string;
  eyebrow?: string;
  ariaLabel?: string;
  showIntermediateInline?: boolean;
}) {
  if (files.total === 0) return null;
  const visiblePreviewFiles = previewFiles.slice(0, showIntermediateInline ? 8 : 4);
  const remainingPreviewFiles = Math.max(previewFiles.length - visiblePreviewFiles.length, 0);
  return (
    <section className="conversation-files-panel" aria-label={ariaLabel}>
      <div className="conversation-files-header">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h3>{title}</h3>
        </div>
        <small>
          {files.final.length} 个最终产物
          {files.intermediate.length > 0 ? ` · ${files.intermediate.length} 个中间产物` : ""}
        </small>
      </div>
      {visiblePreviewFiles.length > 0 ? (
        <div className="conversation-file-link-grid" aria-label="交互区文件链接">
          {visiblePreviewFiles.map((file) => (
            <button
              key={file.id}
              type="button"
              className="conversation-file-link"
              onClick={() => onPreviewFile?.(file)}
              disabled={!onPreviewFile}
              aria-label={`预览文件 ${file.path || file.filename}`}
            >
              <small>{file.operation}</small>
              <strong>{file.path || file.filename}</strong>
              <span>{[file.kind, file.size].filter(Boolean).join(" · ") || "文件"}</span>
            </button>
          ))}
          {remainingPreviewFiles > 0 ? (
            <span className="conversation-file-more">另有 {remainingPreviewFiles} 个文件在工作席</span>
          ) : null}
        </div>
      ) : null}
      {files.final.length > 0 ? (
        <div className="conversation-files-group" aria-label="最终产物">
          {files.final.map((artifact) => (
            <ArtifactFileCard key={artifact.download_url} artifact={artifact} compact />
          ))}
        </div>
      ) : null}
      {files.intermediate.length > 0 && showIntermediateInline ? (
        <div className="conversation-files-group" aria-label="相关文件">
          {files.intermediate.map((artifact) => (
            <ArtifactFileCard key={artifact.download_url} artifact={artifact} compact />
          ))}
        </div>
      ) : null}
      {files.intermediate.length > 0 && !showIntermediateInline ? (
        <details className="conversation-files-details">
          <summary>中间产物</summary>
          <div className="conversation-files-group">
            {files.intermediate.map((artifact) => (
              <ArtifactFileCard key={artifact.download_url} artifact={artifact} compact />
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function RunInteractionArtifactSummary({
  detail,
  files,
  onPreviewFile,
}: {
  detail: RunDetail;
  files: WorkbenchFileItem[];
  onPreviewFile: (file: WorkbenchFileItem) => void;
}) {
  const visibleFiles = files.slice(0, 6);
  const planFiles = visibleFiles.filter((file) =>
    /(^|\/)(plan|implementation|requirements|readme|verification|skill)\.(md|json)$/i.test(file.path || file.filename),
  );
  const sourceFiles = visibleFiles.filter((file) =>
    /\.(?:js|jsx|ts|tsx|py|css|html|json|md|sql|sh)$/i.test(file.path || file.filename),
  );
  const failureText = detail.status === "failed" ? failureSummaryForChat(detail) : null;
  const outcome =
    failureText && failureText.trim()
      ? conciseProcessText(failureText, "本轮运行中断，已保留可查看的阶段产物。")
      : detail.status === "completed"
        ? "主 Agent 已整理本轮交付，文件可直接预览。"
        : "主 Agent 正在整理本轮交付，已生成的文件会在这里汇总。";
  if (files.length === 0) return null;
  const resultBullets = deliveryResultBullets(detail, files, planFiles, sourceFiles);
  return (
    <section className="conversation-artifact-summary" aria-label="本轮产物摘要">
      <div className="conversation-artifact-summary-header">
        <div>
          <span className="eyebrow">Delivery</span>
          <strong>本轮产物</strong>
        </div>
        <small>{displayChatRunStatus(detail.status)} · {files.length} 个文件/产物</small>
      </div>
      <div className="conversation-delivery-result">
        <strong>{detail.status === "completed" ? "任务已完成" : displayChatRunStatus(detail.status)}</strong>
        <p>{outcome}</p>
        {resultBullets.length > 0 ? (
          <ul>
            {resultBullets.map((bullet) => (
              <li key={bullet.label}>
                <span>{bullet.label}</span>
                <p>{bullet.value}</p>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div className="conversation-artifact-metrics" aria-label="产物分类">
        <span>计划 {planFiles.length}</span>
        <span>源码 {sourceFiles.length}</span>
        <span>附件 {detail.artifacts.filter(hasArtifactDownload).length}</span>
      </div>
      {visibleFiles.length > 0 ? (
        <div className="conversation-file-link-grid" aria-label="本轮文件链接">
          {visibleFiles.map((file) => (
            <button
              key={file.id}
              type="button"
              className="conversation-file-link"
              onClick={() => onPreviewFile(file)}
              aria-label={`预览文件 ${file.path || file.filename}`}
            >
              <small>{file.operation}</small>
              <strong>{file.path || file.filename}</strong>
              <span>{[file.kind, file.size].filter(Boolean).join(" · ") || "文件"}</span>
            </button>
          ))}
          {files.length > visibleFiles.length ? (
            <span className="conversation-file-more">另有 {files.length - visibleFiles.length} 个文件在工作席</span>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function deliveryResultBullets(
  detail: RunDetail,
  files: WorkbenchFileItem[],
  planFiles: WorkbenchFileItem[],
  sourceFiles: WorkbenchFileItem[],
) {
  const finalFiles = files.filter((file) => file.download?.presentation === "final_attachment");
  const finalNames = finalFiles.length > 0 ? finalFiles : files;
  const fileNameList = finalNames
    .slice(0, 3)
    .map((file) => file.path || file.filename)
    .filter(Boolean)
    .join("、");
  const verification = deliveryVerificationSignal(detail);
  const bullets = [
    fileNameList ? { label: "核心产物", value: `${fileNameList}${finalNames.length > 3 ? ` 等 ${finalNames.length} 个文件` : ""}` } : null,
    planFiles.length > 0
      ? {
          label: "计划与说明",
          value: planFiles
            .slice(0, 3)
            .map((file) => file.path || file.filename)
            .join("、"),
        }
      : null,
    sourceFiles.length > 0
      ? {
          label: "代码文件",
          value: `${sourceFiles.length} 个源码/配置文件已关联，可点下方文件链接预览内容。`,
        }
      : null,
    verification ? { label: "验证", value: verification } : null,
  ].filter((item): item is { label: string; value: string } => Boolean(item && item.value.trim()));
  return bullets.slice(0, 4);
}

function deliveryVerificationSignal(detail: RunDetail) {
  const event = [...orderedRunEvents(detail.events)]
    .reverse()
    .find((candidate) => {
      const text = [
        candidate.summary,
        candidate.message,
        candidate.step_id,
        candidate.tool_name,
        formatEventPayloadValue(candidate.payload.summary),
        formatEventPayloadValue(candidate.payload.result),
        formatEventPayloadValue(candidate.payload.output),
      ]
        .filter(Boolean)
        .join(" ");
      return /验证|测试|通过|构建|build|test|verify|passed|success/i.test(text);
    });
  if (event) return conciseProcessText(eventSummaryText(event, new Map()), "验证记录已生成");
  if (detail.status === "completed") return "本轮运行已完成，详细验证记录可在 Agent 工作席查看。";
  if (detail.status === "failed") return "运行中断，失败原因和保留产物可在工作席查看。";
  return "";
}

function RunInteractionSummaryInline({
  agentNames,
  mainAgentModelName,
  onPreviewFile,
  run,
}: {
  agentNames: Map<string, string>;
  mainAgentModelName?: string;
  onPreviewFile: (file: WorkbenchFileItem) => void;
  run: RunDetail;
}) {
  const processItems = runProcessItems(run, agentNames, mainAgentModelName);
  const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, processItems);
  return <RunInteractionArtifactSummary detail={run} files={files} onPreviewFile={onPreviewFile} />;
}

function inlineFileMessageId(messages: ChatMessage[]) {
  const assistantMessages = [...messages].reverse().filter((message) => message.role === "assistant");
  return (
    assistantMessages.find((message) => message.title === "回复")?.id ??
    assistantMessages.find((message) => Boolean(message.artifact))?.id ??
    null
  );
}

function isTextPreviewCandidate(file: WorkbenchFileItem) {
  const mime = file.mimeType?.toLowerCase() ?? "";
  const name = file.filename.toLowerCase();
  if (file.text?.trim()) return true;
  if (mime.startsWith("text/") || mime.includes("json") || mime.includes("xml") || mime.includes("javascript")) return true;
  return /\.(?:txt|md|json|js|jsx|ts|tsx|py|css|html|xml|yaml|yml|toml|ini|sh|sql|csv|log)$/i.test(name);
}

function workbenchFileOperationFromArtifact(artifact: DownloadableFile) {
  const kind = artifact.kind?.toLowerCase() ?? "";
  const title = artifact.title?.toLowerCase() ?? "";
  if (kind.includes("workspace_bundle") || title.includes("文件夹")) return "文件夹";
  if (kind.includes("workspace_file")) return "创建文件";
  return "产物";
}

function safeWorkspaceFileText(value: unknown) {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function workspaceFileOperationKind(value: unknown) {
  const normalized = safeWorkspaceFileText(value)?.toLowerCase();
  if (
    normalized === "file_create" ||
    normalized === "file_edit" ||
    normalized === "file_write" ||
    normalized === "file_read" ||
    normalized === "terminal" ||
    normalized === "browser" ||
    normalized === "generic"
  ) {
    return normalized;
  }
  return null;
}

function workspaceFileArtifactsForEvent(event: RunEvent): DownloadableFile[] {
  const files = event.payload.workspace_files;
  if (!Array.isArray(files)) return [];
  return files.flatMap((file, index) => {
    if (!file || typeof file !== "object") return [];
    const item = file as Record<string, unknown>;
    const path = safeWorkspaceFileText(item.path);
    const downloadUrl = safeWorkspaceFileText(item.download_url);
    if (!path || !downloadUrl) return [];
    const filename = safeWorkspaceFileText(item.filename) ?? path.split("/").at(-1) ?? path;
    const mimeType = safeWorkspaceFileText(item.mime_type);
    const operationKind = workspaceFileOperationKind(item.operation_kind);
    const sha256 = safeWorkspaceFileText(item.sha256);
    const sizeBytes = typeof item.size_bytes === "number" && Number.isFinite(item.size_bytes) ? item.size_bytes : null;
    return [
      {
        id: `workspace-file-${event.sequence}-${index}`,
        kind: "workspace_file",
        title: path,
        path,
        text: null,
        filename,
        operation_kind: operationKind,
        mime_type: mimeType,
        size_bytes: sizeBytes,
        sha256,
        download_url: downloadUrl,
      },
    ];
  });
}

function asWorkbenchFileOperation(value: string): WorkbenchFileItem["operation"] | null {
  if (value === "创建文件" || value === "编辑文件" || value === "读取文件" || value === "产物" || value === "文件夹") return value;
  return null;
}

function workbenchFileOperationFromProcessItem(item: ProcessDetailTarget | null, artifact: DownloadableFile) {
  const fileOperation = artifact.operation_kind ? toolOperationKindLabel(artifact.operation_kind) : "";
  const directFileOperation = asWorkbenchFileOperation(fileOperation);
  if (directFileOperation && directFileOperation !== "产物" && directFileOperation !== "文件夹") return directFileOperation;
  if (!item) return workbenchFileOperationFromArtifact(artifact);
  const badge = item.badge;
  const text = `${item.title} ${item.message} ${item.rows.map((row) => `${row.label} ${row.value}`).join(" ")}`;
  const toolOperation = asWorkbenchFileOperation(badge);
  if (toolOperation && toolOperation !== "产物" && toolOperation !== "文件夹") return toolOperation;
  if (/操作类别\s+file_create|file_create/i.test(text)) return "创建文件";
  if (/操作类别\s+(file_edit|file_write)|file_edit|file_write/i.test(text)) return "编辑文件";
  if (/操作类别\s+file_read|file_read/i.test(text)) return "读取文件";
  if (/编辑文件|修改|patch|edit/i.test(`${badge} ${text}`)) return "编辑文件";
  if (/创建文件|生成文件|write|create/i.test(`${badge} ${text}`)) return "创建文件";
  if (/读取文件|read/i.test(`${badge} ${text}`)) return "读取文件";
  return workbenchFileOperationFromArtifact(artifact);
}

function workbenchFileFromArtifact(
  artifact: DownloadableFile,
  source: ProcessDetailTarget | null,
): WorkbenchFileItem {
  const filename = artifactFileName(artifact);
  return {
    id: `${artifact.download_url}:${source?.id ?? "file"}`,
    title: artifact.title || filename,
    filename,
    path: artifact.path ?? artifact.title ?? null,
    kind: artifact.kind || "file",
    operation: workbenchFileOperationFromProcessItem(source, artifact),
    mimeType: artifact.mime_type ?? null,
    size: formatFileSize(artifact.size_bytes),
    sha256: artifact.sha256 ?? null,
    text: isGenericArtifactText(artifact.text) ? "" : artifact.text?.trim() || "",
    download: artifact,
    source,
  };
}

export function workbenchFileItems(
  runs: RunDetail[],
  workspaceFiles: ConversationWorkspaceFileBuckets,
  processItems: ProcessDetailTarget[],
): WorkbenchFileItem[] {
  const seen = new Set<string>();
  const files: WorkbenchFileItem[] = [];
  const processByDownloadUrl = new Map(
    processItems.flatMap((item) => (item.artifact ? [[item.artifact.download_url.trim(), item] as const] : [])),
  );
  const processBySourceSequence = new Map(
    processItems.flatMap((item) =>
      typeof item.sourceSequence === "number" ? [[`${item.runId}:${item.sourceSequence}`, item] as const] : [],
    ),
  );
  const append = (artifact: RunArtifact | NonNullable<RunEvent["artifact"]> | DownloadableFile | null | undefined, source: ProcessDetailTarget | null) => {
    const downloadUrl = artifact?.download_url?.trim();
    if (!artifact || !downloadUrl) return;
    const resolvedSource = source ?? processByDownloadUrl.get(downloadUrl) ?? null;
    const keepPerAction = artifact.kind === "workspace_file";
    const key = `${keepPerAction ? resolvedSource?.id ?? "global" : "global"}:${downloadUrl}`;
    if (!key || seen.has(key)) return;
    seen.add(key);
    files.push(workbenchFileFromArtifact({ ...artifact, download_url: downloadUrl }, resolvedSource));
  };

  orderedConversationRuns(runs).forEach((run) => {
    run.artifacts.forEach((artifact) => append(artifact, null));
    orderedRunEvents(run.events).forEach((event) => {
      const eventSource = processBySourceSequence.get(`${run.id}:${event.sequence}`) ?? null;
      workspaceFileArtifactsForEvent(event).forEach((artifact) => append(artifact, eventSource));
      append(event.artifact, null);
    });
  });
  workspaceFiles.final.forEach((artifact) => append(artifact, null));
  workspaceFiles.intermediate.forEach((artifact) => append(artifact, null));
  return files;
}

async function readWorkbenchPreviewText(payload: unknown): Promise<string> {
  if (typeof payload === "string") return payload;
  if (payload instanceof Response) return payload.text();
  if (payload instanceof ArrayBuffer) return new TextDecoder().decode(payload);
  if (payload && typeof payload === "object") {
    const maybeText = payload as { text?: () => Promise<string>; arrayBuffer?: () => Promise<ArrayBuffer> };
    if (typeof maybeText.text === "function") return maybeText.text();
    if (typeof maybeText.arrayBuffer === "function") return new TextDecoder().decode(await maybeText.arrayBuffer());
  }
  return "";
}

function WorkbenchFilePreview({
  file,
  onOpenSource,
}: {
  file: WorkbenchFileItem;
  onOpenSource: (target: ProcessDetailTarget) => void;
}) {
  const [previewText, setPreviewText] = useState<string | null>(file.text);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canPreview = isTextPreviewCandidate(file);

  async function loadPreview() {
    if (!file.download || !canPreview || previewText) return;
    setLoading(true);
    setError(null);
    try {
      const blob = await api.downloadGeneratedArtifact(file.download.download_url);
      const text = await readWorkbenchPreviewText(blob);
      setPreviewText(text.length > 8000 ? `${text.slice(0, 8000)}\n\n...已截断，仅预览前 8000 字符` : text);
    } catch (caught) {
      setError(formatApiError(caught, "文件预览失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (canPreview && !previewText) void loadPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file.id]);

  return (
    <article className="agent-workbench-file-preview" aria-label={`${file.filename}预览`}>
      <div className="agent-workbench-file-preview-header">
        <div>
          <small>{file.operation}</small>
          <strong>{file.path || file.filename}</strong>
        </div>
        {file.download ? <ArtifactFileCard artifact={file.download} compact /> : null}
      </div>
      <dl>
        <div>
          <dt>类型</dt>
          <dd>{[file.kind, file.mimeType, file.size].filter(Boolean).join(" · ") || "文件"}</dd>
        </div>
        {file.sha256 ? (
          <div>
            <dt>SHA-256</dt>
            <dd>{file.sha256.slice(0, 16)}</dd>
          </div>
        ) : null}
      </dl>
      {file.source ? (
        <button type="button" className="secondary-action" onClick={() => onOpenSource(file.source as ProcessDetailTarget)}>
          查看来源动作
        </button>
      ) : null}
      {canPreview ? (
        <pre className="agent-workbench-file-code">{loading ? "正在读取文件预览..." : previewText || "暂无可预览内容"}</pre>
      ) : (
        <p className="agent-workbench-compressed-note">该文件不适合直接预览，请下载查看。</p>
      )}
      {error ? <p role="alert" className="form-error">{error}</p> : null}
    </article>
  );
}

function ConversationFilePreviewDrawer({
  file,
  onClose,
  onOpenSource,
}: {
  file: WorkbenchFileItem;
  onClose: () => void;
  onOpenSource: (target: ProcessDetailTarget) => void;
}) {
  return createPortal(
    <div className="process-drawer-backdrop" role="presentation" onClick={onClose}>
      <section
        className="process-drawer conversation-file-preview-drawer"
        role="dialog"
        aria-label="文件内容预览"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="process-drawer-handle" aria-hidden="true" />
        <div className="process-drawer-header">
          <div>
            <span className="eyebrow">File preview</span>
            <h3>{file.path || file.filename}</h3>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>
            关闭
          </button>
        </div>
        <WorkbenchFilePreview
          file={file}
          onOpenSource={(target) => {
            onClose();
            onOpenSource(target);
          }}
        />
      </section>
    </div>,
    document.body,
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

export function mergeConversationRuns(previous: RunDetail[] | undefined, incoming: RunDetail[]) {
  if (!previous || previous.length === 0) return orderedConversationRuns(incoming);
  if (incoming.length === 0) return orderedConversationRuns(previous);
  const incomingById = new Map(incoming.map((run) => [run.id, run]));
  const previousIds = new Set(previous.map((run) => run.id));
  const merged = previous.map((run) => {
    const incomingRun = incomingById.get(run.id);
    return incomingRun ? preferredRunSnapshot(run, incomingRun) : run;
  });
  for (const run of incoming) {
    if (!previousIds.has(run.id)) merged.push(run);
  }
  const ordered = orderedConversationRuns(merged);
  if (
    ordered.length === previous.length &&
    ordered.every((run, index) => sameRunSnapshot(run, previous[index]))
  ) {
    return previous;
  }
  return ordered;
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
  consumedDownloadUrls: Set<string>,
) {
  if (event.artifact) {
    const downloadKey = artifactDownloadKey(event.artifact);
    if (downloadKey && consumedDownloadUrls.has(downloadKey)) return null;
    if (downloadKey) consumedDownloadUrls.add(downloadKey);
    return event.artifact;
  }
  const explicitArtifactId =
    formatEventPayloadValue(event.payload.artifact_id) ||
    formatEventPayloadValue(event.payload.artifactId) ||
    formatEventPayloadValue(event.payload.id);
  if (explicitArtifactId) {
    const matched = artifacts.find((artifact) => artifact.id === explicitArtifactId);
    if (matched) {
      consumedArtifactIds.add(matched.id);
      const downloadKey = artifactDownloadKey(matched);
      if (downloadKey && consumedDownloadUrls.has(downloadKey)) return null;
      if (downloadKey) consumedDownloadUrls.add(downloadKey);
      return matched;
    }
  }
  if (event.kind !== "artifact.created" && event.kind !== "message.created") return null;
  const byActor = event.actor
    ? artifacts.find((artifact) => artifact.title === event.actor && !consumedArtifactIds.has(artifact.id))
    : null;
  const canUseOrderedFallback = Boolean(event.actor || event.step_id || event.tool_name);
  const byOrder = canUseOrderedFallback
    ? artifacts.find((artifact) => !consumedArtifactIds.has(artifact.id) && !hasArtifactDownload(artifact))
    : null;
  const matched = byActor ?? byOrder ?? null;
  if (matched) {
    consumedArtifactIds.add(matched.id);
    const downloadKey = artifactDownloadKey(matched);
    if (downloadKey && consumedDownloadUrls.has(downloadKey)) return null;
    if (downloadKey) consumedDownloadUrls.add(downloadKey);
  }
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

function discussionTracePayload(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function discussionTraceValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => formatEventPayloadValue(item)).filter(Boolean).join("、");
  }
  return formatEventPayloadValue(value);
}

function discussionTraceRows(event: RunEvent, agentNames: Map<string, string>) {
  const trace =
    discussionTracePayload(event.payload.discussion_trace) ??
    discussionTracePayload(event.payload.dispatch_discussion_trace) ??
    discussionTracePayload(event.payload.coordination_trace);
  if (!trace) return [];
  const rows: Array<{ label: string; value: string }> = [];
  const memberStatements = trace.member_statements;
  if (memberStatements && typeof memberStatements === "object") {
    if (Array.isArray(memberStatements)) {
      memberStatements.forEach((statement, index) => {
        const value = discussionTraceValue(statement);
        if (value) rows.push({ label: `成员发言 ${index + 1}`, value });
      });
    } else {
      Object.entries(memberStatements as Record<string, unknown>).forEach(([actor, statement]) => {
        const value = discussionTraceValue(statement);
        if (value) rows.push({ label: `${agentNames.get(actor) ?? humanizeEventIdentifier(actor)}意见`, value });
      });
    }
  }
  const disagreement = discussionTraceValue(trace.disagreements) || discussionTraceValue(trace.disagreement_summary);
  if (disagreement) rows.push({ label: "分歧与风险", value: disagreement });
  const verification = discussionTraceValue(trace.verification_steps);
  if (verification) rows.push({ label: "求证与验证", value: verification });
  const finalDecision = discussionTraceValue(trace.final_decision);
  if (finalDecision) rows.push({ label: "最终决策", value: finalDecision });
  return rows;
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
  rows.push(...discussionTraceRows(event, agentNames));
  if (judgement) rows.push({ label: "主 Agent 裁决", value: judgement });
  return rows;
}

function eventSummaryText(
  event: RunDetail["events"][number],
  agentNames: Map<string, string>,
  artifact?: RunArtifact | NonNullable<RunEvent["artifact"]> | null,
) {
  const safeSummary = eventSafeSummary(event);
  if (safeSummary) return conciseProcessText(localizedEventSummaryText(safeSummary, event, agentNames), "记录了一步过程");
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
  if (event.kind === "runtime.recovered") {
    return runtimeRecoveryEventSummary(event);
  }
  if (event.kind === "tool.requested") {
    const requestedTool = formatEventPayloadValue(event.payload.name) || event.tool_name || "工具";
    const requestedOperation = toolOperationLabelForEvent(event);
    const requestedDisplay = requestedOperation === "使用工具" ? requestedTool : requestedOperation;
    return `工具请求：${conciseProcessText(requestedDisplay, "工具")}`;
  }
  if (event.kind === "tool.started") {
    return `${subject} ${toolSummaryWithDisplay(event)}`;
  }
  if (event.kind === "tool.completed") {
    return `${subject} ${toolSummaryWithDisplay(event, "完成")}`;
  }
  if (event.kind === "tool.failed") {
    return `${subject} ${toolSummaryWithDisplay(event, "失败")}`;
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
    return `等待确认：${conciseProcessText(safeActionLabel(event.action) || safeIntentValue(event, ["operation_kind", "status"]), "需要你确认后继续")}`;
  }
  if (event.kind === "step.retrying") {
    const retrySignal =
      safeActionLabel(event.action) ||
      repairFailureKindLabel(safeIntentValue(event, ["failure_kind"])) ||
      safeIntentValue(event, ["attempt", "status"]) ||
      "失败后重试";
    return `${subject} 重试：${conciseProcessText(retrySignal, "失败后重试")}`;
  }
  if (isRepairIntentEvent(event)) {
    const repairSignal =
      repairActionLabel(safeIntentValue(event, ["repair_action", "repair_kind"])) ||
      repairFailureKindLabel(safeIntentValue(event, ["failure_kind"])) ||
      safeIntentValue(event, ["status"]) ||
      safeActionLabel(event.action) ||
      "准备修复";
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
  if (event.kind === "runtime.recovered") return "断点续跑";
  if (isRepairIntentEvent(event)) return "修复意图";
  if (event.kind === "artifact.created" || event.kind === "message.created" || event.kind === "step.completed") {
    return "中间产物";
  }
  if (event.kind === "review.completed" || event.kind.startsWith("decision.")) return "裁决过程";
  if (event.kind.startsWith("discussion.")) return "讨论过程";
  if (event.kind === "model.reasoning_delta") return "思考过程";
  if (event.kind === "model.text_delta") return "输出进度";
  if (event.kind === "tool.started" || event.kind === "tool.completed" || event.kind === "tool.failed") {
    return toolOperationLabelForEvent(event);
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
      sourceSequence: lastEvent.sequence,
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

function lifecycleActionTargetRows(events: RunEvent[], existingLabels: Set<string>) {
  const rows: Array<{ label: string; value: string }> = [];
  events.forEach((event) => {
    safeActionTargetRows(event).forEach((row) => {
      if (existingLabels.has(row.label)) return;
      existingLabels.add(row.label);
      rows.push(row);
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
  const rows = [...lifecycleRows, ...baseRows, ...lifecycleActionTargetRows(events, labels), ...safeLifecyclePayloadRows(events, labels)];
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
      sourceSequence: lastEvent.sequence,
      sourceStepId: lastEvent.step_id,
      sourceActor: lastEvent.actor,
    },
  ];
}

function processItemsForApiToolLifecycles(
  detail: RunDetail,
  coveredToolKeys: Set<string>,
): ProcessDetailTarget[] {
  return detail.tool_lifecycle.flatMap((lifecycle, index) => {
    if (coveredToolKeys.has(lifecycle.tool_call_id)) return [];
    const operation = toolOperationKindLabel(lifecycle.operation_kind) || toolOperationLabel(lifecycle.tool_name);
    const status = TOOL_STATUS_LABELS[lifecycle.status] ?? lifecycle.status;
    const sequenceRange =
      lifecycle.sequences.length > 0
        ? `#${lifecycle.sequences[0]}${lifecycle.sequences.length > 1 ? `-#${lifecycle.sequences.at(-1)}` : ""}`
        : "";
    const artifact =
      lifecycle.artifact_id?.trim()
        ? detail.artifacts.find((item) => item.id === lifecycle.artifact_id) ?? null
        : null;
    const rows = [
      lifecycle.step_id ? { label: "目标", value: lifecycle.step_id } : null,
      { label: "工具", value: lifecycle.tool_name },
      { label: "操作类别", value: lifecycle.operation_kind },
      { label: "状态", value: status },
      lifecycle.actor ? { label: "执行者", value: lifecycle.actor } : null,
      lifecycle.step_id ? { label: "步骤", value: lifecycle.step_id } : null,
      sequenceRange ? { label: "事件范围", value: sequenceRange } : null,
      lifecycle.argument_bytes !== null ? { label: "参数字节数", value: String(lifecycle.argument_bytes) } : null,
      lifecycle.output_bytes !== null ? { label: "输出字节数", value: String(lifecycle.output_bytes) } : null,
      lifecycle.exit_code !== null ? { label: "退出码", value: String(lifecycle.exit_code) } : null,
      lifecycle.failure_kind ? { label: "失败类型", value: lifecycle.failure_kind } : null,
      lifecycle.approval_id ? { label: "审批 ID", value: lifecycle.approval_id } : null,
      lifecycle.replay_safe !== null ? { label: "可重放", value: lifecycle.replay_safe ? "是" : "否" } : null,
      ...eventArtifactRows(artifact),
    ].filter((row): row is { label: string; value: string } => Boolean(row));
    return [
      {
        id: `${detail.id}-tool-lifecycle-${lifecycle.tool_call_id}-${index}`,
        runId: detail.id,
        conversationId: runConversationId(detail),
        title: `${operation} ${status}`,
        message: `${operation} ${status}`,
        badge: operation,
        rows,
        createdAt: null,
        artifact: artifactDetailDownload(artifact),
        sourceKind: "tool.lifecycle",
        sourceSequence: lifecycle.terminal_sequence ?? lifecycle.started_sequence ?? undefined,
        sourceStepId: lifecycle.step_id ?? null,
        sourceActor: lifecycle.actor ?? null,
      },
    ];
  });
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
    sourceSequence: event.sequence,
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

export function runProcessItems(
  detail: RunDetail,
  agentNames: Map<string, string>,
  mainAgentModelName?: string,
): ProcessDetailTarget[] {
  const orderedDetail: RunDetail = { ...detail, events: orderedRunEvents(detail.events) };
  const routingRows = processRoutingRows(orderedDetail, agentNames, mainAgentModelName);
  const routingAgentPool = displayAgentPool(orderedDetail.explicit_details.selected_agent_ids, agentNames);
  const routingItem =
    routingRows.length > 0
      ? [
          {
            id: `${orderedDetail.id}-routing`,
            runId: orderedDetail.id,
            conversationId: runConversationId(orderedDetail),
            title: "主 Agent 调度判断",
            message: `主 Agent 选择${displayMode(orderedDetail.mode)}${routingAgentPool ? `：${routingAgentPool}` : ""}`,
            badge: "调度判断",
            rows: routingRows,
            createdAt: null,
          },
        ]
      : [];
  const consumedArtifactIds = new Set<string>();
  const toolGroups = new Map<string, EventGroupItem[]>();
  orderedDetail.events.forEach((event, index) => {
    if (!isToolEvent(event)) return;
    const key = toolLifecycleKey(event);
    toolGroups.set(key, [...(toolGroups.get(key) ?? []), { event, index }]);
  });
  const finalToolEvents = new Map<RunEvent, EventGroupItem[]>();
  toolGroups.forEach((group) => {
    const finalEvent = group.at(-1)?.event;
    if (finalEvent) finalToolEvents.set(finalEvent, group);
  });
  const actionEvents = orderedDetail.events.flatMap((event, index) => (isActionEvent(event) ? [{ event, index }] : []));
  const eventItems: ProcessDetailTarget[] = [];
  const consumedDownloadUrls = new Set(
    dedupeDownloadableArtifacts(orderedDetail.artifacts.filter(isFinalDownloadableArtifact)).map((artifact) =>
      artifact.download_url.trim(),
    ),
  );
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
        const artifact = fallbackArtifactForEvent(event, orderedDetail.artifacts, consumedArtifactIds, consumedDownloadUrls);
        eventItems.push(...processItemsForEvent(orderedDetail, event, eventIndex, agentNames, artifact));
        continue;
      }
      eventItems.push(
        ...processItemsForModelDeltaGroup(
          orderedDetail,
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
      const artifact = fallbackArtifactForEvent(finalEvent, orderedDetail.artifacts, consumedArtifactIds, consumedDownloadUrls);
      eventItems.push(
        ...processItemsForToolLifecycle(
          orderedDetail,
          group.map((item) => item.event),
          group[0]?.index ?? eventIndex,
          agentNames,
          artifact,
        ),
      );
      continue;
    }
    if (isWrappedToolFailureEvent(event, orderedDetail.events)) continue;
    const artifact = fallbackArtifactForEvent(event, orderedDetail.artifacts, consumedArtifactIds, consumedDownloadUrls);
    if (event.kind === "artifact.created" && !artifact && !hasUsefulPayload(event)) continue;
    eventItems.push(...processItemsForEvent(orderedDetail, event, eventIndex, agentNames, artifact));
  }
  return [
    ...routingItem,
    ...eventItems,
    ...processItemsForApiToolLifecycles(orderedDetail, new Set(toolGroups.keys())),
  ];
}

function RunFailureDiagnosticsPanel({ diagnostics }: { diagnostics: RunFailureDiagnostic[] }) {
  if (diagnostics.length === 0) return null;
  return (
    <section className="run-failure-diagnostics" aria-label="故障诊断">
      <div className="run-failure-diagnostics-header">
        <span aria-hidden="true">!</span>
        <strong>故障诊断</strong>
        <small>{diagnostics.length} 个待处理信号</small>
      </div>
      <div className="run-failure-diagnostic-list">
        {diagnostics.map((diagnostic) => (
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
  );
}

function RunExecutionIntentsPanel({ intents }: { intents: RunExecutionIntent[] }) {
  if (intents.length === 0) return null;
  return (
    <section className="run-execution-intents" aria-label="执行意图">
      <div className="run-execution-intents-header">
        <span aria-hidden="true">◇</span>
        <strong>执行意图</strong>
        <small>{intents.length} 个关键意图</small>
      </div>
      <div className="run-execution-intent-list">
        {intents.map((intent) => (
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
  );
}

function AgentCoordinationEvidence({
  sections,
  onOpen,
}: {
  sections: WorkbenchCoordinationSection[];
  onOpen: (target: ProcessDetailTarget) => void;
}) {
  return (
    <section className="agent-workbench-brief" aria-label="调度简报">
      <div className="agent-workbench-brief-header">
        <strong>调度简报</strong>
        <small>派工、分歧、求证和决策</small>
      </div>
      <div className="agent-workbench-brief-grid">
        {sections.map((section) => (
          <article key={section.key} className="agent-workbench-brief-section">
            <div>
              <strong>{section.title}</strong>
              <small>{section.snippets.length > 0 ? `${section.snippets.length} 条` : section.empty}</small>
            </div>
            {section.snippets.length > 0 ? (
              <div>
                {section.snippets.map((snippet) => (
                  <button key={snippet.id} type="button" onClick={() => onOpen(snippet.target)}>
                    <small>{snippet.label}</small>
                    <span>{snippet.text}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function WorkbenchActionRow({
  files,
  item,
  onOpen,
  onOpenFile,
}: {
  files: WorkbenchFileItem[];
  item: ProcessDetailTarget;
  onOpen: (target: ProcessDetailTarget) => void;
  onOpenFile: (file: WorkbenchFileItem) => void;
}) {
  const descriptor = workbenchActionDescriptor(item, files);
  return (
    <article className="agent-workbench-action-row">
      <button type="button" className="run-process-toggle process-intermediate-card" onClick={() => onOpen(item)}>
        <span aria-hidden="true">›</span>
        <small className="process-card-badge">{descriptor.operation}</small>
        <strong>{descriptor.target}</strong>
        <small>{descriptor.summary}</small>
        {descriptor.meta.length > 0 ? (
          <span className="agent-workbench-action-meta">
            {descriptor.meta.map((meta) => (
              <span key={meta}>{meta}</span>
            ))}
          </span>
        ) : null}
        {item.artifact ? <small>{artifactDisplayName(item.artifact)}</small> : null}
      </button>
      {files.length > 0 ? (
        <div className="agent-workbench-action-files" aria-label={`${item.message}关联文件`}>
          {files.map((file) => (
            <button key={file.id} type="button" onClick={() => onOpenFile(file)} aria-label={`预览文件 ${file.path || file.filename}`}>
              <small>{file.operation}</small>
              <strong>{file.path || file.filename}</strong>
              {file.source ? (
                <span>
                  {[file.source.sourceActor, file.source.message].filter(Boolean).join(" · ")}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function AgentWorkbenchDrawer({
  dispatchCards,
  executionIntents,
  failureDiagnostics,
  files,
  initialAgentId,
  items,
  taskChain,
  onClose,
  onOpen,
}: {
  dispatchCards: AgentDispatchCard[];
  executionIntents: RunExecutionIntent[];
  failureDiagnostics: RunFailureDiagnostic[];
  files: WorkbenchFileItem[];
  initialAgentId?: string | null;
  items: ProcessDetailTarget[];
  taskChain: TaskChainStep[];
  onClose: () => void;
  onOpen: (target: ProcessDetailTarget) => void;
}) {
  const [showAllActions, setShowAllActions] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(initialAgentId ?? null);
  const [activeView, setActiveView] = useState<"overview" | "actions">("overview");
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const previewPaneRef = useRef<HTMLElement | null>(null);
  const selectedAgent = dispatchCards.find((card) => card.id === selectedAgentId) ?? null;
  const coordinationItems = items.filter(isWorkbenchCoordinationItem);
  const actionItems = items.filter((item) => !isWorkbenchCoordinationItem(item));
  const coordinationSections = coordinationEvidenceSections(coordinationItems);
  const selectedAgentItems = selectedAgent ? agentActivityItems(selectedAgent, items) : [];
  const actionPreview = recentPreview(selectedAgentItems, WORKBENCH_ACTION_PREVIEW_LIMIT, showAllActions);
  const recoveryCount = failureDiagnostics.length + executionIntents.length;
  const defaultFile = files.find(isTextPreviewCandidate) ?? files[0] ?? null;
  const selectedFile = files.find((file) => file.id === selectedFileId) ?? defaultFile;
  const filesBySourceId = new Map<string, WorkbenchFileItem[]>();
  files.forEach((file) => {
    if (!file.source) return;
    filesBySourceId.set(file.source.id, [...(filesBySourceId.get(file.source.id) ?? []), file]);
  });
  const filesForAction = (item: ProcessDetailTarget) => filesBySourceId.get(item.id) ?? [];
  const openFile = (file: WorkbenchFileItem) => {
    setSelectedFileId(file.id);
    if (selectedAgentId) {
      setSelectedAgentId(null);
      setActiveView("actions");
    }
  };
  useEffect(() => {
    setSelectedAgentId(initialAgentId ?? null);
    setActiveView("overview");
    setShowAllActions(false);
  }, [initialAgentId]);
  useEffect(() => {
    if (!selectedFileId || !previewPaneRef.current) return;
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    if (!window.matchMedia("(max-width: 980px)").matches) return;
    previewPaneRef.current.scrollIntoView({ block: "start", inline: "nearest" });
  }, [selectedFileId, activeView]);
  const actionWorkspaceItems = activeView === "actions" ? actionItems : [];
  const actionWorkspaceLabel = "动作与文件";
  const actionWorkspaceAria = "过程轨迹";
  const actionWorkspaceEmpty = "暂无动作或文件记录";
  return createPortal(
    <div className="process-drawer-backdrop" role="presentation" onClick={onClose}>
      <section
        className="process-drawer agent-workbench-drawer"
        role="dialog"
        aria-label="Agent 工作席详情"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="process-drawer-handle" aria-hidden="true" />
        <div className="process-drawer-header">
          <div>
            <span className="eyebrow">Agent workbench</span>
            <h3>Agent 工作席</h3>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="agent-workbench-detail">
          {selectedAgent ? (
            <section className="agent-workbench-actions" aria-label={`${agentDisplayTitle(selectedAgent)}工作调度`}>
              <div className="agent-workbench-actions-header">
                <strong>{agentDisplayTitle(selectedAgent)}工作调度</strong>
                <small>{selectedAgentItems.length} 个动作</small>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => {
                    setSelectedAgentId(null);
                    setShowAllActions(false);
                  }}
                >
                  返回调度总览
                </button>
                {selectedAgentItems.length > WORKBENCH_ACTION_PREVIEW_LIMIT ? (
                  <button type="button" className="secondary-action" onClick={() => setShowAllActions((current) => !current)}>
                    {showAllActions ? "收起动作" : "显示全部动作"}
                  </button>
                ) : null}
              </div>
              {actionPreview.hiddenCount > 0 ? (
                <p className="agent-workbench-compressed-note">已折叠 {actionPreview.hiddenCount} 个较早动作</p>
              ) : null}
              <div className="agent-cluster-actions">
                {actionPreview.visible.map((item) => (
                  <WorkbenchActionRow
                    key={item.id}
                    files={filesForAction(item)}
                    item={item}
                    onOpen={onOpen}
                    onOpenFile={openFile}
                  />
                ))}
              </div>
            </section>
          ) : (
            <>
              <div className="agent-workbench-tabs" aria-label="工作席视图">
                <button
                  type="button"
                  aria-label="助手总览"
                  aria-pressed={activeView === "overview"}
                  className={activeView === "overview" ? "active" : ""}
                  onClick={() => setActiveView("overview")}
                >
                  总览
                  <small>{dispatchCards.length} 个 Agent</small>
                </button>
                <button
                  type="button"
                  aria-label="动作与文件"
                  aria-pressed={activeView === "actions"}
                  className={activeView === "actions" ? "active" : ""}
                  onClick={() => setActiveView("actions")}
                >
                  动作与文件
                  <small>{actionItems.length + files.length + recoveryCount} 条</small>
                </button>
              </div>
              {activeView === "overview" ? (
                <>
                  {coordinationItems.length > 0 ? <AgentCoordinationEvidence sections={coordinationSections} onOpen={onOpen} /> : null}
                  <div className="agent-workbench-list">
                    {dispatchCards.map((card) => {
                      const activityItems = agentActivityItems(card, items);
                      return (
                        <button
                          key={card.id}
                          type="button"
                          className={`agent-workbench-agent-card status-${card.status}`}
                          aria-label={`打开${agentDisplayTitle(card)}工作调度`}
                          onClick={() => {
                            setSelectedAgentId(card.id);
                            setShowAllActions(false);
                          }}
                        >
                  <div className="agent-workbench-agent-header">
                    <div className="agent-workbench-avatar" aria-hidden="true">
                      {card.name.slice(0, 1)}
                    </div>
                    <div>
                      <strong>{agentDisplayTitle(card)}</strong>
                      <small>{card.model}</small>
                    </div>
                    <span>{card.status}</span>
                  </div>
                  <p>{card.summary}</p>
                  {card.purpose || card.taskInputs.length > 0 || card.dependencyInputs.length > 0 || card.toolInputs.length > 0 ? (
                    <div className="agent-workbench-inputs" aria-label={`${agentDisplayTitle(card)}调度输入`}>
                      {card.purpose ? <span>职责 {card.purpose}</span> : null}
                      {card.taskInputs.map((item) => (
                        <span key={`task-${item}`}>任务 {item}</span>
                      ))}
                      {card.dependencyInputs.map((item) => (
                        <span key={`dependency-${item}`}>依赖 {item}</span>
                      ))}
                      {card.toolInputs.map((item) => (
                        <span key={`tool-${item}`}>工具 {item}</span>
                      ))}
                    </div>
                  ) : null}
                  {activityItems.length > 0 ? (
                    <div className="agent-workbench-activity">
                      <small>活动轨迹</small>
                      <p>{activityItems.length} 条，点击查看</p>
                    </div>
                  ) : null}
                        </button>
                      );
                    })}
                  </div>
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
                </>
              ) : null}
              {activeView === "actions" ? (
                <section
                  className="agent-workbench-action-workspace"
                  aria-label={`${actionWorkspaceLabel}工作区`}
                >
                  <section className="agent-workbench-action-list-pane" aria-label="动作列表">
                    <div className="agent-workbench-actions-header">
                      <strong>{actionWorkspaceLabel}</strong>
                      <small>{actionWorkspaceItems.length} 条</small>
                    </div>
                    <div className="agent-cluster-actions" role="region" aria-label={actionWorkspaceAria}>
                      {actionWorkspaceItems.map((item) => (
                        <WorkbenchActionRow
                          key={item.id}
                          files={filesForAction(item)}
                          item={item}
                          onOpen={onOpen}
                          onOpenFile={openFile}
                        />
                      ))}
                    </div>
                    {actionWorkspaceItems.length === 0 ? <p className="agent-workbench-compressed-note">{actionWorkspaceEmpty}</p> : null}
                    {files.length > 0 ? (
                      <section className="agent-workbench-files-inline" aria-label="文件窗口">
                        <div className="agent-workbench-actions-header">
                          <strong>文件</strong>
                          <small>{files.length} 个文件/产物</small>
                        </div>
                        <div className="agent-workbench-file-list compact" aria-label="文件操作列表">
                          {files.map((file) => (
                            <button
                              key={file.id}
                              type="button"
                              className={selectedFile?.id === file.id ? "active" : ""}
                              aria-pressed={selectedFile?.id === file.id}
                              onClick={() => setSelectedFileId(file.id)}
                            >
                              <small>{file.operation}</small>
                              <strong>{file.path || file.filename}</strong>
                              <span>{[file.kind, file.size].filter(Boolean).join(" · ") || "文件"}</span>
                            </button>
                          ))}
                        </div>
                      </section>
                    ) : null}
                    {recoveryCount > 0 ? (
                      <div className="agent-workbench-recovery-inline">
                        <RunFailureDiagnosticsPanel diagnostics={failureDiagnostics} />
                        <RunExecutionIntentsPanel intents={executionIntents} />
                      </div>
                    ) : null}
                  </section>
                  <section
                    className={`agent-workbench-preview-pane${selectedFile ? " is-selected" : ""}`}
                    ref={previewPaneRef}
                    aria-label="关联文件预览"
                  >
                    <div className="agent-workbench-actions-header">
                      <strong>关联文件预览</strong>
                      <small>{selectedFile ? selectedFile.path || selectedFile.filename : `${files.length} 个文件/产物`}</small>
                    </div>
                    {selectedFile ? (
                      <WorkbenchFilePreview
                        key={selectedFile.id}
                        file={selectedFile}
                        onOpenSource={(target) => {
                          onOpen(target);
                        }}
                      />
                    ) : (
                      <p className="agent-workbench-compressed-note">点击动作里的文件，可在这里直接预览内容。</p>
                    )}
                  </section>
                </section>
              ) : null}
            </>
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}

function RunAgentActivityStrip({
  cards,
  items,
  onOpenAgent,
}: {
  cards: AgentDispatchCard[];
  items: ProcessDetailTarget[];
  onOpenAgent: (agentId: string) => void;
}) {
  if (cards.length === 0) return null;
  const visibleCards = cards.slice(0, 4);
  const hiddenCount = cards.length - visibleCards.length;
  return (
    <div className="run-agent-activity-strip" aria-label="本轮参与 Agent">
      {visibleCards.map((card) => {
        const activityCount = agentActivityItems(card, items).length;
        const summary = agentInlineSummary(card);
        return (
          <button
            key={card.id}
            type="button"
            className={`run-agent-chip status-${card.status}`}
            onClick={() => onOpenAgent(card.id)}
            aria-label={`打开 ${agentDisplayTitle(card)} 调度详情`}
          >
            <span aria-hidden="true">{card.name.slice(0, 1)}</span>
            <strong>{agentDisplayTitle(card)}</strong>
            <small>
              {card.status}
              {activityCount > 0 ? ` · ${activityCount} 步` : ""}
            </small>
            <em>{summary}</em>
          </button>
        );
      })}
      {hiddenCount > 0 ? <span className="run-agent-chip-more">另 {hiddenCount} 个</span> : null}
    </div>
  );
}

function RunProcessSummary({
  detail,
  onOpen,
  agentNames,
  mainAgentModelName,
  workspaceFiles,
}: {
  detail: RunDetail;
  onOpen: (target: ProcessDetailTarget) => void;
  agentNames: Map<string, string>;
  mainAgentModelName?: string;
  workspaceFiles: ConversationWorkspaceFileBuckets;
}) {
  const [isWorkbenchOpen, setIsWorkbenchOpen] = useState(false);
  const [initialWorkbenchAgentId, setInitialWorkbenchAgentId] = useState<string | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const items = runProcessItems(detail, agentNames, mainAgentModelName);
  const dispatchCards = dispatchAgentCards(detail, agentNames);
  const taskChain = plannedTaskChain(detail, agentNames);
  const failureDiagnostics = failureDiagnosticsForRun(detail, agentNames);
  const executionIntents = executionIntentsForRun(detail, agentNames);
  const fileItems = workbenchFileItems([detail], workspaceFiles, items);
  const coordinationItems = items.filter(isWorkbenchCoordinationItem);
  const shouldShowSummary =
    items.length > 0 ||
    dispatchCards.length > 0 ||
    taskChain.length > 0 ||
    failureDiagnostics.length > 0 ||
    executionIntents.length > 0 ||
    fileItems.length > 0;
  const hasWorkbench = shouldShowSummary;
  const workbenchMeta = agentWorkbenchMeta({
    cards: dispatchCards,
    diagnostics: failureDiagnostics,
    files: fileItems,
    intents: executionIntents,
    items,
    taskChain,
  });
  useEffect(() => {
    if (!isWorkbenchOpen) return undefined;
    previouslyFocused.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousBodyOverflow = document.body.style.overflow;
    const previousDocumentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const processDetailOpen = document.querySelector('[role="dialog"][aria-label="运行过程详情"]');
      if (processDetailOpen) return;
      setIsWorkbenchOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousBodyOverflow || "";
      document.documentElement.style.overflow = previousDocumentOverflow || "";
      previouslyFocused.current?.focus();
    };
  }, [isWorkbenchOpen]);
  if (!shouldShowSummary) return null;
  return (
    <section className="run-process-summary" aria-label="Agent 集群动作">
      {!hasWorkbench && items.length > 0 ? (
        <div className="agent-cluster-status" role="status" aria-label={`Agent 集群，${items.length} 个关键动作`}>
          <span aria-hidden="true">⌘</span>
          <strong>Agent 集群</strong>
          <small>{items.length} 个关键动作</small>
        </div>
      ) : null}
      {hasWorkbench ? (
        <section className="agent-workbench" aria-label="Agent 工作席">
          <RunAgentActivityStrip
            cards={dispatchCards}
            items={items}
            onOpenAgent={(agentId) => {
              setInitialWorkbenchAgentId(agentId);
              setIsWorkbenchOpen(true);
            }}
          />
          <button
            type="button"
            className="agent-workbench-trigger"
            aria-expanded={isWorkbenchOpen}
            onClick={() => {
              setInitialWorkbenchAgentId(null);
              setIsWorkbenchOpen((current) => !current);
            }}
          >
            <span aria-hidden="true">⌘</span>
            <strong>Agent 工作席</strong>
            <small className="agent-workbench-meta">{workbenchMeta}</small>
          </button>
          {isWorkbenchOpen ? (
            <AgentWorkbenchDrawer
              dispatchCards={dispatchCards}
              executionIntents={executionIntents}
              failureDiagnostics={failureDiagnostics}
              files={fileItems}
              initialAgentId={initialWorkbenchAgentId}
              items={items}
              taskChain={taskChain}
              onClose={() => setIsWorkbenchOpen(false)}
              onOpen={(item) => {
                onOpen(item);
              }}
            />
          ) : null}
        </section>
      ) : null}
      {!hasWorkbench && taskChain.length > 0 ? (
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
      {!hasWorkbench ? <RunFailureDiagnosticsPanel diagnostics={failureDiagnostics} /> : null}
      {!hasWorkbench ? <RunExecutionIntentsPanel intents={executionIntents} /> : null}
      {!hasWorkbench && items.length > 0 ? (
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
  if (!first) return `${group.rows.length} 项摘要`;
  const presentation = processDetailValuePresentation(first);
  if (presentation.kind === "json") return "查看结构化 JSON";
  if (presentation.kind === "code") return `查看 ${presentation.label} 内容`;
  return conciseProcessText(presentation.text, `${group.rows.length} 项摘要`);
}

function BoundedTextBlock({
  copyAriaLabel,
  label,
  text,
  shouldCollapse: shouldCollapseOverride,
  collapseLabel = "展开",
  expandLabel = "收起",
}: {
  copyAriaLabel: string;
  label: string;
  text: string;
  shouldCollapse?: boolean;
  collapseLabel?: string;
  expandLabel?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const contentId = useId();
  const contentRef = useRef<HTMLPreElement>(null);
  const shouldCollapse = shouldCollapseOverride ?? (text.length > 1200 || text.split("\n").length > 18);
  useEffect(() => {
    if (expanded) contentRef.current?.focus();
  }, [expanded]);
  return (
    <div
      className={`bounded-text-block${shouldCollapse && !expanded ? " is-collapsed" : ""}${shouldCollapse && expanded ? " is-expanded" : ""}`}
    >
      <div className="bounded-text-block-header">
        <span>{label}</span>
        <div>
          {shouldCollapse ? (
            <button
              type="button"
              className="text-button"
              aria-expanded={expanded}
              aria-controls={contentId}
              onClick={() => setExpanded((current) => !current)}
            >
              {expanded ? expandLabel : collapseLabel}
            </button>
          ) : null}
          <button
            type="button"
            className="text-button"
            aria-label={copied ? `已${copyAriaLabel}` : copyAriaLabel}
            onClick={() => {
              void copyTextToClipboard(text)
                .then(() => {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1600);
                })
                .catch(() => undefined);
            }}
          >
            {copied ? "已复制" : "复制"}
          </button>
        </div>
      </div>
      <pre id={contentId} ref={contentRef} tabIndex={0} aria-label={`${label}完整内容`}>
        {text}
      </pre>
    </div>
  );
}

function ProcessDetailValueBlock({ row }: { row: { label: string; value: string } }) {
  const presentation = processDetailValuePresentation(row);
  if (presentation.kind === "plain" && !presentation.shouldCollapse) {
    return <span>{presentation.text}</span>;
  }
  if (presentation.kind === "plain") {
    return (
      <BoundedTextBlock
        copyAriaLabel={presentation.copyLabel}
        label={row.label}
        text={presentation.text}
        shouldCollapse={presentation.shouldCollapse}
        collapseLabel="展开全文"
        expandLabel="收起"
      />
    );
  }
  return (
    <BoundedTextBlock
      copyAriaLabel={presentation.copyLabel}
      label={presentation.label}
      text={presentation.text}
      shouldCollapse={presentation.shouldCollapse}
    />
  );
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
      {openGroup
        ? createPortal(
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
                      <dd>
                        <ProcessDetailValueBlock row={row} />
                      </dd>
                    </Fragment>
                  ))}
                </dl>
              </section>
            </div>,
            document.body,
          )
        : null}
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

export function workspacePathError(value: string): string | null {
  const path = value.trim();
  if (!path) return "请填写工作区目录名。";
  if (path.includes("/") || path.includes("\\") || path.includes("..") || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(path)) {
    return "工作区目录名只能包含字母、数字、短横线和下划线，不能填写绝对路径或上级目录。";
  }
  return null;
}

type NewConversationDraft = {
  conversationId: string;
  title: string;
  projectId: string;
  projectLabel: string;
  workspacePath: string;
  referenceConversationId: string | null;
};

type ConversationProjectOption = {
  id: string;
  label: string;
  workspacePath: string;
  legacyWorkspaceCount: number;
};

type NewProjectDraft = {
  projectId: string;
  label: string;
  workspacePath: string;
};

function keepFocusInsideDialog(event: KeyboardEvent, dialog: HTMLElement) {
  if (event.key !== "Tab") return;
  const focusable = [...dialog.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
  )].filter((element) => {
    const collapsedDetails = element.closest("details:not([open])");
    return !element.hidden && (!collapsedDetails || element.tagName === "SUMMARY");
  });
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function ConversationDialogShell({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const focusTimer = window.setTimeout(() => {
      dialog?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    }, 0);
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (dialog) keepFocusInsideDialog(event, dialog);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      window.setTimeout(() => opener?.focus(), 0);
    };
  }, []);
  return createPortal(
    <div
      className="conversation-dialog-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="conversation-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <div>
            <span className="eyebrow">Conversation</span>
            <h3>{title}</h3>
          </div>
          <button type="button" className="conversation-dialog-close" aria-label={`关闭${title}`} onClick={onClose}>
            ×
          </button>
        </header>
        {children}
      </section>
    </div>,
    document.body,
  );
}

function NewConversationDialog({
  draft,
  projects,
  pending,
  error,
  onChange,
  onClose,
  onSubmit,
}: {
  draft: NewConversationDraft;
  projects: ConversationProjectOption[];
  pending: boolean;
  error: string | null;
  onChange: (next: NewConversationDraft) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const knownProject = projects.find((project) => project.id === draft.projectId) ?? null;
  return (
    <ConversationDialogShell title="新建会话" onClose={onClose}>
      <form
        className="conversation-dialog-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label>
          会话标题
          <input
            aria-label="会话标题"
            value={draft.title}
            maxLength={120}
            onChange={(event) => onChange({ ...draft, title: event.target.value })}
            placeholder="可选，默认显示为新会话"
          />
        </label>
        <label>
          所属项目
          <select
            aria-label="所属项目"
            value={knownProject?.id ?? ""}
            onChange={(event) => {
              const project = projects.find((item) => item.id === event.target.value);
              if (project) {
                onChange({
                  ...draft,
                  projectId: project.id,
                  projectLabel: project.label,
                  workspacePath: project.workspacePath,
                });
              }
            }}
          >
            {projects.length === 0 ? <option value="">请先创建项目工作区</option> : null}
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.label}{project.legacyWorkspaceCount > 1 ? `（历史目录 ${project.legacyWorkspaceCount} 个）` : ""}
              </option>
            ))}
          </select>
        </label>
        {knownProject ? (
          <small>项目工作区：<code>{workspacePreviewPath(knownProject.id, knownProject.workspacePath)}</code></small>
        ) : (
          <p className="form-error" role="status">请先关闭此窗口并创建项目工作区。</p>
        )}
        {draft.referenceConversationId ? (
          <small>创建后会引用原会话 <code>{draft.referenceConversationId}</code> 作为上下文。</small>
        ) : null}
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <div className="conversation-dialog-actions">
          <button type="button" className="secondary-action" onClick={onClose}>取消</button>
          <button type="submit" disabled={pending || !knownProject}>
            {pending ? "创建中..." : "创建会话"}
          </button>
        </div>
      </form>
    </ConversationDialogShell>
  );
}

function NewProjectDialog({
  draft,
  pending,
  error,
  onChange,
  onClose,
  onSubmit,
}: {
  draft: NewProjectDraft;
  pending: boolean;
  error: string | null;
  onChange: (next: NewProjectDraft) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const workspaceError = workspacePathError(draft.workspacePath);
  const projectIdError = workspacePathError(draft.projectId);
  return (
    <ConversationDialogShell title="新建项目工作区" onClose={onClose}>
      <form
        className="conversation-dialog-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label>
          项目名称
          <input
            aria-label="项目名称"
            value={draft.label}
            required
            maxLength={80}
            onChange={(event) => onChange({ ...draft, label: event.target.value })}
            placeholder="例如 魔方 Agent"
          />
        </label>
        <label>
          项目标识
          <input
            aria-label="项目 ID"
            value={draft.projectId}
            required
            aria-invalid={Boolean(projectIdError)}
            onChange={(event) => onChange({ ...draft, projectId: event.target.value })}
            placeholder="例如 mofang-agent"
          />
        </label>
        <label>
          共享工作区名称
          <input
            aria-label="共享工作区名称"
            value={draft.workspacePath}
            required
            aria-invalid={Boolean(workspaceError)}
            onChange={(event) => onChange({ ...draft, workspacePath: event.target.value })}
            placeholder="例如 main"
          />
        </label>
        <small>
          该项目下的多个会话将共享 <code>{workspacePreviewPath(draft.projectId, draft.workspacePath)}</code>。
        </small>
        {projectIdError ? <p className="form-error" role="alert">项目标识格式不正确。</p> : null}
        {workspaceError ? <p className="form-error" role="alert">{workspaceError}</p> : null}
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <div className="conversation-dialog-actions">
          <button type="button" className="secondary-action" onClick={onClose}>取消</button>
          <button
            type="submit"
            disabled={pending || !draft.label.trim() || Boolean(projectIdError) || Boolean(workspaceError)}
          >
            {pending ? "创建中..." : "创建项目"}
          </button>
        </div>
      </form>
    </ConversationDialogShell>
  );
}

function RenameConversationDialog({
  value,
  pending,
  error,
  onChange,
  onClose,
  onSubmit,
}: {
  value: string;
  pending: boolean;
  error: string | null;
  onChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  return (
    <ConversationDialogShell title="重命名会话" onClose={onClose}>
      <form
        className="conversation-dialog-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label>
          会话标题
          <input aria-label="会话标题" value={value} maxLength={120} onChange={(event) => onChange(event.target.value)} autoFocus />
        </label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <div className="conversation-dialog-actions">
          <button type="button" className="secondary-action" onClick={onClose}>取消</button>
          <button type="submit" disabled={pending || !value.trim()}>{pending ? "保存中..." : "保存名称"}</button>
        </div>
      </form>
    </ConversationDialogShell>
  );
}

type MessageBodyBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "code"; language: string; text: string }
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
    const fenceMatch = lines[index].match(/^```\s*([A-Za-z0-9_-]+)?\s*$/);
    if (fenceMatch) {
      flushParagraph();
      const language = fenceMatch[1]?.trim() || "text";
      index += 1;
      const codeLines: string[] = [];
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ kind: "code", language, text: codeLines.join("\n") });
      continue;
    }
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

function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return Promise.reject(new Error("clipboard unavailable"));
}

function CollapsibleMessageParagraph({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const lines = text.split("\n");
  const shouldCollapse = text.length > 900 || lines.length > 12;
  const preview = lines.slice(0, 10).join("\n");
  const displayText = shouldCollapse && !expanded
    ? `${preview.slice(0, 900)}${text.length > 900 || lines.length > 10 ? "\n..." : ""}`
    : text;
  const copyable = shouldCollapse || text.length > 240 || lines.length > 4;
  return (
    <div className={`message-paragraph${shouldCollapse ? " is-collapsible" : ""}`}>
      {copyable ? (
        <div className="message-paragraph-tools" aria-label="消息文本工具">
          {shouldCollapse ? (
            <button type="button" className="message-expand-button" onClick={() => setExpanded((current) => !current)}>
              {expanded ? "收起" : "展开全文"}
            </button>
          ) : null}
          <button
            type="button"
            className="message-expand-button"
            onClick={() => {
              void copyTextToClipboard(text).then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1600);
              }).catch(() => undefined);
            }}
          >
            {copied ? "已复制" : "复制全文"}
          </button>
        </div>
      ) : null}
      <p>{displayText}</p>
    </div>
  );
}

export function MessageBody({ text, title }: { text: string; title: string }) {
  const blocks = markdownMessageBlocks(text);
  if (blocks.length === 0) return null;
  let tableIndex = 0;
  return (
    <div className="message-body">
      {blocks.map((block, index) => {
        if (block.kind === "paragraph") {
          return <CollapsibleMessageParagraph key={`paragraph-${index}`} text={block.text} />;
        }
        if (block.kind === "code") {
          return (
            <BoundedTextBlock
              key={`code-${index}`}
              copyAriaLabel={`复制 ${block.language} 代码`}
              label={block.language}
              text={block.text}
            />
          );
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
  const navigate = useNavigate();
  const location = useLocation();
  const linkedConversationId = conversationIdFromSearch(location.search);
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
  const executionBackends = useQuery({
    queryKey: ["execution-backends"],
    queryFn: () => api.executionBackends(),
  });
  const mainAgent = useQuery({ queryKey: ["main-agent"], queryFn: () => api.mainAgent() });
  const conversations = useQuery({
    queryKey: ["conversations", false],
    queryFn: () => api.conversations(false),
  });
  const archivedConversations = useQuery({
    queryKey: ["conversations", true],
    queryFn: () => api.conversations(true),
  });
  const projectWorkspaces = useQuery({
    queryKey: ["project-workspaces"],
    queryFn: () => api.projectWorkspaces(),
  });
  const conversationProjects = useMemo(() => {
    const projects = new Map<string, ConversationProjectOption>();
    for (const project of projectWorkspaces.data ?? []) {
      projects.set(project.project_id, {
        id: project.project_id,
        label: project.label,
        workspacePath: project.workspace_path,
        legacyWorkspaceCount: project.legacy_workspace_count,
      });
    }
    for (const conversation of [...(conversations.data ?? []), ...(archivedConversations.data ?? [])]) {
      const id = conversation.project_id?.trim();
      if (!id || projects.has(id)) continue;
      projects.set(id, {
        id,
        label: conversation.project_label?.trim() || (id === "default" ? "默认项目" : id),
        workspacePath: conversation.workspace_path?.trim() || "main",
        legacyWorkspaceCount: 1,
      });
    }
    return [...projects.values()].sort((left, right) => left.label.localeCompare(right.label, "zh-CN"));
  }, [archivedConversations.data, conversations.data, projectWorkspaces.data]);
  const [message, setMessage] = useState("");
  const [editingQueueItemId, setEditingQueueItemId] = useState<string | null>(null);
  const [editingQueueMessage, setEditingQueueMessage] = useState("");
  const [mode, setMode] = useState<RunMode>("auto");
  const [workflowId, setWorkflowId] = useState("");
  const [agentIds, setAgentIds] = useState<string[]>([]);
  const [conversationId, setConversationId] = useState(
    () => linkedConversationId ?? newConversationId(),
  );
  const [projectId, setProjectId] = useState("default");
  const [projectLabel, setProjectLabel] = useState("");
  const [sandboxProfile, setSandboxProfile] = useState<SandboxProfile>("workspace_write");
  const [executionBackend, setExecutionBackend] = useState<ExecutionBackendId>("systemd");
  const [referenceConversationId, setReferenceConversationId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedConversationIds, setSelectedConversationIds] = useState<string[]>([]);
  const [submitNotice, setSubmitNotice] = useState<string | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const configDialogRef = useRef<HTMLElement>(null);
  const configTriggerRef = useRef<HTMLButtonElement>(null);
  const [directModel, setDirectModel] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [newConversationOpen, setNewConversationOpen] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const initialProjectSetupPromptedRef = useRef(false);
  const [newProjectDraft, setNewProjectDraft] = useState<NewProjectDraft>({
    projectId: "",
    label: "",
    workspacePath: "main",
  });
  const [newConversationDraft, setNewConversationDraft] = useState<NewConversationDraft>(() => {
    const draftConversationId = newConversationId();
    return {
      conversationId: draftConversationId,
      title: "",
      projectId: "default",
      projectLabel: "",
      workspacePath: draftConversationId,
      referenceConversationId: null,
    };
  });
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false);
  const [renameConversationOpen, setRenameConversationOpen] = useState(false);
  const [renameConversationTitle, setRenameConversationTitle] = useState("");
  const [conversationSearch, setConversationSearch] = useState("");
  const [processDetailTarget, setProcessDetailTarget] = useState<ProcessDetailTarget | null>(null);
  const [conversationPreviewFile, setConversationPreviewFile] = useState<WorkbenchFileItem | null>(null);
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
    confirmed: boolean;
  } | null>(null);
  const [dismissedScheduleApprovalRunIds, setDismissedScheduleApprovalRunIds] = useState<string[]>([]);
  const [dismissedEvolutionApprovalRunIds, setDismissedEvolutionApprovalRunIds] = useState<string[]>([]);
  const [dismissedOpenClawApprovalRunIds, setDismissedOpenClawApprovalRunIds] = useState<string[]>([]);
  const [dismissedProjectPreflightApprovalRunIds, setDismissedProjectPreflightApprovalRunIds] = useState<string[]>([]);
  const [dismissedRepairApprovalRunIds, setDismissedRepairApprovalRunIds] = useState<string[]>([]);
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
  const [projectPreflightApproval, setProjectPreflightApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: ProjectPreflightProposal;
  } | null>(null);
  const [repairApproval, setRepairApproval] = useState<{
    runId: string;
    decisionToken: string;
    version: number;
    proposal: RepairProposal;
  } | null>(null);
  const [capabilityApproval, setCapabilityApproval] = useState<CapabilityApproval | null>(null);
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
  const listedConversationMetadata = [
    ...(conversations.data ?? []),
    ...(archivedConversations.data ?? []),
  ].find((item) => item.conversation_id === activeConversationId);
  const activeConversationKnown =
    Boolean(selectedRun.data) ||
    Boolean(conversationRunCache[activeConversationId]) ||
    linkedConversationId === activeConversationId ||
    Boolean(listedConversationMetadata) ||
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
  const conversationQueue = useQuery({
    queryKey: ["conversation-queue", activeConversationId],
    queryFn: () => api.conversationQueue(activeConversationId),
    enabled: Boolean(activeConversationId && activeConversationKnown),
    refetchInterval: 1000,
    refetchIntervalInBackground: true,
  });
  const activeWorkspaceProjectId =
    activeConversation.data?.project_id?.trim() || listedConversationMetadata?.project_id?.trim() || projectId.trim() || "default";
  const activeWorkspaceSessionId =
    activeConversation.data?.workspace_path?.trim() || listedConversationMetadata?.workspace_path?.trim() || activeConversationId;
  const activeWorkspaceFiles = useQuery({
    queryKey: ["workspace-files", activeWorkspaceProjectId, activeWorkspaceSessionId],
    queryFn: () => api.workspaceFiles(activeWorkspaceProjectId, activeWorkspaceSessionId),
    enabled: Boolean(activeWorkspaceSessionId),
    refetchInterval: 1500,
    refetchIntervalInBackground: true,
  });

  async function refreshRunSurfaces(run: { id: string; conversation_id?: string | null }) {
    await queryClient.invalidateQueries({ queryKey: ["runs"] });
    await queryClient.invalidateQueries({ queryKey: ["run", run.id] });
    await queryClient.invalidateQueries({ queryKey: ["hermes"] });
    const surfaceConversationId = run.conversation_id?.trim() || activeConversationId;
    if (surfaceConversationId) {
      await queryClient.invalidateQueries({ queryKey: ["conversation", surfaceConversationId] });
      await queryClient.invalidateQueries({ queryKey: ["workspace-files"] });
    }
  }

  useEffect(() => {
    const closeHistoryDrawer = () => setHistoryOpen(false);
    window.addEventListener("agent-hub:close-history-drawer", closeHistoryDrawer);
    return () => window.removeEventListener("agent-hub:close-history-drawer", closeHistoryDrawer);
  }, []);
  useEffect(() => {
    if (!linkedConversationId || linkedConversationId === conversationId) return;
    setConversationId(linkedConversationId);
    setSelectedRunId(null);
  }, [conversationId, linkedConversationId]);
  useEffect(() => {
    if (linkedConversationId || selectedRunId || activeConversationKnown || message.trim()) return;
    const latest = conversations.data?.[0];
    if (!latest) return;
    setConversationId(latest.conversation_id);
    if (latest.project_id?.trim()) setProjectId(latest.project_id);
    setProjectLabel(latest.project_label?.trim() ?? "");
  }, [activeConversationKnown, conversations.data, linkedConversationId, message, selectedRunId]);
  useEffect(() => {
    if (!settings.data) return;
    setMode("auto");
    setWorkflowId(settings.data.default_workflow_id ?? "");
    setAgentIds(settings.data.default_agent_ids);
    setExecutionBackend(settings.data.default_execution_backend);
  }, [settings.data]);

  useEffect(() => {
    const firstAvailableBackend = executionBackends.data?.find((backend) => backend.available);
    if (!firstAvailableBackend) return;
    if (executionBackends.data?.some((backend) => backend.id === executionBackend && backend.available)) return;
    setExecutionBackend(firstAvailableBackend.id);
  }, [executionBackend, executionBackends.data]);

  useEffect(() => {
    if (!activeConversation.data) return;
    if (activeConversation.data.project_id?.trim()) setProjectId(activeConversation.data.project_id);
    setProjectLabel(activeConversation.data.project_label?.trim() ?? "");
  }, [activeConversation.data]);

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
      setProjectPreflightApproval(null);
      setRepairApproval(null);
      setCapabilityApproval(null);
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
      setProjectPreflightApproval(null);
      setRepairApproval(null);
      setCapabilityApproval(null);
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
      setProjectPreflightApproval(null);
      setRepairApproval(null);
      setCapabilityApproval(null);
      setEvolutionApproval((current) =>
        current && current.runId === proposedEvolution.runId ? current : proposedEvolution,
      );
    }
    const proposedOpenClaw = openClawApprovalFromRunDetail(selectedRun.data);
    if (proposedOpenClaw && !dismissedOpenClawApprovalRunIds.includes(proposedOpenClaw.runId)) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setProjectPreflightApproval(null);
      setRepairApproval(null);
      setCapabilityApproval(null);
      setOpenClawApproval((current) =>
        current && current.runId === proposedOpenClaw.runId ? current : proposedOpenClaw,
      );
    }
    const proposedRepair = repairApprovalFromRunDetail(selectedRun.data);
    if (proposedRepair && !dismissedRepairApprovalRunIds.includes(proposedRepair.runId)) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setProjectPreflightApproval(null);
      setCapabilityApproval(null);
      setRepairApproval((current) =>
        current &&
        current.runId === proposedRepair.runId &&
        current.version === proposedRepair.version &&
        current.decisionToken === proposedRepair.decisionToken
          ? current
          : proposedRepair,
      );
    }
    const proposedProjectPreflight = projectPreflightApprovalFromRunDetail(selectedRun.data);
    if (proposedProjectPreflight && !dismissedProjectPreflightApprovalRunIds.includes(proposedProjectPreflight.runId)) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setRepairApproval(null);
      setCapabilityApproval(null);
      setProjectPreflightApproval((current) =>
        current &&
        current.runId === proposedProjectPreflight.runId &&
        current.version === proposedProjectPreflight.version &&
        current.decisionToken === proposedProjectPreflight.decisionToken
          ? current
          : proposedProjectPreflight,
      );
    } else if (selectedRun.data && projectPreflightApproval?.runId === selectedRun.data.id) {
      setProjectPreflightApproval(null);
    }
    const proposedCapabilityApproval = capabilityApprovalFromRunDetail(selectedRun.data);
    if (proposedCapabilityApproval) {
      setModeSelection(null);
      setTemporaryApproval(null);
      setScheduleApproval(null);
      setEvolutionApproval(null);
      setOpenClawApproval(null);
      setRepairApproval(null);
      setProjectPreflightApproval(null);
      setCapabilityApproval((current) =>
        current &&
        current.runId === proposedCapabilityApproval.runId &&
        current.approvalId === proposedCapabilityApproval.approvalId &&
        current.version === proposedCapabilityApproval.version
          ? current
          : proposedCapabilityApproval,
      );
    } else if (selectedRun.data && capabilityApproval?.runId === selectedRun.data.id) {
      setCapabilityApproval(null);
    }
  }, [capabilityApproval, dismissedEvolutionApprovalRunIds, dismissedOpenClawApprovalRunIds, dismissedProjectPreflightApprovalRunIds, dismissedRepairApprovalRunIds, dismissedScheduleApprovalRunIds, modeSelection, projectPreflightApproval, selectedRun.data, temporaryApproval]);

  useEffect(() => {
    setProcessDetailTarget(null);
    setConversationPreviewFile(null);
  }, [selectedRunId]);

  const pageOverlayOpen =
    Boolean(processDetailTarget) ||
    Boolean(conversationPreviewFile) ||
    historyOpen ||
    configOpen ||
    newProjectOpen ||
    newConversationOpen ||
    renameConversationOpen;
  useEffect(() => {
    if (!pageOverlayOpen) return undefined;
    const previousBodyOverflow = document.body.style.overflow;
    const previousDocumentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow || "";
      document.documentElement.style.overflow = previousDocumentOverflow || "";
    };
  }, [pageOverlayOpen]);

  useEffect(() => {
    if (
      initialProjectSetupPromptedRef.current ||
      !projectWorkspaces.isSuccess ||
      !conversations.isSuccess ||
      !archivedConversations.isSuccess ||
      conversations.data.length > 0 ||
      archivedConversations.data.length > 0
    ) return;
    initialProjectSetupPromptedRef.current = true;
    const firstProject = conversationProjects[0];
    if (firstProject) {
      const nextConversationId = newConversationId();
      setProjectId(firstProject.id);
      setProjectLabel(firstProject.label);
      setNewConversationDraft({
        conversationId: nextConversationId,
        title: "",
        projectId: firstProject.id,
        projectLabel: firstProject.label,
        workspacePath: firstProject.workspacePath,
        referenceConversationId: null,
      });
      setNewProjectOpen(false);
      setNewConversationOpen(true);
      setSubmitNotice("请选择项目并创建第一条会话。");
      return;
    }
    setNewProjectDraft({ projectId: "", label: "", workspacePath: "main" });
    setNewConversationOpen(false);
    setNewProjectOpen(true);
    setSubmitNotice("请先创建项目工作区，再创建第一条会话。");
  }, [archivedConversations.data, archivedConversations.isSuccess, conversationProjects, conversations.data, conversations.isSuccess, projectWorkspaces.isSuccess]);

  useEffect(() => {
    if (!configOpen) return undefined;
    const dialog = configDialogRef.current;
    const focusTimer = window.setTimeout(() => {
      dialog?.querySelector<HTMLElement>("button, select, input, textarea")?.focus();
    }, 0);
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setConfigOpen(false);
        return;
      }
      if (dialog) keepFocusInsideDialog(event, dialog);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      window.setTimeout(() => configTriggerRef.current?.focus(), 0);
    };
  }, [configOpen]);

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
    mutationFn: async (override?: RunSubmissionOverride) => {
      const runMessage = (override?.message ?? message).trim();
      let conversationMetadata = activeConversation.data ?? listedConversationMetadata;
      if (!conversationMetadata?.created_at) {
        try {
          const created = await api.createConversation({
            conversation_id: conversationId,
            project_id: projectId.trim() || "default",
            project_label: projectLabel.trim() || null,
            workspace_path: conversationId.trim(),
          });
          conversationMetadata = created;
          queryClient.setQueryData<Conversation>(["conversation", created.conversation_id], {
            ...created,
            runs: activeConversation.data?.runs ?? [],
          });
          await queryClient.invalidateQueries({ queryKey: ["conversations"] });
        } catch (error) {
          if (!(error instanceof ApiError) || error.code !== "conversation_conflict") throw error;
          conversationMetadata = await api.conversation(conversationId);
        }
      }
      return api.createRun({
        message: runMessage,
        mode: "auto",
        reference_workflow_id: workflowId || null,
        allow_workflow_adjustment: false,
        agent_ids: [],
        direct_model: null,
        conversation_id: conversationId,
        reference_conversation_id: referenceConversationId.trim() || null,
        project_id: conversationMetadata.project_id?.trim() || projectId.trim() || null,
        project_label: conversationMetadata.project_label?.trim() || projectLabel.trim() || null,
        workspace_session_id: conversationMetadata.workspace_path?.trim() || conversationId.trim() || null,
        sandbox_profile: sandboxProfile,
        ...(selectedExecutionBackend?.available ? { execution_backend: executionBackend } : {}),
        requested_permissions: requestedPermissionsForSandbox(sandboxProfile),
        attachment_ids: attachmentDraft?.attachment ? [attachmentDraft.attachment.id] : [],
        skip_evolution_proposal: override?.skipEvolutionProposal === true ? true : undefined,
      });
    },
    onSuccess: async (run, override) => {
      setSelectedRunId(run.id);
      if (run.conversation_id) setConversationId(run.conversation_id);
      const selection = modeSelectionFromSubmittedRun(run);
      const submittedMode: RunMode = "auto";
      if (selection && submittedMode !== "auto") {
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setProjectPreflightApproval(null);
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
        setProjectPreflightApproval(null);
        setDismissedRepairApprovalRunIds((current) => current.filter((id) => id !== repair.runId));
        setRepairApproval(repair);
        setSubmitNotice("运行失败已生成受控自修复建议，需要确认后才会重新排队。");
      } else if (run.openclaw_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setProjectPreflightApproval(null);
        setRepairApproval(null);
        setOpenClawApproval({ runId: run.id, proposal: run.openclaw_proposal, createdOperationId: null });
        setSubmitNotice("主 Agent 已识别为 OpenClaw 操作请求，请到 OpenClaw 管理页确认权限和执行边界。");
      } else if (run.project_preflight_proposal && run.decision_token) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setRepairApproval(null);
        setProjectPreflightApproval({
          runId: run.id,
          decisionToken: run.decision_token,
          version: run.version,
          proposal: run.project_preflight_proposal,
        });
        setSubmitNotice("主 Agent 已生成超大型项目预检计划，请确认后再开始执行。");
      } else if (run.schedule_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setProjectPreflightApproval(null);
        setRepairApproval(null);
        setDismissedScheduleApprovalRunIds((current) => current.filter((id) => id !== run.id));
        setScheduleApproval({ runId: run.id, proposal: run.schedule_proposal, createdScheduleId: null, confirmed: false });
        setSubmitNotice("主 Agent 已识别为计划任务，确认后会加入计划任务列表。");
      } else if (run.evolution_proposal) {
        setModeSelection(null);
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setOpenClawApproval(null);
        setProjectPreflightApproval(null);
        setRepairApproval(null);
        setDismissedEvolutionApprovalRunIds((current) => current.filter((id) => id !== run.id));
        setEvolutionApproval({ runId: run.id, proposal: run.evolution_proposal, createdEvolutionId: null });
        setSubmitNotice("主 Agent 已识别为进化任务，确认后会加入进化记录。");
      } else if (run.temporary_agent_proposal && run.decision_token) {
        setModeSelection(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setProjectPreflightApproval(null);
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
        setProjectPreflightApproval(null);
        setRepairApproval(null);
        setModeSelection(selection);
        setSubmitNotice("主 Agent 对这轮回复的模式判断不够确定，请直接在输入框回复编号或关键词继续。");
      } else {
        setTemporaryApproval(null);
        setScheduleApproval(null);
        setEvolutionApproval(null);
        setOpenClawApproval(null);
        setProjectPreflightApproval(null);
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

  const queueMessage = useMutation({
    mutationFn: (queuedMessage: string) =>
      api.queueConversationMessage(
        activeConversationId,
        `queue-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        {
          message: queuedMessage,
          mode: "auto",
          reference_conversation_id: referenceConversationId.trim() || null,
          project_id: activeWorkspaceProjectId || null,
          project_label: projectLabel.trim() || null,
          workspace_session_id: activeWorkspaceSessionId || null,
          sandbox_profile: sandboxProfile,
          ...(selectedExecutionBackend?.available ? { execution_backend: executionBackend } : {}),
          requested_permissions: requestedPermissionsForSandbox(sandboxProfile),
          attachment_ids: attachmentDraft?.attachment ? [attachmentDraft.attachment.id] : [],
        },
      ),
    onSuccess: async () => {
      setMessage("");
      setAttachmentDraft(null);
      setArchiveInstallFile(null);
      setSubmitNotice("消息已排队，将在当前任务结束后执行。");
      await queryClient.invalidateQueries({ queryKey: ["conversation-queue", activeConversationId] });
    },
    onError: (error, queuedMessage) => {
      if (error instanceof ApiError && error.code === "conversation_not_active") {
        createRun.mutate({ message: queuedMessage, mode: "auto" });
      }
    },
  });

  const editQueueItem = useMutation({
    mutationFn: ({ item, nextMessage }: { item: ConversationQueueItem; nextMessage: string }) =>
      api.editConversationQueueItem(item.id, { version: item.version, message: nextMessage }),
    onSuccess: async () => {
      setEditingQueueItemId(null);
      setEditingQueueMessage("");
      setSubmitNotice("排队信息已更新，顺序和附件保持不变。");
      await queryClient.invalidateQueries({ queryKey: ["conversation-queue", activeConversationId] });
    },
  });

  const redirectQueueItem = useMutation({
    mutationFn: (item: ConversationQueueItem) =>
      api.redirectConversationQueueItem(item.id, { version: item.version }),
    onSuccess: async () => {
      setSubmitNotice("已请求改变方向。当前任务安全停止后，将执行这条排队信息。");
      await queryClient.invalidateQueries({ queryKey: ["conversation-queue", activeConversationId] });
      await queryClient.invalidateQueries({ queryKey: ["conversation", activeConversationId] });
    },
  });

  const cancelQueueItem = useMutation({
    mutationFn: (item: ConversationQueueItem) =>
      api.cancelConversationQueueItem(item.id, { version: item.version }),
    onSuccess: async (item) => {
      if (editingQueueItemId === item.id) {
        setEditingQueueItemId(null);
        setEditingQueueMessage("");
      }
      setSubmitNotice("已取消这条排队信息。");
      await queryClient.invalidateQueries({ queryKey: ["conversation-queue", activeConversationId] });
    },
  });

  const createProjectWorkspace = useMutation({
    mutationFn: (draft: NewProjectDraft) =>
      api.createProjectWorkspace({
        project_id: draft.projectId.trim(),
        label: draft.label.trim(),
        workspace_path: draft.workspacePath.trim(),
      }),
    onSuccess: (created: ProjectWorkspace) => {
      queryClient.setQueryData<ProjectWorkspace[]>(["project-workspaces"], (current) => [
        created,
        ...(current ?? []).filter((item) => item.project_id !== created.project_id),
      ]);
      setProjectId(created.project_id);
      setProjectLabel(created.label);
      setNewProjectOpen(false);
      const nextConversationId = newConversationId();
      setNewConversationDraft({
        conversationId: nextConversationId,
        title: "",
        projectId: created.project_id,
        projectLabel: created.label,
        workspacePath: created.workspace_path,
        referenceConversationId: null,
      });
      setNewConversationOpen(true);
      setSubmitNotice("项目工作区已创建，现在可以在该项目下创建第一条会话。");
    },
  });

  const createConversation = useMutation({
    mutationFn: (draft: NewConversationDraft) =>
      api.createConversation({
        conversation_id: draft.conversationId,
        ...(draft.title.trim() ? { title: draft.title.trim() } : {}),
        project_id: draft.projectId.trim(),
        project_label: draft.projectLabel.trim() || null,
        workspace_path: draft.workspacePath.trim(),
      }),
    onSuccess: (created, draft) => {
      queryClient.setQueryData<Conversation>(["conversation", created.conversation_id], {
        ...created,
        runs: [],
      });
      setConversationRunCache((current) => ({ ...current, [created.conversation_id]: [] }));
      setConversationId(created.conversation_id);
      setProjectId(created.project_id?.trim() || draft.projectId.trim());
      setProjectLabel(created.project_label?.trim() || draft.projectLabel.trim());
      setSelectedRunId(null);
      clearConversationTransientState();
      setReferenceConversationId(draft.referenceConversationId ?? "");
      setNewConversationOpen(false);
      setHistoryOpen(false);
      setSubmitNotice(
        draft.referenceConversationId
          ? `分支会话已创建，将引用 ${draft.referenceConversationId} 作为上下文。`
          : "会话已创建，后续运行由主 Agent 自动判断模式和角色。",
      );
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      if (linkedConversationId) navigate("/", { replace: true });
    },
  });

  const updateConversation = useMutation({
    mutationFn: ({
      conversationId: targetConversationId,
      title,
      archived,
    }: {
      conversationId: string;
      title?: string;
      archived?: boolean;
    }) => api.updateConversation(targetConversationId, { title, archived }),
    onSuccess: async (updated, variables) => {
      queryClient.setQueryData<Conversation>(["conversation", updated.conversation_id], (current) => ({
        ...updated,
        runs: current?.runs ?? conversationRunCache[updated.conversation_id] ?? [],
      }));
      setRenameConversationOpen(false);
      setConversationMenuOpen(false);
      setSubmitNotice(
        variables.title !== undefined
          ? "会话名称已更新。"
          : variables.archived
            ? "会话已归档，需要时可从会话菜单恢复。"
            : "会话已恢复。",
      );
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
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

  const cancelSelfRepair = () => {
    const approval = repairApproval;
    if (!approval) return;
    setDismissedRepairApprovalRunIds((current) =>
      current.includes(approval.runId) ? current : [...current, approval.runId],
    );
    setRepairApproval(null);
    setSubmitNotice("已取消本次受控自修复建议。");
  };

  const approveProjectPreflight = useMutation({
    mutationFn: () => {
      if (!projectPreflightApproval) throw new Error("project preflight approval is unavailable");
      return api.approveProjectPreflight(projectPreflightApproval.runId, {
        decision_token: projectPreflightApproval.decisionToken,
        version: projectPreflightApproval.version,
      });
    },
    onSuccess: async (run) => {
      setProjectPreflightApproval(null);
      setSubmitNotice("已批准项目架构预检，主 Agent 已按计划进入执行队列。");
      await refreshRunSurfaces(run);
    },
  });

  const cancelProjectPreflightApproval = () => {
    const approval = projectPreflightApproval;
    if (!approval) return;
    setDismissedProjectPreflightApprovalRunIds((current) =>
      current.includes(approval.runId) ? current : [...current, approval.runId],
    );
    setProjectPreflightApproval(null);
    setSubmitNotice("已取消项目架构预检，本轮不会自动进入执行。你可以继续补充要求或重新发送。");
  };

  const approveCapability = useMutation({
    mutationFn: () => {
      if (!capabilityApproval) throw new Error("capability approval is unavailable");
      return api.approveCapability(capabilityApproval.runId, {
        approval_id: capabilityApproval.approvalId,
        version: capabilityApproval.version,
      });
    },
    onSuccess: async (run) => {
      setCapabilityApproval(null);
      setSubmitNotice("已允许本次沙箱工具调用，当前任务已重新排队继续执行。");
      await refreshRunSurfaces(run);
    },
  });

  const rejectCapability = useMutation({
    mutationFn: () => {
      if (!capabilityApproval) throw new Error("capability approval is unavailable");
      return api.rejectCapability(capabilityApproval.runId, {
        approval_id: capabilityApproval.approvalId,
        version: capabilityApproval.version,
      });
    },
    onSuccess: async (run) => {
      setCapabilityApproval(null);
      setSubmitNotice("已拒绝本次沙箱工具调用，当前任务已取消。");
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
      if (!scheduleApproval.confirmed) throw new Error("schedule approval requires explicit confirmation");
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

  const cancelOpenClawApproval = () => {
    const approval = openClawApproval;
    if (!approval) return;
    setDismissedOpenClawApprovalRunIds((current) =>
      current.includes(approval.runId) ? current : [...current, approval.runId],
    );
    setOpenClawApproval(null);
    setSubmitNotice("已取消 OpenClaw 操作建议，不会创建待审批动作。");
  };

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
    const slashCommand = slashCommandsForQuery(trimmed)[0];
    if (slashCommand && trimmed.toLowerCase() === `/${slashCommand.id}`) {
      executeSlashCommand(slashCommand.id);
      return;
    }
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
        { value: "cancel", label: "取消修复", aliases: ["取消", "忽略", "不修复", "拒绝", "cancel", "reject", "no"] },
      ]);
      if (!choice) {
        setSubmitNotice("请回复 1/接受/修复，或 2/取消/不修复。");
        return;
      }
      setMessage("");
      if (choice.option.value === "cancel") {
        cancelSelfRepair();
        return;
      }
      setSubmitNotice("已选择接受受控自修复，正在重新排队。");
      acceptSelfRepair.mutate();
      return;
    }
    if (capabilityApproval) {
      const choice = parseChoiceText(trimmed, [
        { value: "approve", label: "允许一次", aliases: ["允许", "同意", "通过", "approve", "yes", "allow"] },
        { value: "reject", label: "拒绝", aliases: ["拒绝", "取消", "reject", "no", "deny"] },
      ]);
      if (!choice) {
        setSubmitNotice("请回复 1/允许一次，或 2/拒绝本次沙箱工具调用。");
        return;
      }
      setMessage("");
      if (choice.option.value === "approve") {
        setSubmitNotice("已选择允许一次，正在重新排队继续执行。");
        approveCapability.mutate();
        return;
      }
      setSubmitNotice("已选择拒绝，本次任务会取消。");
      rejectCapability.mutate();
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
    if (canStopLatestRun) {
      queueMessage.mutate(trimmed);
      return;
    }
    createRun.mutate({ message: trimmed, mode: "auto" });
  }

  function clearConversationTransientState() {
    setReferenceConversationId("");
    setMessage("");
    setMode("auto");
    setWorkflowId(settings.data?.default_workflow_id ?? "");
    setAgentIds(settings.data?.default_agent_ids ?? []);
    setExecutionBackend(settings.data?.default_execution_backend ?? "systemd");
    setDirectModel("");
    setTemporaryApproval(null);
    setScheduleApproval(null);
    setEvolutionApproval(null);
    setOpenClawApproval(null);
    setProjectPreflightApproval(null);
    setRepairApproval(null);
    setCapabilityApproval(null);
    setModeSelection(null);
    setProcessDetailTarget(null);
  }

  function startNewConversation() {
    const selectedProject =
      conversationProjects.find((item) => item.id === projectId.trim()) ?? conversationProjects[0];
    if (!selectedProject) {
      if (projectWorkspaces.isLoading || conversations.isLoading || archivedConversations.isLoading) {
        setSubmitNotice("正在读取项目工作区，请稍候再试。");
        return;
      }
      if (projectWorkspaces.isError) {
        setSubmitNotice("项目工作区读取失败，请重试后再新建会话。");
        void projectWorkspaces.refetch();
        return;
      }
      setNewProjectDraft({ projectId: "", label: "", workspacePath: "main" });
      createProjectWorkspace.reset();
      setNewProjectOpen(true);
      setSubmitNotice("请先创建项目工作区，再在项目下新建会话。");
      return;
    }
    const nextConversationId = newConversationId();
    setNewConversationDraft({
      conversationId: nextConversationId,
      title: "",
      projectId: selectedProject.id,
      projectLabel: selectedProject.label,
      workspacePath: selectedProject.workspacePath,
      referenceConversationId: null,
    });
    createConversation.reset();
    setNewConversationOpen(true);
    setHistoryOpen(false);
  }

  function startNewProject() {
    setNewProjectDraft({ projectId: "", label: "", workspacePath: "main" });
    createProjectWorkspace.reset();
    setNewProjectOpen(true);
    setNewConversationOpen(false);
    setHistoryOpen(false);
  }

  function startBranchConversation(sourceConversationId?: string | null) {
    const trimmedSourceConversationId = sourceConversationId?.trim() || runConversationId(selectedRun.data) || conversationId.trim();
    if (!trimmedSourceConversationId) {
      setSubmitNotice("当前没有可引用的会话。");
      return;
    }
    const nextConversationId = newConversationId();
    const selectedProject =
      conversationProjects.find((item) => item.id === projectId.trim()) ?? conversationProjects[0];
    if (!selectedProject) {
      if (projectWorkspaces.isLoading || conversations.isLoading || archivedConversations.isLoading) {
        setSubmitNotice("正在读取项目工作区，请稍候再创建分支会话。");
        return;
      }
      if (projectWorkspaces.isError) {
        setSubmitNotice("项目工作区读取失败，请重试后再创建分支会话。");
        void projectWorkspaces.refetch();
        return;
      }
      startNewProject();
      setSubmitNotice("请先创建项目工作区，再创建分支会话。");
      return;
    }
    setNewConversationDraft({
      conversationId: nextConversationId,
      title: "",
      projectId: selectedProject.id,
      projectLabel: selectedProject.label,
      workspacePath: selectedProject.workspacePath,
      referenceConversationId: trimmedSourceConversationId,
    });
    createConversation.reset();
    setNewConversationOpen(true);
    setHistoryOpen(false);
  }

  function cancelBranchReference() {
    setReferenceConversationId("");
    setSubmitNotice("已取消引用会话。");
  }

  function executeSlashCommand(commandId: SlashCommand["id"]) {
    if (commandId === "new") {
      startNewConversation();
      return;
    }
    if (commandId === "mode") {
      setConfigOpen(true);
      setMessage("");
      setSubmitNotice("已打开本轮运行设置，可调整参考方案、执行环境和沙箱权限。");
      return;
    }
    if (commandId === "memory") {
      setMessage("");
      navigate("/memory");
      return;
    }
    if (commandId === "skills") {
      setMessage("");
      navigate("/skills");
      return;
    }
    setMessage("");
    navigate("/logs");
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
  const archivedConversationIds = new Set(
    (archivedConversations.data ?? []).map((item) => item.conversation_id),
  );
  const conversationListItems = items.filter(
    (item) => !archivedConversationIds.has(item.conversation_id ?? ""),
  );
  const visibleConversationItems = conversationListItems.filter(
    (item) => conversationMatchesSearch(item, conversationSearch, items),
  );
  const runConversationIds = new Set(
    items.map((item) => item.conversation_id).filter((value): value is string => Boolean(value)),
  );
  const metadataConversationItems = [
    ...(conversations.data ?? []).filter((item) => !runConversationIds.has(item.conversation_id)),
    ...(archivedConversations.data ?? []),
  ];
  const normalizedConversationSearch = conversationSearch.trim().toLocaleLowerCase();
  const visibleMetadataConversationItems = metadataConversationItems.filter((item) =>
    !normalizedConversationSearch
      ? true
      : [item.title, item.conversation_id, item.project_label, item.project_id, item.workspace_path]
          .filter(Boolean)
          .some((value) => value?.toLocaleLowerCase().includes(normalizedConversationSearch)),
  );
  const totalConversationCount = conversationListItems.length + metadataConversationItems.length;
  const visibleConversationCount = visibleConversationItems.length + visibleMetadataConversationItems.length;
  const selectedSandboxLabel = displaySandboxProfile(sandboxProfile);
  const availableExecutionBackends = executionBackends.data?.filter((backend) => backend.available) ?? [];
  const selectedExecutionBackend = executionBackends.data?.find((item) => item.id === executionBackend);
  const configuredExecutionBackend = executionBackends.data?.find(
    (backend) => backend.id === settings.data?.default_execution_backend,
  );
  const executionBackendFallbackNotice =
    availableExecutionBackends.length > 0 &&
    settings.data &&
    (!configuredExecutionBackend || !configuredExecutionBackend.available) &&
    executionBackend === availableExecutionBackends[0].id
      ? `默认执行环境不可用，已自动切换到${availableExecutionBackends[0].name}。`
      : null;
  const executionBackendNotice = executionBackends.isLoading
    ? "正在探测执行环境，请稍候。"
    : executionBackends.isError
      ? "执行环境探测失败，普通对话仍可发送；本次不会指定 Skill 执行环境。"
      : availableExecutionBackends.length === 0
        ? "当前没有可用的 Skill 执行环境，普通对话仍可发送。"
        : null;
  const executionBackendLabel = selectedExecutionBackend
    ? `${selectedExecutionBackend.name}${selectedExecutionBackend.available ? "" : "（不可用）"}`
    : executionBackendNotice
      ? "未指定 Skill 执行环境"
      : executionBackend;
  const executionBackendStatus =
    executionBackendFallbackNotice ??
    executionBackendNotice ??
    (selectedExecutionBackend
      ? `${selectedExecutionBackend.isolation} · ${selectedExecutionBackend.cost}`
      : "正在确认执行环境...");
  const slashCommandSuggestions = slashCommandsForQuery(message);
  const savedAgents = agents.data ?? [];
  const savedModels = models.data ?? [];
  const savedWorkflows = workflows.data ?? [];
  const agentNameMap = new Map(savedAgents.map((agent) => [agent.id, agent.name]));
  const cachedConversationRuns = activeConversationId ? conversationRunCache[activeConversationId] : undefined;
  const activeConversationRuns =
    activeConversation.data?.conversation_id === activeConversationId ? activeConversation.data.runs : undefined;
  const conversationVisibleRuns = activeConversationRuns
    ? mergeConversationRuns(cachedConversationRuns, activeConversationRuns)
    : cachedConversationRuns;
  const selectedRunAlreadyVisible =
    selectedRun.data && runConversationId(selectedRun.data) === activeConversationId
      ? conversationVisibleRuns?.find((run) => run.id === selectedRun.data?.id)
      : undefined;
  const shouldMergeSelectedRun =
    selectedRun.data &&
    runConversationId(selectedRun.data) === activeConversationId &&
    (!selectedRunAlreadyVisible || runHasNewerProgress(selectedRun.data, selectedRunAlreadyVisible));
  const visibleRuns =
    shouldMergeSelectedRun
      ? mergeConversationRuns(conversationVisibleRuns, [selectedRun.data])
      : conversationVisibleRuns ?? activeConversationRuns ?? (selectedRun.data ? [selectedRun.data] : []);
  const messages = conversationMessages(visibleRuns);
  const checkpoints = conversationCheckpoints(messages);
  const workspaceFiles = mergeWorkspaceFileList(
    conversationWorkspaceFiles(visibleRuns),
    activeWorkspaceFiles.data,
  );
  const inlineWorkspaceFilesMessageId = inlineFileMessageId(messages);
  const temporaryApprovalVisibleInMessages =
    !!temporaryApproval &&
    messages.some((item) => item.id === `${temporaryApproval.runId}-temporary-agent-approval`);
  const scheduleApprovalVisibleInMessages =
    !!scheduleApproval && messages.some((item) => item.id === `${scheduleApproval.runId}-schedule-approval`);
  const openClawApprovalVisibleInMessages =
    !!openClawApproval && messages.some((item) => item.id === `${openClawApproval.runId}-openclaw-approval`);
  const projectPreflightApprovalVisibleInMessages =
    !!projectPreflightApproval &&
    messages.some((item) => item.id === `${projectPreflightApproval.runId}-project-preflight-approval`);
  const repairApprovalVisibleInMessages =
    !!repairApproval && messages.some((item) => item.id === `${repairApproval.runId}-repair-approval`);
  const blockedQueueSuccessorIds = new Set(
    (conversationQueue.data ?? [])
      .filter((item) => item.status === "queued" || item.status === "redirecting")
      .map((item) => item.successor_run_id),
  );
  const latestVisibleRun =
    [...visibleRuns]
      .reverse()
      .find((run) => !blockedQueueSuccessorIds.has(run.id)) ??
    (selectedRun.data && !blockedQueueSuccessorIds.has(selectedRun.data.id)
      ? selectedRun.data
      : undefined);
  const canStopLatestRun = Boolean(latestVisibleRun && !TERMINAL_STATUSES.has(latestVisibleRun.status));
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
  const deletableConversationIds = conversationSelectionIds(items, conversationSearch);
  const selectedDeletableConversationIds = selectedConversationIds.filter((id) =>
    deletableConversationIds.includes(id),
  );
  const allDeletableSelected =
    deletableConversationIds.length > 0 &&
    deletableConversationIds.every((id) => selectedConversationIds.includes(id));
  const currentConversationListItem = items.find((item) => item.conversation_id === activeConversationId);
  const currentConversationTitle =
    activeConversation.data?.title?.trim() ||
    (currentConversationListItem ? conversationTitle(currentConversationListItem, items) : conversationId);
  const currentConversationArchived = Boolean(activeConversation.data?.archived_at);
  const currentConversationPersisted = Boolean(
    activeConversation.data?.created_at || listedConversationMetadata?.created_at,
  );

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
      <h2>对话</h2>
      <p className="compact-page-intro">
        连续对话窗口，可承接多轮任务、上下文压缩和历史会话。
      </p>

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
              <span>
                {conversationSearch.trim() ? `${visibleConversationCount}/${totalConversationCount}` : totalConversationCount} 条
              </span>
            </div>
            <div className="conversation-list-actions">
              <button type="button" className="conversation-close-button" aria-label="关闭历史对话" onClick={() => setHistoryOpen(false)}>
                ×
              </button>
            </div>
          </div>
          <input
            type="search"
            className="conversation-history-search"
            aria-label="搜索历史会话"
            placeholder="搜索最近会话、问题或 ID"
            value={conversationSearch}
            onChange={(event) => setConversationSearch(event.target.value)}
          />
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
                全选当前结果
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
          {totalConversationCount === 0 ? (
            <p className="field-help">还没有会话。直接发送消息即可开始。</p>
          ) : visibleConversationCount === 0 ? (
            <p className="field-help">没有匹配的历史会话。</p>
          ) : (
            <>
              {visibleMetadataConversationItems.map((conversation: ConversationMetadata) => {
                const title = conversation.title?.trim() || conversation.conversation_id;
                const archived = Boolean(conversation.archived_at);
                return (
                  <div
                    key={`metadata-${conversation.conversation_id}`}
                    className={`conversation-row${activeConversationId === conversation.conversation_id ? " conversation-row-active" : ""}`}
                  >
                    <span className="conversation-select-placeholder" aria-hidden="true" />
                    <button
                      type="button"
                      className="conversation-item"
                      aria-label={`进入会话 ${title}`}
                      onClick={() => {
                        setConversationId(conversation.conversation_id);
                        setSelectedRunId(null);
                        if (conversation.project_id?.trim()) setProjectId(conversation.project_id);
                        setProjectLabel(conversation.project_label?.trim() ?? "");
                        setHistoryOpen(false);
                      }}
                    >
                      <span className="conversation-mode-chip">{archived ? "归档" : "会话"}</span>
                      <strong className="conversation-title-text">{title}</strong>
                      <small className="conversation-meta-line">
                        {conversation.project_label?.trim() || conversation.project_id || "默认项目"}
                      </small>
                    </button>
                    <button
                      type="button"
                      className="conversation-branch-button"
                      aria-label={`按原思路新建分支 ${title}`}
                      title="引用这段会话新建分支"
                      onClick={() => startBranchConversation(conversation.conversation_id)}
                    >
                      分支
                    </button>
                    <span className="conversation-metadata-status">{archived ? "已归档" : "未开始"}</span>
                  </div>
                );
              })}
              {visibleConversationItems.map((run) => {
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
              })}
            </>
          )}
          {deleteRun.isError ? (
            <p className="form-error" role="alert">
              {formatApiError(deleteRun.error, "对话删除失败")}
            </p>
          ) : null}
        </nav>

        <div className="chat-panel">
          {configOpen ? createPortal(
            <div
              className="composer-settings-backdrop"
              onPointerDown={(event) => {
                if (event.target === event.currentTarget) setConfigOpen(false);
              }}
            >
              <section
                ref={configDialogRef}
                className="composer-settings-dialog"
                role="dialog"
                aria-modal="true"
                aria-label="本次运行设置"
              >
                <header className="composer-settings-header">
                  <div>
                    <span className="eyebrow">Run settings</span>
                    <h3>本次运行设置</h3>
                  </div>
                  <button type="button" className="secondary-action" aria-label="关闭运行设置" onClick={() => setConfigOpen(false)}>
                    关闭
                  </button>
                </header>
                <div className="composer-config-sheet" role="region" aria-label="本次运行更多设置">
          <div className="composer-config-summary" aria-label="本次运行设置概览">
            <strong>本次运行配置</strong>
            <div>
              <span>主 Agent 自动</span>
              <span>{selectedSandboxLabel}</span>
              <span>{executionBackendLabel}</span>
              <span>{workflowId ? `参考 ${selectedWorkflow?.name ?? workflowId}` : "无参考方案"}</span>
            </div>
          </div>
          <details className="run-settings-panel" aria-label="本次运行设置">
            <summary aria-label="展开或收起本次运行设置">详细设置</summary>
            <div className="chat-config-strip" aria-label="本次对话运行设置">
            <label htmlFor="run-workflow">
              参考方案
              <select
                id="run-workflow"
                aria-label="参考方案"
                value={workflowId}
                onChange={(event) => setWorkflowId(event.target.value)}
              >
                <option value="">不使用参考方案</option>
                {savedWorkflows
                  .filter((workflow) => workflow.enabled)
                  .map((workflow) => (
                    <option key={workflow.id} value={workflow.id}>
                      {workflow.name}
                    </option>
                ))}
              </select>
              <span className="field-help">只把步骤与交付物作为主 Agent 的参考，不会固定模式或角色。</span>
            </label>
            <div className="sandbox-settings" aria-label="沙箱权限">
              <span className="field-label">沙箱权限</span>
              <div className="sandbox-choice-row" role="group" aria-label="选择本次运行沙箱权限">
                {SANDBOX_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className={`sandbox-choice${sandboxProfile === option.value ? " selected" : ""}`}
                    aria-pressed={sandboxProfile === option.value}
                    onClick={() => setSandboxProfile(option.value)}
                  >
                    <strong>{option.label}</strong>
                    <span>{option.summary}</span>
                  </button>
                ))}
              </div>
            </div>
            <label htmlFor="execution-backend">
              执行环境
              <select
                id="execution-backend"
                aria-label="执行环境"
                value={selectedExecutionBackend ? executionBackend : ""}
                disabled={
                  executionBackends.isLoading ||
                  executionBackends.isError ||
                  availableExecutionBackends.length === 0
                }
                onChange={(event) => setExecutionBackend(event.target.value as ExecutionBackendId)}
              >
                {!executionBackends.data || executionBackends.data.length === 0 ? (
                  <option value="" disabled>
                    {executionBackends.isLoading ? "正在探测执行环境..." : "暂无可用执行环境"}
                  </option>
                ) : null}
                {(executionBackends.data ?? []).map((backend) => (
                  <option key={backend.id} value={backend.id} disabled={!backend.available}>
                    {backend.name}{backend.available ? "" : "（不可用）"}
                  </option>
                ))}
              </select>
              <span className="field-help" role="status">
                {executionBackendStatus}
              </span>
            </label>
            <div className="conversation-workspace-summary">
              <span className="field-label">会话工作区</span>
              <strong>{activeConversation.data?.project_label?.trim() || projectLabel || projectId}</strong>
              <code>{workspacePreviewPath(activeWorkspaceProjectId, activeWorkspaceSessionId)}</code>
              <small>项目归类和工作区在新建会话时确定，避免同一会话中途切换目录。</small>
            </div>
            <label htmlFor="reference-conversation-id">
              参考会话
              <input
                id="reference-conversation-id"
                aria-label="参考会话 ID"
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
              <span className="eyebrow">当前设置</span>
              <p>
                工作区：{workspacePreviewPath(activeWorkspaceProjectId, activeWorkspaceSessionId)} · 主 Agent 自动 · {selectedSandboxLabel} ·
                {executionBackendLabel}
              </p>
              {settings.isLoading ? <p>正在加载默认运行设置...</p> : null}
              {settings.isError ? (
                <p role="alert">{formatApiError(settings.error, "系统设置加载失败")}</p>
              ) : null}
              {workflows.isError ? (
                <p role="alert">{formatApiError(workflows.error, "工作流列表加载失败")}</p>
              ) : null}
              {selectedWorkflow ? (
                <>
                  <p>参考方案：{selectedWorkflow.name}{selectedWorkflow.task_type ? ` · ${selectedWorkflow.task_type}` : ""}</p>
                  <p>仅供主 Agent 参考，不会覆盖自动模式或角色判断。</p>
                </>
              ) : (
                <p>未选择参考方案，由主 Agent 按任务自动判断。</p>
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

                </div>
              </section>
            </div>,
            document.body,
          ) : null}

          <div className="chat-stream" role="region" aria-label="主对话内容" aria-live="polite">
            {selectedRun.isLoading ? <p>正在加载会话...</p> : null}
            {selectedRun.isError ? <p role="alert">{formatApiError(selectedRun.error, "会话加载失败")}</p> : null}
            {activeConversation.isLoading ? <p>正在读取当前会话...</p> : null}
            {activeConversation.isError ? (
              <p role="alert">{formatApiError(activeConversation.error, "当前会话读取失败")}</p>
            ) : null}
            <div className="chat-session-toolbar" aria-label="当前对话操作">
              <div className="chat-conversation-heading">
                <p className="chat-conversation-status">会话：{currentConversationTitle}</p>
                {currentConversationArchived ? <span className="conversation-archived-badge">已归档</span> : null}
              </div>
              <div className="chat-session-actions">
                <button type="button" className="secondary-action" aria-label="新建项目工作区" onClick={startNewProject}>
                  项目
                </button>
                <button type="button" className="secondary-action" aria-label="新建对话" onClick={startNewConversation}>
                  新建
                </button>
                <div className="conversation-menu-anchor">
                  <button
                    type="button"
                    className="conversation-menu-trigger"
                    aria-label="会话操作"
                    aria-expanded={conversationMenuOpen}
                    disabled={!currentConversationPersisted}
                    onClick={() => setConversationMenuOpen((current) => !current)}
                  >
                    ⋯
                  </button>
                  {conversationMenuOpen ? (
                    <div className="conversation-menu" role="menu">
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setRenameConversationTitle(currentConversationTitle);
                          updateConversation.reset();
                          setRenameConversationOpen(true);
                          setConversationMenuOpen(false);
                        }}
                      >
                        重命名
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        disabled={updateConversation.isPending}
                        onClick={() =>
                          updateConversation.mutate({
                            conversationId: activeConversationId,
                            archived: !currentConversationArchived,
                          })
                        }
                      >
                        {currentConversationArchived ? "恢复" : "归档"}
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
            <ConversationCheckpointNav checkpoints={checkpoints} conversationId={activeConversationId} />
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
            {projectPreflightApproval && !projectPreflightApprovalVisibleInMessages ? (
              <article className="chat-message assistant" aria-label="项目架构预检确认">
                <span className="eyebrow">{APP_BRAND_NAME}</span>
                <h3>{projectPreflightApproval.proposal.title}</h3>
                <p>{projectPreflightProposalBody(projectPreflightApproval.proposal)}</p>
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
                <article
                  className={`chat-message ${item.role}`}
                  id={chatMessageAnchorId(item.id)}
                >
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
                  {item.id === inlineWorkspaceFilesMessageId ? (
                    <ConversationWorkspaceFiles
                      files={workspaceFiles}
                      previewFiles={workbenchFileItems(
                        visibleRuns,
                        workspaceFiles,
                        visibleRuns.flatMap((run) => runProcessItems(run, agentNameMap, mainAgentModelName)),
                      )}
                      onPreviewFile={setConversationPreviewFile}
                      title="交付文件"
                      eyebrow="Files"
                      ariaLabel="交付文件"
                      showIntermediateInline
                    />
                  ) : null}
                </article>
                {item.id.endsWith("-request") && item.run ? (
                  <>
                    <RunInteractionSummaryInline
                      run={item.run}
                      agentNames={agentNameMap}
                      mainAgentModelName={mainAgentModelName}
                      onPreviewFile={setConversationPreviewFile}
                    />
                    <RunProcessSummary
                      detail={item.run}
                      onOpen={setProcessDetailTarget}
                      agentNames={agentNameMap}
                      mainAgentModelName={mainAgentModelName}
                      workspaceFiles={workspaceFiles}
                    />
                  </>
                ) : null}
              </Fragment>
            ))}
            {inlineWorkspaceFilesMessageId ? null : (
              <ConversationWorkspaceFiles
                files={workspaceFiles}
                previewFiles={workbenchFileItems(
                  visibleRuns,
                  workspaceFiles,
                  visibleRuns.flatMap((run) => runProcessItems(run, agentNameMap, mainAgentModelName)),
                )}
                onPreviewFile={setConversationPreviewFile}
              />
            )}
          </div>
          {conversationPreviewFile ? (
            <ConversationFilePreviewDrawer
              file={conversationPreviewFile}
              onClose={() => setConversationPreviewFile(null)}
              onOpenSource={setProcessDetailTarget}
            />
          ) : null}
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
                  <div className="composer-card-actions">
                    <button type="button" onClick={() => createOpenClawFromProposal.mutate()} disabled={createOpenClawFromProposal.isPending}>
                      {createOpenClawFromProposal.isPending ? "创建中..." : "创建待审批操作"}
                    </button>
                    <button type="button" className="secondary-action" disabled={createOpenClawFromProposal.isPending} onClick={cancelOpenClawApproval}>
                      取消操作建议
                    </button>
                  </div>
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
            {approveProjectPreflight.isError ? (
              <p role="alert">{formatApiError(approveProjectPreflight.error, "项目预检确认失败")}</p>
            ) : null}
            {approveCapability.isError ? (
              <p role="alert">{formatApiError(approveCapability.error, "沙箱权限确认失败")}</p>
            ) : null}
            {rejectCapability.isError ? (
              <p role="alert">{formatApiError(rejectCapability.error, "沙箱权限拒绝失败")}</p>
            ) : null}
            {repairApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="自修复确认">
                <div>
                  <span className="eyebrow">自修复待确认</span>
                  <strong>{repairApproval.proposal.title}</strong>
                  <small>{repairApproval.proposal.summary}</small>
                </div>
                <p>{repairProposalBody(repairApproval.proposal)}</p>
                <div className="composer-card-actions">
                  <button type="button" disabled={acceptSelfRepair.isPending} onClick={() => acceptSelfRepair.mutate()}>
                    {acceptSelfRepair.isPending ? "排队中..." : "接受修复"}
                  </button>
                  <button type="button" className="secondary-action" disabled={acceptSelfRepair.isPending} onClick={cancelSelfRepair}>
                    取消修复
                  </button>
                </div>
              </aside>
            ) : null}
            {projectPreflightApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="项目架构预检确认">
                <div>
                  <span className="eyebrow">项目预检待确认</span>
                  <strong>{projectPreflightApproval.proposal.title}</strong>
                  <small>{projectPreflightApproval.proposal.summary}</small>
                </div>
                <p>{projectPreflightProposalBody(projectPreflightApproval.proposal)}</p>
                <div className="composer-card-actions">
                  <button type="button" disabled={approveProjectPreflight.isPending} onClick={() => approveProjectPreflight.mutate()}>
                    {approveProjectPreflight.isPending ? "排队中..." : "批准并开始执行"}
                  </button>
                  <button type="button" className="secondary-action" disabled={approveProjectPreflight.isPending} onClick={cancelProjectPreflightApproval}>
                    取消预检
                  </button>
                </div>
              </aside>
            ) : null}
            {capabilityApproval ? (
              <aside className="composer-attachment-card" role="status" aria-label="沙箱权限确认">
                <div>
                  <span className="eyebrow">沙箱权限待确认</span>
                  <strong>工具调用需要授权</strong>
                  <small>审批 {capabilityApproval.approvalId}</small>
                </div>
                <p>{capabilityApproval.summary}</p>
                <div className="composer-card-actions">
                  <button
                    type="button"
                    disabled={approveCapability.isPending || rejectCapability.isPending}
                    onClick={() => approveCapability.mutate()}
                  >
                    {approveCapability.isPending ? "允许中..." : "允许一次"}
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={approveCapability.isPending || rejectCapability.isPending}
                    onClick={() => rejectCapability.mutate()}
                  >
                    {rejectCapability.isPending ? "拒绝中..." : "拒绝"}
                  </button>
                </div>
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
                  <>
                    <label className="inline-check">
                      <input
                        type="checkbox"
                        checked={scheduleApproval.confirmed}
                        onChange={(event) =>
                          setScheduleApproval((current) =>
                            current ? { ...current, confirmed: event.target.checked } : current,
                          )
                        }
                      />
                      我确认这是计划任务，不作为普通对话继续
                    </label>
                    <div className="composer-card-actions">
                      <button
                        type="button"
                        disabled={createScheduleFromProposal.isPending || !scheduleApproval.confirmed}
                        onClick={() => createScheduleFromProposal.mutate()}
                      >
                        {createScheduleFromProposal.isPending ? "加入中..." : "确认加入计划"}
                      </button>
                      <button type="button" className="secondary-action" disabled={createScheduleFromProposal.isPending} onClick={cancelScheduleApproval}>
                        取消计划
                      </button>
                    </div>
                  </>
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
            {slashCommandSuggestions.length > 0 ? (
              <div className="slash-command-panel" role="listbox" aria-label="Slash 命令">
                {slashCommandSuggestions.map((command) => (
                  <button
                    key={command.id}
                    type="button"
                    role="option"
                    aria-selected="false"
                    onClick={() => executeSlashCommand(command.id)}
                  >
                    <strong>{command.label}</strong>
                    <small>{command.description}</small>
                  </button>
                ))}
              </div>
            ) : null}
            {(conversationQueue.data ?? []).some((item) =>
              ["queued", "redirecting", "released", "running"].includes(item.status),
            ) ? (
              <section className="conversation-queue" aria-label="排队信息">
                <div className="conversation-queue-heading">
                  <strong>等待执行</strong>
                  <span>
                    {(conversationQueue.data ?? []).filter((item) =>
                      ["queued", "redirecting", "released", "running"].includes(item.status),
                    ).length} 条
                  </span>
                </div>
                {(conversationQueue.data ?? [])
                  .filter((item) => ["queued", "redirecting", "released", "running"].includes(item.status))
                  .map((item) => (
                    <article className="conversation-queue-item" key={item.id}>
                      <div className="conversation-queue-order" aria-label={`排队顺序 ${item.position}`}>
                        {item.position}
                      </div>
                      <div className="conversation-queue-content">
                        {editingQueueItemId === item.id ? (
                          <label className="conversation-queue-editor">
                            <span>编辑排队信息</span>
                            <textarea
                              aria-label="编辑排队信息"
                              value={editingQueueMessage}
                              onChange={(event) => setEditingQueueMessage(event.target.value)}
                              autoFocus
                            />
                          </label>
                        ) : (
                          <p>{item.message}</p>
                        )}
                        <small>
                          {item.status === "redirecting"
                            ? "正在安全停止当前任务"
                            : item.status === "released" || item.status === "running"
                              ? "已进入执行"
                              : `等待当前任务完成${item.attachment_count ? ` · ${item.attachment_count} 个附件` : ""}`}
                        </small>
                      </div>
                      <div className="conversation-queue-actions">
                        {editingQueueItemId === item.id ? (
                          <>
                            <button
                              type="button"
                              disabled={!editingQueueMessage.trim() || editQueueItem.isPending}
                              onClick={() => editQueueItem.mutate({ item, nextMessage: editingQueueMessage.trim() })}
                            >
                              保存
                            </button>
                            <button
                              type="button"
                              className="secondary-action"
                              onClick={() => {
                                setEditingQueueItemId(null);
                                setEditingQueueMessage("");
                              }}
                            >
                              取消
                            </button>
                          </>
                        ) : item.status === "queued" ? (
                          <>
                            <button
                              type="button"
                              className="secondary-action conversation-queue-redirect"
                              disabled={redirectQueueItem.isPending}
                              onClick={() => {
                                if (window.confirm("停止当前任务，并改为优先执行这条排队信息？")) {
                                  redirectQueueItem.mutate(item);
                                }
                              }}
                            >
                              改变方向
                            </button>
                            <button
                              type="button"
                              className="conversation-queue-icon"
                              aria-label="编辑排队信息"
                              title="编辑排队信息"
                              onClick={() => {
                                setEditingQueueItemId(item.id);
                                setEditingQueueMessage(item.message);
                              }}
                            >
                              ✎
                            </button>
                            <button
                              type="button"
                              className="conversation-queue-icon conversation-queue-cancel"
                              aria-label="取消排队"
                              title="取消排队"
                              disabled={cancelQueueItem.isPending}
                              onClick={() => {
                                if (window.confirm("取消这条排队信息？此操作不会停止当前任务。")) {
                                  cancelQueueItem.mutate(item);
                                }
                              }}
                            >
                              ×
                            </button>
                          </>
                        ) : null}
                      </div>
                    </article>
                  ))}
              </section>
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
                  ref={configTriggerRef}
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
                  {[
                    "主 Agent 自动",
                    selectedSandboxLabel,
                    workflowId ? `参考 ${selectedWorkflow?.name ?? workflowId}` : null,
                    referenceConversationId.trim() ? "已引用" : null,
                  ].filter(Boolean).join(" · ")}
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
                  disabled={
                    createRun.isPending ||
                    queueMessage.isPending ||
                    message.trim().length === 0 ||
                    currentConversationArchived
                  }
                  title={canStopLatestRun ? "当前任务完成后执行这条消息" : "发送消息"}
                >
                  {queueMessage.isPending
                    ? "排队中..."
                    : createRun.isPending
                      ? "发送中..."
                      : canStopLatestRun
                        ? "排队"
                        : "发送"}
                </button>
              </div>
            </div>
            {executionBackendFallbackNotice ? <p className="field-help" role="status">{executionBackendFallbackNotice}</p> : null}
            {executionBackendNotice && !executionBackends.isLoading ? (
              <p className="field-help" role="status">{executionBackendNotice}</p>
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
            {queueMessage.isError && (!(queueMessage.error instanceof ApiError) || queueMessage.error.code !== "conversation_not_active") ? (
              <p role="alert">{formatApiError(queueMessage.error, "消息排队失败")}</p>
            ) : null}
            {editQueueItem.isError ? <p role="alert">{formatApiError(editQueueItem.error, "排队信息编辑失败；你的修改仍保留在输入框中")}</p> : null}
            {redirectQueueItem.isError ? <p role="alert">{formatApiError(redirectQueueItem.error, "改变方向失败")}</p> : null}
            {cancelQueueItem.isError ? <p role="alert">{formatApiError(cancelQueueItem.error, "取消排队失败")}</p> : null}
            {stopCurrentRun.isError ? <p role="alert">{formatApiError(stopCurrentRun.error, "停止运行失败")}</p> : null}
          </form>
        </div>
      </div>

      {newProjectOpen ? (
        <NewProjectDialog
          draft={newProjectDraft}
          pending={createProjectWorkspace.isPending}
          error={createProjectWorkspace.isError ? formatApiError(createProjectWorkspace.error, "项目工作区创建失败") : null}
          onChange={(next) => {
            createProjectWorkspace.reset();
            setNewProjectDraft(next);
          }}
          onClose={() => {
            if (!createProjectWorkspace.isPending) setNewProjectOpen(false);
          }}
          onSubmit={() => {
            if (
              !newProjectDraft.label.trim() ||
              workspacePathError(newProjectDraft.projectId) ||
              workspacePathError(newProjectDraft.workspacePath)
            ) return;
            createProjectWorkspace.mutate(newProjectDraft);
          }}
        />
      ) : null}
      {newConversationOpen ? (
        <NewConversationDialog
          draft={newConversationDraft}
          projects={conversationProjects}
          pending={createConversation.isPending}
          error={createConversation.isError ? formatApiError(createConversation.error, "会话创建失败") : null}
          onChange={(next) => {
            createConversation.reset();
            setNewConversationDraft(next);
          }}
          onClose={() => {
            if (!createConversation.isPending) setNewConversationOpen(false);
          }}
          onSubmit={() => {
            if (!newConversationDraft.projectId.trim()) return;
            createConversation.mutate(newConversationDraft);
          }}
        />
      ) : null}
      {renameConversationOpen ? (
        <RenameConversationDialog
          value={renameConversationTitle}
          pending={updateConversation.isPending}
          error={updateConversation.isError ? formatApiError(updateConversation.error, "会话重命名失败") : null}
          onChange={(value) => {
            updateConversation.reset();
            setRenameConversationTitle(value);
          }}
          onClose={() => {
            if (!updateConversation.isPending) setRenameConversationOpen(false);
          }}
          onSubmit={() => {
            const title = renameConversationTitle.trim();
            if (!title || !activeConversationId) return;
            updateConversation.mutate({ conversationId: activeConversationId, title });
          }}
        />
      ) : null}

    </section>
  );
}
