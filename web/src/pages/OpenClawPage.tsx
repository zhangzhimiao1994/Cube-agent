import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  api,
  formatApiError,
  type OpenClawAdapter,
  type OpenClawOperation,
  type OpenClawOperationRequest,
  type OpenClawSession,
  type OpenClawSessionRequest,
  type SystemSettings,
} from "../api/client";

type OpenClawRemoteAdapterSetting = SystemSettings["openclaw_remote_adapters"][number];

const DEFAULT_OPENCLAW_REMOTE_ADAPTER: OpenClawRemoteAdapterSetting = {
  platform: "windows",
  target_type: "computer",
  target: "local-windows-pc",
  base_url: "http://127.0.0.1:8765",
  credential_ref: "secret://openclaw-local-adapter",
};

const OPENCLAW_POLICY_COPY: Record<SystemSettings["openclaw_mode"], { badge: string; summary: string; title: string }> = {
  ask: {
    badge: "人工审批",
    title: "默认审核",
    summary: "所有操作先进入待审批；批准后仍必须命中 allowlist，危险 shell 包装命令会被拦截。",
  },
  read_only: {
    badge: "禁止写操作",
    title: "只读模式",
    summary: "允许屏幕、文件读取等受控请求进入流程；命令执行和桌面写操作会被拒绝。",
  },
  auto_review: {
    badge: "低风险自动",
    title: "自动审核",
    summary: "只有低风险且精确命中 allowlist 的命令会自动批准；未命中仍等待人工审批。",
  },
  trusted_auto: {
    badge: "受信环境",
    title: "受信自动",
    summary: "适合已确认的本机或服务器适配器；执行仍受 allowlist、shell 拦截和会话状态限制。",
  },
};

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function parseStringList(value: string, fieldName: string): string[] {
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string" || item.trim() === "")) {
    throw new Error(`${fieldName} must be a JSON string array.`);
  }
  return parsed as string[];
}

function parseCommandList(value: string): string[][] {
  const parsed: unknown = JSON.parse(value);
  if (
    !Array.isArray(parsed) ||
    parsed.some(
      (command) =>
        !Array.isArray(command) ||
        command.length === 0 ||
        command.some((item) => typeof item !== "string" || item.trim() === ""),
    )
  ) {
    throw new Error("OpenClaw allowed commands must be a JSON array of argv arrays.");
  }
  return parsed as string[][];
}

function parseOpenClawRemoteAdapters(value: string): SystemSettings["openclaw_remote_adapters"] {
  const parsed: unknown = JSON.parse(value);
  const platforms = new Set(["linux", "windows", "macos"]);
  const targetTypes = new Set(["server", "computer", "desktop", "filesystem", "screen"]);
  if (
    !Array.isArray(parsed) ||
    parsed.some(
      (adapter) =>
        typeof adapter !== "object" ||
        adapter === null ||
        Array.isArray(adapter) ||
        !platforms.has(String((adapter as Record<string, unknown>).platform)) ||
        !targetTypes.has(String((adapter as Record<string, unknown>).target_type)) ||
        typeof (adapter as Record<string, unknown>).target !== "string" ||
        String((adapter as Record<string, unknown>).target).trim() === "" ||
        typeof (adapter as Record<string, unknown>).base_url !== "string" ||
        String((adapter as Record<string, unknown>).base_url).trim() === "" ||
        typeof (adapter as Record<string, unknown>).credential_ref !== "string" ||
        String((adapter as Record<string, unknown>).credential_ref).trim() === "",
    )
  ) {
    throw new Error("OpenClaw remote adapters must be JSON objects with platform, target_type, target, base_url, and credential_ref.");
  }
  return parsed as SystemSettings["openclaw_remote_adapters"];
}

function commandKey(command: string[]) {
  return JSON.stringify(command);
}

function commandLabel(command: string[]) {
  return command.join(" ");
}

function defaultSessionTarget(platform: OpenClawSessionRequest["platform"], targetType: OpenClawSessionRequest["target_type"]) {
  if (targetType === "server") return platform === "linux" ? "agent-hub-server" : "remote-server";
  if (targetType === "desktop") return "desktop";
  return platform === "windows" ? "local-windows-pc" : "local-computer";
}

