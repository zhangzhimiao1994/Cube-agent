import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError } from "../api/client";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: runId.length > 0,
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

  if (run.isLoading) return <p>正在加载运行详情...</p>;
  if (run.isError || !run.data) {
    return <p role="alert">{formatApiError(run.error, "运行详情加载失败")}</p>;
  }

  const canPause = ["queued", "running"].includes(run.data.status);
  const canResume = run.data.status === "paused";
  const canCancel = !TERMINAL_STATUSES.has(run.data.status);

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
        {!canPause && !canResume && canCancel ? (
          <p className="field-help">当前状态不支持暂停或恢复，只能取消。</p>
        ) : null}
        {control.isError ? <p role="alert">{formatApiError(control.error, "运行控制失败")}</p> : null}
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
