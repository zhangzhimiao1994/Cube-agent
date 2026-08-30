import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError, type RunDetail } from "../api/client";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MANUAL_RUN_MODES = [
  { value: "direct", label: "直接执行", description: "让主 Agent 或指定角色直接回答。" },
  { value: "dispatch", label: "派单式", description: "拆分任务并分派给多个角色。" },
  { value: "discuss", label: "讨论式", description: "让多个角色先讨论，再形成结论。" },
  { value: "hybrid", label: "混合式", description: "先讨论方案，再分工执行，最后审查。" },
] as const;

type ManualRunMode = (typeof MANUAL_RUN_MODES)[number]["value"];
type RunEvent = RunDetail["events"][number];

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

function displayRunStatus(status: string) {
  return RUN_STATUS_LABELS[status] ?? status;
}

function displayRunMode(mode: string) {
  return RUN_MODE_LABELS[mode] ?? mode;
}

function displayToolStatus(status: string) {
  return TOOL_STATUS_LABELS[status] ?? status;
}

function displayToolOperation(kind: string) {
  return TOOL_OPERATION_LABELS[kind] ?? kind;
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

function isGenericEventMessage(event: RunEvent) {
  return event.message === event.kind || /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(event.message);
}

function safeDetailEventSummary(event: RunEvent) {
  if (event.summary?.trim()) return event.summary.trim();
  if (event.kind === "model.reasoning_delta") return "模型正在分析";
  if (event.kind === "model.text_delta") return "模型正在生成";
  if (event.kind.startsWith("tool.")) return `${event.tool_name ?? "工具"} ${event.kind.replace("tool.", "")}`;
  if (event.kind.startsWith("approval.")) return event.action ?? event.decision ?? "等待确认";
  if (isGenericEventMessage(event)) return "事件已记录";
  return event.message;
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

function isRepairIntentEvent(event: RunEvent) {
  const kind = event.kind.toLowerCase();
  if (kind.includes("repair") || kind.includes("remediation")) return true;
  return ["repair_action", "repair_kind", "self_repair", "remediation_action"].some((key) =>
    hasPositiveIntentSignal(event.payload[key]),
  );
}

function approvalStateFromEvents(events: RunEvent[]) {
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
      detail: finalEvent.tool_name ?? payloadString(finalEvent.payload, "name") ?? "工具调用",
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
      detail: event.action || safeIntentValue(event, ["decision", "status"]) || "需要确认后继续",
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
    if (event.kind.startsWith("approval.")) return;
    if (event.kind === "step.retrying") {
      const attempt = safeIntentValue(event, ["attempt"]);
      pushUniqueIntent(intents, {
        id: `${detail.id}-intent-retry-${event.step_id ?? event.sequence}`,
        label: "重试意图",
        title: "准备重试",
        detail: event.action || safeIntentValue(event, ["failure_kind", "status"]) || "失败后重试",
        meta: [
          attempt ? `第 ${attempt} 次` : "",
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
        detail: event.action && event.action !== repairAction ? event.action : safeIntentValue(event, ["failure_kind", "status"]) || "等待执行",
        meta: [
          isPayloadFlagTrue(event.payload.requires_approval) ? "需要确认" : "",
          safeIntentValue(event, ["failure_kind"]),
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

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: runId.length > 0,
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && !TERMINAL_STATUSES.has(data.status) ? 1000 : false;
    },
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
      const parsedVersion = Number(run.data.explicit_details.version ?? "0");
      return api.chooseMode(runId, {
        mode,
        decision_token: run.data.decision_token,
        version: Number.isInteger(parsedVersion) && parsedVersion > 0 ? parsedVersion : 0,
      });
    },
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
  const observerNotices = collectObserverNotices(run.data.events);
  const timelineItems = detailTimelineItems(run.data.events);
  const toolLifecycles = toolLifecycleFromApi(run.data);
  const posture = detailPosture(run.data);
  const explicitRows = explicitDetailRows(run.data.explicit_details);
  const executionIntents = executionIntentsForDetail(run.data);

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
          <li>故障 <strong>{run.data.failure_diagnostics.length}</strong></li>
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
        )}
        {!isWaitingForMode && !canPause && !canResume && canCancel ? (
          <p className="field-help">当前状态不支持暂停或恢复，只能取消。</p>
        ) : null}
        {control.isError ? <p role="alert">{formatApiError(control.error, "运行控制失败")}</p> : null}
        {chooseMode.isError ? <p role="alert">{formatApiError(chooseMode.error, "运行模式确认失败")}</p> : null}
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
                  <small>来源：{notice.sourceKind} #{notice.sourceSequence}</small>
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

      {run.data.failure_diagnostics.length > 0 ? (
        <section className="run-failure-diagnostics" aria-label="故障诊断">
          <div className="run-failure-diagnostics-header">
            <span>Failure diagnostics</span>
            <strong>故障诊断</strong>
            <small>{run.data.failure_diagnostics.length} 条</small>
          </div>
          <div className="run-failure-diagnostic-list">
            {run.data.failure_diagnostics.map((diagnostic) => (
              <article key={`${diagnostic.sequence}-${diagnostic.category}`} className="run-failure-diagnostic diagnostic-tool">
                <small>{diagnostic.category}</small>
                <strong>{diagnostic.tool_name ?? diagnostic.logical_model ?? diagnostic.stage}</strong>
                <span>{diagnostic.reason}</span>
                <p>{diagnostic.recommendation}</p>
                <div aria-label="故障元数据">
                  <em>#{diagnostic.sequence}</em>
                  {diagnostic.step_id ? <em>步骤 {diagnostic.step_id}</em> : null}
                  {diagnostic.failure_kind ? <em>{diagnostic.failure_kind}</em> : null}
                  {diagnostic.status_code ? <em>status {diagnostic.status_code}</em> : null}
                </div>
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
                    <strong>{lastEvent.kind}</strong>
                    <span>{modelDeltaSummary(item.events)}</span>
                  </li>
                );
              }
              return (
                <li key={item.event.sequence}>
                  <span className="event-log-sequence">#{item.event.sequence}</span>
                  <time dateTime={item.event.created_at}>{runEventTimestamp(item.event.created_at)}</time>
                  <strong>{item.event.kind}</strong>
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
            {run.data.artifacts.map((artifact) => (
              <li key={artifact.id}>
                {artifact.kind}：{artifact.title}
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
