import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { api, formatApiError } from "../api/client";

type RoleTemplate = {
  id: string;
  name: string;
  role: string;
  prompt: string;
  skills: string[];
};

const ROLE_TEMPLATES: RoleTemplate[] = [
  { id: "custom-agent", name: "自定义角色", role: "", prompt: "", skills: [] },
  {
    id: "director",
    name: "导演",
    role: "导演",
    prompt: "负责拆解目标、规划叙事结构、镜头语言、节奏控制和最终质量把关。",
    skills: [],
  },
  {
    id: "copywriter",
    name: "文案生成",
    role: "文案生成",
    prompt: "负责生成标题、脚本、口播稿、卖点表达和多版本文案，并保持表达清晰可执行。",
    skills: [],
  },
  {
    id: "editor",
    name: "剪辑师",
    role: "剪辑师",
    prompt: "负责将内容拆解为镜头、转场、字幕、音效和剪辑节奏建议。",
    skills: [],
  },
  {
    id: "economic-analyst",
    name: "经济分析师",
    role: "经济分析师",
    prompt: "负责宏观经济、行业、公司和数据逻辑分析，必须区分事实、推断和不确定性。",
    skills: [],
  },
  {
    id: "researcher",
    name: "研究员",
    role: "研究员",
    prompt: "负责收集信息、校验来源、整理证据链，并输出可复核的结论。",
    skills: [],
  },
  {
    id: "critic",
    name: "审查员",
    role: "审查员",
    prompt: "负责发现逻辑漏洞、风险、遗漏条件和潜在失败路径，不直接替代最终决策。",
    skills: [],
  },
  {
    id: "operator",
    name: "执行官",
    role: "执行官",
    prompt: "负责把计划拆成可执行步骤，调用允许的非危险工具，并记录结果和错误原因。",
    skills: [],
  },
  {
    id: "product-manager",
    name: "产品经理",
    role: "产品经理",
    prompt: "负责梳理用户需求、功能范围、优先级、验收标准和上线风险，避免需求发散。",
    skills: [],
  },
  {
    id: "engineer",
    name: "工程师",
    role: "工程师",
    prompt: "负责技术实现方案、接口设计、边界条件、部署约束和可维护性评估。",
    skills: [],
  },
  {
    id: "qa-tester",
    name: "测试工程师",
    role: "测试工程师",
    prompt: "负责设计测试用例、回归范围、故障注入和生产可用性核验清单。",
    skills: [],
  },
  {
    id: "ops-engineer",
    name: "运维工程师",
    role: "运维工程师",
    prompt: "负责 Linux 部署、服务状态、日志、权限、反向代理、证书和故障恢复方案。",
    skills: [],
  },
  {
    id: "security-reviewer",
    name: "安全审查员",
    role: "安全审查员",
    prompt: "负责识别密钥泄露、越权、SSRF、命令执行、危险工具调用和审计缺口。",
    skills: [],
  },
  {
    id: "finance-analyst",
    name: "财务分析师",
    role: "财务分析师",
    prompt: "负责财务指标、现金流、成本结构、预算、利润和风险敏感性分析。",
    skills: [],
  },
  {
    id: "market-analyst",
    name: "市场分析师",
    role: "市场分析师",
    prompt: "负责市场规模、竞品、用户画像、渠道策略和增长机会分析。",
    skills: [],
  },
  {
    id: "legal-assistant",
    name: "合规助理",
    role: "合规助理",
    prompt: "负责识别合同、隐私、版权、平台规则和合规风险，但不替代专业法律意见。",
    skills: [],
  },
  {
    id: "customer-support",
    name: "客服专家",
    role: "客服专家",
    prompt: "负责用户问题分流、回复草稿、故障解释、升级条件和服务体验优化。",
    skills: [],
  },
];