function defaultSessionPurpose(platform: OpenClawSessionRequest["platform"], targetType: OpenClawSessionRequest["target_type"]) {
  if (targetType === "server") return `Keep a bounded OpenClaw control session for ${platform} server maintenance`;
  if (targetType === "desktop") return `Keep a bounded OpenClaw control session for ${platform} desktop operation`;
  return `Keep a bounded OpenClaw control session for ${platform} computer operation`;
}
function defaultOperationTarget(platform: OpenClawOperationRequest["platform"], kind: OpenClawOperationRequest["kind"]) {
  if (kind === "file_read") return platform === "windows" ? "C:\\Reports\\daily.txt" : "/var/log/syslog";
  if (kind === "screen_read") return "desktop";
  if (kind === "desktop_action") return "desktop";
  return platform === "linux" ? "agent-hub-server" : "local-computer";
}

function defaultOperationArgv(kind: OpenClawOperationRequest["kind"]) {
  return kind === "server_command" ? '["python", "--version"]' : "[]";
}

function parseOperationArgv(value: string, kind: OpenClawOperationRequest["kind"]): string[] {
  const argv = parseStringList(value, "OpenClaw argv");
  if (kind === "server_command" && argv.length === 0) {
    throw new Error("server_command requires a non-empty argv array.");
  }
  return argv;
}
function sortOpenClawAdapters(adapters: OpenClawAdapter[]) {
  const platformRank = new Map([
    ["linux", 0],
    ["windows", 1],
    ["macos", 2],
  ]);
  const kindRank = new Map([
    ["server_command", 0],
    ["desktop_action", 1],
    ["screen_read", 2],
    ["file_read", 3],
  ]);
  return [...adapters].sort(
    (left, right) =>
      (platformRank.get(left.platform) ?? 99) - (platformRank.get(right.platform) ?? 99) ||
      (kindRank.get(left.kind) ?? 99) - (kindRank.get(right.kind) ?? 99),
  );
}

