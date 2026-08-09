import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError } from "../api/client";

const WORKFLOW_PRESETS = [
  {
    id: "dispatch-default",
    name: "派单式任务",
    description: "适合目标清晰、步骤可拆分的任务，由主 Agent 分配给不同角色执行。",
  },
  {
    id: "discuss-review",
    name: "群聊讨论评审",
    description: "适合方案评审、决策分歧和需要多角色交叉质询的任务。",
  },
  {
    id: "hybrid-production",
    name: "混合生产流程",
    description: "先讨论确定方案，再派单执行，最后由审查角色做收口。",
  },
] as const;

export function WorkflowsPage() {
  const queryClient = useQueryClient();
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  const [presetId, setPresetId] = useState<string>(WORKFLOW_PRESETS[0].id);
  const preset = WORKFLOW_PRESETS.find((item) => item.id === presetId) ?? WORKFLOW_PRESETS[0];
  const [workflowId, setWorkflowId] = useState<string>(preset.id);
  const [name, setName] = useState<string>(preset.name);
  const [enabled, setEnabled] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  const saveWorkflow = useMutation({
    mutationFn: () =>
      api.createWorkflow({
        id: workflowId.trim(),
        name: name.trim(),
        enabled,
      }),
    onSuccess: async () => {
      setMessage("工作流已保存到生产管理资源。");
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  function changePreset(nextId: string) {
    const next = WORKFLOW_PRESETS.find((item) => item.id === nextId) ?? WORKFLOW_PRESETS[0];
    setPresetId(next.id);
    setWorkflowId(next.id);
    setName(next.name);
    setMessage(null);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveWorkflow.mutate();
  }

  if (workflows.isLoading) return <p>正在加载工作流配置...</p>;
  if (workflows.isError) {
    return <p role="alert">{formatApiError(workflows.error, "工作流加载失败")}</p>;
  }

  const savedWorkflows = workflows.data ?? [];

  return (
    <section>
      <p className="eyebrow">Workflow control</p>
      <h2>工作流配置</h2>
      <p>
        工作流用于给常见任务模式命名，方便后续在飞书、网页任务和主 Agent 调度中选择。
        保存后会持久化，不会因为服务重启丢失。
      </p>

      <div className="two-column">
        <form onSubmit={submit} aria-label="保存工作流">
          <h3>新增或更新工作流</h3>
          <label htmlFor="workflow-preset">模板</label>
          <select id="workflow-preset" value={presetId} onChange={(event) => changePreset(event.target.value)}>
            {WORKFLOW_PRESETS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <p className="field-help">{preset.description}</p>

          <label htmlFor="workflow-id">工作流 ID</label>
          <input
            id="workflow-id"
            value={workflowId}
            onChange={(event) => setWorkflowId(event.target.value)}
            placeholder="例如 dispatch-default"
            required
          />

          <label htmlFor="workflow-name">显示名称</label>
          <input id="workflow-name" value={name} onChange={(event) => setName(event.target.value)} required />

          <label className="inline-check">
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
            启用该工作流
          </label>

          <button type="submit" disabled={saveWorkflow.isPending}>
            {saveWorkflow.isPending ? "正在保存..." : "保存工作流"}
          </button>
          {message ? <p role="status">{message}</p> : null}
          {saveWorkflow.isError ? (
            <p role="alert">{formatApiError(saveWorkflow.error, "工作流保存失败")}</p>
          ) : null}
        </form>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>派单式适合明确产物，例如写方案、写脚本、生成报告。</li>
            <li>讨论式适合多个角色意见可能冲突的场景，由主 Agent 做最终裁决。</li>
            <li>混合式适合生产流程：先讨论方案，再分工执行，最后审查。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已保存工作流">
        <h3>已保存工作流</h3>
        {savedWorkflows.length === 0 ? (
          <article>
            <h4>还没有工作流</h4>
            <p>从上方选择一个模板保存即可开始使用。</p>
          </article>
        ) : (
          <div className="card-grid">
            {savedWorkflows.map((workflow) => (
              <article key={workflow.id}>
                <span className="eyebrow">{workflow.enabled ? "enabled" : "disabled"}</span>
                <h3>{workflow.name}</h3>
                <p>ID：{workflow.id}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
