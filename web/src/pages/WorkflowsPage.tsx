import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { useNavSection } from "../app/navSections";
import { api, formatApiError, type WorkflowResource } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

const WORKFLOW_PRESETS: Array<
  Omit<WorkflowResource, "allow_main_agent_override" | "allow_temporary_agents" | "temporary_agent_policy"> & {
    description: string;
    suggested_roles: string[];
  }
> = [
  {
    id: "custom-workflow",
    name: "自定义工作流",
    enabled: true,
    mode: "auto",
    task_type: "",
    suggested_roles: [],
    agent_ids: [],
    objective: "",
    steps: [],
    deliverables: [],
    role_selection_policy: "根据任务目标选择真正需要的角色，不按模板固定派单对象。",
    decision_policy: "如果角色意见冲突，主 Agent 根据用户目标、证据质量、风险和可交付性做最终裁决；无法判断时询问用户。",
    description: "从空白配置开始，适合你自己定义新的任务类型、参与角色、步骤、交付物和裁决规则。",
  },
  {
    id: "short-video-dispatch",
    name: "短视频生产派单",
    enabled: true,
    mode: "dispatch",
    task_type: "短视频、脚本、内容生产",
    suggested_roles: ["director", "copywriter", "editor", "critic"],
    agent_ids: [],
    objective: "把短视频任务拆给导演、文案、剪辑师和审查员，输出可执行脚本和成片建议。",
    steps: ["导演确定选题角度和结构", "文案生成脚本与标题", "剪辑师拆镜头和节奏", "审查员检查风险和可交付性"],
    deliverables: ["短视频脚本", "镜头/剪辑建议", "标题与封面建议", "审查意见"],
    role_selection_policy: "优先选择导演、文案、剪辑师；涉及数据或商业判断时追加市场分析师；最终必须由审查员收口。",
    decision_policy: "如果创意方向冲突，优先选择更符合目标受众、平台限制和可拍摄性的方案。",
    description: "适合抖音、视频号、B站脚本和内容生产。",
  },
  {
    id: "software-dispatch",
    name: "软件工程派单",
    enabled: true,
    mode: "dispatch",
    task_type: "代码、部署、故障修复",
    suggested_roles: ["engineer", "qa-tester", "ops-engineer", "security-reviewer"],
    agent_ids: [],
    objective: "把工程任务拆给工程师、测试、运维和安全审查角色，减少生产事故。",
    steps: ["工程师定位实现方案", "测试工程师设计验证", "运维工程师检查部署影响", "安全审查员检查权限和密钥风险"],
    deliverables: ["实现方案", "验证清单", "部署注意事项", "安全审查意见"],
    role_selection_policy: "代码任务不要默认分给内容角色；涉及部署必须包含运维，涉及外部输入/密钥/权限必须包含安全审查。",
    decision_policy: "如果速度和安全冲突，生产环境优先安全和可回滚；无法确认时询问用户。",
    description: "适合代码修复、部署脚本、生产故障和架构调整。",
  },
  {
    id: "finance-analysis-dispatch",
    name: "财经分析派单",
    enabled: true,
    mode: "dispatch",
    task_type: "经济、金融、商业分析",
    suggested_roles: ["economic-analyst", "finance-analyst", "researcher", "critic"],
    agent_ids: [],
    objective: "把财经类问题拆给经济分析、财务分析、研究和审查角色，输出事实与推断分离的结论。",
    steps: ["研究员整理事实和数据来源", "经济分析师分析宏观和行业", "财务分析师分析成本/现金流/利润", "审查员标注风险和不确定性"],
    deliverables: ["事实清单", "分析结论", "风险提示", "可执行建议"],
    role_selection_policy: "财经任务必须包含研究员和审查员；涉及公司经营加入财务分析师；涉及宏观趋势加入经济分析师。",
    decision_policy: "如果结论冲突，优先采用证据来源更清楚、假设更少、风险披露更完整的结论。",
    description: "适合宏观经济、行业、公司、预算和投资相关分析。",
  },
  {
    id: "creative-design-discuss",
    name: "创意设计讨论",
    enabled: true,
    mode: "discuss",
    task_type: "艺术设计、品牌、创意方向",
    suggested_roles: ["director", "copywriter", "market-analyst", "critic"],
    agent_ids: [],
    objective: "围绕创意方向进行多角色讨论，避免把艺术设计任务交给不相关的工程角色。",
    steps: ["提出创意方向", "市场分析师判断受众和定位", "文案生成表达方案", "审查员检查一致性和风险", "主 Agent 裁决"],
    deliverables: ["创意方向", "视觉/文案建议", "受众判断", "最终方案"],
    role_selection_policy: "艺术设计默认不选择工程师；除非任务明确要求实现落地或网页代码，才加入工程师。",
    decision_policy: "如果美学与转化冲突，按用户目标决定：品牌表达优先一致性，营销素材优先转化。",
    description: "适合品牌、视觉方向、内容创意和营销表达。",
  },
  {
    id: "hybrid-production",
    name: "混合生产流程",
    enabled: true,
    mode: "hybrid",
    task_type: "复杂生产任务",
    suggested_roles: ["product-manager", "researcher", "engineer", "qa-tester", "critic"],
    agent_ids: [],
    objective: "先讨论方案，再派单执行，最后审查收口。",
    steps: ["讨论并确定方案", "按专业角色分配执行", "生成产物", "审查和修订"],
    deliverables: ["方案", "执行产物", "审查报告"],
    role_selection_policy: "根据任务目标选择角色，不按模式固定角色；需要内容就选内容角色，需要工程就选工程角色，需要财经就选分析角色。",
    decision_policy: "讨论阶段求异，执行阶段求稳，收口阶段以质量和可交付性为准。",
    description: "适合需要先定方向、再分工执行、最后验收的复杂任务。",
  },
];

