import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";

import { api, formatApiError, type MainAgentConfig } from "../api/client";

type ApiProtocol = "openai_compatible" | "anthropic_messages";

const DEFAULT_POLICY = "choose workflow, select role pool, then make the final decision";
const DEFAULT_STYLE =
  "控场优先：先澄清目标和风险，再选择执行模式；直连时明确回答者，派单/讨论时明确角色池；意见冲突时按证据、风险、产物质量裁决；失败后沉淀 Hermes 学习。";

const CONTROL_MODE_LABELS: Record<MainAgentConfig["control_mode"], string> = {
  supervisor: "监督调度：主 Agent 负责拆解、派单、审查和最终裁决",
  planner: "规划优先：主 Agent 先做任务规划，再交给工作流执行",
  reviewer: "审查优先：主 Agent 主要做质量复核与冲突裁决",
  autonomous: "自主执行：主 Agent 在安全边界内自动选择模式与角色",
};

const HERMES_POLICY_LABELS: Record<MainAgentConfig["hermes_policy"], string> = {
  off: "关闭：不读取 Hermes 学习经验",
  observe: "观察：只记录经验，不影响调度",
  suggest: "建议：给出模式、角色、工具建议",
  confirm_before_apply: "确认后应用：学习建议需要人工确认才进入主 Agent 决策",
};

const API_PROTOCOL_LABELS: Record<ApiProtocol, string> = {
  openai_compatible: "OpenAI-compatible（根域名或 /v1）",
  anthropic_messages: "Anthropic Messages / Claude Code API（根域名、/v1 或 /v1/messages）",
};

function normalizeApiBase(value: string, apiProtocol: ApiProtocol) {
  const normalized = value.trim().replace(/\/chat\/completions\/?$/i, "").replace(/\/+$/, "");
  if (apiProtocol !== "anthropic_messages") {
    try {
      const parsed = new URL(normalized);
      return parsed.pathname === "/" || parsed.pathname === "" ? `${normalized}/v1` : normalized;
    } catch {
      return normalized;
    }
  }
  if (/\/messages$/i.test(normalized)) return normalized;
  if (/\/v1$/i.test(normalized)) return `${normalized}/messages`;
  return `${normalized}/v1/messages`;
}