function parseSkills(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function AgentsPage() {
  const queryClient = useQueryClient();
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.agents() });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const [templateId, setTemplateId] = useState(ROLE_TEMPLATES[0].id);
  const selectedTemplate = useMemo(
    () => ROLE_TEMPLATES.find((item) => item.id === templateId) ?? ROLE_TEMPLATES[0],
    [templateId],
  );
  const [agentId, setAgentId] = useState(selectedTemplate.id);
  const [name, setName] = useState(selectedTemplate.name);
  const [role, setRole] = useState(selectedTemplate.role);
  const [prompt, setPrompt] = useState(selectedTemplate.prompt);
  const [model, setModel] = useState("");
  const [skills, setSkills] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const modelOptions = useMemo(
    () => Array.from(new Set((models.data ?? []).map((item) => item.logical_model))).sort(),
    [models.data],
  );
  const selectedModel = model || modelOptions[0] || "";

  const saveAgent = useMutation({
    mutationFn: () =>
      api.createAgent({
        id: agentId.trim(),
        name: name.trim(),
        enabled: true,
        role: role.trim(),
        prompt: prompt.trim(),
        model: selectedModel,
        skills: parseSkills(skills),
      }),
    onSuccess: async () => {
      setMessage("Agent 已保存，并写入当前生产配置。");
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["config-current"] });
    },
  });

  const deleteAgent = useMutation({
    mutationFn: (id: string) => api.deleteAgent(id),
    onSuccess: async () => {
      setMessage("Agent 已删除。");
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["config-current"] });
    },
  });

  function changeTemplate(nextId: string) {
    const next = ROLE_TEMPLATES.find((item) => item.id === nextId) ?? ROLE_TEMPLATES[0];
    setTemplateId(next.id);
    setAgentId(next.id === "custom-agent" ? "" : next.id);
    setName(next.id === "custom-agent" ? "" : next.name);
    setRole(next.role);
    setPrompt(next.prompt);
    setSkills(next.skills.join(","));
    setMessage(null);
  }

  function editAgent(agent: {
    id: string;
    name: string;
    role?: string | null;
    prompt?: string | null;
    model?: string | null;
    skills?: string[];
  }) {
    setTemplateId("custom-agent");
    setAgentId(agent.id);
    setName(agent.name);
    setRole(agent.role ?? agent.name);
    setPrompt(agent.prompt ?? "");
    setModel(agent.model ?? "");
    setSkills((agent.skills ?? []).join(","));
    setMessage(`已载入 ${agent.name}，修改后点击保存。`);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveAgent.mutate();
  }

  function confirmDelete(agent: { id: string; name: string }) {
    if (!window.confirm(`确定删除 Agent「${agent.name}」吗？已使用它的历史对话不会被删除，但新任务不能再派给它。`)) {
      return;
    }
    setMessage(null);
    deleteAgent.mutate(agent.id);
  }

  if (agents.isLoading || models.isLoading) return <p>正在加载 Agent 配置...</p>;
  if (agents.isError) return <p role="alert">{formatApiError(agents.error, "Agent 加载失败")}</p>;
  if (models.isError) return <p role="alert">{formatApiError(models.error, "模型加载失败")}</p>;

  return (
    <section>
      <p className="eyebrow">Role orchestration</p>
      <h2>Agent 角色编排</h2>
      <p>
        子 Agent 的角色不是写死的。模板只是快速起点，你可以创建完全自定义的角色、提示词、模型绑定和 Skill 白名单。
      </p>

      <article>
        <h3>配置指引</h3>
        <ol>
          <li>先在“模型”页面添加并测试可用模型，再创建 Agent。</li>
          <li>如果模板不合适，选择“自定义角色”，自己填写 Agent ID、显示名称、角色名称和提示词。</li>
          <li>讨论模式下，主 Agent 会按任务临时选择相关角色；如果你在任务或工作流中指定角色，则优先使用指定角色。</li>
          <li>Skill 字段填写允许该角色使用的 Skill ID，多个 ID 用英文逗号分隔。</li>
        </ol>
      </article>

      {modelOptions.length === 0 ? (
        <p role="alert">还没有可用模型。请先到“模型”页面添加模型，并通过 API 可用性测试。</p>
      ) : null}

      <form onSubmit={submit} aria-label="保存 Agent">
        <label htmlFor="role-template">
          角色模板
          <select id="role-template" value={templateId} onChange={(event) => changeTemplate(event.target.value)}>
            {ROLE_TEMPLATES.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>

        <div className="form-grid">
          <label htmlFor="agent-id">
            Agent ID
            <input
              id="agent-id"
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
              placeholder="例如 short-video-director"
              required
            />
          </label>
          <label htmlFor="agent-name">
            显示名称
            <input id="agent-name" value={name} onChange={(event) => setName(event.target.value)} required />
          </label>
          <label htmlFor="agent-role">
            角色名称
            <input id="agent-role" value={role} onChange={(event) => setRole(event.target.value)} required />
          </label>
          <label htmlFor="agent-model">
            绑定逻辑模型
            <select
              id="agent-model"
              value={selectedModel}
              onChange={(event) => setModel(event.target.value)}
              disabled={modelOptions.length === 0}
              required
            >
              {modelOptions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label htmlFor="agent-skills">
          允许使用的 Skill ID
          <input
            id="agent-skills"
            value={skills}
            onChange={(event) => setSkills(event.target.value)}
            placeholder="例如 script_review,safe_search"
          />
        </label>
        <p className="field-help">留空表示该 Agent 暂不绑定 Skill；危险工具仍会被系统权限边界拦截。</p>

        <label htmlFor="agent-prompt">
          系统提示词
          <textarea id="agent-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} required />
        </label>

        <button type="submit" disabled={saveAgent.isPending || modelOptions.length === 0}>
          {saveAgent.isPending ? "正在保存..." : "保存 Agent"}
        </button>
        {message ? <p role="status">{message}</p> : null}
        {saveAgent.isError ? <p role="alert">{formatApiError(saveAgent.error, "Agent 保存失败")}</p> : null}
      </form>

      <section aria-label="已保存 Agent">
        <h3>已保存 Agent</h3>
        {(agents.data ?? []).length === 0 ? <p>当前还没有 Agent。可以从上方模板或自定义角色创建第一个角色。</p> : null}
        <div className="card-grid">
          {(agents.data ?? []).map((agent) => (
            <article key={agent.id}>
              <span className="eyebrow">{agent.enabled ? "已启用" : "已停用"}</span>
              <h3>{agent.name}</h3>
              <p>ID：{agent.id}</p>
              <p>角色：{agent.role ?? agent.name}</p>
              <p>模型：{agent.model ?? "未绑定"}</p>
              <p>Skill：{(agent.skills ?? []).join(", ") || "无"}</p>
              {agent.prompt ? <p>提示词：{agent.prompt}</p> : null}
              <button type="button" onClick={() => editAgent(agent)}>
                编辑 Agent
              </button>
              <button
                type="button"
                className="danger-action"
                onClick={() => confirmDelete({ id: agent.id, name: agent.name })}
                disabled={deleteAgent.isPending}
              >
                删除 Agent
              </button>
            </article>
          ))}
        </div>
        {deleteAgent.isError ? <p role="alert">{formatApiError(deleteAgent.error, "Agent 删除失败")}</p> : null}
      </section>
    </section>
  );
}
