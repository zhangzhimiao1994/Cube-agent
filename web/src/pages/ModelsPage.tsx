import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { ApiError, api, formatApiError, type ModelDeployment } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

const CUSTOM_PROVIDER = "custom";
const CUSTOM_MODEL = "__custom_model__";
const CHAT_COMPLETIONS_SUFFIX = /\/chat\/completions\/?$/i;

type ApiProtocol = "openai_compatible" | "anthropic_messages";
type ModelCategory = "normal" | "multimedia";

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
  defaultLogicalModel?: string;
  defaultMaxConcurrency?: number;
  defaultRpm?: number;
  defaultTpm?: number;
  label: string;
  modelHelp?: string;
  modelEntryMode?: "catalog" | "freeform";
  models: ModelPreset[];
  providerValue?: string;
  quotaScope: string;
  value: string;
};

const NORMAL_PROVIDERS: ProviderPreset[] = [
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

const MULTIMEDIA_PROVIDERS: ProviderPreset[] = [
  {
    label: "OpenAI Sora",
    value: "openai-sora",
    providerValue: "openai",
    apiBase: "https://api.openai.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "openai-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelHelp: "Sora 视频 API 使用 /v1/videos，不是聊天补全接口；保存后由多媒体执行器调用。",
    models: [
      { label: "Sora 2", value: "sora-2", capabilities: ["video_generation"] },
      { label: "Sora 2 Pro", value: "sora-2-pro", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "OpenAI Audio",
    value: "openai-audio",
    providerValue: "openai",
    apiBase: "https://api.openai.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "openai-audio-account",
    defaultLogicalModel: "audio_primary",
    capabilities: ["audio_generation"],
    modelHelp: "OpenAI Audio 用于语音、音频等生成能力，不作为聊天模型保存。",
    models: [
      { label: "GPT-4o Mini TTS", value: "gpt-4o-mini-tts", capabilities: ["audio_generation"] },
      { label: "TTS 1", value: "tts-1", capabilities: ["audio_generation"] },
      { label: "TTS 1 HD", value: "tts-1-hd", capabilities: ["audio_generation"] },
    ],
  },
  {
    label: "MiniMax Hailuo",
    value: "minimax-hailuo",
    providerValue: "minimax",
    apiBase: "https://api.minimaxi.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "minimax-video-account",
    defaultLogicalModel: "video_primary",
    defaultRpm: 3,
    capabilities: ["video_generation"],
    concurrencyHelp: "MiniMax Hailuo 视频按用户要求默认每天 3 条；并发建议保持 1。",
    models: [
      { label: "MiniMax Hailuo 02", value: "MiniMax-Hailuo-02", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "MiniMax Audio",
    value: "minimax-audio",
    providerValue: "minimax",
    apiBase: "https://api.minimaxi.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "minimax-audio-account",
    defaultLogicalModel: "audio_primary",
    defaultRpm: 30,
    capabilities: ["audio_generation"],
    modelHelp: "MiniMax 音频模型可复用同账号 Key；这里保存为 audio_generation 能力，由多媒体执行器调用。",
    models: [
      { label: "Speech 2.8 Turbo", value: "speech-2.8-turbo", capabilities: ["audio_generation"] },
      { label: "Speech 2.8 HD", value: "speech-2.8-hd", capabilities: ["audio_generation"] },
      { label: "Speech 2.6 Turbo", value: "speech-2.6-turbo", capabilities: ["audio_generation"] },
      { label: "Speech 2.6 HD", value: "speech-2.6-hd", capabilities: ["audio_generation"] },
      { label: "Speech 02 Turbo", value: "speech-02-turbo", capabilities: ["audio_generation"] },
      { label: "Speech 02 HD", value: "speech-02-hd", capabilities: ["audio_generation"] },
    ],
  },
  {
    label: "Google Veo",
    value: "google-veo",
    providerValue: "google",
    apiBase: "https://generativelanguage.googleapis.com/v1beta",
    apiProtocol: "openai_compatible",
    quotaScope: "google-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelHelp: "Veo 3.1 通过 Gemini generateContent 视频接口调用，保存后由对应多媒体适配器执行。",
    models: [
      { label: "Veo 3.1 Preview", value: "veo-3.1-generate-preview", capabilities: ["video_generation"] },
      { label: "Veo 3.1 Fast Preview", value: "veo-3.1-fast-generate-preview", capabilities: ["video_generation"] },
      { label: "Veo 3.1 Lite Preview", value: "veo-3.1-lite-generate-preview", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "Runway",
    value: "runway",
    providerValue: "runway",
    apiBase: "https://api.dev.runwayml.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "runway-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    models: [
      { label: "Runway Gen-4.5", value: "gen4.5", capabilities: ["video_generation"] },
      { label: "Runway Gen-4 Turbo", value: "gen4_turbo", capabilities: ["video_generation"] },
      { label: "Runway Seedance 2", value: "seedance2", capabilities: ["video_generation"] },
      { label: "Runway Seedance 2 Fast", value: "seedance2_fast", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "Kling",
    value: "kling",
    providerValue: "kling",
    apiBase: "https://api.klingai.com/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "kling-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelEntryMode: "freeform",
    modelHelp: "Kling API 模型名和接口版本以控制台为准；这里保存为视频生成模型。",
    models: [
      { label: "Kling 3.0", value: "kling-3.0", capabilities: ["video_generation"] },
      { label: "Kling 2.5 Turbo", value: "kling-2.5-turbo", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "Luma Ray",
    value: "luma",
    providerValue: "luma",
    apiBase: "https://api.lumalabs.ai/dream-machine/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "luma-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    models: [
      { label: "Ray 2", value: "ray-2", capabilities: ["video_generation"] },
      { label: "Ray 2 Flash", value: "ray-flash-2", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "阿里百炼 Token Plan 多媒体",
    value: "alibaba-token-plan-media",
    providerValue: "qwen-token-plan",
    apiBase: "",
    apiProtocol: "openai_compatible",
    quotaScope: "qwen-token-plan-media-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelEntryMode: "freeform",
    modelHelp: "Token Plan 多媒体模型请填写控制台“我的订阅 / API Key”里的专属 Base URL；模型名可填 happyhorse、wan、qwen-image 或音频系列。",
    models: [
      { label: "HappyHorse 1.1 文生视频", value: "happyhorse-1.1-t2v", capabilities: ["video_generation"] },
      { label: "HappyHorse 1.1 图生视频", value: "happyhorse-1.1-i2v", capabilities: ["video_generation"] },
      { label: "HappyHorse 1.1 参考生视频", value: "happyhorse-1.1-r2v", capabilities: ["video_generation"] },
      { label: "Qwen Image 2.0", value: "qwen-image-2.0", capabilities: ["image_generation"] },
      { label: "Qwen Image 2.0 Pro", value: "qwen-image-2.0-pro", capabilities: ["image_generation"] },
      { label: "Wan 2.7 Image", value: "wan2.7-image", capabilities: ["image_generation"] },
      { label: "Qwen TTS", value: "qwen-tts", capabilities: ["audio_generation"] },
      { label: "CosyVoice", value: "cosyvoice-v2", capabilities: ["audio_generation"] },
      { label: "Sambert", value: "sambert", capabilities: ["audio_generation"] },
    ],
  },
  {
    label: "阿里 Wan / 通义万相",
    value: "alibaba-wan",
    providerValue: "dashscope",
    apiBase: "https://dashscope.aliyuncs.com/api/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "dashscope-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelHelp: "Wan 视频生成是异步任务接口；如使用业务空间专属域名，请替换 API Base。",
    models: [
      { label: "Wan 2.7 文生视频", value: "wan2.7-t2v-2026-06-12", capabilities: ["video_generation"] },
      { label: "Wan 2.7 图生视频", value: "wan2.7-i2v-2026-04-25", capabilities: ["video_generation"] },
      { label: "Wan 2.7 参考生视频", value: "wan2.7-r2v-2026-06-12", capabilities: ["video_generation"] },
      { label: "Wan 2.2 T2V Plus", value: "wan2.2-t2v-plus", capabilities: ["video_generation"] },
    ],
  },
  {
    label: "阿里音频 / CosyVoice",
    value: "alibaba-audio",
    providerValue: "dashscope",
    apiBase: "https://dashscope.aliyuncs.com/api/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "dashscope-audio-account",
    defaultLogicalModel: "audio_primary",
    capabilities: ["audio_generation"],
    modelEntryMode: "freeform",
    modelHelp: "阿里音频模型包括 Qwen-TTS、CosyVoice、Sambert 等；具体模型 ID 请以百炼控制台为准。",
    models: [
      { label: "Qwen TTS", value: "qwen-tts", capabilities: ["audio_generation"] },
      { label: "CosyVoice V2", value: "cosyvoice-v2", capabilities: ["audio_generation"] },
      { label: "Sambert", value: "sambert", capabilities: ["audio_generation"] },
    ],
  },
  {
    label: "ElevenLabs Audio",
    value: "elevenlabs-audio",
    providerValue: "elevenlabs",
    apiBase: "https://api.elevenlabs.io/v1",
    apiProtocol: "openai_compatible",
    quotaScope: "elevenlabs-audio-account",
    defaultLogicalModel: "audio_primary",
    capabilities: ["audio_generation"],
    models: [
      { label: "Eleven Multilingual V2", value: "eleven_multilingual_v2", capabilities: ["audio_generation"] },
      { label: "Eleven Flash V2.5", value: "eleven_flash_v2_5", capabilities: ["audio_generation"] },
      { label: "Eleven Turbo V2.5", value: "eleven_turbo_v2_5", capabilities: ["audio_generation"] },
    ],
  },
  {
    label: "Seedance",
    value: "seedance",
    providerValue: "seedance",
    apiBase: "",
    apiProtocol: "openai_compatible",
    quotaScope: "seedance-video-account",
    defaultLogicalModel: "video_primary",
    capabilities: ["video_generation"],
    modelEntryMode: "freeform",
    modelHelp: "Seedance API 入口和模型名按你的平台控制台填写；也可以通过 Runway 的 Seedance 预设配置。",
    models: [
      { label: "Seedance 2", value: "seedance2", capabilities: ["video_generation"] },
      { label: "Seedance 2 Fast", value: "seedance2_fast", capabilities: ["video_generation"] },
    ],
  },
];

const NORMAL_CAPABILITIES = [
  { label: "文本", value: "text" },
  { label: "图片理解", value: "vision" },
  { label: "语音理解", value: "audio" },
  { label: "工具调用", value: "tool_calling" },
  { label: "结构化输出", value: "structured_output" },
];

const MULTIMEDIA_CAPABILITIES = [
  { label: "图片生成", value: "image_generation" },
  { label: "视频生成", value: "video_generation" },
  { label: "音频生成", value: "audio_generation" },
];

const ALL_CAPABILITIES = [...NORMAL_CAPABILITIES, ...MULTIMEDIA_CAPABILITIES];

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

function findPresetForSavedModel(model: ModelDeployment): (ProviderPreset & { category: ModelCategory }) | null {
  const allPresets: Array<ProviderPreset & { category: ModelCategory }> = [
    ...NORMAL_PROVIDERS.map((preset) => ({ ...preset, category: "normal" as const })),
    ...MULTIMEDIA_PROVIDERS.map((preset) => ({ ...preset, category: "multimedia" as const })),
  ];
  return (
    allPresets.find(
      (preset) =>
        (preset.providerValue ?? preset.value) === model.provider &&
        preset.models.some((item) => item.value === model.upstream_model),
    ) ??
    allPresets.find((preset) => (preset.providerValue ?? preset.value) === model.provider) ??
    null
  );
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

type ModelSortKey = "category" | "logical" | "provider" | "upstream" | "apiBase" | "capabilities" | "slots" | "quota" | "policy";

type ModelColumnFilters = {
  apiBase: string;
  capabilities: string;
  category: "all" | ModelCategory;
  logical: string;
  provider: string;
  quota: string;
  upstream: string;
};

const EMPTY_MODEL_FILTERS: ModelColumnFilters = {
  apiBase: "",
  capabilities: "",
  category: "all",
  logical: "",
  provider: "",
  quota: "",
  upstream: "",
};

function savedModelCategory(model: ModelDeployment): ModelCategory {
  return model.capabilities.some((item) => item === "image_generation" || item === "video_generation" || item === "audio_generation")
    ? "multimedia"
    : "normal";
}

function savedModelCategoryLabel(model: ModelDeployment) {
  return savedModelCategory(model) === "multimedia" ? "多媒体 AI" : "普通模型";
}

function modelCapabilitiesText(model: ModelDeployment) {
  return model.capabilities.map(displayCapability).join("、");
}

function matchesModelSearch(model: ModelDeployment, searchTerm: string) {
  return textContains(
    [
      savedModelCategoryLabel(model),
      model.logical_model,
      model.provider,
      model.upstream_model,
      model.api_base,
      modelCapabilitiesText(model),
      model.quota_scope,
      displaySaturationPolicy(model.saturation_policy),
    ].join(" "),
    searchTerm,
  );
}

function matchesModelColumns(model: ModelDeployment, filters: ModelColumnFilters) {
  return (
    (filters.category === "all" || savedModelCategory(model) === filters.category) &&
    textContains(model.logical_model, filters.logical) &&
    textContains(model.provider, filters.provider) &&
    textContains(model.upstream_model, filters.upstream) &&
    textContains(model.api_base, filters.apiBase) &&
    textContains(modelCapabilitiesText(model), filters.capabilities) &&
    textContains(model.quota_scope, filters.quota)
  );
}

function sortedSavedModels(models: ModelDeployment[], sort: SortState<ModelSortKey>) {
  const copy = [...models];
  if (false) return copy;
  const direction = sort.direction === "asc" ? 1 : -1;
  return copy.sort((left, right) => {
    let result = 0;
    if (sort.key === "category") result = compareText(savedModelCategoryLabel(left), savedModelCategoryLabel(right), "asc");
    if (sort.key === "logical") result = compareText(left.logical_model, right.logical_model, "asc");
    if (sort.key === "provider") result = compareText(left.provider, right.provider, "asc");
    if (sort.key === "upstream") result = compareText(left.upstream_model, right.upstream_model, "asc");
    if (sort.key === "apiBase") result = compareText(left.api_base, right.api_base, "asc");
    if (sort.key === "capabilities") result = compareText(modelCapabilitiesText(left), modelCapabilitiesText(right), "asc");
    if (sort.key === "slots") result = left.effective_slots - right.effective_slots;
    if (sort.key === "quota") result = compareText(left.quota_scope, right.quota_scope, "asc");
    if (sort.key === "policy") result = compareText(displaySaturationPolicy(left.saturation_policy), displaySaturationPolicy(right.saturation_policy), "asc");
    return sort.direction === "asc" ? result : -result;
  });
}

export function ModelsPage() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const [modelCategory, setModelCategory] = useState<ModelCategory>("normal");
  const [provider, setProvider] = useState(NORMAL_PROVIDERS[0].value);
  const [customProvider, setCustomProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState(NORMAL_PROVIDERS[0].models[0].value);
  const [customModel, setCustomModel] = useState("");
  const [apiBase, setApiBase] = useState(NORMAL_PROVIDERS[0].apiBase);
  const [apiProtocol, setApiProtocol] = useState<ApiProtocol>(NORMAL_PROVIDERS[0].apiProtocol);
  const [apiKey, setApiKey] = useState("");
  const [logicalModel, setLogicalModel] = useState("main");
  const [quotaScope, setQuotaScope] = useState(NORMAL_PROVIDERS[0].quotaScope);
  const [capabilities, setCapabilities] = useState<string[]>(NORMAL_PROVIDERS[0].models[0].capabilities);
  const [maxConcurrency, setMaxConcurrency] = useState("1");
  const [rpm, setRpm] = useState("60");
  const [tpm, setTpm] = useState("100000");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [editingModel, setEditingModel] = useState<ModelDeployment | null>(null);
  const [modelSearchTerm, setModelSearchTerm] = useState("");
  const [modelColumnFilters, setModelColumnFilters] = useState<ModelColumnFilters>(EMPTY_MODEL_FILTERS);
  const [modelSort, setModelSort] = useState<SortState<ModelSortKey>>({ key: "logical", direction: "asc" });

  const availableProviders = modelCategory === "normal" ? NORMAL_PROVIDERS : MULTIMEDIA_PROVIDERS;
  const capabilityOptions = modelCategory === "normal" ? NORMAL_CAPABILITIES : MULTIMEDIA_CAPABILITIES;
  const selectedProviderPreset = useMemo(
    () => availableProviders.find((item) => item.value === provider),
    [availableProviders, provider],
  );
  const modelOptions = selectedProviderPreset?.models ?? [];
  const isCustomProvider = provider === CUSTOM_PROVIDER;
  const isFreeformProvider = selectedProviderPreset?.modelEntryMode === "freeform";
  const isCustomModel = isCustomProvider || isFreeformProvider || selectedModel === CUSTOM_MODEL;
  const canChooseProtocol = isCustomProvider || isFreeformProvider;

  function resetModelForm(nextCategory: ModelCategory = "normal") {
    const presets = nextCategory === "normal" ? NORMAL_PROVIDERS : MULTIMEDIA_PROVIDERS;
    const preset = presets[0];
    const defaultModel = preset.models[0];
    setModelCategory(nextCategory);
    setProvider(preset.value);
    setCustomProvider("");
    setSelectedModel(defaultModel.value);
    setCustomModel("");
    setApiBase(preset.apiBase);
    setApiProtocol(preset.apiProtocol);
    setApiKey("");
    setLogicalModel(preset.defaultLogicalModel ?? "main");
    setQuotaScope(preset.quotaScope);
    setCapabilities(defaultModel.capabilities);
    setMaxConcurrency(String(preset.defaultMaxConcurrency ?? 1));
    setRpm(String(preset.defaultRpm ?? 60));
    setTpm(String(preset.defaultTpm ?? 100000));
    setEditingModel(null);
  }

  const saveModel = useMutation({
    mutationFn: async () => {
      const resolvedProvider = isCustomProvider ? customProvider.trim() : selectedProviderPreset?.providerValue ?? provider;
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
    onSuccess: async ({ model, credentialRef }) => {
      setSaveMessage(`模型已通过可用性测试并${editingModel ? "更新" : "保存"}，Key 引用：${credentialRef}`);
      queryClient.setQueryData<ModelDeployment[]>(["models"], (current) => {
        if (!current) return [model];
        const existingIndex = current.findIndex((item) => item.id === model.id);
        if (existingIndex === -1) return [...current, model];
        return current.map((item, index) => (index === existingIndex ? model : item));
      });
      resetModelForm();
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
      setCapabilities(modelCategory === "normal" ? ["text"] : ["video_generation"]);
      setLogicalModel(modelCategory === "normal" ? "main" : "video_primary");
      setMaxConcurrency("1");
      setRpm("60");
      setTpm("100000");
      return;
    }
    const preset = availableProviders.find((item) => item.value === nextProvider) ?? availableProviders[0];
    const defaultModel = preset.models[0];
    setSelectedModel(preset.modelEntryMode === "freeform" ? CUSTOM_MODEL : defaultModel.value);
    setCustomModel("");
    setApiBase(preset.apiBase);
    setApiProtocol(preset.apiProtocol);
    setQuotaScope(preset.quotaScope);
    setCapabilities(preset.modelEntryMode === "freeform" ? preset.capabilities : defaultModel.capabilities);
    setLogicalModel(preset.defaultLogicalModel ?? (modelCategory === "normal" ? "main" : "video_primary"));
    setMaxConcurrency(String(preset.defaultMaxConcurrency ?? 1));
    setRpm(String(preset.defaultRpm ?? 60));
    setTpm(String(preset.defaultTpm ?? 100000));
  }

  function changeModelCategory(nextCategory: ModelCategory) {
    setSaveMessage(null);
    setEditingModel(null);
    resetModelForm(nextCategory);
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
    const preset = findPresetForSavedModel(model);
    setEditingModel(model);
    setSaveMessage(null);
    if (preset) {
      setModelCategory(preset.category);
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
      const inferredCategory = model.capabilities.some((item) =>
        item === "image_generation" || item === "video_generation" || item === "audio_generation"
      )
        ? "multimedia"
        : "normal";
      setModelCategory(inferredCategory);
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

  function updateModelColumnFilter<Key extends keyof ModelColumnFilters>(key: Key, value: ModelColumnFilters[Key]) {
    setModelColumnFilters((current) => ({ ...current, [key]: value }));
  }

  if (models.isLoading) return <p>加载模型...</p>;
  if (models.isError) {
    return <p role="alert">{formatApiError(models.error, "模型加载失败")}</p>;
  }

  const savedModels = models.data ?? [];
  const filteredSavedModels = savedModels.filter((model) => matchesModelSearch(model, modelSearchTerm) && matchesModelColumns(model, modelColumnFilters));
  const visibleSavedModels = sortedSavedModels(filteredSavedModels, modelSort);
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
        <p>先选择模型大类：普通模型用于对话、图片/语音理解、工具调用和结构化输出；多媒体 AI 用于图片、视频和音频生成。</p>

        <label htmlFor="model-category">模型大类</label>
        <select
          id="model-category"
          value={modelCategory}
          onChange={(event) => changeModelCategory(event.target.value as ModelCategory)}
        >
          <option value="normal">普通模型</option>
          <option value="multimedia">多媒体 AI</option>
        </select>

        <label htmlFor="provider">服务商</label>
        <select id="provider" value={provider} onChange={(event) => changeProvider(event.target.value)}>
          {availableProviders.map((item) => (
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

        {selectedProviderPreset?.modelHelp ? <p>{selectedProviderPreset.modelHelp}</p> : null}

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
          {capabilityOptions.map((capability) => (
            <label key={capability.value}>
              <input
                value={capability.value}
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
          <>
            <div className="list-toolbar">
              <label>
                快速搜索模型
                <input
                  type="search"
                  aria-label="快速搜索模型"
                  value={modelSearchTerm}
                  onChange={(event) => setModelSearchTerm(event.currentTarget.value)}
                  placeholder="逻辑模型、服务商、能力或 Quota"
                />
              </label>
              <button type="button" className="secondary-action" onClick={() => { setModelSearchTerm(""); setModelColumnFilters(EMPTY_MODEL_FILTERS); }}>
                清空筛选
              </button>
            </div>
            {visibleSavedModels.length === 0 ? (
              <article>
                <h4>当前筛选没有匹配模型</h4>
                <p>调整列筛选或清空筛选查看全部模型。</p>
              </article>
            ) : (
              <table aria-label="已保存模型列表">
                <thead>
                  <tr>
                    <th><SortHeader column="category" label="类别" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>类别</SortHeader></th>
                    <th><SortHeader column="logical" label="逻辑模型" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>逻辑模型</SortHeader></th>
                    <th><SortHeader column="provider" label="服务商" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>服务商</SortHeader></th>
                    <th><SortHeader column="upstream" label="上游模型" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>上游模型</SortHeader></th>
                    <th><SortHeader column="apiBase" label="API Base" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>API Base</SortHeader></th>
                    <th><SortHeader column="capabilities" label="能力" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>能力</SortHeader></th>
                    <th><SortHeader column="slots" label="有效并发" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>有效并发</SortHeader></th>
                    <th>限流</th>
                    <th><SortHeader column="quota" label="Quota Scope" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>Quota Scope</SortHeader></th>
                    <th>操作</th>
                    <th><SortHeader column="policy" label="策略" sort={modelSort} onSort={(column) => setModelSort((current) => nextSortState(current, column))}>策略</SortHeader></th>
                  </tr>
                  <tr className="table-filter-row">
                    <th>
                      <select aria-label="按模型类别筛选" value={modelColumnFilters.category} onChange={(event) => updateModelColumnFilter("category", event.currentTarget.value as ModelColumnFilters["category"])}>
                        <option value="all">全部</option>
                        <option value="normal">普通模型</option>
                        <option value="multimedia">多媒体 AI</option>
                      </select>
                    </th>
                    <th><input aria-label="按逻辑模型筛选" value={modelColumnFilters.logical} onChange={(event) => updateModelColumnFilter("logical", event.currentTarget.value)} placeholder="逻辑模型" /></th>
                    <th><input aria-label="按服务商筛选" value={modelColumnFilters.provider} onChange={(event) => updateModelColumnFilter("provider", event.currentTarget.value)} placeholder="服务商" /></th>
                    <th><input aria-label="按上游模型筛选" value={modelColumnFilters.upstream} onChange={(event) => updateModelColumnFilter("upstream", event.currentTarget.value)} placeholder="上游模型" /></th>
                    <th><input aria-label="按 API Base 筛选" value={modelColumnFilters.apiBase} onChange={(event) => updateModelColumnFilter("apiBase", event.currentTarget.value)} placeholder="API Base" /></th>
                    <th><input aria-label="按模型能力筛选" value={modelColumnFilters.capabilities} onChange={(event) => updateModelColumnFilter("capabilities", event.currentTarget.value)} placeholder="能力" /></th>
                    <th aria-label="有效并发筛选占位" />
                    <th aria-label="限流筛选占位" />
                    <th><input aria-label="按 Quota Scope 筛选" value={modelColumnFilters.quota} onChange={(event) => updateModelColumnFilter("quota", event.currentTarget.value)} placeholder="Quota Scope" /></th>
                    <th aria-label="模型操作筛选占位" />
                    <th aria-label="策略筛选占位" />
                  </tr>
                </thead>
                <tbody>
                  {visibleSavedModels.map((model) => (
                    <tr key={model.id}>
                      <td>{savedModelCategoryLabel(model)}</td>
                      <td>{model.logical_model}</td>
                      <td>{model.provider}</td>
                      <td>{model.upstream_model}</td>
                      <td>{model.api_base}</td>
                      <td>{modelCapabilitiesText(model)}</td>
                      <td>{model.effective_slots}</td>
                      <td>
                        RPM {model.rpm ?? "未设置"} / TPM {model.tpm ?? "未设置"}
                      </td>
                      <td>{model.quota_scope}</td>
                      <td className="table-actions">
                        <button type="button" data-testid={`edit-model-${model.id}`} onClick={() => editSavedModel(model)}>
                          编辑模型
                        </button>
                        <button
                          type="button"
                          className="danger-button"
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
          </>
        )}
        {deleteModel.isError ? <p role="alert">{formatApiError(deleteModel.error, "模型删除失败")}</p> : null}
      </section>
    </section>
  );
}

