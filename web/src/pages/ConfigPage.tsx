import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  api,
  formatApiError,
  type ConfigRevision,
  type SystemSettings,
} from "../api/client";

type EditableConfig = {
  models: Record<string, unknown>;
  agents: unknown[];
};

const EMPTY_CONFIG: EditableConfig = { models: {}, agents: [] };
function formatDocument(document: EditableConfig) {
  return `${JSON.stringify(document, null, 2)}\n`;
}

function parseConfigJson(value: string): EditableConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    const message = error instanceof Error ? error.message : "格式错误";
    throw new Error(`JSON 解析失败：${message}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("JSON 校验失败：配置根节点必须是对象。");
  }
  const document = parsed as Record<string, unknown>;
  if (typeof document.models !== "object" || document.models === null || Array.isArray(document.models)) {
    throw new Error("配置校验失败：models 必须是对象。");
  }
  if (!Array.isArray(document.agents)) {
    throw new Error("配置校验失败：agents 必须是数组。");
  }
  return {
    models: document.models as Record<string, unknown>,
    agents: document.agents,
  };
}

function currentOrEmpty(revision: ConfigRevision | undefined, error: unknown) {
  if (revision) return revision.document;
  if (error instanceof ApiError && error.status === 404) return EMPTY_CONFIG;
  return null;
}

function toggle(list: string[], value: string) {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

export function ConfigPage() {
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["config-current"], queryFn: () => api.currentConfig() });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: () => api.settings() });
  const agentsQuery = useQuery({ queryKey: ["agents"], queryFn: () => api.agents() });
  const workflowsQuery = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const document = useMemo(
    () => currentOrEmpty(current.data, current.error),
    [current.data, current.error],
  );
  const [json, setJson] = useState(formatDocument(EMPTY_CONFIG));
  const [localError, setLocalError] = useState<string | null>(null);
  const [published, setPublished] = useState<string | null>(null);
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [settingsLocalError, setSettingsLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (document) setJson(formatDocument(document));
  }, [document]);

  useEffect(() => {
    if (settingsQuery.data) {
      setSettings(settingsQuery.data);
    }
  }, [settingsQuery.data]);

  const saveSettings = useMutation({
    mutationFn: async () => {
      if (!settings) throw new Error("设置尚未加载完成");
      setSettingsLocalError(null);
      return api.updateSettings(settings);
    },
    onSuccess: async (saved) => {
      setSettings(saved);
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => {
      if (error instanceof Error && !(error instanceof ApiError)) {
        setSettingsLocalError(error.message);
      }
    },
  });

  const publish = useMutation({
    mutationFn: async () => {
      setLocalError(null);
      setPublished(null);
      const parsed = parseConfigJson(json);
      const draft = await api.createConfigDraft(parsed);
      return api.publishConfigDraft(draft.id);
    },
    onSuccess: async (revision) => {
      setPublished(`已发布配置版本 ${revision.version}`);
      await queryClient.invalidateQueries({ queryKey: ["config-current"] });
      await queryClient.invalidateQueries({ queryKey: ["models"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => {
      if (error instanceof Error && !(error instanceof ApiError)) {
        setLocalError(error.message);
      }
    },
  });

  if (
    current.isLoading ||
    settingsQuery.isLoading ||
    agentsQuery.isLoading ||
    workflowsQuery.isLoading ||
    modelsQuery.isLoading
  ) {
    return <p>正在加载系统设置...</p>;
  }
  if (current.isError && !(current.error instanceof ApiError && current.error.status === 404)) {
    return <p role="alert">{formatApiError(current.error, "生产配置加载失败")}</p>;
  }
  if (settingsQuery.isError) return <p role="alert">{formatApiError(settingsQuery.error, "系统设置加载失败")}</p>;
  if (agentsQuery.isError) return <p role="alert">{formatApiError(agentsQuery.error, "Agent 列表加载失败")}</p>;
  if (workflowsQuery.isError) return <p role="alert">{formatApiError(workflowsQuery.error, "工作流列表加载失败")}</p>;
  if (modelsQuery.isError) return <p role="alert">{formatApiError(modelsQuery.error, "模型列表加载失败")}</p>;
  if (!settings) return <p role="alert">系统设置加载失败：后端没有返回设置内容。</p>;

  const agents = agentsQuery.data ?? [];
  const workflows = workflowsQuery.data ?? [];
  const modelCount = Object.keys(document?.models ?? {}).length || (modelsQuery.data ?? []).length;
  const agentCount = document?.agents.length || agents.length;

  function updateSettings(patch: Partial<SystemSettings>) {
    setSettings((currentSettings) => (currentSettings ? { ...currentSettings, ...patch } : currentSettings));
  }

  function submitSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    saveSettings.mutate();
  }

  return (
    <section>
      <p className="eyebrow">Settings center</p>
      <h2>系统设置</h2>
      <p>
        这里是面向生产环境的配置中心。常用配置直接在本页完成；模型、Agent、工作流、通道等复杂配置可以从下方入口进入专门页面。
      </p>

      <div className="status-grid" aria-label="配置状态">
        <article className="status-card">
          <span>当前发布版本</span>
          <p>{current.data ? `版本 ${current.data.version}` : "暂无已发布配置"}</p>
        </article>
        <article className="status-card">
          <span>模型数量</span>
          <p>{modelCount}</p>
        </article>
        <article className="status-card">
          <span>Agent 数量</span>
          <p>{agentCount}</p>
        </article>
      </div>

      <div className="status-grid" aria-label="核心能力设置">
        <article className="status-card">
          <span>实时调度</span>
          <p>
            默认模式：{settings.default_mode}；
            {settings.allow_main_agent_override ? "允许主 Agent 临场调整" : "严格按预设执行"}。
          </p>
        </article>
        <article className="status-card">
          <span>工具防护</span>
          <p>
            {settings.safe_tools_enabled ? "已允许非危险工具" : "未允许非危险工具"}；
            {settings.require_approval_for_tools ? "高风险工具必须审批" : "高风险工具未强制审批"}。
          </p>
        </article>
        <article className="status-card">
          <span>Hermes 学习</span>
          <p>{settings.hermes_enabled ? "已启用经验沉淀，但不绕过审批。" : "已关闭经验沉淀。"}</p>
        </article>
        <article className="status-card">
          <span>Vibe Coding</span>
          <p>{settings.vibe_coding_enabled ? "已允许对话内代码协作。" : "已关闭对话内代码协作。"}</p>
        </article>
        <article className="status-card">
          <span>多媒体生成</span>
          <p>{settings.multimedia_generation_enabled ? "已允许图片、视频和音频生成。" : "已关闭图片、视频和音频生成。"}</p>
        </article>
        <article className="status-card">
          <span>OpenClaw</span>
          <p>{settings.openclaw_enabled ? `已启用，权限模式：${settings.openclaw_mode}。` : "已关闭长时间电脑操作能力。"}</p>
        </article>
      </div>

      <div className="settings-shortcuts">
        <Link className="settings-shortcut-card" to="/models">配置模型与 API Key</Link>
        <Link className="settings-shortcut-card" to="/openclaw">配置 OpenClaw</Link>
        <Link className="settings-shortcut-card" to="/agents">配置 Agent 角色</Link>
        <Link className="settings-shortcut-card" to="/workflows">配置工作流</Link>
        <Link className="settings-shortcut-card" to="/channels">配置聊天通道</Link>
        <Link className="settings-shortcut-card" to="/logs">查看错误日志</Link>
      </div>

      <form onSubmit={submitSettings} aria-label="保存系统设置" className="settings-form">
        <h3>运行默认值</h3>
        <div className="form-grid">
          <label htmlFor="default-mode">
            默认运行模式
            <select
              id="default-mode"
              value={settings.default_mode}
              onChange={(event) => updateSettings({ default_mode: event.target.value as SystemSettings["default_mode"] })}
            >
              <option value="auto">自动识别</option>
              <option value="direct">直接执行</option>
              <option value="dispatch">派单式</option>
              <option value="discuss">讨论式</option>
              <option value="hybrid">混合式</option>
            </select>
          </label>
          <label htmlFor="default-workflow">
            默认工作流
            <select
              id="default-workflow"
              value={settings.default_workflow_id ?? ""}
              onChange={(event) => updateSettings({ default_workflow_id: event.target.value || null })}
            >
              <option value="">不固定，由任务选择</option>
              {workflows.map((workflow) => (
                <option key={workflow.id} value={workflow.id}>
                  {workflow.name}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="log-level">
            日志收集等级
            <select
              id="log-level"
              value={settings.log_level}
              onChange={(event) => updateSettings({ log_level: event.target.value as SystemSettings["log_level"] })}
            >
              <option value="warning">warning：只收集警告和错误</option>
              <option value="error">error：只收集错误</option>
            </select>
          </label>
        </div>

        <fieldset>
          <legend>默认参与角色</legend>
          {agents.length === 0 ? (
            <p className="field-help">还没有 Agent。请先进入 Agent 页面创建角色。</p>
          ) : (
            agents.map((agent) => (
              <label key={agent.id} className="inline-check">
                <input
                  type="checkbox"
                  checked={settings.default_agent_ids.includes(agent.id)}
                  onChange={() => updateSettings({ default_agent_ids: toggle(settings.default_agent_ids, agent.id) })}
                />
                {agent.name}（{agent.id}）
              </label>
            ))
          )}
        </fieldset>

        <h3>安全与学习</h3>
        <fieldset>
          <legend>生产安全策略</legend>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={settings.hermes_enabled}
              onChange={(event) => updateSettings({ hermes_enabled: event.target.checked })}
            />
            启用 Hermes 学习，但不绕过审批
          </label>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={settings.safe_tools_enabled}
              onChange={(event) => updateSettings({ safe_tools_enabled: event.target.checked })}
            />
            允许非危险工具操作
          </label>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={settings.require_approval_for_tools}
              onChange={(event) => updateSettings({ require_approval_for_tools: event.target.checked })}
            />
            高风险工具调用必须审批
          </label>
          <label className="inline-check">
            <input
              type="checkbox"
              data-testid="vibe-coding-toggle"
              checked={settings.vibe_coding_enabled}
              onChange={(event) => updateSettings({ vibe_coding_enabled: event.target.checked })}
            />
            Vibe Coding 对话能力开关
          </label>
          <label className="inline-check">
            <input
              type="checkbox"
              data-testid="multimedia-generation-toggle"
              checked={settings.multimedia_generation_enabled}
              onChange={(event) => updateSettings({ multimedia_generation_enabled: event.target.checked })}
            />
            多媒体生成开关
          </label>
          <div className="inline-guide" role="region" aria-label="OpenClaw 配置入口">
            <h4>OpenClaw 控制</h4>
            <p>
              当前{settings.openclaw_enabled ? `已启用，权限模式：${settings.openclaw_mode}` : "已关闭"}；
              allowlist {settings.openclaw_allowed_commands.length} 条，远程适配器 {settings.openclaw_remote_adapters.length} 个。
              跨平台电脑/服务器接管、会话、审批和执行统一在独立 OpenClaw 控制页管理。
            </p>
            <Link className="secondary-action" to="/openclaw">打开 OpenClaw 控制</Link>
          </div>
        </fieldset>
        <h3>主 Agent 全局临场策略</h3>
        <p className="field-help">
          这里控制所有工作流共用的调度边界。工作流只定义模板；主 Agent 能不能临场调整、能不能申请临时子 Agent，
          都在这里统一开关。开启后仍必须先向用户核对，不能静默改工作流。
        </p>
        <fieldset>
          <legend>临场调度边界</legend>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={settings.allow_main_agent_override}
              onChange={(event) => updateSettings({ allow_main_agent_override: event.target.checked })}
            />
            允许主 Agent 提出临场调整，执行前必须向用户核对
          </label>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={settings.allow_temporary_agents}
              onChange={(event) => updateSettings({ allow_temporary_agents: event.target.checked })}
            />
            允许主 Agent 在能力不足时申请临时子 Agent
          </label>
        </fieldset>
        <label htmlFor="temporary-agent-policy">
          临时 Agent 补位规则
          <textarea
            id="temporary-agent-policy"
            value={settings.temporary_agent_policy}
            onChange={(event) => updateSettings({ temporary_agent_policy: event.target.value })}
          />
          <small>
            建议写清：什么时候可以申请、必须说明哪些信息、是否允许任务后永久化。该规则会写入运行调度说明。
          </small>
        </label>

        <label htmlFor="channel-entry">
          默认交互入口
          <select
            id="channel-entry"
            value={settings.channel_entry}
            onChange={(event) => updateSettings({ channel_entry: event.target.value })}
          >
            <option value="web">网页工作台</option>
            <option value="feishu">飞书</option>
            <option value="dingtalk">钉钉</option>
            <option value="wecom_bot">企业微信机器人</option>
            <option value="telegram">Telegram</option>
            <option value="slack">Slack</option>
            <option value="custom_webhook">自定义 Webhook</option>
          </select>
        </label>

        <h3>附件存储</h3>
        <p className="field-help">
          网页上传、通道收到的文件和图片都会进入附件存储；请按服务器磁盘容量设置生命周期，避免附件长期堆积。
        </p>
        <div className="form-grid">
          <label htmlFor="attachment-retention-days">
            附件保留天数
            <input
              id="attachment-retention-days"
              type="number"
              min={1}
              max={365}
              value={settings.attachment_retention_days}
              onChange={(event) => updateSettings({ attachment_retention_days: Number(event.target.value) })}
            />
          </label>
          <label htmlFor="attachment-max-mb">
            单个附件最大 MB
            <input
              id="attachment-max-mb"
              type="number"
              min={1}
              max={200}
              value={settings.attachment_max_mb}
              onChange={(event) => updateSettings({ attachment_max_mb: Number(event.target.value) })}
            />
          </label>
        </div>

        <button type="submit" data-testid="save-system-settings" disabled={saveSettings.isPending}>
          {saveSettings.isPending ? "正在保存设置..." : "保存系统设置"}
        </button>
        {saveSettings.isSuccess ? <p role="status">系统设置已保存，并会在后续任务提交时作为默认值使用。</p> : null}
        {settingsLocalError ? <p role="alert">{settingsLocalError}</p> : null}
        {saveSettings.isError ? <p role="alert">{formatApiError(saveSettings.error, "系统设置保存失败")}</p> : null}
      </form>

      <details className="inline-guide">
        <summary>高级：直接编辑生产配置 JSON</summary>
        <p>
          只有需要批量调整或排错时才建议编辑这里。普通模型、Agent、工作流和通道配置请优先使用对应页面，避免手写字段出错。
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            publish.mutate();
          }}
          aria-label="发布生产配置"
        >
          <label htmlFor="config-json">配置 JSON</label>
          <textarea
            id="config-json"
            value={json}
            onChange={(event) => {
              setJson(event.target.value);
              setLocalError(null);
              setPublished(null);
            }}
            spellCheck={false}
          />
          <button type="submit" disabled={publish.isPending}>
            {publish.isPending ? "正在创建草稿并发布..." : "创建草稿并发布"}
          </button>
          {published ? <p role="status">{published}</p> : null}
          {localError ? <p role="alert">{localError}</p> : null}
          {publish.isError && !localError ? (
            <p role="alert">{formatApiError(publish.error, "配置发布失败")}</p>
          ) : null}
        </form>
      </details>
    </section>
  );
}
