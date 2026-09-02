import { Fragment, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createPortal } from "react-dom";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError, type RunDetail } from "../api/client";
import { ArtifactFileCard, hasArtifactDownload } from "../components/ArtifactFileCard";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MANUAL_RUN_MODES = [
  { value: "direct", label: "直接执行", description: "让主 Agent 或指定角色直接回答。" },
  { value: "dispatch", label: "派单式", description: "拆分任务并分派给多个角色。" },
  { value: "discuss", label: "讨论式", description: "让多个角色先讨论，再形成结论。" },
  { value: "hybrid", label: "混合式", description: "先讨论方案，再分工执行，最后审查。" },
] as const;

type ManualRunMode = (typeof MANUAL_RUN_MODES)[number]["value"];
type RunEvent = RunDetail["events"][number];
type RunArtifact = RunDetail["artifacts"][number];

type ObserverNotice = {
  sequence: number;
  trigger: string;
  action: string;
  severity: string;
  sourceKind: string | null;
  sourceSequence: number | null;
  actor: string | null;
  failureEvents: number | null;
  retryEvents: number | null;
  messageEvents: number | null;
  artifactEvents: number | null;
};

type DetailTimelineItem =
  | { type: "event"; event: RunEvent }
  | { type: "model_delta_group"; events: RunEvent[] };

type DetailProcessCard = {
  id: string;
  label: string;
  title: string;
  detail: string;
  meta: string[];
  rows: DetailProcessRow[];
  createdAt: string | null;
  artifact?: DownloadableArtifact;
  sourceKind?: string;
  sourceStepId?: string | null;
  sourceActor?: string | null;
};

type DetailProcessRow = {
  label: string;
  value: string;
};

type DetailProcessGroup = {
  key: string;
  label: string;
  rows: DetailProcessRow[];
};

type DownloadableArtifact = RunArtifact & {
  download_url: string;
};

type ApprovalState = {
  pending: RunEvent[];
  resolved: RunEvent[];
};

type CapabilityApproval = {
  runId: string;
  approvalId: string;
  version: number;
  summary: string;
};

type ToolLifecycle = {
  key: string;
  toolName: string;
  status: string;
  operationKind: string;
  actor: string | null;
  stepId: string | null;
  sequences: number[];
  argumentBytes: number | null;
  outputBytes: number | null;
  exitCode: number | null;
  failureKind: string | null;
  approvalId: string | null;
  replaySafe: boolean | null;
  artifactId: string | null;
};

type RunExecutionIntent = {
  id: string;
  label: string;
  title: string;
  detail: string;
  meta: string[];
  tone: "pending" | "retry" | "replay" | "repair" | "done";
};

type DetailFailureDiagnostic = {
  id: string;
  label: "工具执行失败" | "模型链路失败" | "运行阶段失败" | "等待人工确认";
  title: string;
  detail: string;
  recommendation: string;
  meta: string[];
  tone: "tool" | "model" | "runtime" | "approval";
};

const OBSERVER_TRIGGER_LABELS: Record<string, string> = {
  model_capacity_pressure: "模型容量拥堵",
  empty_model_response: "模型空响应",
  repeated_failure: "连续失败",
  step_retrying: "正在重试",
  context_compaction_recommended: "建议压缩上下文",
};

const OBSERVER_ACTION_LABELS: Record<string, string> = {
  reschedule_or_reassign_model: "建议改派模型或重新调度",
  retry_fallback_or_reassign_model: "建议重试、切换备用模型或改派",
  pause_and_request_scheduler_review: "建议暂停并等待调度复核",
  preserve_partial_outputs: "保留失败前产物用于复盘",
  watch_retry_budget: "继续观察重试预算",
  compact_context_before_next_model_call: "下次模型调用前压缩上下文",
};

const OBSERVER_SEVERITY_LABELS: Record<string, string> = {
  info: "提示",
  warning: "警告",
  error: "错误",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "已排队",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行异常",
  cancelled: "已取消",
  waiting_approval: "等待确认",
  waiting_user_mode: "等待模式确认",
};

const RUN_MODE_LABELS: Record<string, string> = {
  direct: "直接执行",
  dispatch: "派单式",
  discuss: "讨论式",
  hybrid: "混合式",
  auto: "自动路由",
};

const TOOL_STATUS_LABELS: Record<string, string> = {
  running: "进行中",
  started: "进行中",
  completed: "已完成",
  succeeded: "已完成",
  failed: "异常",
};

const TOOL_OPERATION_LABELS: Record<string, string> = {
  terminal: "终端",
  file_edit: "文件编辑",
  file_read: "文件读取",
  browser: "浏览器",
  generic: "工具",
};

const DETAIL_EVENT_KIND_LABELS: Record<string, string> = {
  "artifact.created": "产物生成",
  "approval.requested": "等待确认",
  "approval.resolved": "确认完成",
  "decision.completed": "决策完成",
  "decision.started": "开始决策",
  "dispatch.completed": "派单完成",
  "dispatch.started": "开始派单",
  "discussion.completed": "讨论完成",
  "discussion.started": "开始讨论",
  "harness.completed": "Harness 完成",
  "harness.failed": "Harness 失败",
  "harness.started": "Harness 启动",
  "message.created": "消息产出",
  "model.completed": "模型完成",
  "model.failed": "模型失败",
  "model.reasoning_delta": "模型思考",
  "model.started": "模型开始",
  "model.text_delta": "模型输出",
  "observer.notice": "调度观察",
  "review.completed": "审查完成",
  "runtime.failed": "运行失败",
  "step.completed": "执行完成",
  "step.failed": "执行失败",
  "step.retrying": "执行重试",
  "step.started": "执行步骤",
  "temporary_agent.proposed": "临时 Agent",
  "tool.completed": "工具完成",
  "tool.failed": "工具失败",
  "tool.requested": "工具请求",
  "tool.started": "工具动作",
};

const DETAIL_EVENT_SCOPE_LABELS: Record<string, string> = {
  artifact: "产物",
  approval: "审批",
  checkpoint: "检查点",
  decision: "决策",
  dispatch: "调度",
  discussion: "讨论",
  harness: "Harness",
  memory: "记忆",
  message: "消息",
  model: "模型",
  observer: "观察",
  repair: "修复",
  review: "审查",
  runtime: "运行",
  step: "步骤",
  temporary_agent: "临时 Agent",
  tool: "工具",
};

const DETAIL_EVENT_ACTION_LABELS: Record<string, string> = {
  accepted: "接受",
  cancelled: "取消",
  completed: "完成",
  created: "生成",
  failed: "失败",
  proposed: "提议",
  requested: "请求",
  resolved: "完成",
  retrying: "重试",
  saved: "保存",
  skipped: "跳过",
  started: "启动",
  updated: "更新",
};

const EXPLICIT_DETAIL_LABELS: Record<string, string> = {
  workflow_id: "工作流",
  workflow_adjustment_policy: "工作流调整",
  selected_agent_ids: "角色池",
  routing_reason: "路由原因",
  conversation_id: "会话",
  direct_model: "直连模型",
  harness_provider: "Harness 服务商",
  harness_model: "Harness 模型",
  harness_logical_model: "逻辑模型",
  harness_requires_approval: "审批策略",
  harness_capabilities: "工程能力",
  harness_policy: "策略原因",
  harness_context: "上下文信号",
  harness_fallbacks: "备选路径",
};

