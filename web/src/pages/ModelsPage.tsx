import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { ApiError, api, formatApiError, type ModelDeployment } from "../api/client";

const CUSTOM_PROVIDER = "custom";
const CUSTOM_MODEL = "__custom_model__";
const CHAT_COMPLETIONS_SUFFIX = /\/chat\/completions\/?$/i;

type ApiProtocol = "openai_compatible" | "anthropic_messages";

type ModelPreset = {
  label: string;
  value: string;
  capabilities: string[];
};

type ProviderPreset = {
  apiBase: string;
  apiProtocol: ApiProtocol;
  capabilities: string[];
  concurrencyHelp?: string;
  defaultMaxConcurrency?: number;
  defaultRpm?: number;
  defaultTpm?: number;
  label: string;
  modelHelp?: string;
  modelEntryMode?: "catalog" | "freeform";
  models: ModelPreset[];
  quotaScope: string;
  value: string;
};

const PROVIDERS: ProviderPreset[] = [
  {
    label: "OpenAI",
    value: "openai",
    apiBase: "https://api.openai.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "openai-account",
    capabilities: ["text", "tool_calling", "structured_output"],
    concurrencyHelp: "OpenAI 官方通常按项目/模型展示 RPM、TPM 等额度；请以控制台 Limits 为准，生产建议先从 1-4 并发验证。",
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
    quotaScope: "deepseek-account",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "DeepSeek 官方账号级并发：deepseek-v4-pro 500、deepseek-v4-flash 2500；系统仍建议按成本和业务压力从小并发开始。",
    defaultMaxConcurrency: 4,
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
    quotaScope: "anthropic-account",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Anthropic 官方额度通常以组织/工作区的 RPM、TPM、输入/输出 token 限额为准；Claude Code 中转站请以中转后台为准。",
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
    quotaScope: "kimi-account",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Kimi 限流按账号共享，具体 RPM/TPM 以控制台和 429 返回为准；建议先从并发 1-2 开始。",
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
    quotaScope: "qwen-token-plan-account",
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
    quotaScope: "qwen-account",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "DashScope/Qwen 不同模型和接口额度不同，常见为 QPS/RPM/TPM 或账号配额；请以百炼控制台额度为准。",
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
    quotaScope: "minimax-account",
    capabilities: ["text"],
    concurrencyHelp: "MiniMax 官方公开的主要是 RPM/TPM；文本接口常见 500 RPM、20,000,000 TPM，具体以账号额度为准。",
    defaultRpm: 500,
    defaultTpm: 20000000,
    models: [
      { label: "MiniMax M3", value: "MiniMax-M3", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "OpenAI 兼容中转站 / 混合模型池",
    value: "openai-compatible",
    apiBase: "",
    apiProtocol: "openai_compatible",
    quotaScope: "relay-account",
    capabilities: ["text", "tool_calling", "structured_output"],
    concurrencyHelp: "中转站可能混合多个上游模型。请按中转站后台或 CC-Switch 显示的模型、Base URL、协议和限流填写；未知时先设并发 1。",
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
    quotaScope: "claude-code-relay-account",
    capabilities: ["text", "tool_calling"],
    concurrencyHelp: "Claude Code API 中转站通常遵循 Anthropic Messages 请求格式，但限流由中转站决定；未知时先设并发 1。",
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

const ALL_CAPABILITIES = [
  { label: "文本", value: "text" },
  { label: "工具调用", value: "tool_calling" },
  { label: "结构化输出", value: "structured_output" },
  { label: "图片生成", value: "image_generation" },
  { label: "视频生成", value: "video_generation" },
];

const API_PROTOCOL_LABELS: Record<ApiProtocol, string> = {
  openai_compatible: "OpenAI-compatible（/v1/chat/completions）",
  anthropic_messages: "Anthropic Messages / Claude Code API（/v1/messages）",
};

function toPositiveNumber(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function toOptionalPositiveNumber(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

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

function displayCapability(capability: string) {
  return ALL_CAPABILITIES.find((item) => item.value === capability)?.label ?? capability;
}

function displaySaturationPolicy(policy: string) {
  return policy === "queue_first_then_fallback" ? "先排队，超时后降级" : policy;
}

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

export function ModelsPage() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const [provider, setProvider] = useState(PROVIDERS[0].value);
  const [customProvider, setCustomProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState(PROVIDERS[0].models[0].value);
  const [customModel, setCustomModel] = useState("");
  const [apiBase, setApiBase] = useState(PROVIDERS[0].apiBase);
  const [apiProtocol, setApiProtocol] = useState<ApiProtocol>(PROVIDERS[0].apiProtocol);
  const [apiKey, setApiKey] = useState("");
  const [logicalModel, setLogicalModel] = useState("main");
  const [quotaScope, setQuotaScope] = useState(PROVIDERS[0].quotaScope);
  const [capabilities, setCapabilities] = useState<string[]>(PROVIDERS[0].models[0].capabilities);
  const [maxConcurrency, setMaxConcurrency] = useState("1");
  const [rpm, setRpm] = useState("60");
  const [tpm, setTpm] = useState("100000");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [editingModel, setEditingModel] = useState<ModelDeployment | null>(null);

  const selectedProviderPreset = useMemo(
    () => PROVIDERS.find((item) => item.value === provider),
    [provider],
  );
  const modelOptions = selectedProviderPreset?.models ?? [];
  const isCustomProvider = provider === CUSTOM_PROVIDER;
  const isFreeformProvider = selectedProviderPreset?.modelEntryMode === "freeform";
  const isCustomModel = isCustomProvider || isFreeformProvider || selectedModel === CUSTOM_MODEL;
  const canChooseProtocol = isCustomProvider || isFreeformProvider;

  const saveModel = useMutation({
    mutationFn: async () => {
      const resolvedProvider = isCustomProvider ? customProvider.trim() : provider;
      const resolvedModel = isCustomModel ? customModel.trim() : selectedModel;
      const credentialRef =
        apiKey.trim() !== ""
          ? (await api.createSecret(`${logicalModel.trim()} ${resolvedProvider}`, apiKey)).ref
          : editingModel?.credential_ref;
      if (!credentialRef) throw new Error("API Key is required for a new model");
      const payload = {
        provider: resolvedProvider,
        api_base: normalizeApiBase(apiBase, apiProtocol),
        api_protocol: apiProtocol,
        upstream_model: resolvedModel,
        logical_model: logicalModel.trim(),
        capabilities,
        credential_ref: credentialRef,
        quota_scope: quotaScope.trim() || `${resolvedProvider}-account`,
        max_concurrency: toPositiveNumber(maxConcurrency, 1),
        target_utilization: 0.8,
        reserved_capacity: 0,
        rpm: toOptionalPositiveNumber(rpm),
        tpm: toOptionalPositiveNumber(tpm),
        queue_timeout_seconds: 60,
        fallback: null,
        weight: 100,
      };
      const model =
        editingModel === null
          ? await api.createModel(payload)
          : await api.updateModel(editingModel.id, payload);
      return { model, credentialRef };
    },
    onSuccess: async ({ credentialRef }) => {
      setSaveMessage(`模型已通过可用性测试并${editingModel ? "更新" : "保存"}，Key 引用：${credentialRef}`);
      setApiKey("");
      setEditingModel(null);
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
      await queryClient.invalidateQueries({ queryKey: ["logs", "model_error"] });
    },
  });

  const deleteModel = useMutation({
    mutationFn: (id: string) => api.deleteModel(id),
    onSuccess: async () => {
      setSaveMessage("模型配置已删除。");
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  function changeProvider(nextProvider: string) {
    setProvider(nextProvider);
    setSaveMessage(null);
    if (nextProvider === CUSTOM_PROVIDER) {
      setSelectedModel(CUSTOM_MODEL);
      setApiBase("");
      setApiProtocol("openai_compatible");
      setQuotaScope("");
      setCapabilities(["text"]);
      setMaxConcurrency("1");
      setRpm("60");
      setTpm("100000");
      return;
    }
    const preset = PROVIDERS.find((item) => item.value === nextProvider) ?? PROVIDERS[0];
    const defaultModel = preset.models[0];
    setSelectedModel(preset.modelEntryMode === "freeform" ? CUSTOM_MODEL : defaultModel.value);
    setCustomModel("");
    setApiBase(preset.apiBase);
    setApiProtocol(preset.apiProtocol);
    setQuotaScope(preset.quotaScope);
    setCapabilities(preset.modelEntryMode === "freeform" ? preset.capabilities : defaultModel.capabilities);
    setMaxConcurrency(String(preset.defaultMaxConcurrency ?? 1));
    setRpm(String(preset.defaultRpm ?? 60));
    setTpm(String(preset.defaultTpm ?? 100000));
  }

  function changeModel(nextModel: string) {
    setSelectedModel(nextModel);
    setSaveMessage(null);
    if (nextModel === CUSTOM_MODEL) {
      setCapabilities(selectedProviderPreset?.capabilities ?? ["text"]);
      return;
    }
    const presetModel = modelOptions.find((model) => model.value === nextModel);
    setCapabilities(presetModel?.capabilities ?? selectedProviderPreset?.capabilities ?? ["text"]);
  }

  function editSavedModel(model: ModelDeployment) {
    const preset = PROVIDERS.find((item) => item.value === model.provider);
    setEditingModel(model);
    setSaveMessage(null);
    if (preset) {
      setProvider(preset.value);
      setCustomProvider("");
      const presetModel = preset.models.find((item) => item.value === model.upstream_model);
      if (preset.modelEntryMode === "freeform" || !presetModel) {
        setSelectedModel(CUSTOM_MODEL);
        setCustomModel(model.upstream_model);
      } else {
        setSelectedModel(presetModel.value);
        setCustomModel("");
      }
      setApiProtocol(preset.apiProtocol);
    } else {
      setProvider(CUSTOM_PROVIDER);
      setCustomProvider(model.provider);
      setSelectedModel(CUSTOM_MODEL);
      setCustomModel(model.upstream_model);
      setApiProtocol(model.api_protocol);
    }
    setApiBase(model.api_base);
    setLogicalModel(model.logical_model);
    setQuotaScope(model.quota_scope);
    setCapabilities(model.capabilities);
    setMaxConcurrency(String(model.max_concurrency));
    setRpm(model.rpm === null ? "" : String(model.rpm));
    setTpm(model.tpm === null ? "" : String(model.tpm));
    setApiKey("");
  }

  function cancelEdit() {
    setEditingModel(null);
    setSaveMessage(null);
    setApiKey("");
  }

  function toggleCapability(capability: string) {
    setCapabilities((current) =>
      current.includes(capability)
        ? current.filter((item) => item !== capability)
        : [...current, capability],
    );
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaveMessage(null);
    saveModel.mutate();
  }

  if (models.isLoading) return <p>加载模型...</p>;
  if (models.isError) {
    return <p role="alert">{formatApiError(models.error, "模型加载失败")}</p>;
  }

  const savedModels = models.data ?? [];
  const protocolHint =
    apiProtocol === "anthropic_messages"
      ? "Claude Code API 管理工具（例如 CC-Switch）如果显示 Anthropic Messages 兼容接口，请填写根域名、/v1 或完整 /v1/messages；保存前会统一成 /v1/messages。"
      : "OpenAI-compatible 聚合 API 通常填写根域名或 /v1；如果粘贴 /v1/chat/completions，保存前会自动修正为 /v1。";

  return (
    <section>
      <p className="eyebrow">Model control</p>
      <h2>模型与 API</h2>
      <p>保存模型前系统会自动发起一次最小请求测试；测试失败不会发布该模型配置。</p>
      <p>同一服务商账号下的多个 Key 可能共享配额，不要把并发设置到跑满额度。</p>

      <article>
        <h3>填写指引</h3>
        <div className="detail-grid">
          <div>
            <span className="eyebrow">服务商 / 模型</span>
            <p>普通服务商只显示其下属模型；中转站是混合模型池，请直接填写中转站后台给出的完整模型 ID。</p>
          </div>
          <div>
            <span className="eyebrow">API Base / Key</span>
            <p>API Base 填服务商兼容 OpenAI 或 Anthropic 的接口地址；API Key 会加密保存，页面不会回显明文。</p>
          </div>
          <div>
            <span className="eyebrow">逻辑模型名</span>
            <p>Agent 只引用逻辑模型名，例如 main、planner、critic；以后更换供应商时不需要改角色配置。</p>
          </div>
          <div>
            <span className="eyebrow">并发与限流</span>
            <p>同一账号共用配额时保持相同 Quota Scope；新 Key 建议先从并发 1、RPM 60 开始，稳定后再提升。</p>
          </div>
        </div>
      </article>

      <form onSubmit={submit} aria-label="添加或编辑模型配置">
        <h3>{editingModel ? "编辑模型配置" : "添加模型配置"}</h3>
        {editingModel ? (
          <p className="field-hint">
            正在编辑已保存模型：{editingModel.logical_model} / {editingModel.upstream_model}。不填写新 API Key
            时会复用原 Key 引用；填写新 Key 会替换并重新测试。
          </p>
        ) : null}
        <p>选择服务商后，普通服务商只显示其下属模型；中转站支持直接输入任意上游模型名，并会照常做 API 可用性测试。</p>

        <label htmlFor="provider">服务商</label>
        <select id="provider" value={provider} onChange={(event) => changeProvider(event.target.value)}>
          {PROVIDERS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
          <option value={CUSTOM_PROVIDER}>自定义服务商</option>
        </select>

        {isCustomProvider ? (
          <>
            <label htmlFor="custom-provider">自定义服务商</label>
            <input
              id="custom-provider"
              value={customProvider}
              onChange={(event) => setCustomProvider(event.target.value)}
              placeholder="例如 my-ai-proxy"
              required
            />
          </>
        ) : null}

        {!isCustomProvider && !isFreeformProvider ? (
          <>
            <label htmlFor="model">模型</label>
            <select id="model" value={selectedModel} onChange={(event) => changeModel(event.target.value)}>
              {modelOptions.map((model) => (
                <option key={model.value} value={model.value}>
                  {model.label}
                </option>
              ))}
              <option value={CUSTOM_MODEL}>自定义模型</option>
            </select>
          </>
        ) : null}

        {isFreeformProvider && selectedProviderPreset?.modelHelp ? <p>{selectedProviderPreset.modelHelp}</p> : null}

        {isCustomModel ? (
          <>
            <label htmlFor="custom-model">{isFreeformProvider ? "中转站模型名" : "自定义模型"}</label>
            <input
              id="custom-model"
              list={isFreeformProvider ? "relay-model-suggestions" : undefined}
              value={customModel}
              onChange={(event) => setCustomModel(event.target.value)}
              placeholder={isFreeformProvider ? "粘贴中转站后台提供的模型 ID" : "填写服务商实际模型名"}
              required
            />
            {isFreeformProvider ? (
              <datalist id="relay-model-suggestions">
                {modelOptions.map((model) => (
                  <option key={model.value} value={model.value}>
                    {model.label}
                  </option>
                ))}
              </datalist>
            ) : null}
          </>
        ) : null}

        {canChooseProtocol ? (
          <label htmlFor="api-protocol">
            接口类型
            <select
              id="api-protocol"
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
        ) : (
          <p className="field-hint">官方预设已内置接口类型：{API_PROTOCOL_LABELS[apiProtocol]}。</p>
        )}

        <label htmlFor="api-base">API Base</label>
        <input
          id="api-base"
          value={apiBase}
          onChange={(event) => setApiBase(event.target.value)}
          placeholder="https://api.example.com/v1"
          required
        />
        <p className="field-hint">
          中转站可以填写根域名、/v1 或 /v1/messages；如果粘贴 /v1/chat/completions，保存时会自动修正为 /v1。
        </p>
        <p className="field-hint">{protocolHint}</p>

        <label htmlFor="api-key">API Key</label>
        <input
          id="api-key"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder="sk-..."
          required={!editingModel}
        />

        <label htmlFor="logical-model">逻辑模型名</label>
        <input
          id="logical-model"
          value={logicalModel}
          onChange={(event) => setLogicalModel(event.target.value)}
          placeholder="例如 main / planner / critic"
          required
        />

        <label htmlFor="quota-scope">Quota Scope</label>
        <input
          id="quota-scope"
          value={quotaScope}
          onChange={(event) => setQuotaScope(event.target.value)}
          placeholder="同一账号/同一配额池使用相同 scope"
        />

        <fieldset>
          <legend>能力</legend>
          {ALL_CAPABILITIES.map((capability) => (
            <label key={capability.value}>
              <input
                type="checkbox"
                checked={capabilities.includes(capability.value)}
                onChange={() => toggleCapability(capability.value)}
              />
              {capability.label}
            </label>
          ))}
        </fieldset>

        <label htmlFor="max-concurrency">最大并发</label>
        <input
          id="max-concurrency"
          type="number"
          min="1"
          value={maxConcurrency}
          onChange={(event) => setMaxConcurrency(event.target.value)}
          required
        />
        <p className="field-hint">
          {selectedProviderPreset?.concurrencyHelp ??
            "自定义服务商未提供官方预设；请查服务商控制台，或从并发 1 开始测试。"}
        </p>

        <label htmlFor="rpm">RPM</label>
        <input id="rpm" type="number" min="1" value={rpm} onChange={(event) => setRpm(event.target.value)} />

        <label htmlFor="tpm">TPM</label>
        <input id="tpm" type="number" min="1" value={tpm} onChange={(event) => setTpm(event.target.value)} />

        <button type="submit" disabled={saveModel.isPending}>
          {saveModel.isPending ? "测试并保存中..." : editingModel ? "测试并更新模型" : "测试并保存模型"}
        </button>
        {editingModel ? (
          <button type="button" onClick={cancelEdit} disabled={saveModel.isPending}>
            取消编辑
          </button>
        ) : null}
        {saveMessage ? <p role="status">{saveMessage}</p> : null}
        {saveModel.isError ? (
          <div role="alert">
            <p>
              {formatApiError(saveModel.error, "模型测试或保存失败，请检查 API Key、API Base、模型名或后端日志")}
            </p>
            {modelErrorDiagnostics(saveModel.error).length > 0 ? (
              <section className="error-log-panel" aria-label="模型配置错误日志">
                <h4>模型配置错误日志</h4>
                <p>下面是后端返回的脱敏诊断信息，不包含 API Key，可直接用于排查服务商配置。</p>
                <dl className="diagnostic-grid">
                  {modelErrorDiagnostics(saveModel.error).map((item) => (
                    <div key={item.key} className="diagnostic-row">
                      <dt>{item.label}</dt>
                      <dd>{item.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            ) : null}
          </div>
        ) : null}
      </form>

      <section aria-label="已保存模型">
        <h3>已保存模型</h3>
        <p>
          这里展示当前生产配置中已经保存的模型。Agent 绑定的是“逻辑模型”，实际请求会落到对应的服务商和上游模型。
        </p>
        {savedModels.length === 0 ? (
          <article>
            <h4>还没有保存模型</h4>
            <p>先在上方添加模型并通过 API 可用性测试；保存成功后会立即出现在这里。</p>
          </article>
        ) : (
          <table>
            <thead>
              <tr>
                <th>逻辑模型</th>
                <th>服务商</th>
                <th>上游模型</th>
                <th>API Base</th>
                <th>能力</th>
                <th>有效并发</th>
                <th>限流</th>
                <th>Quota Scope</th>
                <th>操作</th>
                <th>策略</th>
              </tr>
            </thead>
            <tbody>
              {savedModels.map((model) => (
                <tr key={model.id}>
                  <td>{model.logical_model}</td>
                  <td>{model.provider}</td>
                  <td>{model.upstream_model}</td>
                  <td>{model.api_base}</td>
                  <td>{model.capabilities.map(displayCapability).join("、")}</td>
                  <td>{model.effective_slots}</td>
                  <td>
                    RPM {model.rpm ?? "未设置"} / TPM {model.tpm ?? "未设置"}
                  </td>
                  <td>{model.quota_scope}</td>
                  <td>
                    <button type="button" data-testid={`edit-model-${model.id}`} onClick={() => editSavedModel(model)}>
                      编辑模型
                    </button>
                    <button
                      type="button"
                      data-testid={`delete-model-${model.id}`}
                      onClick={() => deleteModel.mutate(model.id)}
                      disabled={deleteModel.isPending}
                    >
                      删除模型
                    </button>
                  </td>
                  <td>{displaySaturationPolicy(model.saturation_policy)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {deleteModel.isError ? <p role="alert">{formatApiError(deleteModel.error, "模型删除失败")}</p> : null}
      </section>
    </section>
  );
}