export function MainAgentPage() {
  const queryClient = useQueryClient();
  const config = useQuery({ queryKey: ["main-agent"], queryFn: () => api.mainAgent() });
  const [provider, setProvider] = useState("openai-compatible");
  const [apiProtocol, setApiProtocol] = useState<ApiProtocol>("openai_compatible");
  const [apiBase, setApiBase] = useState("");
  const [upstreamModel, setUpstreamModel] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [controlMode, setControlMode] = useState<MainAgentConfig["control_mode"]>("supervisor");
  const [hermesPolicy, setHermesPolicy] = useState<MainAgentConfig["hermes_policy"]>("observe");
  const [directAnswerer, setDirectAnswerer] = useState("main_agent");
  const [decisionPolicy, setDecisionPolicy] = useState(DEFAULT_POLICY);
  const [operatingStyle, setOperatingStyle] = useState(DEFAULT_STYLE);
  const [maxReviewRounds, setMaxReviewRounds] = useState("2");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!config.data) return;
    const model = config.data.model;
    if (model) {
      setProvider(model.provider);
      setApiProtocol(model.api_protocol);
      setApiBase(model.api_base);
      setUpstreamModel(model.upstream_model);
      setCredentialRef(model.credential_ref);
    }
    setControlMode(config.data.control_mode);
    setHermesPolicy(config.data.hermes_policy);
    setDecisionPolicy(config.data.decision_policy);
    setOperatingStyle(config.data.operating_style);
    setDirectAnswerer(config.data.direct_answerer);
    setMaxReviewRounds(String(config.data.max_review_rounds));
  }, [config.data]);

  const save = useMutation({
    mutationFn: async () => {
      const savedSecret = apiKey.trim()
        ? await api.createSecret(`main-agent ${provider.trim()}`, apiKey.trim())
        : null;
      const resolvedCredentialRef = savedSecret?.ref ?? credentialRef.trim();
      const result = await api.updateMainAgent({
        model: {
          provider: provider.trim(),
          api_base: normalizeApiBase(apiBase, apiProtocol),
          api_protocol: apiProtocol,
          upstream_model: upstreamModel.trim(),
          credential_ref: resolvedCredentialRef,
          capabilities: ["text", "tool_calling"],
        },
        control_mode: controlMode,
        hermes_policy: hermesPolicy,
        decision_policy: decisionPolicy.trim() || DEFAULT_POLICY,
        operating_style: operatingStyle.trim() || DEFAULT_STYLE,
        direct_answerer: directAnswerer.trim() || "main_agent",
        max_review_rounds: Math.max(1, Math.min(20, Number(maxReviewRounds) || 2)),
      });
      return { result, savedSecret };
    },
    onSuccess: async () => {
      setApiKey("");
      setMessage("主 Agent 配置已保存。");
      await queryClient.invalidateQueries({ queryKey: ["main-agent"] });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    save.mutate();
  }

  if (config.isLoading) return <p>正在加载主 Agent 配置...</p>;
  if (config.isError) return <p role="alert">{formatApiError(config.error, "主 Agent 配置加载失败")}</p>;

  const savedModel = config.data?.model;

  return (
    <section data-testid="main-agent-page">
      <p className="eyebrow">Main agent control</p>
      <h2>主 Agent</h2>
      <p>
        主 Agent 独立配置自己的模型/API，不占用普通子 Agent 的模型绑定。它负责控场：判断模式、选择回答者或角色池、
        裁决冲突、管控 Hermes 学习和失败复盘。
      </p>

      <div className="status-grid" aria-label="主 Agent 当前配置">
        <article className="status-card">
          <span>当前专属模型</span>
          <p>{savedModel ? `${savedModel.provider} · ${savedModel.upstream_model}` : "未配置"}</p>
        </article>
        <article className="status-card">
          <span>API Base</span>
          <p>{savedModel?.api_base ?? "保存后显示主 Agent 专属 API 地址"}</p>
        </article>
        <article className="status-card">
          <span>直连回答者</span>
          <p>{config.data?.direct_answerer ?? "main_agent"}</p>
        </article>
      </div>

      <form onSubmit={submit} aria-label="主 Agent 配置" className="settings-form">
        <h3>主 Agent 专属模型/API</h3>
        <div className="form-grid">
          <label htmlFor="main-agent-provider">
            服务商
            <input
              id="main-agent-provider"
              data-testid="main-agent-provider"
              value={provider}
              onChange={(event) => setProvider(event.target.value)}
              placeholder="例如 openai-compatible / claude-code-relay"
              required
            />
          </label>
          <label htmlFor="main-agent-api-protocol">
            接口类型
            <select
              id="main-agent-api-protocol"
              data-testid="main-agent-api-protocol"
              value={apiProtocol}
              onChange={(event) => setApiProtocol(event.target.value as ApiProtocol)}
            >
              {Object.entries(API_PROTOCOL_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="main-agent-api-base">
            API Base
            <input
              id="main-agent-api-base"
              data-testid="main-agent-api-base"
              value={apiBase}
              onChange={(event) => setApiBase(event.target.value)}
              placeholder="https://api.example.com 或 https://api.example.com/v1"
              required
            />
          </label>
          <label htmlFor="main-agent-upstream-model">
            上游模型名
            <input
              id="main-agent-upstream-model"
              data-testid="main-agent-upstream-model"
              value={upstreamModel}
              onChange={(event) => setUpstreamModel(event.target.value)}
              placeholder="例如 deepseek-chat / claude-sonnet-4-6"
              required
            />
          </label>
          <label htmlFor="main-agent-credential-ref">
            Key 引用
            <input
              id="main-agent-credential-ref"
              data-testid="main-agent-credential-ref"
              value={credentialRef}
              onChange={(event) => setCredentialRef(event.target.value)}
              placeholder="例如 secret://main-agent；Key 明文仍在密钥页保存"
              required={!apiKey.trim()}
            />
          </label>
          <label htmlFor="main-agent-api-key">
            API Key（可选，保存后不回显）
            <input
              id="main-agent-api-key"
              data-testid="main-agent-api-key"
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder="填写新 Key 会先保存为密钥，再测试主 Agent 模型可用性"
            />
          </label>
          <label htmlFor="main-agent-control-mode">
            整体把控方式
            <select
              id="main-agent-control-mode"
              data-testid="main-agent-control-mode"
              value={controlMode}
              onChange={(event) => setControlMode(event.target.value as MainAgentConfig["control_mode"])}
            >
              {Object.entries(CONTROL_MODE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="main-agent-hermes-policy">
            Hermes 介入策略
            <select
              id="main-agent-hermes-policy"
              data-testid="main-agent-hermes-policy"
              value={hermesPolicy}
              onChange={(event) => setHermesPolicy(event.target.value as MainAgentConfig["hermes_policy"])}
            >
              {Object.entries(HERMES_POLICY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="main-agent-direct-answerer">
            直连默认回答者
            <input
              id="main-agent-direct-answerer"
              data-testid="main-agent-direct-answerer"
              value={directAnswerer}
              onChange={(event) => setDirectAnswerer(event.target.value)}
              placeholder="main_agent 或某个 Agent 角色 ID"
              required
            />
          </label>
          <label htmlFor="main-agent-review-rounds">
            复盘轮数
            <input
              id="main-agent-review-rounds"
              data-testid="main-agent-review-rounds"
              type="number"
              min="1"
              max="20"
              value={maxReviewRounds}
              onChange={(event) => setMaxReviewRounds(event.target.value)}
            />
          </label>
        </div>

        <label htmlFor="main-agent-operating-style">
          控场风格 / 行事原则
          <textarea
            id="main-agent-operating-style"
            data-testid="main-agent-operating-style"
            value={operatingStyle}
            onChange={(event) => setOperatingStyle(event.currentTarget.value)}
          />
        </label>

        <label htmlFor="main-agent-decision-policy">
          决策规则
          <textarea
            id="main-agent-decision-policy"
            data-testid="main-agent-decision-policy"
            value={decisionPolicy}
            onChange={(event) => setDecisionPolicy(event.currentTarget.value)}
          />
        </label>

        <article className="inline-guide">
          <h4>直连模式怎么决定谁回答？</h4>
          <p>
            对话页选择 direct 时，如果勾选了角色，角色列表第一个就是本次直连回答者；如果没有勾选角色，
            主 Agent 使用这里的“直连默认回答者”兜底。填 main_agent 表示由主 Agent 自己回答。
          </p>
        </article>

        <button data-testid="main-agent-save" type="submit" disabled={save.isPending}>
          {save.isPending ? "正在保存..." : "保存主 Agent 配置"}
        </button>
        {message ? <p>{message}</p> : null}
        {save.isError ? <p role="alert">{formatApiError(save.error, "主 Agent 配置保存失败")}</p> : null}
      </form>
    </section>
  );
}
