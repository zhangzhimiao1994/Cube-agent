import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api, formatApiError, type MainAgentConfig } from "../api/client";
import { useNavSection } from "../app/navSections";

type ApiProtocol = "openai_compatible" | "anthropic_messages";

type ModelPreset = {
  capabilities: string[];
  label: string;
  value: string;
};

type ProviderPreset = {
  apiBase: string;
  apiProtocol: ApiProtocol;
  capabilities: string[];
  concurrencyHelp?: string;
  label: string;
  modelEntryMode?: "catalog" | "freeform";
  modelHelp?: string;
  models: ModelPreset[];
  value: string;
};

const CUSTOM_PROVIDER = "custom";
const CUSTOM_MODEL = "__custom_model__";
const DEFAULT_POLICY = "choose workflow, select role pool, then make the final decision";
const DEFAULT_STYLE =
  "控场优先：先澄清目标和风险，再选择执行模式；直连时明确回答者，派单/讨论时明确角色池；意见冲突时按证据、风险、产物质量裁决；失败后沉淀 Hermes 学习。";
const CHAT_COMPLETIONS_SUFFIX = /\/chat\/completions\/?$/i;

const PROVIDERS: ProviderPreset[] = [
  {
    label: "OpenAI",
    value: "openai",
    apiBase: "https://api.openai.com/v1",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling", "structured_output"],
    concurrencyHelp: "OpenAI 官方额度以控制台 Limits 为准；主 Agent 建议选稳定、上下文较强的模型。",
    models: [
      { label: "GPT-5.6 Terra", value: "gpt-5.6-terra", capabilities: ["text", "tool_calling", "structured_output"] },
      { label: "GPT-5.6 Sol", value: "gpt-5.6-sol", capabilities: ["text", "tool_calling", "structured_output"] },
    ],
  },
  {
    label: "DeepSeek",
    value: "deepseek",
    apiBase: "https://api.deepseek.com/v1",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "DeepSeek 官方文档给出账号级并发：deepseek-v4-pro 500、deepseek-v4-flash 2500；主 Agent 仍建议控制并发和成本。",
    models: [
      { label: "DeepSeek V4 Flash", value: "deepseek-v4-flash", capabilities: ["text", "tool_calling"] },
      { label: "DeepSeek V4 Pro", value: "deepseek-v4-pro", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "Anthropic",
    value: "anthropic",
    apiBase: "https://api.anthropic.com/v1/messages",
    apiProtocol: "anthropic_messages",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Anthropic/Claude 额度以组织或中转站后台为准；Claude Code 中转请确认 Anthropic Messages 协议。",
    models: [
      { label: "Claude Code / Claude Sonnet 5", value: "claude-sonnet-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Code / Claude Fable 5", value: "claude-fable-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Code / Claude Sonnet 4.6", value: "claude-sonnet-4-6", capabilities: ["text", "tool_calling"] },
      { label: "Claude Opus 5", value: "claude-opus-5", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "Kimi / Moonshot",
    value: "kimi",
    apiBase: "https://api.moonshot.cn/v1",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Kimi 限流按用户级共享，具体额度以控制台和 429 返回为准。",
    models: [
      { label: "Kimi K2.6", value: "kimi-k2.6", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.5", value: "kimi-k2.5", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.7 Code", value: "kimi-k2.7-code", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "阿里百炼 Token Plan / Qwen Code",
    value: "qwen-token-plan",
    apiBase: "",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Token Plan 请使用控制台“我的订阅 / API Key”里的专属 Base URL；并发 Agent 参考：Lite 1-2、Standard 3-4、Pro 6-8。",
    modelEntryMode: "freeform",
    modelHelp: "Token Plan 的 Base URL 不是普通 DashScope /compatible-mode/v1；请复制专属 Base URL。模型名可选官方推荐，也可以填写控制台最新 Model ID。",
    models: [
      { label: "Qwen3.8 Max Preview", value: "qwen3.8-max-preview", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.7 Max", value: "qwen3.7-max", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.7 Plus", value: "qwen3.7-plus", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.6 Plus", value: "qwen3.6-plus", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.6 Flash", value: "qwen3.6-flash", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.7 Code", value: "kimi-k2.7-code", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.6", value: "kimi-k2.6", capabilities: ["text", "tool_calling"] },
      { label: "DeepSeek V4 Flash", value: "deepseek-v4-flash", capabilities: ["text", "tool_calling"] },
      { label: "MiniMax M2.5", value: "MiniMax-M2.5", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "阿里 Qwen / DashScope",
    value: "qwen",
    apiBase: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "DashScope/Qwen 额度因模型和接口不同，请以百炼控制台 QPS/RPM/TPM 配额为准。",
    models: [
      { label: "Qwen3.7 Max", value: "qwen3.7-max", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3 Max", value: "qwen3-max", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3 Max Preview", value: "qwen3-max-preview", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3 Coder Plus", value: "qwen3-coder-plus", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "MiniMax",
    value: "minimax",
    apiBase: "https://api.minimax.chat/v1",
    apiProtocol: "openai_compatible",
    capabilities: ["text"],
    concurrencyHelp: "MiniMax 官方主要公开 RPM/TPM；文本接口常见 500 RPM、20,000,000 TPM，具体以账号额度为准。",
    models: [
      { label: "MiniMax M3", value: "MiniMax-M3", capabilities: ["text", "tool_calling"] },
      { label: "MiniMax Hailuo 02 视频", value: "MiniMax-Hailuo-02", capabilities: ["text", "video_generation"] },
    ],
  },
  {
    label: "OpenAI 兼容中转站 / 混合模型池",
    value: "openai-compatible",
    apiBase: "",
    apiProtocol: "openai_compatible",
    capabilities: ["text", "tool_calling", "structured_output"],
    concurrencyHelp: "中转站限流由中转站决定；如果 CC-Switch 能用，请按它显示的 Base URL、模型 ID 和协议配置。",
    modelEntryMode: "freeform",
    modelHelp: "中转站通常会混合多个厂商模型，请填写中转站后台显示的完整模型 ID。",
    models: [
      { label: "DeepSeek V4 Flash", value: "deepseek-v4-flash", capabilities: ["text", "tool_calling"] },
      { label: "DeepSeek V4 Pro", value: "deepseek-v4-pro", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.6", value: "kimi-k2.6", capabilities: ["text", "tool_calling"] },
      { label: "Kimi K2.7 Code", value: "kimi-k2.7-code", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.8 Max Preview", value: "qwen3.8-max-preview", capabilities: ["text", "tool_calling"] },
      { label: "Qwen3.7 Max", value: "qwen3.7-max", capabilities: ["text", "tool_calling"] },
      { label: "Claude Sonnet 5", value: "claude-sonnet-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Fable 5", value: "claude-fable-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Sonnet 4.6", value: "claude-sonnet-4-6", capabilities: ["text", "tool_calling"] },
      { label: "GPT-5.6 Terra", value: "gpt-5.6-terra", capabilities: ["text", "tool_calling", "structured_output"] },
      { label: "MiniMax M3", value: "MiniMax-M3", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "Claude Code API 中转站 / Anthropic Messages",
    value: "claude-code-relay",
    apiBase: "",
    apiProtocol: "anthropic_messages",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Claude Code API 中转站通常使用 Anthropic Messages；并发/限流以中转后台为准。",
    modelEntryMode: "freeform",
    modelHelp: "如果中转站遵守 CC-Switch / Claude Code 的 Anthropic Messages 规则，请选择此项并填写后台给出的模型 ID。",
    models: [
      { label: "Claude Sonnet 4.6", value: "claude-sonnet-4-6", capabilities: ["text", "tool_calling"] },
      { label: "Claude Sonnet 5", value: "claude-sonnet-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Fable 5", value: "claude-fable-5", capabilities: ["text", "tool_calling"] },
      { label: "Claude Opus 5", value: "claude-opus-5", capabilities: ["text", "tool_calling"] },
    ],
  },
];

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

const MODEL_ERROR_LABELS: Record<string, string> = {
  stage: "阶段",
  provider: "服务商",
  api_base: "API Base",
  logical_model: "逻辑模型",
  upstream_model: "上游模型",
  status_code: "HTTP 状态",
  reason: "失败原因",
  hint: "处理建议",
};

function normalizeApiBase(value: string, apiProtocol: ApiProtocol) {
  const normalized = value.trim().replace(CHAT_COMPLETIONS_SUFFIX, "").replace(/\/+$/, "");
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

function providerFor(value: string) {
  return PROVIDERS.find((item) => item.value === value);
}

function firstModel(provider: ProviderPreset) {
  return provider.models[0] ?? { capabilities: provider.capabilities, label: "", value: "" };
}

function capabilitiesFor(provider: ProviderPreset | undefined, selectedModel: string) {
  if (!provider) return ["text"];
  if (provider.modelEntryMode === "freeform" || selectedModel === CUSTOM_MODEL) return provider.capabilities;
  return provider.models.find((model) => model.value === selectedModel)?.capabilities ?? provider.capabilities;
}

function toPositiveNumber(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function effectiveModelSlots(maxConcurrency: number, targetUtilization = 0.8, reservedCapacity = 0) {
  return Math.max(1, Math.min(Math.floor(maxConcurrency * targetUtilization), maxConcurrency - reservedCapacity));
}

function concurrencyNeededForSlots(slots: number, targetUtilization = 0.8, reservedCapacity = 0) {
  const desired = Math.max(1, Math.ceil(slots));
  let value = Math.max(1, desired + reservedCapacity);
  while (effectiveModelSlots(value, targetUtilization, reservedCapacity) < desired) value += 1;
  return value;
}

function modelErrorDiagnostics(error: unknown) {
  if (!(error instanceof ApiError) || !error.details) return [];
  return Object.entries(error.details)
    .filter(([, value]) => value !== null && value !== "")
    .map(([key, value]) => ({
      key,
      label: MODEL_ERROR_LABELS[key] ?? key,
      value: String(value),
    }));
}

export function MainAgentPage() {
  const queryClient = useQueryClient();
  const { navTargetProps } = useNavSection();
  const config = useQuery({ queryKey: ["main-agent"], queryFn: () => api.mainAgent() });
  const [provider, setProvider] = useState(PROVIDERS[0].value);
  const [customProvider, setCustomProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState(PROVIDERS[0].models[0].value);
  const [customModel, setCustomModel] = useState("");
  const [apiProtocol, setApiProtocol] = useState<ApiProtocol>(PROVIDERS[0].apiProtocol);
  const [apiBase, setApiBase] = useState(PROVIDERS[0].apiBase);
  const [credentialRef, setCredentialRef] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [maxConcurrency, setMaxConcurrency] = useState("1");
  const [controlMode, setControlMode] = useState<MainAgentConfig["control_mode"]>("supervisor");
  const [hermesPolicy, setHermesPolicy] = useState<MainAgentConfig["hermes_policy"]>("observe");
  const [decisionPolicy, setDecisionPolicy] = useState(DEFAULT_POLICY);
  const [operatingStyle, setOperatingStyle] = useState(DEFAULT_STYLE);
  const [maxReviewRounds, setMaxReviewRounds] = useState("2");
  const [message, setMessage] = useState<string | null>(null);

  const selectedProviderPreset = useMemo(() => providerFor(provider), [provider]);
  const modelOptions = selectedProviderPreset?.models ?? [];
  const isCustomProvider = provider === CUSTOM_PROVIDER;
  const isFreeformProvider = selectedProviderPreset?.modelEntryMode === "freeform";
  const isCustomModel = isCustomProvider || isFreeformProvider || selectedModel === CUSTOM_MODEL;
  const canChooseProtocol = isCustomProvider || isFreeformProvider;

  useEffect(() => {
    if (!config.data) return;
    const model = config.data.model;
    if (model) {
      const preset = providerFor(model.provider);
      if (preset) {
        setProvider(preset.value);
        setCustomProvider("");
        if (preset.modelEntryMode === "freeform") {
          setSelectedModel(CUSTOM_MODEL);
          setCustomModel(model.upstream_model);
        } else if (preset.models.some((item) => item.value === model.upstream_model)) {
          setSelectedModel(model.upstream_model);
          setCustomModel("");
        } else {
          setSelectedModel(CUSTOM_MODEL);
          setCustomModel(model.upstream_model);
        }
      } else {
        setProvider(CUSTOM_PROVIDER);
        setCustomProvider(model.provider);
        setSelectedModel(CUSTOM_MODEL);
        setCustomModel(model.upstream_model);
      }
      setApiProtocol(model.api_protocol);
      setApiBase(model.api_base);
      setCredentialRef(model.credential_ref);
      setMaxConcurrency(String(model.max_concurrency ?? 1));
    }
    setControlMode(config.data.control_mode);
    setHermesPolicy(config.data.hermes_policy);
    setDecisionPolicy(config.data.decision_policy);
    setOperatingStyle(config.data.operating_style);
    setMaxReviewRounds(String(config.data.max_review_rounds));
  }, [config.data]);

  function changeProvider(nextProvider: string) {
    setProvider(nextProvider);
    setMessage(null);
    if (nextProvider === CUSTOM_PROVIDER) {
      setCustomProvider("");
      setSelectedModel(CUSTOM_MODEL);
      setCustomModel("");
      setApiBase("");
      setApiProtocol("openai_compatible");
      setMaxConcurrency("1");
      return;
    }
    const preset = providerFor(nextProvider) ?? PROVIDERS[0];
    const model = firstModel(preset);
    setCustomProvider("");
    setSelectedModel(preset.modelEntryMode === "freeform" ? CUSTOM_MODEL : model.value);
    setCustomModel("");
    setApiBase(preset.apiBase);
    setApiProtocol(preset.apiProtocol);
    setMaxConcurrency("1");
  }

  function changeModel(nextModel: string) {
    setSelectedModel(nextModel);
    setMessage(null);
    if (nextModel !== CUSTOM_MODEL) setCustomModel("");
  }

  const savedModel = config.data?.model;
  const resolvedProvider = isCustomProvider ? customProvider.trim() : provider;
  const normalizedApiBase = normalizeApiBase(apiBase, apiProtocol);
  const hasConnectionChanged =
    !savedModel ||
    savedModel.provider !== resolvedProvider ||
    savedModel.api_protocol !== apiProtocol ||
    savedModel.api_base !== normalizedApiBase;
  const requiresNewApiKey = hasConnectionChanged && !apiKey.trim();
  const configuredMaxConcurrency = Math.max(1, Math.floor(toPositiveNumber(maxConcurrency, 1)));
  const previewEffectiveSlots = effectiveModelSlots(configuredMaxConcurrency);
  const maxConcurrencyForTwoSlots = concurrencyNeededForSlots(2);

  const save = useMutation({
    mutationFn: async () => {
      const resolvedModel = isCustomModel ? customModel.trim() : selectedModel;
      const savedSecret = apiKey.trim()
        ? await api.createSecret(`main-agent ${resolvedProvider}`, apiKey.trim())
        : null;
      const resolvedCredentialRef = savedSecret?.ref ?? credentialRef.trim();
      const result = await api.updateMainAgent({
        model: {
          provider: resolvedProvider,
          api_base: normalizedApiBase,
          api_protocol: apiProtocol,
          upstream_model: resolvedModel,
          credential_ref: resolvedCredentialRef,
          capabilities: capabilitiesFor(selectedProviderPreset, selectedModel),
          max_concurrency: configuredMaxConcurrency,
        },
        control_mode: controlMode,
        hermes_policy: hermesPolicy,
        decision_policy: decisionPolicy.trim() || DEFAULT_POLICY,
        operating_style: operatingStyle.trim() || DEFAULT_STYLE,
        direct_answerer: "manual_selection",
        max_review_rounds: Math.max(1, Math.min(20, Number(maxReviewRounds) || 2)),
      });
      return { result, savedSecret };
    },
    onSuccess: async () => {
      setApiKey("");
      setMessage("主 Agent 配置已保存。");
      await queryClient.invalidateQueries({ queryKey: ["main-agent"] });
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
      await queryClient.invalidateQueries({ queryKey: ["logs", "model_error"] });
    },
  });

  const deleteModel = useMutation({
    mutationFn: async () => {
      if (!config.data) return null;
      return api.updateMainAgent({ ...config.data, model: null });
    },
    onSuccess: async () => {
      setCredentialRef("");
      setApiKey("");
      setMessage("主 Agent 专属模型已删除。");
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

  const protocolHint =
    apiProtocol === "anthropic_messages"
      ? "Claude Code API 管理工具（例如 CC-Switch）里如果显示的是 Anthropic Messages 兼容接口，请把根域名、/v1 或完整 /v1/messages 填到这里；保存前会统一成 /v1/messages。"
      : "OpenAI-compatible 中转站通常填写根域名或 /v1；如果粘贴 /v1/chat/completions，保存前会自动修正为 /v1。";

  return (
    <section data-testid="main-agent-page">
      <p className="eyebrow">Main agent control</p>
      <h2>主 Agent</h2>
      <p>
        主 Agent 独立配置自己的模型/API，不占用普通子 Agent 的模型绑定。它负责控场：判断模式、提出角色和提示词、
        裁决冲突、管控 Hermes 学习和失败复盘；具体用哪个模型运行子 Agent 由你在配置或确认时选择。
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
          <span>直连规则</span>
          <p>每个新对话指定一次子 Agent</p>
        </article>
      </div>

      <form onSubmit={submit} aria-label="主 Agent 配置" className="settings-form">
        <h3 {...navTargetProps("model")}>主 Agent 专属模型/API</h3>
        <p className="field-hint">
          这里和“模型与 API”页面保持同一套注册流程：先选服务商，再选模型；中转站和自定义服务商可以直接填写后台给出的模型 ID。
        </p>
        <div className="form-grid">
          <label htmlFor="main-agent-provider">
            服务商
            <select
              id="main-agent-provider"
              data-testid="main-agent-provider"
              value={provider}
              onChange={(event) => changeProvider(event.target.value)}
            >
              {PROVIDERS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
              <option value={CUSTOM_PROVIDER}>自定义服务商</option>
            </select>
          </label>

          {isCustomProvider ? (
            <label htmlFor="main-agent-custom-provider">
              自定义服务商
              <input
                id="main-agent-custom-provider"
                data-testid="main-agent-custom-provider"
                value={customProvider}
                onChange={(event) => setCustomProvider(event.target.value)}
                placeholder="例如 my-ai-proxy"
                required
              />
            </label>
          ) : null}

          {!isCustomProvider && !isFreeformProvider ? (
            <label htmlFor="main-agent-model">
              模型
              <select
                id="main-agent-model"
                data-testid="main-agent-model"
                value={selectedModel}
                onChange={(event) => changeModel(event.target.value)}
              >
                {modelOptions.map((model) => (
                  <option key={model.value} value={model.value}>
                    {model.label}
                  </option>
                ))}
                <option value={CUSTOM_MODEL}>自定义模型</option>
              </select>
            </label>
          ) : null}

          {isCustomModel ? (
            <label htmlFor="main-agent-custom-model">
              {isFreeformProvider ? "中转站模型名" : "自定义模型"}
              <input
                id="main-agent-custom-model"
                data-testid="main-agent-custom-model"
                list={isFreeformProvider ? "main-agent-model-suggestions" : undefined}
                value={customModel}
                onChange={(event) => setCustomModel(event.target.value)}
                placeholder={isFreeformProvider ? "粘贴中转站后台提供的模型 ID" : "填写服务商实际模型名"}
                required
              />
              {isFreeformProvider ? (
                <datalist id="main-agent-model-suggestions">
                  {modelOptions.map((model) => (
                    <option key={model.value} value={model.value}>
                      {model.label}
                    </option>
                  ))}
                </datalist>
              ) : null}
            </label>
          ) : null}

          <label htmlFor="main-agent-api-protocol">
            接口类型
            <select
              id="main-agent-api-protocol"
              data-testid="main-agent-api-protocol"
              value={apiProtocol}
              onChange={(event) => setApiProtocol(event.target.value as ApiProtocol)}
              disabled={!canChooseProtocol}
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
          <p className="field-hint">
            {selectedProviderPreset?.concurrencyHelp ??
              "自定义服务商未提供官方预设；请以服务商控制台或中转站后台限流说明为准。"}
          </p>
          <label htmlFor="main-agent-max-concurrency" {...navTargetProps("concurrency")}>
            最大并发
            <input
              id="main-agent-max-concurrency"
              data-testid="main-agent-max-concurrency"
              type="number"
              min="1"
              value={maxConcurrency}
              onChange={(event) => setMaxConcurrency(event.target.value)}
              required
            />
          </label>
          <p className="field-hint" aria-live="polite">
            <strong>实际最大并发：{previewEffectiveSlots}</strong>
            （配置最大并发 {configuredMaxConcurrency}，目标利用率 80%）
          </p>
          <p className="field-hint">
            实际有效并发槽 {previewEffectiveSlots} 个；要让 2 个子 Agent 同时运行，最大并发至少填 {maxConcurrencyForTwoSlots}。
          </p>

          <label htmlFor="main-agent-api-key">
            API Key（可选，保存后不回显）
            <input
              id="main-agent-api-key"
              data-testid="main-agent-api-key"
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder="填写新 Key 会先保存为密钥，再测试主 Agent 模型可用性"
              required={requiresNewApiKey}
            />
          </label>
          <label htmlFor="main-agent-control-mode" {...navTargetProps("scheduler")}>
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
          <label htmlFor="main-agent-hermes-policy" {...navTargetProps("hermes")}>
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

        {selectedProviderPreset?.modelHelp ? <p className="field-hint">{selectedProviderPreset.modelHelp}</p> : null}
        <p className="field-hint">{protocolHint}</p>
        <p className="field-hint" data-testid="main-agent-key-state">
          {requiresNewApiKey
            ? "服务商、接口类型或 API Base 已变化，需要填写新的 API Key。"
            : "可沿用当前已保存 Key"}
        </p>

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
            主 Agent 不直接替子 Agent 干活。对话页进入直连模式时，由你在本次对话里指定一个子 Agent；
            后续同一对话会沿用这个回答者，新建对话后再重新选择。
          </p>
        </article>

        <button data-testid="main-agent-save" type="submit" disabled={save.isPending || requiresNewApiKey}>
          {save.isPending ? "正在保存..." : "保存主 Agent 配置"}
        </button>
        {message ? <p>{message}</p> : null}
        {save.isError ? (
          <div role="alert">
            <p>{formatApiError(save.error, "主 Agent 配置保存失败")}</p>
            {modelErrorDiagnostics(save.error).length > 0 ? (
              <section className="error-log-panel" aria-label="主 Agent 模型配置错误日志">
                <h4>主 Agent 模型配置错误日志</h4>
                <p>
                  后端已经把这次失败写入“模型配置与调用错误”日志。下面是脱敏诊断信息，不包含 API Key，可直接用于排查中转站、
                  API Base、协议类型或模型名。
                </p>
                <dl className="diagnostic-grid">
                  {modelErrorDiagnostics(save.error).map((item) => (
                    <div key={item.key} className="diagnostic-row">
                      <dt>{item.label}</dt>
                      <dd>{item.value}</dd>
                    </div>
                  ))}
                </dl>
                <Link to="/logs/model" className="button-link">
                  查看模型日志
                </Link>
              </section>
            ) : null}
          </div>
        ) : null}
      </form>
      <section aria-label="当前主 Agent 模型情况" className="error-log-panel">
        <h3>当前主 Agent 模型情况</h3>
        {savedModel ? (
          <>
            <dl className="diagnostic-grid">
              <div className="diagnostic-row">
                <dt>服务商</dt>
                <dd>{savedModel.provider}</dd>
              </div>
              <div className="diagnostic-row">
                <dt>接口类型</dt>
                <dd>{API_PROTOCOL_LABELS[savedModel.api_protocol]}</dd>
              </div>
              <div className="diagnostic-row">
                <dt>API Base</dt>
                <dd>{savedModel.api_base}</dd>
              </div>
              <div className="diagnostic-row">
                <dt>上游模型</dt>
                <dd>{savedModel.upstream_model}</dd>
              </div>
              <div className="diagnostic-row">
                <dt>有效/最大并发</dt>
                <dd>{effectiveModelSlots(savedModel.max_concurrency ?? 1)} / {savedModel.max_concurrency ?? 1}</dd>
              </div>
              <div className="diagnostic-row">
                <dt>Key 状态</dt>
                <dd>已加密保存，页面不显示明文。</dd>
              </div>
            </dl>
            <button
              type="button"
              data-testid="main-agent-delete-model"
              onClick={() => deleteModel.mutate()}
              disabled={deleteModel.isPending}
            >
              {deleteModel.isPending ? "正在删除..." : "删除主 Agent 专属模型"}
            </button>
          </>
        ) : (
          <p>当前没有配置主 Agent 专属模型。保存模型后这里会显示当前连接状态。</p>
        )}
        {deleteModel.isError ? (
          <p role="alert">{formatApiError(deleteModel.error, "主 Agent 专属模型删除失败")}</p>
        ) : null}
      </section>
    </section>
  );
}