const EXPLICIT_DETAIL_KEYS = Object.keys(EXPLICIT_DETAIL_LABELS);

function payloadString(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function payloadNumber(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function payloadDisplayValue(value: unknown) {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function safeDiagnosticIdentifier(value: unknown, fallback: string) {
  const text = payloadDisplayValue(value);
  if (!text || text.length > 80) return fallback;
  return /^[A-Za-z0-9_.:/@-]+$/.test(text) ? text : fallback;
}

function safeDiagnosticFailureKind(value: unknown) {
  const text = safeDiagnosticIdentifier(value, "");
  return text && /^[A-Za-z0-9_.-]+$/.test(text) ? text : "";
}

function displayRunStatus(status: string) {
  return RUN_STATUS_LABELS[status] ?? status;
}

function displayRunMode(mode: string) {
  return RUN_MODE_LABELS[mode] ?? mode;
}

function runDetailVersion(run: RunDetail) {
  const fallbackVersion = Number(run.explicit_details.version ?? "0");
  if (typeof run.version === "number" && Number.isInteger(run.version) && run.version > 0) return run.version;
  return Number.isInteger(fallbackVersion) && fallbackVersion > 0 ? fallbackVersion : 0;
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

function displayToolStatus(status: string) {
  return TOOL_STATUS_LABELS[status] ?? status;
}

function displayToolOperation(kind: string) {
  return TOOL_OPERATION_LABELS[kind] ?? kind;
}

function displayDetailActor(actor: string | null | undefined) {
  if (!actor) return "";
  if (actor === "main_agent" || actor === "main") return "主 Agent";
  return actor;
}

function collectObserverNotices(events: RunEvent[]): ObserverNotice[] {
  return events.flatMap((event) => {
    if (event.kind !== "observer.notice") return [];
    const trigger = payloadString(event.payload, "trigger");
    const action = payloadString(event.payload, "action");
    const severity = payloadString(event.payload, "severity") ?? "info";
    if (!trigger || !action) return [];
    return [
      {
        sequence: event.sequence,
        trigger,
        action,
        severity,
        sourceKind: payloadString(event.payload, "source_kind"),
        sourceSequence: payloadNumber(event.payload, "source_sequence"),
        actor: event.actor ?? null,
        failureEvents: payloadNumber(event.payload, "failure_events"),
        retryEvents: payloadNumber(event.payload, "retry_events"),
        messageEvents: payloadNumber(event.payload, "message_events"),
        artifactEvents: payloadNumber(event.payload, "artifact_events"),
      },
    ];
  });
}

function observerTriggerLabel(trigger: string) {
  return OBSERVER_TRIGGER_LABELS[trigger] ?? trigger;
}

function observerActionLabel(action: string) {
  return OBSERVER_ACTION_LABELS[action] ?? action;
}

function observerSeverityLabel(severity: string) {
  return OBSERVER_SEVERITY_LABELS[severity] ?? severity;
}

function displayDetailEventKind(kind: string) {
  const explicit = DETAIL_EVENT_KIND_LABELS[kind];
  if (explicit) return explicit;
  const [scope, action] = kind.split(".");
  const scopeLabel = DETAIL_EVENT_SCOPE_LABELS[scope];
  const actionLabel = DETAIL_EVENT_ACTION_LABELS[action];
  if (scopeLabel && actionLabel) return `${scopeLabel}${actionLabel}`;
  if (scopeLabel) return `${scopeLabel}过程`;
  return "过程记录";
}

function isKnownDetailEventKind(kind: string) {
  return Object.prototype.hasOwnProperty.call(DETAIL_EVENT_KIND_LABELS, kind);
}

function observerSourceLabel(kind: string) {
  if (kind.startsWith("step.")) return "执行步骤";
  return displayDetailEventKind(kind);
}

function runEventTimestamp(value: string) {
  if (!value.trim()) return "unknown time";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().replace(".000Z", "Z");
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

function isModelDeltaEvent(event: RunEvent) {
  return event.kind === "model.reasoning_delta" || event.kind === "model.text_delta";
}

function formatPayloadValue(value: unknown) {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : "";
}

function formatDetailValue(value: unknown): string {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value === "string") return value.trim();
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

function conciseProcessText(value: string, fallback: string) {
  const normalized = value
    .replace(/```[\s\S]*?```/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return fallback;
  const sentence = normalized.split(/(?<=[。！？.!?])\s+/)[0]?.trim() || normalized;
  return sentence.length > 38 ? `${sentence.slice(0, 38)}...` : sentence;
}

function isGenericDetailText(value: string | null | undefined) {
  const normalized = value?.replace(/\s+/g, " ").trim();
  if (!normalized) return false;
  return normalized === "artifact.created" || normalized === "message.created" || /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(normalized);
}

function detailPayloadLabel(key: string) {
  const labels: Record<string, string> = {
    artifact_id: "产物 ID",
    artifactId: "产物 ID",
    output: "输出内容",
    result: "得到结果",
    summary: "执行摘要",
    input: "输入内容",
    prompt: "提示词/指令",
    instruction: "下发指令",
    instructions: "下发指令",
    task: "下发任务",
    final_decision: "最终裁决",
    main_agent_judgement: "主 Agent 判断",
    main_agent_judgment: "主 Agent 判断",
    model: "调用模型",
    logical_model: "逻辑模型",
    provider: "服务商",
    status: "状态",
    exit_code: "退出码",
    output_bytes: "输出字节数",
    argument_bytes: "参数字节数",
    operation_kind: "操作类别",
    failure_kind: "失败类型",
    replay_safe: "可回放",
  };
  return labels[key] ?? key.replace(/_/g, " ");
}

function isSensitivePayloadKey(key: string) {
  return /api[_-]?key|secret|token|password|credential/i.test(key);
}

function hasSensitiveNestedValue(value: unknown): boolean {
  if (value === null || typeof value === "undefined") return false;
  if (typeof value === "string") return /api[_-]?key|secret|token|password|credential/i.test(value);
  if (typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((item) => hasSensitiveNestedValue(item));
  return Object.entries(value as Record<string, unknown>).some(
    ([key, item]) => isSensitivePayloadKey(key) || hasSensitiveNestedValue(item),
  );
}

function artifactText(artifact: RunArtifact | null | undefined) {
  const text = artifact?.text?.trim() ?? "";
  return isGenericDetailText(text) ? "" : text;
}

function formatArtifactSize(sizeBytes: number | null | undefined) {
  if (typeof sizeBytes !== "number" || !Number.isFinite(sizeBytes) || sizeBytes < 0) return "";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = sizeBytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function artifactRows(artifact: RunArtifact | null | undefined): DetailProcessRow[] {
  if (!artifact) return [];
  return [
    artifact.title ? { label: "产物标题", value: artifact.title } : null,
    artifact.kind ? { label: "产物类型", value: artifact.kind } : null,
    artifact.filename ? { label: "文件名", value: artifact.filename } : null,
    artifact.mime_type ? { label: "文件类型", value: artifact.mime_type } : null,
    formatArtifactSize(artifact.size_bytes) ? { label: "文件大小", value: formatArtifactSize(artifact.size_bytes) } : null,
    artifact.sha256 ? { label: "SHA-256", value: artifact.sha256 } : null,
    artifactText(artifact) ? { label: "输出内容", value: artifactText(artifact) } : null,
  ].filter((row): row is DetailProcessRow => Boolean(row));
}

function downloadableArtifact(artifact: RunArtifact | null | undefined): DownloadableArtifact | undefined {
  return hasArtifactDownload(artifact) ? artifact : undefined;
}

function dedupeArtifactDownloads(artifacts: RunArtifact[]) {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    if (!hasArtifactDownload(artifact)) return true;
    const key = artifact.download_url.trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function artifactForEvent(event: RunEvent, artifacts: RunArtifact[]) {
  if (event.artifact) return event.artifact;
  const artifactId =
    formatDetailValue(event.payload.artifact_id) ||
    formatDetailValue(event.payload.artifactId) ||
    formatDetailValue(event.payload.id);
  return artifactId ? artifacts.find((artifact) => artifact.id === artifactId) ?? null : null;
}

function eventDetailRows(event: RunEvent, artifact: RunArtifact | null | undefined): DetailProcessRow[] {
  const rows: DetailProcessRow[] = [
    { label: "时间", value: runEventTimestamp(event.created_at) },
    { label: "事件类型", value: displayDetailEventKind(event.kind) },
  ];
  if (!isKnownDetailEventKind(event.kind)) rows.push({ label: "原始事件类型", value: event.kind });
  if (event.actor) rows.push({ label: "执行者", value: displayDetailActor(event.actor) });
  if (event.participants.length > 0) rows.push({ label: "参与者", value: event.participants.join("、") });
  if (event.step_id) rows.push({ label: "步骤", value: event.step_id });
  if (event.tool_name) rows.push({ label: "工具", value: event.tool_name });
  if (event.tool_call_id) rows.push({ label: "调用 ID", value: event.tool_call_id });
  if (event.approval_id) rows.push({ label: "审批 ID", value: event.approval_id });
  if (safeDiagnosticIdentifier(event.action, "")) rows.push({ label: "动作", value: safeDiagnosticIdentifier(event.action, "") });
  if (safeDiagnosticIdentifier(event.decision, "")) rows.push({ label: "决策", value: safeDiagnosticIdentifier(event.decision, "") });
  if (event.summary?.trim()) rows.push({ label: "安全摘要", value: event.summary.trim() });
  if (!isGenericDetailText(event.message)) rows.push({ label: "事件内容", value: event.message.trim() });
  Object.entries(event.payload).forEach(([key, value]) => {
    if (isSensitivePayloadKey(key) || hasSensitiveNestedValue(value)) return;
    const formatted = formatDetailValue(value);
    if (formatted) rows.push({ label: detailPayloadLabel(key), value: formatted });
  });
  return [...rows, ...artifactRows(artifact)];
}

function numericPayloadValue(event: RunEvent, key: string) {
  const value = event.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function modelDeltaGroupKey(event: RunEvent) {
  return [
    event.kind,
    event.actor ?? "",
    event.step_id ?? "",
    formatPayloadValue(event.payload.phase),
    formatPayloadValue(event.payload.delta_kind),
  ].join("|");
}

function modelDeltaEventsCanMerge(left: RunEvent, right: RunEvent) {
  return modelDeltaGroupKey(left) === modelDeltaGroupKey(right);
}

function modelDeltaActivityLabel(event: RunEvent) {
  return event.kind === "model.reasoning_delta" ? "模型正在分析" : "模型正在生成";
}

function modelDeltaSummary(events: RunEvent[]) {
  const lastEvent = events.at(-1);
  if (!lastEvent) return "模型流式进度已记录";
  const totalBytes = events.reduce((total, event) => total + numericPayloadValue(event, "text_bytes"), 0);
  const duration = durationBetween(events[0]?.created_at, lastEvent.created_at);
  const parts = [`${events.length} 个分片`];
  if (totalBytes > 0) parts.push(`${totalBytes} bytes`);
  if (duration) parts.push(duration);
  return `${modelDeltaActivityLabel(lastEvent)}，${parts.join("，")}`;
}

function detailTimelineItems(events: RunEvent[]): DetailTimelineItem[] {
  const items: DetailTimelineItem[] = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (!isModelDeltaEvent(event)) {
      items.push({ type: "event", event });
      continue;
    }
    const group = [event];
    while (index + 1 < events.length) {
      const next = events[index + 1];
      if (!isModelDeltaEvent(next) || !modelDeltaEventsCanMerge(group.at(-1) ?? event, next)) break;
      group.push(next);
      index += 1;
    }
    items.push(group.length > 1 ? { type: "model_delta_group", events: group } : { type: "event", event });
  }
  return items;
}

function detailProcessLabel(event: RunEvent) {
  if (event.kind.startsWith("tool.")) return "工具动作";
  if (event.kind.startsWith("model.")) return "模型过程";
  if (event.kind.startsWith("harness.")) return "Harness";
  if (event.kind.startsWith("discussion.")) return "讨论过程";
  if (event.kind.startsWith("decision.")) return "决策过程";
  if (event.kind.startsWith("dispatch.")) return "调度过程";
  if (event.kind.startsWith("approval.")) return "审批意图";
  if (event.kind === "artifact.created" || event.kind === "message.created" || event.kind.startsWith("step.")) {
    return "执行过程";
  }
  return displayDetailEventKind(event.kind);
}

function detailProcessCards(items: DetailTimelineItem[], artifacts: RunArtifact[]): DetailProcessCard[] {
  return items.flatMap((item) => {
    if (item.type === "model_delta_group") {
      const firstEvent = item.events[0];
      const lastEvent = item.events.at(-1) ?? firstEvent;
      const sequence = `事件 #${firstEvent.sequence}${lastEvent.sequence !== firstEvent.sequence ? `-#${lastEvent.sequence}` : ""}`;
      const summary = modelDeltaSummary(item.events);
      return [
        {
          id: `model-delta-${firstEvent.sequence}-${lastEvent.sequence}`,
          label: "模型过程",
          title: conciseProcessText(summary, "模型流式进度已记录"),
          detail: summary,
          meta: [sequence, displayDetailActor(lastEvent.actor), lastEvent.step_id ? `步骤 ${lastEvent.step_id}` : ""].filter(Boolean),
          rows: [
            { label: "时间", value: runEventTimestamp(lastEvent.created_at) },
            { label: "事件范围", value: sequence },
            { label: "活动", value: summary },
            displayDetailActor(lastEvent.actor) ? { label: "执行者", value: displayDetailActor(lastEvent.actor) } : null,
            lastEvent.step_id ? { label: "步骤", value: lastEvent.step_id } : null,
          ].filter((row): row is DetailProcessRow => Boolean(row)),
          createdAt: lastEvent.created_at,
          sourceKind: lastEvent.kind,
          sourceStepId: lastEvent.step_id,
          sourceActor: lastEvent.actor,
        },
      ];
    }
    const { event } = item;
    const rawKindMeta = isKnownDetailEventKind(event.kind) ? "" : `事件类型 ${event.kind}`;
    const artifact = artifactForEvent(event, artifacts);
    const summary = safeDetailEventSummary(event);
    return [
      {
        id: `event-${event.sequence}`,
        label: detailProcessLabel(event),
        title: conciseProcessText(summary, displayDetailEventKind(event.kind)),
        detail: summary,
        meta: [
          displayDetailEventKind(event.kind),
          rawKindMeta,
          `事件 #${event.sequence}`,
          displayDetailActor(event.actor),
          event.step_id ? `步骤 ${event.step_id}` : "",
          event.tool_name && event.kind.startsWith("tool.") ? `工具 ${event.tool_name}` : "",
        ].filter(Boolean),
        rows: eventDetailRows(event, artifact),
        createdAt: event.created_at,
        artifact: downloadableArtifact(artifact),
        sourceKind: event.kind,
        sourceStepId: event.step_id,
        sourceActor: event.actor,
      },
    ];
  });
}

function refreshedDetailProcessCard(currentCard: DetailProcessCard, candidates: DetailProcessCard[]) {
  const matchedByStableSource =
    currentCard.sourceKind || currentCard.sourceStepId || currentCard.sourceActor
      ? candidates.filter(
          (card) =>
            card.sourceKind === currentCard.sourceKind &&
            card.sourceStepId === currentCard.sourceStepId &&
            card.sourceActor === currentCard.sourceActor,
        )
      : [];
  return matchedByStableSource.at(-1) ?? candidates.find((card) => card.id === currentCard.id) ?? currentCard;
}

function isGenericEventMessage(event: RunEvent) {
  return event.message === event.kind || /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message);
}

function safeDetailEventSummary(event: RunEvent) {
  if (event.summary?.trim()) return conciseProcessText(event.summary, displayDetailEventKind(event.kind));
  if (event.kind === "artifact.created") {
    const artifactTitle = event.artifact?.title?.trim();
    if (artifactTitle) return `生成了${artifactTitle}`;
    const payloadTitle = formatDetailValue(event.payload.title);
    if (payloadTitle) return `生成了${payloadTitle}`;
    return "生成了一个运行产物";
  }
  if (event.kind === "message.created") {
    const payloadSummary = formatDetailValue(event.payload.summary) || formatDetailValue(event.payload.title);
    return payloadSummary ? conciseProcessText(payloadSummary, "生成了阶段消息") : "生成了阶段消息";
  }
  if (event.kind === "model.reasoning_delta") return "模型正在分析";
  if (event.kind === "model.text_delta") return "模型正在生成";
  if (event.kind.startsWith("tool.")) return `${event.tool_name ?? "工具"} ${event.kind.replace("tool.", "")}`;
  if (event.kind.startsWith("approval.")) {
    return safeDiagnosticIdentifier(event.action, "") || safeDiagnosticIdentifier(event.decision, "") || "等待确认";
  }
  if (isGenericEventMessage(event)) return "事件已记录";
  return displayDetailEventKind(event.kind);
}

function toolLifecycleKey(event: RunEvent) {
  return (
    event.tool_call_id ??
    payloadString(event.payload, "tool_call_id") ??
    payloadString(event.payload, "id") ??
    `${event.tool_name ?? "tool"}-${event.sequence}`
  );
}

function collectToolLifecycles(events: RunEvent[]): ToolLifecycle[] {
  const lifecycles = new Map<string, ToolLifecycle>();
  for (const event of events) {
    if (!event.kind.startsWith("tool.")) continue;
    const key = toolLifecycleKey(event);
    const existing = lifecycles.get(key);
    const status = payloadString(event.payload, "status") ?? event.kind.replace("tool.", "");
    const operationKind = payloadString(event.payload, "operation_kind") ?? "generic";
    const next: ToolLifecycle = existing ?? {
      key,
      toolName: event.tool_name ?? payloadString(event.payload, "name") ?? "tool",
      status,
      operationKind,
      actor: event.actor ?? null,
      stepId: event.step_id ?? null,
      sequences: [],
      argumentBytes: null,
      outputBytes: null,
      exitCode: null,
      failureKind: null,
      approvalId: null,
      replaySafe: null,
      artifactId: null,
    };
    next.status = status;
    next.operationKind = operationKind;
    next.actor = event.actor ?? next.actor;
    next.stepId = event.step_id ?? next.stepId;
    next.sequences = [...next.sequences, event.sequence];
    next.argumentBytes = payloadNumber(event.payload, "argument_bytes") ?? next.argumentBytes;
    next.outputBytes = payloadNumber(event.payload, "output_bytes") ?? next.outputBytes;
    next.exitCode = payloadNumber(event.payload, "exit_code") ?? next.exitCode;
    next.failureKind = payloadString(event.payload, "failure_kind") ?? next.failureKind;
    next.approvalId = payloadString(event.payload, "approval_id") ?? next.approvalId;
    next.replaySafe = typeof event.payload.replay_safe === "boolean" ? event.payload.replay_safe : next.replaySafe;
    next.artifactId = payloadString(event.payload, "artifact_id") ?? next.artifactId;
    lifecycles.set(key, next);
  }
  return Array.from(lifecycles.values());
}

function toolLifecycleFromApi(detail: RunDetail): ToolLifecycle[] {
  if (detail.tool_lifecycle.length === 0) return collectToolLifecycles(detail.events);
  return detail.tool_lifecycle.map((item) => ({
    key: item.tool_call_id,
    toolName: item.tool_name,
    status: item.status,
    operationKind: item.operation_kind,
    actor: item.actor ?? null,
    stepId: item.step_id ?? null,
    sequences: item.sequences,
    argumentBytes: item.argument_bytes ?? null,
    outputBytes: item.output_bytes ?? null,
    exitCode: item.exit_code ?? null,
    failureKind: item.failure_kind ?? null,
    approvalId: item.approval_id ?? null,
    replaySafe: item.replay_safe ?? null,
    artifactId: item.artifact_id ?? null,
  }));
}

function detailPosture(detail: RunDetail) {
  if (detail.failure_diagnostics.length > 0 || detail.events.some((event) => event.kind.endsWith(".failed"))) {
    return "执行异常";
  }
  return displayRunStatus(detail.status);
}

function replaySafetyLabel(value: unknown) {
  if (value === false || value === "false") return "不可回放";
  if (value === true || value === "true") return "可回放";
  return "";
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

function safeIntentValue(event: RunEvent, keys: string[]) {
  for (const key of keys) {
    const value = event.payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    if (typeof value === "boolean") return value ? "true" : "false";
  }
  return "";
}

function safeIntentIdentifier(event: RunEvent, keys: string[], fallback = "") {
  for (const key of keys) {
    const text = safeDiagnosticIdentifier(event.payload[key], "");
    if (text) return text;
  }
  return fallback;
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
  return safeIntentIdentifier(event, ["status"], "等待执行");
}

function isRepairIntentEvent(event: RunEvent) {
  const kind = event.kind.toLowerCase();
  if (kind.includes("repair") || kind.includes("remediation")) return true;
  return ["repair_action", "repair_kind", "self_repair", "remediation_action"].some((key) =>
    hasPositiveIntentSignal(event.payload[key]),
  );
}

function eventFailureStatus(event: RunEvent) {
  const candidates = [payloadDisplayValue(event.payload.status_code), payloadDisplayValue(event.payload.http_status), event.message];
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
    safeDiagnosticIdentifier(event.payload.logical_model, "") ||
    safeDiagnosticIdentifier(event.payload.model, "") ||
    safeDiagnosticIdentifier(event.payload.upstream_model, "")
  );
}

function isModelFailureEvent(event: RunEvent) {
  if (event.kind.startsWith("model.") && event.kind.endsWith(".failed")) return true;
  const text = [
    event.message,
    payloadDisplayValue(event.payload.failure_kind),
    payloadDisplayValue(event.payload.provider),
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

function isActionEventForDiagnostics(event: RunEvent) {
  if (["model.reasoning_delta", "model.text_delta", "checkpoint.saved", "cost.recorded"].includes(event.kind)) return false;
  return true;
}

function isWrappedToolFailureEvent(event: RunEvent, events: RunEvent[]) {
  if (event.kind !== "runtime.failed" && event.kind !== "step.failed") return false;
  if (isModelFailureEvent(event)) return false;
  return events.some((candidate) => {
    if (candidate.kind !== "tool.failed" || candidate.sequence > event.sequence) return false;
    if (candidate.step_id && event.step_id && candidate.step_id === event.step_id) return true;
    return !events.some(
      (between) =>
        between.sequence > candidate.sequence &&
        between.sequence < event.sequence &&
        isActionEventForDiagnostics(between),
    );
  });
}

function diagnosticLabel(category: string): DetailFailureDiagnostic["label"] {
  if (category === "tool") return "工具执行失败";
  if (category === "model") return "模型链路失败";
  if (category === "approval") return "等待人工确认";
  return "运行阶段失败";
}

function diagnosticTone(category: string): DetailFailureDiagnostic["tone"] {
  if (category === "tool") return "tool";
  if (category === "model") return "model";
  if (category === "approval") return "approval";
  return "runtime";
}

function diagnosticRecommendation(category: string, fallback: string) {
  if (fallback.trim()) return fallback;
  if (category === "tool") return "检查工具权限、参数和运行环境，再决定是否重试或改派。";
  if (category === "model") return "检查模型配置、API Key、上游状态码和限流，再重试或切换模型。";
  if (category === "approval") return "处理审批或拒绝高风险动作，再继续执行。";
  return "按失败阶段查看上下文，优先保留已有产物并缩小重试范围。";
}

function pushUniqueDiagnostic(diagnostics: DetailFailureDiagnostic[], diagnostic: DetailFailureDiagnostic) {
  const key = `${diagnostic.label}:${diagnostic.title}:${diagnostic.detail}:${diagnostic.meta.join("|")}`;
  if (diagnostics.some((existing) => `${existing.label}:${existing.title}:${existing.detail}:${existing.meta.join("|")}` === key)) return;
  diagnostics.push(diagnostic);
}

function detailDiagnosticFromApi(
  runId: string,
  diagnostic: RunDetail["failure_diagnostics"][number],
  index: number,
): DetailFailureDiagnostic {
  const label = diagnosticLabel(diagnostic.category);
  const actor = displayDetailActor(diagnostic.actor);
  const statusCode = diagnostic.status_code ? `status=${diagnostic.status_code}` : "";
  const detail = [diagnostic.failure_kind, statusCode, detailDiagnosticReason(diagnostic)]
    .filter(Boolean)
    .filter((value, position, list) => list.indexOf(value) === position)
    .join("；");
  return {
    id: `${runId}-api-diagnostic-${diagnostic.sequence}-${index}`,
    label,
    title: diagnostic.tool_name || diagnostic.logical_model || diagnostic.action || actor || label,
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

function detailDiagnosticReason(diagnostic: RunDetail["failure_diagnostics"][number]) {
  if (diagnostic.error_code === "model.empty_response" || diagnostic.error_category === "empty_response") {
    return "模型返回了空内容";
  }
  return diagnostic.reason;
}

function pendingApprovalDiagnostics(detail: RunDetail): DetailFailureDiagnostic[] {
  return approvalStateFromEvents(detail.events).pending.map((event) => ({
    id: `${detail.id}-diagnostic-approval-${event.approval_id ?? event.sequence}`,
    label: "等待人工确认",
    title: safeDiagnosticIdentifier(event.action, "需要确认"),
    detail:
      safeDiagnosticIdentifier(event.action, "") ||
      safeDiagnosticIdentifier(event.payload.decision, "") ||
      safeDiagnosticIdentifier(event.payload.status, "") ||
      "需要确认后继续",
    recommendation: "处理审批或拒绝高风险动作，再继续执行。",
    meta: [
      event.approval_id ? `审批 ${event.approval_id}` : "",
      displayDetailActor(event.actor),
      replaySafetyLabel(event.payload.replay_safe),
    ].filter(Boolean),
    tone: "approval",
  }));
}

function failureDiagnosticsForDetail(detail: RunDetail): DetailFailureDiagnostic[] {
  if (detail.failure_diagnostics.length > 0) {
    return detail.failure_diagnostics.map((diagnostic, index) => detailDiagnosticFromApi(detail.id, diagnostic, index));
  }

  const diagnostics: DetailFailureDiagnostic[] = [];
  detail.events.forEach((event) => {
    if (event.kind === "tool.failed") {
      const toolName = safeDiagnosticIdentifier(event.tool_name, safeDiagnosticIdentifier(event.payload.name, "工具"));
      const failureKind = safeDiagnosticFailureKind(event.payload.failure_kind);
      const exitCode = payloadDisplayValue(event.payload.exit_code);
      const outputBytes = payloadDisplayValue(event.payload.output_bytes);
      pushUniqueDiagnostic(diagnostics, {
        id: `${detail.id}-diagnostic-tool-${toolLifecycleKey(event)}`,
        label: "工具执行失败",
        title: toolName,
        detail:
          [
            failureKind ? `失败类型 ${failureKind}` : "工具调用未完成",
            exitCode ? `退出码 ${exitCode}` : "",
            outputBytes ? `输出 ${outputBytes} 字节` : "",
          ]
            .filter(Boolean)
            .join("；") || "工具调用未完成",
        recommendation: "检查工具权限、参数和运行环境，再决定是否重试或改派。",
        meta: [displayDetailActor(event.actor), event.step_id ? `步骤 ${event.step_id}` : "", `#${event.sequence}`].filter(Boolean),
        tone: "tool",
      });
      return;
    }

    if (!["runtime.failed", "step.failed", "model.failed"].includes(event.kind) || isWrappedToolFailureEvent(event, detail.events)) return;

    const actor = displayDetailActor(event.actor) || "运行时";
    const status = eventFailureStatus(event);
    const model = eventModelName(event);
    const failureKind = safeDiagnosticFailureKind(event.payload.failure_kind);
    const label = isModelFailureEvent(event) ? "模型链路失败" : "运行阶段失败";
    pushUniqueDiagnostic(diagnostics, {
      id: `${detail.id}-diagnostic-${event.sequence}`,
      label,
      title: label === "模型链路失败" ? model || "模型链路" : actor,
      detail:
        [failureKind, status, model && label !== "模型链路失败" ? model : ""].filter(Boolean).join("；") ||
        (label === "模型链路失败" ? "模型调用失败" : "运行失败，已记录安全摘要"),
      recommendation:
        label === "模型链路失败"
          ? "检查模型配置、API Key、上游状态码和限流，再重试或切换模型。"
          : "按失败阶段查看上下文，优先保留已有产物并缩小重试范围。",
      meta: [actor, event.step_id ? `步骤 ${event.step_id}` : "", `#${event.sequence}`].filter(Boolean),
      tone: label === "模型链路失败" ? "model" : "runtime",
    });
  });

  pendingApprovalDiagnostics(detail).forEach((diagnostic) => pushUniqueDiagnostic(diagnostics, diagnostic));
  return diagnostics;
}

function approvalStateFromEvents(events: RunEvent[]): ApprovalState {
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

function pushUniqueIntent(intents: RunExecutionIntent[], intent: RunExecutionIntent) {
  const key = `${intent.label}:${intent.title}:${intent.detail}:${intent.meta.join("|")}`;
  if (intents.some((existing) => `${existing.label}:${existing.title}:${existing.detail}:${existing.meta.join("|")}` === key)) return;
  intents.push(intent);
}

function executionIntentsForDetail(detail: RunDetail): RunExecutionIntent[] {
  const intents: RunExecutionIntent[] = [];
  const toolGroups = new Map<string, RunEvent[]>();
  detail.events.forEach((event) => {
    if (!event.kind.startsWith("tool.")) return;
    const key = toolLifecycleKey(event);
    toolGroups.set(key, [...(toolGroups.get(key) ?? []), event]);
  });

  toolGroups.forEach((events, key) => {
    const finalEvent = events.at(-1);
    if (!finalEvent) return;
    const replaySafe = events
      .map((event) => event.payload.replay_safe)
      .find((value) => value !== null && typeof value !== "undefined");
    const replayLabel = replaySafetyLabel(replaySafe);
    if (!replayLabel) return;
    const operationKind = payloadString(finalEvent.payload, "operation_kind") ?? "generic";
    pushUniqueIntent(intents, {
      id: `${detail.id}-intent-replay-${key}`,
      label: "回放意图",
      title: replayLabel,
      detail: safeDiagnosticIdentifier(finalEvent.tool_name, safeDiagnosticIdentifier(finalEvent.payload.name, "工具调用")),
      meta: [displayToolOperation(operationKind), displayToolStatus(payloadString(finalEvent.payload, "status") ?? finalEvent.kind.replace("tool.", ""))],
      tone: replayLabel === "不可回放" ? "replay" : "done",
    });
  });

  const approvalState = approvalStateFromEvents(detail.events);
  approvalState.pending.forEach((event) => {
    pushUniqueIntent(intents, {
      id: `${detail.id}-intent-approval-${event.approval_id ?? event.sequence}`,
      label: "审批意图",
      title: "等待确认",
      detail:
        safeDiagnosticIdentifier(event.action, "") ||
        safeIntentIdentifier(event, ["decision", "status"]) ||
        "需要确认后继续",
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
      detail:
        safeDiagnosticIdentifier(event.decision, "") ||
        safeIntentIdentifier(event, ["decision", "status"]) ||
        "已处理",
      meta: [event.approval_id ? `审批 ${event.approval_id}` : ""].filter(Boolean),
      tone: "done",
    });
  });

  detail.events.forEach((event) => {
    if (event.kind.startsWith("approval.")) return;
    if (event.kind === "step.retrying") {
      const attempt = safeIntentValue(event, ["attempt"]);
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-retry-${event.step_id ?? event.sequence}`,
        label: "重试意图",
        title: "准备重试",
        detail:
          safeDiagnosticIdentifier(event.action, "") ||
          safeIntentIdentifier(event, ["failure_kind", "status"]) ||
          "失败后重试",
        meta: [
          attempt ? `第 ${attempt} 次` : "",
          safeIntentIdentifier(event, ["failure_kind", "status"]),
          replaySafetyLabel(event.payload.replay_safe),
        ].filter(Boolean),
        tone: "retry",
      });
      return;
    }
    if (isRepairIntentEvent(event)) {
      const repairAction =
        safeIntentIdentifier(event, ["repair_action", "repair_kind", "remediation_action"]) ||
        safeDiagnosticIdentifier(event.action, "") ||
        "修复方案";
      const eventAction = safeDiagnosticIdentifier(event.action, "");
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-repair-${event.step_id ?? event.sequence}`,
        label: "修复意图",
        title: repairAction,
        detail: eventAction && eventAction !== repairAction ? eventAction : repairStatusLabel(event),
        meta: [
          repairAttemptLabel(event),
          isPayloadFlagTrue(event.payload.requires_approval) ? "需要确认" : "",
          safeIntentIdentifier(event, ["failure_kind"]),
          replaySafetyLabel(event.payload.replay_safe),
        ].filter(Boolean),
        tone: "repair",
      });
    }
  });
  return intents;
}

function explicitDetailRows(details: Record<string, string>) {
  return EXPLICIT_DETAIL_KEYS.flatMap((key) => {
    const value = details[key];
    if (!value?.trim()) return [];
    return [{ key, label: EXPLICIT_DETAIL_LABELS[key], value }];
  });
}

const DETAIL_PROCESS_GROUPS: Array<{
  key: string;
  label: string;
  match: (row: DetailProcessRow) => boolean;
}> = [
  {
    key: "conclusion",
    label: "结论",
    match: (row) => /结论|纪要|共识|得到结果|执行摘要|安全摘要/.test(row.label),
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
    match: (row) => /执行者|参与者|模型|服务商|能力|步骤|工具|事件|时间|耗时|字节|字段|分片|状态流|参数|类型|ID/.test(row.label),
  },
];

function detailProcessGroups(rows: DetailProcessRow[]): DetailProcessGroup[] {
  const groups = new Map<string, DetailProcessGroup>();
  DETAIL_PROCESS_GROUPS.forEach((group) => groups.set(group.key, { key: group.key, label: group.label, rows: [] }));
  groups.set("activity", { key: "activity", label: "活动", rows: [] });
  rows.forEach((row) => {
    const group = DETAIL_PROCESS_GROUPS.find((candidate) => candidate.match(row));
    groups.get(group?.key ?? "activity")?.rows.push(row);
  });
  return [...groups.values()].filter((group) => group.rows.length > 0);
}

function detailProcessGroupSummary(group: DetailProcessGroup) {
  const first = group.rows.find((row) => row.value.trim().length > 0);
  return conciseProcessText(first?.value ?? "", `${group.rows.length} 项摘要`);
}

function DetailProcessCards({ card }: { card: DetailProcessCard }) {
  const [openGroupKey, setOpenGroupKey] = useState<string | null>(null);
  const groups = detailProcessGroups(card.rows);
  const openGroup = groups.find((group) => group.key === openGroupKey) ?? null;
  useEffect(() => {
    if (!openGroup) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenGroupKey(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openGroup]);
  if (groups.length === 0) return null;
  return (
    <>
      <div className="process-detail-card-grid" role="group" aria-label="运行详情摘要">
        {groups.map((group) => (
          <button
            key={`${card.id}-${group.key}`}
            type="button"
            className={`process-detail-card process-detail-card-${group.key}`}
            onClick={() => setOpenGroupKey(group.key)}
            aria-label={`${group.label}：${detailProcessGroupSummary(group)}`}
          >
            <span>{group.label}</span>
            <small>{group.rows.length} 项</small>
            <strong>{detailProcessGroupSummary(group)}</strong>
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
                <span className="eyebrow">{card.label}</span>
                <h4>{openGroup.label}</h4>
              </div>
              <button type="button" className="secondary-action" onClick={() => setOpenGroupKey(null)}>
                关闭
              </button>
            </div>
            <dl>
              {openGroup.rows.map((row, index) => (
                <Fragment key={`${card.id}-${openGroup.key}-${row.label}-${index}`}>
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

function DetailProcessDrawer({ card, dialogId, onClose }: { card: DetailProcessCard; dialogId: string; onClose: () => void }) {
  return createPortal(
    <div className="process-drawer-backdrop" role="presentation" onClick={onClose}>
      <section
        id={dialogId}
        className="process-drawer"
        role="dialog"
        aria-label="Agent 动作详情"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="process-drawer-handle" aria-hidden="true" />
        <div className="process-drawer-header">
          <div>
            <span className="eyebrow">{card.label}</span>
            <h3>{card.title}</h3>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="run-process-detail">
          <article>
            <p>{card.detail}</p>
            {card.artifact ? (
              <div className="artifact-download-list" aria-label="中间产物">
                <ArtifactFileCard artifact={card.artifact} compact />
              </div>
            ) : null}
            <DetailProcessCards card={card} />
            {card.createdAt ? <small>{runEventTimestamp(card.createdAt)}</small> : null}
          </article>
        </div>
      </section>
    </div>,
    document.body,
  );
}

function DetailProcessSummary({ cards }: { cards: DetailProcessCard[] }) {
  const [selectedCard, setSelectedCard] = useState<DetailProcessCard | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const selected = selectedCard ? refreshedDetailProcessCard(selectedCard, cards) : null;
  const drawerOpen = Boolean(selectedCard);
  useEffect(() => {
    if (!drawerOpen) return undefined;
    previouslyFocused.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousBodyOverflow = document.body.style.overflow;
    const previousBodyTouchAction = document.body.style.touchAction;
    const previousDocumentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.body.style.touchAction = "none";
    document.documentElement.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectedCard(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousBodyOverflow || "";
      document.body.style.touchAction = previousBodyTouchAction || "";
      document.documentElement.style.overflow = previousDocumentOverflow || "";
      previouslyFocused.current?.focus();
    };
  }, [drawerOpen]);
  if (cards.length === 0) return null;
  return (
    <section className="run-detail-process-summary" aria-label="Agent 集群动作">
      <div className="run-failure-diagnostics-header">
        <span>Agent process</span>
        <strong>Agent 集群动作</strong>
        <small>{cards.length} 条</small>
      </div>
      <div className="agent-cluster-actions">
        {cards.map((card) => (
          <button
            key={card.id}
            type="button"
            className="run-process-toggle process-intermediate-card"
            aria-controls={`run-detail-process-${card.id}`}
            aria-expanded={selected?.id === card.id}
            onClick={() => setSelectedCard(card)}
          >
            <span aria-hidden="true">›</span>
            <small className="process-card-badge">{card.label}</small>
            <strong>{card.title}</strong>
            {card.artifact?.filename ? <small>{card.artifact.filename}</small> : null}
          </button>
        ))}
      </div>
      {selected ? (
        <DetailProcessDrawer
          card={selected}
          dialogId={`run-detail-process-${selected.id}`}
          onClose={() => setSelectedCard(null)}
        />
      ) : null}
    </section>
  );
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: runId.length > 0,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      return !TERMINAL_STATUSES.has(data.status) ? 1000 : 5000;
    },
    refetchIntervalInBackground: true,
  });
  const control = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel") => {
      if (action === "pause") return api.pauseRun(runId);
      if (action === "resume") return api.resumeRun(runId);
      return api.cancelRun(runId);
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["run", runId], updated);
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const chooseMode = useMutation({
    mutationFn: (mode: ManualRunMode) => {
      if (!run.data?.decision_token) throw new Error("mode decision token is unavailable");
      const fallbackVersion = Number(run.data.explicit_details.version ?? "0");
      const version =
        typeof run.data.version === "number" && Number.isInteger(run.data.version) && run.data.version > 0
          ? run.data.version
          : Number.isInteger(fallbackVersion) && fallbackVersion > 0
            ? fallbackVersion
            : 0;
      return api.chooseMode(runId, {
        mode,
        decision_token: run.data.decision_token,
        version,
      });
    },
    onSuccess: async (updated) => {
      void updated;
      await queryClient.invalidateQueries({ queryKey: ["run", runId] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const approveCapability = useMutation({
    mutationFn: (approval: CapabilityApproval) =>
      api.approveCapability(approval.runId, {
        approval_id: approval.approvalId,
        version: approval.version,
      }),
    onSuccess: async (updated) => {
      void updated;
      await queryClient.invalidateQueries({ queryKey: ["run", runId] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const rejectCapability = useMutation({
    mutationFn: (approval: CapabilityApproval) =>
      api.rejectCapability(approval.runId, {
        approval_id: approval.approvalId,
        version: approval.version,
      }),
    onSuccess: async (updated) => {
      void updated;
      await queryClient.invalidateQueries({ queryKey: ["run", runId] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  if (run.isLoading) return <p>正在加载运行详情...</p>;
  if (run.isError || !run.data) {
    return <p role="alert">{formatApiError(run.error, "运行详情加载失败")}</p>;
  }

  const canPause = ["queued", "running"].includes(run.data.status);
  const canResume = run.data.status === "paused";
  const canCancel = !TERMINAL_STATUSES.has(run.data.status);
  const isWaitingForMode = run.data.status === "waiting_user_mode" && Boolean(run.data.decision_token);
  const capabilityApproval = capabilityApprovalFromRunDetail(run.data);
  const observerNotices = collectObserverNotices(run.data.events);
  const timelineItems = detailTimelineItems(run.data.events);
  const processCards = detailProcessCards(timelineItems, run.data.artifacts);
  const toolLifecycles = toolLifecycleFromApi(run.data);
  const posture = detailPosture(run.data);
  const explicitRows = explicitDetailRows(run.data.explicit_details);
  const executionIntents = executionIntentsForDetail(run.data);
  const failureDiagnostics = failureDiagnosticsForDetail(run.data);

  return (
    <section>
      <p className="eyebrow">Run detail</p>
      <h2>运行详情</h2>
      <p>
        <Link to="/">返回对话任务</Link>
      </p>

      <div className="detail-grid">
        <article>
          <span className="eyebrow">状态</span>
          <h3>{displayRunStatus(run.data.status)}</h3>
        </article>
        <article>
          <span className="eyebrow">模式</span>
          <h3>{displayRunMode(run.data.mode)}</h3>
        </article>
        <article>
          <span className="eyebrow">排队等待</span>
          <h3>{run.data.queue_wait_ms} ms</h3>
        </article>
        <article>
          <span className="eyebrow">成本</span>
          <h3>${run.data.cost_usd}</h3>
        </article>
      </div>

      <div className="run-process-posture" role="status" aria-label={`任务态势，${posture}`}>
        <div>
          <span>运行排障摘要</span>
          <strong>任务态势</strong>
          <small>{posture}</small>
        </div>
        <ul aria-label="运行详情指标">
          <li>事件 <strong>{run.data.events.length}</strong></li>
          <li>工具 <strong>{toolLifecycles.length}</strong></li>
          <li>故障 <strong>{failureDiagnostics.length}</strong></li>
          <li>产物 <strong>{run.data.artifacts.length}</strong></li>
        </ul>
      </div>

      <article>
        <h3>原始请求</h3>
        <p>{run.data.request}</p>
        {isWaitingForMode ? (
          <div className="composer-approval-popover mode-choice-popover">
            <span className="eyebrow">等待模式确认</span>
            <h3>自动检测没有足够把握</h3>
            <p>请先选择本次运行模式，确认后任务会继续进入队列并开始派单/讨论/执行。</p>
            <div className="mode-choice-grid">
              {MANUAL_RUN_MODES.map((item) => (
                <button
                  type="button"
                  key={item.value}
                  disabled={chooseMode.isPending}
                  onClick={() => chooseMode.mutate(item.value)}
                >
                  <strong>{item.label}</strong>
                  <small>{item.description}</small>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
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
                    onClick={() => approveCapability.mutate(capabilityApproval)}
                  >
                    {approveCapability.isPending ? "允许中..." : "允许一次"}
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={approveCapability.isPending || rejectCapability.isPending}
                    onClick={() => rejectCapability.mutate(capabilityApproval)}
                  >
                    {rejectCapability.isPending ? "拒绝中..." : "拒绝"}
                  </button>
                </div>
              </aside>
            ) : null}
            <div className="toolbar">
              <button type="button" disabled={!canPause || control.isPending} onClick={() => control.mutate("pause")}>
                暂停
              </button>
              <button type="button" disabled={!canResume || control.isPending} onClick={() => control.mutate("resume")}>
                恢复
              </button>
              <button type="button" disabled={!canCancel || control.isPending} onClick={() => control.mutate("cancel")}>
                取消
              </button>
            </div>
          </>
        )}
        {!isWaitingForMode && !canPause && !canResume && canCancel ? (
          <p className="field-help">当前状态不支持暂停或恢复，只能取消。</p>
        ) : null}
        {control.isError ? <p role="alert">{formatApiError(control.error, "运行控制失败")}</p> : null}
        {chooseMode.isError ? <p role="alert">{formatApiError(chooseMode.error, "运行模式确认失败")}</p> : null}
        {approveCapability.isError ? <p role="alert">{formatApiError(approveCapability.error, "沙箱权限确认失败")}</p> : null}
        {rejectCapability.isError ? <p role="alert">{formatApiError(rejectCapability.error, "沙箱权限拒绝失败")}</p> : null}
      </article>

      {observerNotices.length > 0 ? (
        <article>
          <h3>调度观察</h3>
          <p className="field-help">主 Agent 运行监视器记录了需要关注的调度信号，优先用于排查模型拥堵、空响应和重试预算。</p>
          <ul className="compact-list">
            {observerNotices.map((notice) => (
              <li key={notice.sequence}>
                <strong>{observerTriggerLabel(notice.trigger)}</strong>
                <span>{observerSeverityLabel(notice.severity)}</span>
                <strong>{observerActionLabel(notice.action)}</strong>
                {notice.sourceKind && notice.sourceSequence !== null ? (
                  <small>来源：{observerSourceLabel(notice.sourceKind)} #{notice.sourceSequence}</small>
                ) : null}
                {notice.actor ? <small>角色：{notice.actor}</small> : null}
                <small>
                  运行信号：失败 {notice.failureEvents ?? 0} / 重试 {notice.retryEvents ?? 0} / 消息 {notice.messageEvents ?? 0} / 产物 {notice.artifactEvents ?? 0}
                </small>
              </li>
            ))}
          </ul>
        </article>
      ) : null}

      <DetailProcessSummary cards={processCards} />

      {failureDiagnostics.length > 0 ? (
        <section className="run-failure-diagnostics" aria-label="故障诊断">
          <div className="run-failure-diagnostics-header">
            <span>Failure diagnostics</span>
            <strong>故障诊断</strong>
            <small>{failureDiagnostics.length} 条</small>
          </div>
          <div className="run-failure-diagnostic-list">
            {failureDiagnostics.map((diagnostic) => (
              <article key={diagnostic.id} className={`run-failure-diagnostic diagnostic-${diagnostic.tone}`}>
                <small>{diagnostic.label}</small>
                <strong>{diagnostic.title}</strong>
                <span>{diagnostic.detail}</span>
                <p>{diagnostic.recommendation}</p>
                {diagnostic.meta.length > 0 ? (
                  <div aria-label="故障元数据">
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
            <span>Execution intent</span>
            <strong>执行意图</strong>
            <small>{executionIntents.length} 条</small>
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

      {toolLifecycles.length > 0 ? (
        <section aria-label="工具链路">
          <h3>工具链路</h3>
          <ul className="compact-list">
            {toolLifecycles.map((item) => (
              <li key={item.key}>
                <strong>{item.toolName}</strong>
                <span>{displayToolStatus(item.status)}</span>
                <span>{displayToolOperation(item.operationKind)}</span>
                {item.stepId ? <small>步骤 {item.stepId}</small> : null}
                {item.actor ? <small>角色 {item.actor}</small> : null}
                {item.argumentBytes !== null ? <small>参数 {item.argumentBytes} bytes</small> : null}
                {item.outputBytes !== null ? <small>输出 {item.outputBytes} bytes</small> : null}
                {item.exitCode !== null ? <small>退出码 {item.exitCode}</small> : null}
                {item.failureKind ? <small>{item.failureKind}</small> : null}
                {item.approvalId ? <small>审批 {item.approvalId}</small> : null}
                {item.replaySafe !== null ? <small>{replaySafetyLabel(item.replaySafe)}</small> : null}
                {item.artifactId ? <small>产物 {item.artifactId}</small> : null}
                <small>事件 #{item.sequences.join(", #")}</small>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <article>
        <h3>事件日志</h3>
        {timelineItems.length === 0 ? (
          <p>暂无事件。</p>
        ) : (
          <ol className="event-log-list">
            {timelineItems.map((item) => {
              if (item.type === "model_delta_group") {
                const firstEvent = item.events[0];
                const lastEvent = item.events.at(-1) ?? firstEvent;
                const sequence =
                  item.events.length > 1 ? `#${firstEvent.sequence}-${lastEvent.sequence}` : `#${firstEvent.sequence}`;
                return (
                  <li key={`model-delta-${firstEvent.sequence}-${lastEvent.sequence}`}>
                    <span className="event-log-sequence">{sequence}</span>
                    <time dateTime={lastEvent.created_at}>{runEventTimestamp(lastEvent.created_at)}</time>
                    <strong>{displayDetailEventKind(lastEvent.kind)}</strong>
                    <span>{modelDeltaSummary(item.events)}</span>
                  </li>
                );
              }
              return (
                <li key={item.event.sequence}>
                  <span className="event-log-sequence">#{item.event.sequence}</span>
                  <time dateTime={item.event.created_at}>{runEventTimestamp(item.event.created_at)}</time>
                  <strong>{displayDetailEventKind(item.event.kind)}</strong>
                  <span>{safeDetailEventSummary(item.event)}</span>
                </li>
              );
            })}
          </ol>
        )}
      </article>

      <article>
        <h3>产物</h3>
        {run.data.artifacts.length === 0 ? (
          <p>暂无产物。</p>
        ) : (
          <ul>
            {dedupeArtifactDownloads(run.data.artifacts).map((artifact) => (
              <li key={artifact.id}>
                <strong>{artifact.kind}：{artifact.title}</strong>
                {artifact.filename ? <small>{artifact.filename}</small> : null}
                {hasArtifactDownload(artifact) ? <ArtifactFileCard artifact={artifact} compact /> : null}
              </li>
            ))}
          </ul>
        )}
      </article>

      <article>
        <h3>模式、工作流与角色</h3>
        {explicitRows.length === 0 ? (
          <p>暂无显式详情。</p>
        ) : (
          <dl>
            {explicitRows.map((row) => (
              <div key={row.key}>
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </article>
    </section>
  );
}
