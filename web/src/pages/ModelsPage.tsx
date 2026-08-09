import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { ApiError, api, formatApiError } from "../api/client";

const CUSTOM_PROVIDER = "custom";
const CUSTOM_MODEL = "__custom_model__";
const CHAT_COMPLETIONS_SUFFIX = /\/chat\/completions\/?$/i;

type ModelPreset = {
  label: string;
  value: string;
  capabilities: string[];
};

type ProviderPreset = {
  apiBase: string;
  capabilities: string[];
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
    quotaScope: "openai-account",
    capabilities: ["text", "tool_calling", "structured_output"],
    models: [
      {
        label: "GPT-5.6 Terra",
        value: "gpt-5.6-terra",
        capabilities: ["text", "tool_calling", "structured_output"],
      },
      {
        label: "GPT-5.6 Sol",
        value: "gpt-5.6-sol",
        capabilities: ["text", "tool_calling", "structured_output"],
      },
    ],
  },
  {
    label: "DeepSeek",
    value: "deepseek",
    apiBase: "https://api.deepseek.com/v1",
    quotaScope: "deepseek-account",
    capabilities: ["text", "tool_calling"],
    models: [
      { label: "DeepSeek Chat", value: "deepseek-chat", capabilities: ["text", "tool_calling"] },
      { label: "DeepSeek Reasoner", value: "deepseek-reasoner", capabilities: ["text"] },
    ],
  },
  {
    label: "Anthropic / Claude Code",
    value: "anthropic",
    apiBase: "https://api.anthropic.com/v1",
    quotaScope: "anthropic-account",
    capabilities: ["text", "tool_calling"],
    models: [
      {
        label: "Claude Code / Claude Sonnet 4",
        value: "claude-sonnet-4-20250514",
        capabilities: ["text", "tool_calling"],
      },
      {
        label: "Claude Opus 4",
        value: "claude-opus-4-20250514",
        capabilities: ["text", "tool_calling"],
      },
      {
        label: "Claude 3.5 Haiku",
        value: "claude-3-5-haiku-20241022",
        capabilities: ["text"],
      },
    ],
  },
  {
    label: "Kimi / Moonshot",
    value: "kimi",
    apiBase: "https://api.moonshot.cn/v1",
    quotaScope: "kimi-account",
    capabilities: ["text", "tool_calling"],
    models: [
      {
        label: "Kimi K2 Turbo Preview",
        value: "kimi-k2-turbo-preview",
        capabilities: ["text", "tool_calling"],
      },
      { label: "Moonshot v1 128K", value: "moonshot-v1-128k", capabilities: ["text"] },
    ],
  },
  {
    label: "阿里 Qwen / DashScope",
    value: "qwen",
    apiBase: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    quotaScope: "qwen-account",
    capabilities: ["text", "tool_calling"],
    models: [
      { label: "Qwen Plus", value: "qwen-plus", capabilities: ["text", "tool_calling"] },
      { label: "Qwen Max", value: "qwen-max", capabilities: ["text", "tool_calling"] },
      { label: "Qwen Turbo", value: "qwen-turbo", capabilities: ["text", "tool_calling"] },
    ],
  },
  {
    label: "MiniMax",
    value: "minimax",
    apiBase: "https://api.minimax.chat/v1",
    quotaScope: "minimax-account",
    capabilities: ["text"],
    models: [
      { label: "MiniMax M1", value: "minimax-m1", capabilities: ["text"] },
      { label: "abab6.5s-chat", value: "abab6.5s-chat", capabilities: ["text"] },
    ],
  },
  {
    label: "OpenAI 兼容中转站 / 混合模型池",
    value: "openai-compatible",
    apiBase: "",
    quotaScope: "relay-account",
    capabilities: ["text", "tool_calling", "structured_output"],
    modelEntryMode: "freeform",
    modelHelp: "中转站通常会混合多个厂商模型，请填写中转站后台显示的完整模型 ID。",
    models: [
      { label: "DeepSeek Chat", value: "deepseek-chat", capabilities: ["text", "tool_calling"] },
      { label: "DeepSeek Reasoner", value: "deepseek-reasoner", capabilities: ["text"] },
      { label: "Kimi K2 Turbo Preview", value: "kimi-k2-turbo-preview", capabilities: ["text", "tool_calling"] },
      { label: "Qwen Plus", value: "qwen-plus", capabilities: ["text", "tool_calling"] },
      { label: "Claude Sonnet 4", value: "claude-sonnet-4-20250514", capabilities: ["text", "tool_calling"] },
      { label: "GPT-5.6 Terra", value: "gpt-5.6-terra", capabilities: ["text", "tool_calling", "structured_output"] },
      { label: "MiniMax M1", value: "minimax-m1", capabilities: ["text"] },
    ],
  },
];

const ALL_CAPABILITIES = [
  { label: "文本", value: "text" },
  { label: "工具调用", value: "tool_calling" },
  { label: "结构化输出", value: "structured_output" },
];

