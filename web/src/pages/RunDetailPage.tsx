import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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

function payloadString(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function payloadNumber(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
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
          <h3>{run.data.status}</h3>
        </article>
        <article>
          <span className="eyebrow">模式</span>
          <h3>{run.data.mode}</h3>
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

      <article>
        <h3>事件日志</h3>
        {run.data.events.length === 0 ? (
          <p>暂无事件。</p>
        ) : (
          <ol>
            {run.data.events.map((event) => (
              <li key={event.sequence}>
                <strong>{event.kind}</strong>：{event.message}
              </li>
            ))}
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
                {hasArtifactDownload(artifact) ? (
                  <ArtifactFileCard artifact={artifact} compact />
                ) : (
                  <>
                    {artifact.kind}：{artifact.title}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </article>

      <article>
        <h3>模式、工作流与角色</h3>
        {Object.keys(run.data.explicit_details).length === 0 ? (
          <p>暂无显式详情。</p>
        ) : (
          <dl>
            {Object.entries(run.data.explicit_details).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </article>
    </section>
  );
}
