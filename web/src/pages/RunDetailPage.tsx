import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError } from "../api/client";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MANUAL_RUN_MODES = [
  { value: "direct", label: "直接执行", description: "让主 Agent 或指定角色直接回答。" },
  { value: "dispatch", label: "派单式", description: "拆分任务并分派给多个角色。" },
  { value: "discuss", label: "讨论式", description: "让多个角色先讨论，再形成结论。" },
  { value: "hybrid", label: "混合式", description: "先讨论方案，再分工执行，最后审查。" },
] as const;

type ManualRunMode = (typeof MANUAL_RUN_MODES)[number]["value"];

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
                {artifact.kind}：{artifact.title}
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
