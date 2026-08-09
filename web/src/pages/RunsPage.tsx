import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { api, formatApiError } from "../api/client";

const RUN_MODES = [
  { value: "auto", label: "自动识别", description: "由主 Agent 判断应该直接执行、派单还是讨论。" },
  { value: "direct", label: "直接执行", description: "适合简单问答或单步任务。" },
  { value: "dispatch", label: "派单式", description: "适合拆成多个角色并行或串行完成。" },
  { value: "discuss", label: "讨论式", description: "适合多角色意见冲突、方案评审或需要裁决。" },
  { value: "hybrid", label: "混合式", description: "先讨论定方案，再派单执行和审查。" },
] as const;

type RunMode = (typeof RUN_MODES)[number]["value"];

function displayMode(mode: string) {
  return RUN_MODES.find((item) => item.value === mode)?.label ?? mode;
}

export function RunsPage() {
  const queryClient = useQueryClient();
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.runs() });
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState<RunMode>("auto");
  const [createdRunId, setCreatedRunId] = useState<string | null>(null);

  const createRun = useMutation({
    mutationFn: () => api.createRun({ message: message.trim(), mode }),
    onSuccess: async (run) => {
      setCreatedRunId(run.id);
      setMessage("");
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreatedRunId(null);
    createRun.mutate();
  }

  if (runs.isLoading) return <p>正在加载任务...</p>;
  if (runs.isError) return <p role="alert">{formatApiError(runs.error, "任务列表加载失败")}</p>;

  const selectedMode = RUN_MODES.find((item) => item.value === mode) ?? RUN_MODES[0];
  const items = runs.data ?? [];

  return (
    <section>
      <p className="eyebrow">Run operations</p>
      <h2>对话任务</h2>
      <p className="compact-page-intro">从网页或聊天通道提交任务，主 Agent 会选择模式、调度角色，并把错误写入运行事件。</p>

      <div className="run-compose">
        <form onSubmit={submit} aria-label="新建任务">
          <h3>新建任务</h3>
          <div className="form-grid">
            <label htmlFor="run-mode">
              运行模式
              <select id="run-mode" value={mode} onChange={(event) => setMode(event.target.value as RunMode)}>
                {RUN_MODES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="mode-help" aria-live="polite">
              <span className="eyebrow">{selectedMode.label}</span>
              <p>{selectedMode.description}</p>
              <p>模式会按当前选择即时解释，详细规则放在可展开说明里。</p>
            </div>
          </div>

          <label htmlFor="run-message">任务内容</label>
          <textarea
            id="run-message"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="输入你希望主 Agent 完成的任务，例如：让导演、文案、剪辑师讨论一个短视频脚本方案。"
            required
          />

          <button type="submit" disabled={createRun.isPending || message.trim().length === 0}>
            {createRun.isPending ? "正在提交..." : "提交任务"}
          </button>
          {createdRunId ? (
            <p role="status">
              任务已提交：
              <Link to={`/runs/${createdRunId}`}>{createdRunId}</Link>
            </p>
          ) : null}
          {createRun.isError ? <p role="alert">{formatApiError(createRun.error, "任务提交失败")}</p> : null}
          <details className="inline-guide">
            <summary>查看模式选择规则</summary>
            <ol>
              <li>不确定就选“自动识别”；识别不确定时主 Agent 会询问。</li>
              <li>讨论式出现分歧时，按证据质量、任务目标和约束裁决。</li>
              <li>模型、角色、Skill 未配置完整时，详情页会显示失败事件原因。</li>
            </ol>
          </details>
        </form>
      </div>

      <section aria-label="任务列表">
        <h3>任务列表</h3>
        {items.length === 0 ? (
          <article>
            <h4>暂无任务</h4>
            <p>从上方提交一个任务，或从已配置的聊天通道发送消息。</p>
          </article>
        ) : (
          <table>
            <thead>
              <tr>
                <th>任务</th>
                <th>状态</th>
                <th>模式</th>
                <th>排队等待</th>
                <th>容量等待</th>
                <th>成本</th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <Link to={`/runs/${run.id}`}>{run.id}</Link>
                  </td>
                  <td>{run.status}</td>
                  <td>{displayMode(run.mode)}</td>
                  <td>{run.queue_wait_ms} ms</td>
                  <td>{run.capacity_wait_ms} ms</td>
                  <td>${run.cost_usd}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