export function OpenClawPage() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: () => api.settings() });
  const adaptersQuery = useQuery({ queryKey: ["openclaw-adapters"], queryFn: () => api.openClawAdapters() });
  const sessionsQuery = useQuery({ queryKey: ["openclaw-sessions"], queryFn: () => api.openClawSessions() });
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [allowedCommandsText, setAllowedCommandsText] = useState("[]");
  const [remoteAdaptersText, setRemoteAdaptersText] = useState("[]");
  const [adapterDraft, setAdapterDraft] = useState<OpenClawRemoteAdapterSetting>(DEFAULT_OPENCLAW_REMOTE_ADAPTER);
  const [sessionPlatform, setSessionPlatform] = useState<OpenClawSessionRequest["platform"]>("linux");
  const [sessionTargetType, setSessionTargetType] = useState<OpenClawSessionRequest["target_type"]>("server");
  const [sessionTarget, setSessionTarget] = useState("agent-hub-server");
  const [sessionPurpose, setSessionPurpose] = useState(defaultSessionPurpose("linux", "server"));
  const [operationPlatform, setOperationPlatform] = useState<OpenClawOperationRequest["platform"]>("linux");
  const [operationKind, setOperationKind] = useState<OpenClawOperationRequest["kind"]>("server_command");
  const [operationTarget, setOperationTarget] = useState("agent-hub-server");
  const [operationRiskLevel, setOperationRiskLevel] = useState<OpenClawOperationRequest["risk_level"]>("low");
  const [argvText, setArgvText] = useState('["python", "--version"]');
  const [reason, setReason] = useState("Manual OpenClaw operation from dedicated console");
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [operation, setOperation] = useState<OpenClawOperation | null>(null);
  const [executionOutput, setExecutionOutput] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQuery.data) {
      setSettings(settingsQuery.data);
      setAllowedCommandsText(formatJson(settingsQuery.data.openclaw_allowed_commands));
      setRemoteAdaptersText(formatJson(settingsQuery.data.openclaw_remote_adapters));
    }
  }, [settingsQuery.data]);

  const activeSessions = useMemo(
    () => (sessionsQuery.data ?? []).filter((session) => session.status === "active"),
    [sessionsQuery.data],
  );

  const configuredAdapters = useMemo(() => {
    try {
      return parseOpenClawRemoteAdapters(remoteAdaptersText);
    } catch {
      return [];
    }
  }, [remoteAdaptersText]);

  const allowedCommandPreview = useMemo(() => {
    try {
      return { commands: parseCommandList(allowedCommandsText), error: null };
    } catch (error) {
      return {
        commands: [],
        error: error instanceof Error ? error.message : "OpenClaw allowlist JSON 无法解析。",
      };
    }
  }, [allowedCommandsText]);

  const activePolicy = OPENCLAW_POLICY_COPY[settings?.openclaw_mode ?? "ask"];
  const shouldWarnEmptyAllowlist =
    (settings?.openclaw_mode === "auto_review" || settings?.openclaw_mode === "trusted_auto") &&
    !allowedCommandPreview.error &&
    allowedCommandPreview.commands.length === 0;

  useEffect(() => {
    setSelectedSessionId((currentSessionId) =>
      currentSessionId && activeSessions.some((session) => session.id === currentSessionId)
        ? currentSessionId
        : activeSessions[0]?.id ?? "",
    );
  }, [activeSessions]);

  function updateSettings(patch: Partial<SystemSettings>) {
    setSettings((currentSettings) => (currentSettings ? { ...currentSettings, ...patch } : currentSettings));
  }

  function updateAdapterDraft<K extends keyof OpenClawRemoteAdapterSetting>(
    field: K,
    value: OpenClawRemoteAdapterSetting[K],
  ) {
    setAdapterDraft((current) => ({ ...current, [field]: value }));
  }

  function replaceRemoteAdapters(next: OpenClawRemoteAdapterSetting[]) {
    setRemoteAdaptersText(formatJson(next));
    setLocalError(null);
  }

  function addRemoteAdapter() {
    let current: OpenClawRemoteAdapterSetting[];
    try {
      current = parseOpenClawRemoteAdapters(remoteAdaptersText);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "OpenClaw 远程适配器 JSON 无法解析。");
      return;
    }
    const adapter: OpenClawRemoteAdapterSetting = {
      platform: adapterDraft.platform,
      target_type: adapterDraft.target_type,
      target: adapterDraft.target.trim(),
      base_url: adapterDraft.base_url.trim(),
      credential_ref: adapterDraft.credential_ref.trim(),
    };
    if (!adapter.target || !adapter.base_url || !adapter.credential_ref) {
      setLocalError("请填写 OpenClaw 适配器目标、Base URL 和凭据引用。");
      return;
    }
    replaceRemoteAdapters([...current, adapter]);
    setAdapterDraft({ ...adapter, target: "local-windows-pc" });
  }

  function removeRemoteAdapter(index: number) {
    let current: OpenClawRemoteAdapterSetting[];
    try {
      current = parseOpenClawRemoteAdapters(remoteAdaptersText);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "OpenClaw 远程适配器 JSON 无法解析。");
      return;
    }
    replaceRemoteAdapters(current.filter((_, itemIndex) => itemIndex !== index));
  }

  function addOperationCommandToAllowlist() {
    let current: string[][];
    let argv: string[];
    try {
      current = parseCommandList(allowedCommandsText);
      argv = parseOperationArgv(argvText, "server_command");
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "OpenClaw 命令 JSON 无法解析。");
      return;
    }
    if (current.some((command) => commandKey(command) === commandKey(argv))) {
      setLocalError("当前控制台命令已在 allowlist 中。");
      return;
    }
    setAllowedCommandsText(formatJson([...current, argv]));
    setLocalError(null);
  }

  function clearAllowedCommands() {
    setAllowedCommandsText("[]");
    setLocalError(null);
  }

  const saveSettings = useMutation({
    mutationFn: async () => {
      if (!settings) throw new Error("设置尚未加载完成");
      setLocalError(null);
      return api.updateSettings({
        ...settings,
        openclaw_allowed_commands: parseCommandList(allowedCommandsText),
        openclaw_remote_adapters: parseOpenClawRemoteAdapters(remoteAdaptersText),
      });
    },
    onSuccess: async (saved) => {
      setSettings(saved);
      setAllowedCommandsText(formatJson(saved.openclaw_allowed_commands));
      setRemoteAdaptersText(formatJson(saved.openclaw_remote_adapters));
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => {
      if (error instanceof Error && !(error instanceof ApiError)) setLocalError(error.message);
    },
  });

  const createOperation = useMutation({
    mutationFn: async () => {
      setLocalError(null);
      setExecutionOutput(null);
      return api.createOpenClawOperation({
        platform: operationPlatform,
        kind: operationKind,
        target: operationTarget.trim(),
        argv: parseOperationArgv(argvText, operationKind),
        risk_level: operationRiskLevel,
        reason,
        ...(selectedSessionId ? { session_id: selectedSessionId } : {}),
      });
    },
    onSuccess: (nextOperation) => setOperation(nextOperation),
    onError: (error) => {
      if (error instanceof Error && !(error instanceof ApiError)) setLocalError(error.message);
    },
  });

  const approveOperation = useMutation({
    mutationFn: async () => {
      if (!operation) throw new Error("OpenClaw operation is not ready.");
      return api.resolveOpenClawOperation(operation.id, "approve");
    },
    onSuccess: (nextOperation) => setOperation(nextOperation),
  });

  const rejectOperation = useMutation({
    mutationFn: async () => {
      if (!operation) throw new Error("OpenClaw operation is not ready.");
      return api.resolveOpenClawOperation(operation.id, "reject");
    },
    onSuccess: (nextOperation) => setOperation(nextOperation),
  });

  const executeOperation = useMutation({
    mutationFn: async () => {
      if (!operation) throw new Error("OpenClaw operation is not ready.");
      return api.executeOpenClawOperation(operation.id);
    },
    onSuccess: (execution) => {
      setOperation(execution.operation);
      setExecutionOutput(execution.stdout || execution.stderr || `exit_code=${execution.exit_code}`);
    },
  });

  const createSession = useMutation({
    mutationFn: () => {
      setLocalError(null);
      const target = sessionTarget.trim();
      const purpose = sessionPurpose.trim();
      if (!target || !purpose) throw new Error("请填写 OpenClaw 会话目标和用途。");
      return api.createOpenClawSession({
        platform: sessionPlatform,
        target_type: sessionTargetType,
        target,
        purpose,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["openclaw-sessions"] });
    },
  });

  const updateSession = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "pause" | "resume" | "stop" }) => api.updateOpenClawSession(id, action),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["openclaw-sessions"] });
    },
  });

  function submitSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    saveSettings.mutate();
  }

  if (settingsQuery.isLoading || adaptersQuery.isLoading || sessionsQuery.isLoading) return <p>正在加载 OpenClaw 配置...</p>;
  if (settingsQuery.isError) return <p role="alert">{formatApiError(settingsQuery.error, "OpenClaw 设置加载失败")}</p>;
  if (adaptersQuery.isError) return <p role="alert">{formatApiError(adaptersQuery.error, "OpenClaw 适配器加载失败")}</p>;
  if (sessionsQuery.isError) return <p role="alert">{formatApiError(sessionsQuery.error, "OpenClaw 会话加载失败")}</p>;
  if (!settings) return <p role="alert">OpenClaw 设置加载失败：后端没有返回设置内容。</p>;

  const adapters = sortOpenClawAdapters(adaptersQuery.data ?? []);
  const sessions = sessionsQuery.data ?? [];

  return (
    <section>
      <p className="eyebrow">OpenClaw control</p>
      <h2>OpenClaw 控制</h2>
      <p>
        这里统一配置服务器命令、本机电脑接管、远程适配器、长时间控制会话和审批执行。所有操作仍受开关、权限模式、allowlist 和审计边界限制。
      </p>

      <div className="status-grid" aria-label="OpenClaw 状态">
        <article className="status-card">
          <span>功能开关</span>
          <p>{settings.openclaw_enabled ? "已启用" : "已关闭"}</p>
        </article>
        <article className="status-card">
          <span>权限模式</span>
          <p>{settings.openclaw_mode}</p>
        </article>
        <article className="status-card">
          <span>可用适配器</span>
          <p>{adapters.filter((adapter) => adapter.status === "available").length} / {adapters.length}</p>
        </article>
      </div>

      <form onSubmit={submitSettings} aria-label="保存 OpenClaw 设置" className="settings-form">
        <fieldset>
          <legend>权限与边界</legend>
          <label className="inline-check">
            <input
              type="checkbox"
              data-testid="openclaw-page-toggle"
              checked={settings.openclaw_enabled}
              onChange={(event) => updateSettings({ openclaw_enabled: event.target.checked })}
            />
            启用 OpenClaw 电脑/服务器操作
          </label>
          <label htmlFor="openclaw-page-mode">
            权限模式
            <select
              id="openclaw-page-mode"
              value={settings.openclaw_mode}
              onChange={(event) => updateSettings({ openclaw_mode: event.target.value as SystemSettings["openclaw_mode"] })}
            >
              <option value="ask">每次操作前审批</option>
              <option value="read_only">只读</option>
              <option value="auto_review">自动审核低风险操作</option>
              <option value="trusted_auto">受信环境自动执行</option>
            </select>
          </label>
          <div className="openclaw-policy-preview" role="region" aria-label="OpenClaw 审批策略预览">
            <div className="openclaw-policy-header">
              <strong>{activePolicy.title}</strong>
              <span>{activePolicy.badge}</span>
            </div>
            <p>{activePolicy.summary}</p>
            <p className="field-help">当前 allowlist：{allowedCommandPreview.commands.length} 条</p>
            {allowedCommandPreview.error ? <p role="alert">{allowedCommandPreview.error}</p> : null}
            {shouldWarnEmptyAllowlist ? <p className="openclaw-policy-warning">当前没有 allowlist，自动审核不会放行任何命令。</p> : null}
            {allowedCommandPreview.commands.length === 0 ? (
              <p className="field-help">还没有允许执行的命令。建议先在控制台写入测试 argv，再加入 allowlist。</p>
            ) : (
              <ul className="openclaw-allowlist-preview" aria-label="OpenClaw 命令 allowlist">
                {allowedCommandPreview.commands.slice(0, 6).map((command) => (
                  <li key={commandKey(command)}>{commandLabel(command)}</li>
                ))}
              </ul>
            )}
          </div>
          <label htmlFor="openclaw-page-allowed-commands">
            允许执行的命令 JSON
            <textarea
              id="openclaw-page-allowed-commands"
              data-testid="openclaw-page-allowed-commands"
              value={allowedCommandsText}
              onChange={(event) => setAllowedCommandsText(event.target.value)}
              spellCheck={false}
            />
            <small>只允许精确匹配的 argv；shell 包装命令仍会被拦截。</small>
          </label>
          <div className="action-row compact-actions">
            <button type="button" onClick={addOperationCommandToAllowlist}>添加当前控制台命令</button>
            <button type="button" className="danger-action" onClick={clearAllowedCommands}>清空命令 allowlist</button>
          </div>
        </fieldset>

        <fieldset>
          <legend>远程适配器</legend>
          <p className="field-help">Windows 电脑、桌面动作、屏幕读取和远程文件能力必须先接入 Adapter，再由 OpenClaw 审批执行。</p>
          <div className="form-grid">
            <label htmlFor="openclaw-page-adapter-platform">
              平台
              <select
                id="openclaw-page-adapter-platform"
                data-testid="openclaw-page-adapter-platform"
                value={adapterDraft.platform}
                onChange={(event) => updateAdapterDraft("platform", event.target.value as OpenClawRemoteAdapterSetting["platform"])}
              >
                <option value="windows">Windows</option>
                <option value="linux">Linux</option>
                <option value="macos">macOS</option>
              </select>
            </label>
            <label htmlFor="openclaw-page-adapter-target-type">
              目标类型
              <select
                id="openclaw-page-adapter-target-type"
                data-testid="openclaw-page-adapter-target-type"
                value={adapterDraft.target_type}
                onChange={(event) => updateAdapterDraft("target_type", event.target.value as OpenClawRemoteAdapterSetting["target_type"])}
              >
                <option value="computer">本机电脑</option>
                <option value="desktop">桌面</option>
                <option value="server">服务器</option>
                <option value="filesystem">文件系统</option>
                <option value="screen">屏幕</option>
              </select>
            </label>
            <label htmlFor="openclaw-page-adapter-target">
              目标名称
              <input
                id="openclaw-page-adapter-target"
                data-testid="openclaw-page-adapter-target"
                value={adapterDraft.target}
                onChange={(event) => updateAdapterDraft("target", event.target.value)}
              />
            </label>
            <label htmlFor="openclaw-page-adapter-base-url">
              Adapter Base URL
              <input
                id="openclaw-page-adapter-base-url"
                data-testid="openclaw-page-adapter-base-url"
                value={adapterDraft.base_url}
                onChange={(event) => updateAdapterDraft("base_url", event.target.value)}
              />
            </label>
            <label htmlFor="openclaw-page-adapter-credential-ref">
              凭据引用
              <input
                id="openclaw-page-adapter-credential-ref"
                data-testid="openclaw-page-adapter-credential-ref"
                value={adapterDraft.credential_ref}
                onChange={(event) => updateAdapterDraft("credential_ref", event.target.value)}
              />
            </label>
          </div>
          <div className="action-row">
            <button type="button" data-testid="openclaw-page-add-remote-adapter" onClick={addRemoteAdapter}>添加适配器</button>
          </div>
          {configuredAdapters.length === 0 ? (
            <p className="field-help">还没有登记远程适配器。Linux 服务器本地执行不需要填写这里。</p>
          ) : (
            <div className="table-shell">
              <table aria-label="OpenClaw 已配置远程适配器">
                <thead>
                  <tr><th>平台</th><th>目标</th><th>Base URL</th><th>凭据</th><th>操作</th></tr>
                </thead>
                <tbody>
                  {configuredAdapters.map((adapter, index) => (
                    <tr key={`${adapter.platform}-${adapter.target_type}-${adapter.target}-${index}`}>
                      <td>{adapter.platform}</td>
                      <td>{adapter.target_type} · {adapter.target}</td>
                      <td>{adapter.base_url}</td>
                      <td>{adapter.credential_ref}</td>
                      <td>
                        <button
                          type="button"
                          className="danger-action"
                          data-testid={`openclaw-page-remove-remote-adapter-${index}`}
                          onClick={() => removeRemoteAdapter(index)}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <details>
            <summary>高级 JSON</summary>
            <label htmlFor="openclaw-page-remote-adapters">
              远程适配器 JSON
              <textarea
                id="openclaw-page-remote-adapters"
                data-testid="openclaw-page-remote-adapters"
                value={remoteAdaptersText}
                onChange={(event) => setRemoteAdaptersText(event.target.value)}
                spellCheck={false}
              />
            </label>
          </details>
        </fieldset>

        {localError ? <p role="alert">{localError}</p> : null}
        {saveSettings.isError ? <p role="alert">{formatApiError(saveSettings.error, "OpenClaw 设置保存失败")}</p> : null}
        {saveSettings.isSuccess ? <p role="status">OpenClaw 设置已保存</p> : null}
        <button type="submit" data-testid="save-openclaw-settings" disabled={saveSettings.isPending}>保存 OpenClaw 设置</button>
      </form>

      <div className="inline-guide" aria-label="OpenClaw 适配器状态">
        <h3>适配器状态</h3>
        <div className="openclaw-adapter-grid">
          {adapters.map((adapter) => (
            <article key={`${adapter.platform}-${adapter.kind}`} className={`openclaw-adapter-card openclaw-adapter-${adapter.status}`}>
              <div className="openclaw-adapter-header">
                <strong>{adapter.platform} {adapter.kind}</strong>
                <span>{adapter.status}</span>
              </div>
              <p>{adapter.description}</p>
              <dl>
                <div><dt>host</dt><dd>{adapter.execution_host}</dd></div>
                <div><dt>approval</dt><dd>{adapter.requires_user_approval ? "required" : "not required"}</dd></div>
                <div><dt>read-only</dt><dd>{adapter.supports_read_only ? "supported" : "not supported"}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      </div>

      <div className="inline-guide" aria-label="OpenClaw 控制会话">
        <h3>控制会话</h3>
        <p>会话用于登记长时间控制意图。服务器、本机电脑和桌面会话都必须经过 OpenClaw 策略和适配器状态约束。</p>
        <div className="form-grid">
          <label htmlFor="openclaw-page-session-platform">
            会话平台
            <select
              id="openclaw-page-session-platform"
              value={sessionPlatform}
              onChange={(event) => {
                const nextPlatform = event.target.value as OpenClawSessionRequest["platform"];
                setSessionPlatform(nextPlatform);
                setSessionTarget(defaultSessionTarget(nextPlatform, sessionTargetType));
                setSessionPurpose(defaultSessionPurpose(nextPlatform, sessionTargetType));
              }}
            >
              <option value="linux">Linux</option>
              <option value="windows">Windows</option>
              <option value="macos">macOS</option>
            </select>
          </label>
          <label htmlFor="openclaw-page-session-target-type">
            会话目标类型
            <select
              id="openclaw-page-session-target-type"
              value={sessionTargetType}
              onChange={(event) => {
                const nextTargetType = event.target.value as OpenClawSessionRequest["target_type"];
                setSessionTargetType(nextTargetType);
                setSessionTarget(defaultSessionTarget(sessionPlatform, nextTargetType));
                setSessionPurpose(defaultSessionPurpose(sessionPlatform, nextTargetType));
              }}
            >
              <option value="server">服务器</option>
              <option value="computer">电脑</option>
              <option value="desktop">桌面</option>
            </select>
          </label>
          <label htmlFor="openclaw-page-session-target">
            会话目标
            <input id="openclaw-page-session-target" value={sessionTarget} onChange={(event) => setSessionTarget(event.target.value)} />
          </label>
          <label htmlFor="openclaw-page-session-purpose">
            会话用途
            <input id="openclaw-page-session-purpose" value={sessionPurpose} onChange={(event) => setSessionPurpose(event.target.value)} />
          </label>
        </div>
        <div className="action-row">
          <button type="button" data-testid="openclaw-page-create-session" disabled={createSession.isPending} onClick={() => createSession.mutate()}>
            创建控制会话
          </button>
        </div>
        {sessions.length === 0 ? (
          <p className="field-help">还没有 OpenClaw 控制会话。</p>
        ) : (
          <div className="table-shell">
            <table aria-label="OpenClaw 控制会话">
              <thead>
                <tr><th>会话</th><th>状态</th><th>目标</th><th>执行端</th><th>操作</th></tr>
              </thead>
              <tbody>
                {sessions.map((session: OpenClawSession) => (
                  <tr key={session.id}>
                    <td>{session.id}</td>
                    <td>{session.status}</td>
                    <td>{session.platform} {session.target_type} {session.target}</td>
                    <td>{session.execution_host}</td>
                    <td>
                      <div className="action-row compact-actions">
                        <button type="button" data-testid={`openclaw-page-pause-session-${session.id}`} disabled={session.status !== "active" || updateSession.isPending} onClick={() => updateSession.mutate({ id: session.id, action: "pause" })}>暂停</button>
                        <button type="button" data-testid={`openclaw-page-resume-session-${session.id}`} disabled={session.status !== "paused" || updateSession.isPending} onClick={() => updateSession.mutate({ id: session.id, action: "resume" })}>恢复</button>
                        <button type="button" className="danger-action" data-testid={`openclaw-page-stop-session-${session.id}`} disabled={session.status === "stopped" || updateSession.isPending} onClick={() => updateSession.mutate({ id: session.id, action: "stop" })}>停止</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {createSession.isError ? <p role="alert">{formatApiError(createSession.error, "OpenClaw 会话创建失败")}</p> : null}
        {updateSession.isError ? <p role="alert">{formatApiError(updateSession.error, "OpenClaw 会话更新失败")}</p> : null}
      </div>

      <div className="inline-guide" aria-label="OpenClaw 操作控制台">
        <h3>审批执行控制台</h3>
        <div className="form-grid">
          <label htmlFor="openclaw-page-operation-platform">
            操作平台
            <select
              id="openclaw-page-operation-platform"
              value={operationPlatform}
              onChange={(event) => {
                const nextPlatform = event.target.value as OpenClawOperationRequest["platform"];
                setOperationPlatform(nextPlatform);
                setOperationTarget(defaultOperationTarget(nextPlatform, operationKind));
              }}
            >
              <option value="linux">Linux</option>
              <option value="windows">Windows</option>
              <option value="macos">macOS</option>
            </select>
          </label>
          <label htmlFor="openclaw-page-operation-kind">
            操作类型
            <select
              id="openclaw-page-operation-kind"
              value={operationKind}
              onChange={(event) => {
                const nextKind = event.target.value as OpenClawOperationRequest["kind"];
                setOperationKind(nextKind);
                setOperationTarget(defaultOperationTarget(operationPlatform, nextKind));
                setArgvText(defaultOperationArgv(nextKind));
              }}
            >
              <option value="server_command">服务器/终端命令</option>
              <option value="desktop_action">桌面动作</option>
              <option value="screen_read">屏幕读取</option>
              <option value="file_read">文件读取</option>
            </select>
          </label>
          <label htmlFor="openclaw-page-operation-target">
            操作目标
            <input id="openclaw-page-operation-target" value={operationTarget} onChange={(event) => setOperationTarget(event.target.value)} />
          </label>
          <label htmlFor="openclaw-page-operation-risk">
            风险等级
            <select
              id="openclaw-page-operation-risk"
              value={operationRiskLevel}
              onChange={(event) => setOperationRiskLevel(event.target.value as OpenClawOperationRequest["risk_level"])}
            >
              <option value="low">低</option>
              <option value="medium">中</option>
              <option value="high">高</option>
            </select>
          </label>
        </div>
        <label htmlFor="openclaw-page-operation-argv">
          操作参数 JSON
          <textarea id="openclaw-page-operation-argv" data-testid="openclaw-page-operation-argv" value={argvText} onChange={(event) => setArgvText(event.target.value)} spellCheck={false} />
          <small>server_command 必须填写精确 argv；桌面、屏幕和文件能力可填 []，由对应 Adapter 的受控驱动执行。</small>
        </label>
        <label htmlFor="openclaw-page-operation-reason">
          执行原因
          <input id="openclaw-page-operation-reason" value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <label htmlFor="openclaw-page-operation-session">
          控制会话
          <select id="openclaw-page-operation-session" data-testid="openclaw-page-operation-session" value={selectedSessionId} onChange={(event) => setSelectedSessionId(event.target.value)}>
            <option value="">不绑定会话</option>
            {activeSessions.map((session) => <option key={session.id} value={session.id}>{session.id} - {session.target}</option>)}
          </select>
        </label>
        <div className="action-row">
          <button type="button" data-testid="openclaw-page-create-operation" disabled={createOperation.isPending} onClick={() => createOperation.mutate()}>申请审批</button>
          <button type="button" data-testid="openclaw-page-approve-operation" disabled={!operation || operation.status !== "waiting_user_approval" || approveOperation.isPending} onClick={() => approveOperation.mutate()}>批准</button>
          <button type="button" data-testid="openclaw-page-reject-operation" disabled={!operation || operation.status !== "waiting_user_approval" || rejectOperation.isPending} onClick={() => rejectOperation.mutate()}>拒绝</button>
          <button type="button" data-testid="openclaw-page-execute-operation" disabled={!operation || operation.status !== "approved" || executeOperation.isPending} onClick={() => executeOperation.mutate()}>执行</button>
        </div>
        {operation ? <p role="status">OpenClaw operation {operation.id}: {operation.status}</p> : null}
        {executionOutput ? <pre data-testid="openclaw-page-execution-output">{executionOutput}</pre> : null}
        {createOperation.isError ? <p role="alert">{formatApiError(createOperation.error, "OpenClaw 操作申请失败")}</p> : null}
        {approveOperation.isError ? <p role="alert">{formatApiError(approveOperation.error, "OpenClaw 审批失败")}</p> : null}
        {rejectOperation.isError ? <p role="alert">{formatApiError(rejectOperation.error, "OpenClaw 拒绝失败")}</p> : null}
        {executeOperation.isError ? <p role="alert">{formatApiError(executeOperation.error, "OpenClaw 执行失败")}</p> : null}
      </div>
    </section>
  );
}