function toPositiveNumber(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function toOptionalPositiveNumber(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function normalizeApiBase(value: string) {
  return value.trim().replace(CHAT_COMPLETIONS_SUFFIX, "").replace(/\/+$/, "");
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
  const [apiKey, setApiKey] = useState("");
  const [logicalModel, setLogicalModel] = useState("main");
  const [quotaScope, setQuotaScope] = useState(PROVIDERS[0].quotaScope);
  const [capabilities, setCapabilities] = useState<string[]>(PROVIDERS[0].models[0].capabilities);
  const [maxConcurrency, setMaxConcurrency] = useState("1");
  const [rpm, setRpm] = useState("60");
  const [tpm, setTpm] = useState("100000");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  const selectedProviderPreset = useMemo(
    () => PROVIDERS.find((item) => item.value === provider),
    [provider],
  );
  const modelOptions = selectedProviderPreset?.models ?? [];
  const isCustomProvider = provider === CUSTOM_PROVIDER;
  const isFreeformProvider = selectedProviderPreset?.modelEntryMode === "freeform";
  const isCustomModel = isCustomProvider || isFreeformProvider || selectedModel === CUSTOM_MODEL;

  const saveModel = useMutation({
    mutationFn: async () => {
      const resolvedProvider = isCustomProvider ? customProvider.trim() : provider;
      const resolvedModel = isCustomModel ? customModel.trim() : selectedModel;
      const secret = await api.createSecret(`${logicalModel.trim()} ${resolvedProvider}`, apiKey);
      const model = await api.createModel({
        provider: resolvedProvider,
        api_base: normalizeApiBase(apiBase),
        upstream_model: resolvedModel,
        logical_model: logicalModel.trim(),
        capabilities,
        credential_ref: secret.ref,
        quota_scope: quotaScope.trim() || `${resolvedProvider}-account`,
        max_concurrency: toPositiveNumber(maxConcurrency, 1),
        target_utilization: 0.8,
        reserved_capacity: 0,
        rpm: toOptionalPositiveNumber(rpm),
        tpm: toOptionalPositiveNumber(tpm),
        queue_timeout_seconds: 60,
        fallback: null,
        weight: 100,
      });
      return { model, secret };
    },
    onSuccess: async ({ secret }) => {
      setSaveMessage(`模型已通过可用性测试并保存，Key 引用：${secret.ref}`);
      setApiKey("");
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  function changeProvider(nextProvider: string) {
    setProvider(nextProvider);
    setSaveMessage(null);
    if (nextProvider === CUSTOM_PROVIDER) {
      setSelectedModel(CUSTOM_MODEL);
      setApiBase("");
      setQuotaScope("");
      setCapabilities(["text"]);
      return;
    }
    const preset = PROVIDERS.find((item) => item.value === nextProvider) ?? PROVIDERS[0];
    const defaultModel = preset.models[0];
    setSelectedModel(preset.modelEntryMode === "freeform" ? CUSTOM_MODEL : defaultModel.value);
    setCustomModel("");
    setApiBase(preset.apiBase);
    setQuotaScope(preset.quotaScope);
    setCapabilities(preset.modelEntryMode === "freeform" ? preset.capabilities : defaultModel.capabilities);
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
            <p>普通服务商会显示对应模型；中转站是混合模型池，直接填写中转站后台给出的完整模型 ID。</p>
          </div>
          <div>
            <span className="eyebrow">API Base / Key</span>
            <p>API Base 填服务商兼容 OpenAI 或 Anthropic 的接口地址；API Key 会加密保存，页面不会回显明文。</p>
          </div>
          <div>
            <span className="eyebrow">逻辑模型名</span>
            <p>Agent 只引用逻辑模型名，例如 main、planner、critic；以后换供应商不需要改角色配置。</p>
          </div>
          <div>
            <span className="eyebrow">并发与限流</span>
            <p>同一账号共用配额时保持相同 Quota Scope；新 Key 建议先从并发 1、RPM 60 开始，稳定后再提升。</p>
          </div>
        </div>
      </article>

      <form onSubmit={submit} aria-label="添加模型配置">
        <h3>添加模型配置</h3>
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

        <label htmlFor="api-key">API Key</label>
        <input
          id="api-key"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder="sk-..."
          required
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

        <label htmlFor="rpm">RPM</label>
        <input id="rpm" type="number" min="1" value={rpm} onChange={(event) => setRpm(event.target.value)} />

        <label htmlFor="tpm">TPM</label>
        <input id="tpm" type="number" min="1" value={tpm} onChange={(event) => setTpm(event.target.value)} />

        <button type="submit" disabled={saveModel.isPending}>
          {saveModel.isPending ? "测试并保存中..." : "测试并保存模型"}
        </button>
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
                  <td>{displaySaturationPolicy(model.saturation_policy)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