function linesToList(value: string) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function listToLines(value: string[] | undefined) {
  return (value ?? []).join("\n");
}

function toggle(list: string[], value: string) {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

type WorkflowSortKey = "name" | "status" | "mode" | "taskType" | "roles" | "objective";

type WorkflowColumnFilters = {
  mode: "all" | NonNullable<WorkflowResource["mode"]>;
  name: string;
  objective: string;
  roles: string;
  status: "all" | "enabled" | "disabled";
  taskType: string;
};

const EMPTY_WORKFLOW_FILTERS: WorkflowColumnFilters = {
  mode: "all",
  name: "",
  objective: "",
  roles: "",
  status: "all",
  taskType: "",
};

function workflowStatus(workflow: WorkflowResource) {
  return workflow.enabled ? "已启用" : "已停用";
}

function workflowMode(workflow: WorkflowResource) {
  return workflow.mode ?? "auto";
}

function workflowRoles(workflow: WorkflowResource) {
  return (workflow.agent_ids ?? []).join(", ") || "未固定";
}

function workflowSearchText(workflow: WorkflowResource) {
  return [
    workflow.id,
    workflow.name,
    workflowStatus(workflow),
    workflowMode(workflow),
    workflow.task_type ?? "",
    workflowRoles(workflow),
    workflow.objective ?? "",
    workflow.role_selection_policy ?? "",
    workflow.decision_policy ?? "",
    ...(workflow.steps ?? []),
    ...(workflow.deliverables ?? []),
  ].join(" ");
}

function matchesWorkflowSearch(workflow: WorkflowResource, query: string) {
  return textContains(workflowSearchText(workflow), query);
}

function matchesWorkflowColumns(workflow: WorkflowResource, filters: WorkflowColumnFilters) {
  return (
    (filters.status === "all" || (filters.status === "enabled") === workflow.enabled) &&
    (filters.mode === "all" || workflowMode(workflow) === filters.mode) &&
    textContains(`${workflow.name} ${workflow.id}`, filters.name) &&
    textContains(workflow.task_type ?? "", filters.taskType) &&
    textContains(workflowRoles(workflow), filters.roles) &&
    textContains(`${workflow.objective ?? ""} ${workflow.role_selection_policy ?? ""} ${workflow.decision_policy ?? ""}`, filters.objective)
  );
}

function workflowSortValue(workflow: WorkflowResource, key: WorkflowSortKey) {
  if (key === "status") return workflowStatus(workflow);
  if (key === "mode") return workflowMode(workflow);
  if (key === "taskType") return workflow.task_type ?? "";
  if (key === "roles") return workflowRoles(workflow);
  if (key === "objective") return workflow.objective ?? "";
  return `${workflow.name} ${workflow.id}`;
}

function sortedWorkflows(items: WorkflowResource[], sort: SortState<WorkflowSortKey>) {
  return [...items].sort((left, right) => compareText(workflowSortValue(left, sort.key), workflowSortValue(right, sort.key), sort.direction));
}

export function WorkflowsPage() {
  const queryClient = useQueryClient();
  const { navTargetProps } = useNavSection();
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.agents() });
  const [presetId, setPresetId] = useState<string>(WORKFLOW_PRESETS[0].id);
  const preset = WORKFLOW_PRESETS.find((item) => item.id === presetId) ?? WORKFLOW_PRESETS[0];
  const [workflowId, setWorkflowId] = useState<string>(preset.id);
  const [name, setName] = useState<string>(preset.name);
  const [enabled, setEnabled] = useState(true);
  const [mode, setMode] = useState<NonNullable<WorkflowResource["mode"]>>(preset.mode ?? "dispatch");
  const [taskType, setTaskType] = useState<string>(preset.task_type ?? "");
  const [agentIds, setAgentIds] = useState<string[]>([]);
  const [objective, setObjective] = useState<string>(preset.objective ?? "");
  const [roleSelectionPolicy, setRoleSelectionPolicy] = useState<string>(preset.role_selection_policy ?? "");
  const [steps, setSteps] = useState<string>(listToLines(preset.steps));
  const [deliverables, setDeliverables] = useState<string>(listToLines(preset.deliverables));
  const [decisionPolicy, setDecisionPolicy] = useState<string>(preset.decision_policy ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const [workflowSearchTerm, setWorkflowSearchTerm] = useState("");
  const [workflowColumnFilters, setWorkflowColumnFilters] = useState<WorkflowColumnFilters>(EMPTY_WORKFLOW_FILTERS);
  const [workflowSort, setWorkflowSort] = useState<SortState<WorkflowSortKey>>({ key: "name", direction: "asc" });

  const saveWorkflow = useMutation({
    mutationFn: () =>
      api.createWorkflow({
        id: workflowId.trim(),
        name: name.trim(),
        enabled,
        mode,
        allow_main_agent_override: false,
        allow_temporary_agents: false,
        temporary_agent_policy: null,
        task_type: taskType.trim(),
        role_selection_policy: roleSelectionPolicy.trim(),
        agent_ids: agentIds,
        objective: objective.trim(),
        steps: linesToList(steps),
        deliverables: linesToList(deliverables),
        decision_policy: decisionPolicy.trim(),
      }),
    onSuccess: async () => {
      setMessage("工作流配置已保存。它不会立即执行，只会在聊天任务选择该工作流时生效。");
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  const deleteWorkflow = useMutation({
    mutationFn: (id: string) => api.deleteWorkflow(id),
    onSuccess: async () => {
      setMessage("工作流已删除。");
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  function changePreset(nextId: string) {
    const next = WORKFLOW_PRESETS.find((item) => item.id === nextId) ?? WORKFLOW_PRESETS[0];
    setPresetId(next.id);
    setWorkflowId(next.id);
    setName(next.name);
    setEnabled(next.enabled);
    setMode(next.mode ?? "dispatch");
    setTaskType(next.task_type ?? "");
    setAgentIds([]);
    setObjective(next.objective ?? "");
    setRoleSelectionPolicy(next.role_selection_policy ?? "");
    setSteps(listToLines(next.steps));
    setDeliverables(listToLines(next.deliverables));
    setDecisionPolicy(next.decision_policy ?? "");
    setMessage(null);
  }

  function applySuggestedRoles() {
    const available = new Set((agents.data ?? []).map((agent) => agent.id));
    setAgentIds(preset.suggested_roles.filter((id) => available.has(id)));
  }

  function editWorkflow(workflow: WorkflowResource) {
    setPresetId("custom-workflow");
    setWorkflowId(workflow.id);
    setName(workflow.name);
    setEnabled(workflow.enabled);
    setMode(workflow.mode ?? "auto");
    setTaskType(workflow.task_type ?? "");
    setAgentIds(workflow.agent_ids ?? []);
    setObjective(workflow.objective ?? "");
    setRoleSelectionPolicy(workflow.role_selection_policy ?? "");
    setSteps(listToLines(workflow.steps));
    setDeliverables(listToLines(workflow.deliverables));
    setDecisionPolicy(workflow.decision_policy ?? "");
    setMessage(`已载入 ${workflow.name}，修改后点击保存。`);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveWorkflow.mutate();
  }

  function confirmDelete(workflow: { id: string; name: string }) {
    if (!window.confirm(`确定删除工作流「${workflow.name}」吗？历史对话不会删除，但后续不能再选择它。`)) {
      return;
    }
    setMessage(null);
    deleteWorkflow.mutate(workflow.id);
  }

  const savedWorkflows = workflows.data ?? [];
  const savedAgents = agents.data ?? [];
  const visibleWorkflows = useMemo(
    () =>
      sortedWorkflows(
        savedWorkflows.filter(
          (workflow) =>
            matchesWorkflowSearch(workflow, workflowSearchTerm) &&
            matchesWorkflowColumns(workflow, workflowColumnFilters),
        ),
        workflowSort,
      ),
    [savedWorkflows, workflowColumnFilters, workflowSearchTerm, workflowSort],
  );

  if (workflows.isLoading || agents.isLoading) return <p>正在加载工作流配置...</p>;
  if (workflows.isError) return <p role="alert">{formatApiError(workflows.error, "工作流加载失败")}</p>;
  if (agents.isError) return <p role="alert">{formatApiError(agents.error, "Agent 列表加载失败")}</p>;

  return (
    <section>
      <p className="eyebrow">Workflow configuration</p>
      <h2>工作流配置</h2>
      <p>
        这里只负责配置工作流，不会直接执行任务。工作流用于描述某类任务应该用什么模式、哪些角色、哪些步骤和什么裁决规则。
      </p>

      <div className="two-column">
        <form onSubmit={submit} aria-label="保存工作流配置">
          <h3>新增或更新工作流</h3>
          <label htmlFor="workflow-preset">
            任务模板
            <select id="workflow-preset" value={presetId} onChange={(event) => changePreset(event.target.value)}>
              {WORKFLOW_PRESETS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <p className="field-help">
            {preset.description} 模板只负责快速填充，下面所有字段都可以改；如果要完全自定义，选择“自定义工作流”。
          </p>

          <div className="form-grid">
            <label htmlFor="workflow-id">
              工作流 ID
              <input id="workflow-id" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} placeholder="例如 short-video-dispatch" required />
            </label>
            <label htmlFor="workflow-name">
              显示名称
              <input id="workflow-name" value={name} onChange={(event) => setName(event.target.value)} required />
            </label>
            <label htmlFor="workflow-task-type">
              任务类型 / 适用场景
              <input id="workflow-task-type" value={taskType} onChange={(event) => setTaskType(event.target.value)} placeholder="例如短视频、代码修复、财经分析" required />
            </label>
            <label htmlFor="workflow-mode">
              默认运行模式
              <select id="workflow-mode" value={mode} onChange={(event) => setMode(event.target.value as NonNullable<WorkflowResource["mode"]>)}>
                <option value="auto">自动识别</option>
                <option value="direct">直接执行</option>
                <option value="dispatch">派单式</option>
                <option value="discuss">讨论式</option>
                <option value="hybrid">混合式</option>
              </select>
            </label>
          </div>
          <p className="field-help">
            工作流只保存这类任务的默认协作模板；聊天页使用它时可以选择本次角色池，但不会改写模板本身。
          </p>

          <label htmlFor="workflow-objective">
            工作流目标
            <textarea id="workflow-objective" value={objective} onChange={(event) => setObjective(event.target.value)} required />
          </label>

          <fieldset {...navTargetProps("roles")}>
            <legend>默认参与角色</legend>
            <p className="field-help">
              同一个模式可以有不同派单对象。这里选择的是该任务类型默认会派给哪些角色。
            </p>
            <button type="button" onClick={applySuggestedRoles}>
              使用模板建议角色
            </button>
            {savedAgents.length === 0 ? (
              <p className="field-help">还没有 Agent。可以先保存工作流，稍后创建角色后回来选择。</p>
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

          <label htmlFor="workflow-role-selection">
            角色选择规则
            <textarea id="workflow-role-selection" value={roleSelectionPolicy} onChange={(event) => setRoleSelectionPolicy(event.target.value)} required />
          </label>

          <div {...navTargetProps("execution", "nav-form-section")}>
            <label htmlFor="workflow-steps">
              执行步骤（每行一个）
              <textarea id="workflow-steps" value={steps} onChange={(event) => setSteps(event.target.value)} required />
            </label>

            <label htmlFor="workflow-deliverables">
              交付物（每行一个）
              <textarea id="workflow-deliverables" value={deliverables} onChange={(event) => setDeliverables(event.target.value)} required />
            </label>
          </div>

          <label htmlFor="workflow-decision-policy" {...navTargetProps("review")}>
            分歧裁决规则
            <textarea id="workflow-decision-policy" value={decisionPolicy} onChange={(event) => setDecisionPolicy(event.target.value)} required />
          </label>

          <label className="inline-check">
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
            启用该工作流
          </label>

          <button type="submit" disabled={saveWorkflow.isPending}>
            {saveWorkflow.isPending ? "正在保存..." : "保存工作流配置"}
          </button>
          {message ? <p role="status">{message}</p> : null}
          {saveWorkflow.isError ? <p role="alert">{formatApiError(saveWorkflow.error, "工作流保存失败")}</p> : null}
        </form>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>模式只定义协作方式；角色由任务类型和工作流决定。</li>
            <li>同样是派单，短视频、代码、财经、艺术设计应配置不同工作流。</li>
            <li>工作流配置后不会自动执行，只有聊天任务选择它时才会使用。</li>
            <li>如果自动检测不确定，主 Agent 应询问用户，而不是猜错模式。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已保存工作流" {...navTargetProps("list")}>
        <h3>已保存工作流</h3>
        {savedWorkflows.length === 0 ? (
          <article>
            <h4>还没有工作流</h4>
            <p>从上方选择模板并补全细节，保存后即可在对话任务中选择。</p>
          </article>
        ) : (
          <>
            <div className="list-toolbar">
              <label>
                快速搜索工作流
                <input
                  type="search"
                  aria-label="快速搜索工作流"
                  value={workflowSearchTerm}
                  onChange={(event) => setWorkflowSearchTerm(event.currentTarget.value)}
                  placeholder="名称、ID、任务类型、角色或裁决规则"
                />
              </label>
              <button type="button" className="secondary-action" onClick={() => { setWorkflowSearchTerm(""); setWorkflowColumnFilters(EMPTY_WORKFLOW_FILTERS); }}>
                清空工作流筛选
              </button>
              <small>显示 {visibleWorkflows.length} / {savedWorkflows.length}</small>
            </div>
            {visibleWorkflows.length === 0 ? (
              <article>
                <h4>当前筛选没有匹配工作流</h4>
                <p>调整列筛选或清空筛选查看全部工作流。</p>
              </article>
            ) : (
              <table aria-label="已保存工作流列表" className="dense-table">
                <thead>
                  <tr>
                    <th><SortHeader column="status" label="状态" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>状态</SortHeader></th>
                    <th><SortHeader column="name" label="工作流" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>工作流</SortHeader></th>
                    <th><SortHeader column="taskType" label="任务类型" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>任务类型</SortHeader></th>
                    <th><SortHeader column="mode" label="默认模式" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>默认模式</SortHeader></th>
                    <th><SortHeader column="roles" label="默认角色" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>默认角色</SortHeader></th>
                    <th><SortHeader column="objective" label="目标" sort={workflowSort} onSort={(column) => setWorkflowSort((current) => nextSortState(current, column))}>目标</SortHeader></th>
                    <th>操作</th>
                  </tr>
                  <tr className="table-filter-row">
                    <th>
                      <select
                        aria-label="按工作流状态筛选"
                        value={workflowColumnFilters.status}
                        onChange={(event) => {
                          const value = event.currentTarget.value as WorkflowColumnFilters["status"];
                          setWorkflowColumnFilters((current) => ({ ...current, status: value }));
                        }}
                      >
                        <option value="all">全部</option>
                        <option value="enabled">已启用</option>
                        <option value="disabled">已停用</option>
                      </select>
                    </th>
                    <th>
                      <input
                        aria-label="按工作流名称筛选"
                        value={workflowColumnFilters.name}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setWorkflowColumnFilters((current) => ({ ...current, name: value }));
                        }}
                        placeholder="名称或 ID"
                      />
                    </th>
                    <th>
                      <input
                        aria-label="按工作流任务类型筛选"
                        value={workflowColumnFilters.taskType}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setWorkflowColumnFilters((current) => ({ ...current, taskType: value }));
                        }}
                        placeholder="任务类型"
                      />
                    </th>
                    <th>
                      <select
                        aria-label="按工作流默认模式筛选"
                        value={workflowColumnFilters.mode}
                        onChange={(event) => {
                          const value = event.currentTarget.value as WorkflowColumnFilters["mode"];
                          setWorkflowColumnFilters((current) => ({ ...current, mode: value }));
                        }}
                      >
                        <option value="all">全部</option>
                        <option value="auto">自动识别</option>
                        <option value="direct">直接执行</option>
                        <option value="dispatch">派单式</option>
                        <option value="discuss">讨论式</option>
                        <option value="hybrid">混合式</option>
                      </select>
                    </th>
                    <th>
                      <input
                        aria-label="按工作流默认角色筛选"
                        value={workflowColumnFilters.roles}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setWorkflowColumnFilters((current) => ({ ...current, roles: value }));
                        }}
                        placeholder="Agent ID"
                      />
                    </th>
                    <th>
                      <input
                        aria-label="按工作流目标筛选"
                        value={workflowColumnFilters.objective}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setWorkflowColumnFilters((current) => ({ ...current, objective: value }));
                        }}
                        placeholder="目标或规则"
                      />
                    </th>
                    <th aria-label="工作流操作筛选占位" />
                  </tr>
                </thead>
                <tbody>
                  {visibleWorkflows.map((workflow) => (
                    <tr key={workflow.id}>
                      <td>{workflowStatus(workflow)}</td>
                      <td><strong>{workflow.name}</strong><br /><small>{workflow.id}</small></td>
                      <td>{workflow.task_type || "未设置"}</td>
                      <td>{workflowMode(workflow)}</td>
                      <td>{workflowRoles(workflow)}</td>
                      <td>{workflow.objective || "未设置"}</td>
                      <td className="table-actions">
                        <button type="button" onClick={() => editWorkflow(workflow)}>
                          编辑工作流
                        </button>
                        <button
                          type="button"
                          className="danger-action"
                          onClick={() => confirmDelete({ id: workflow.id, name: workflow.name })}
                          disabled={deleteWorkflow.isPending}
                        >
                          删除工作流
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
        {deleteWorkflow.isError ? <p role="alert">{formatApiError(deleteWorkflow.error, "工作流删除失败")}</p> : null}
      </section>
    </section>
  );
}
