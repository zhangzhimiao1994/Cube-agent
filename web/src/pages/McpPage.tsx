import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import {
  api,
  formatApiError,
  type CapabilityManifestItem,
  type McpServer,
  type PluginResource,
} from "../api/client";
import { useAuth } from "../auth/AuthProvider";

function parseCsv(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function fillFromServer(server: McpServer) {
  return {
    id: server.id,
    name: server.name,
    transport: server.transport,
    command: server.command ?? "",
    args: server.args.join(","),
    url: server.url ?? "",
    executableAllowlist: server.executable_allowlist.join(","),
    domainAllowlist: server.domain_allowlist.join(","),
    allowedTools: server.allowed_tools.join(","),
    timeoutSeconds: String(server.timeout_seconds || 10),
  };
}

function fillFromPlugin(plugin: PluginResource) {
  const capability = plugin.capabilities[0];
  return {
    id: plugin.id,
    name: plugin.name,
    description: plugin.description ?? "",
    enabled: plugin.enabled,
    endpointUrl: plugin.endpoint_url ?? "",
    domainAllowlist: plugin.domain_allowlist.join(","),
    timeoutSeconds: String(plugin.timeout_seconds || 10),
    credentialRef: plugin.credential_ref ?? "",
    credentialHeader: plugin.credential_header || "X-Plugin-Credential",
    credentialScheme: plugin.credential_scheme,
    capabilityId: capability?.id ?? "",
    capabilityAdapter: capability?.adapter ?? "http_json",
    permissionClass: capability?.permission_class ?? "plugin.use",
    sandboxProfile: capability?.sandbox_profile ?? "remote_connector",
    replaySafe: capability?.replay_safe ?? false,
    aliases: capability?.aliases.join(",") ?? "",
  };
}

function capabilityKindLabel(kind: string) {
  if (kind === "builtin") return "内置";
  if (kind === "skill") return "Skill";
  if (kind === "mcp") return "MCP";
  if (kind === "plugin") return "插件";
  return kind;
}

function availabilityLabel(capability: CapabilityManifestItem) {
  return capability.available ? "可用" : "不可用";
}

function approvalLabel(capability: CapabilityManifestItem) {
  return capability.replay_safe ? "无需审批" : "运行时策略";
}

export function McpPage() {
  const auth = useAuth();
  const canReadCapabilityManifest = auth.hasPermission("plugin:read");
  const canWritePlugins = auth.hasPermission("plugin:write");
  const queryClient = useQueryClient();
  const servers = useQuery({ queryKey: ["mcp"], queryFn: () => api.mcpServers() });
  const plugins = useQuery({
    queryKey: ["plugins", auth.user?.tenant_id, auth.user?.user_id, auth.user?.role, canReadCapabilityManifest],
    queryFn: () => api.plugins(),
    enabled: canReadCapabilityManifest,
  });
  const capabilityManifest = useQuery({
    queryKey: [
      "capability-manifest",
      auth.user?.tenant_id,
      auth.user?.user_id,
      auth.user?.role,
      canReadCapabilityManifest,
    ],
    queryFn: () => api.capabilityManifest(),
    enabled: canReadCapabilityManifest,
  });
  const visibleCapabilityManifest = canReadCapabilityManifest ? capabilityManifest.data : undefined;
  const [serverId, setServerId] = useState("filesystem");
  const [name, setName] = useState("Filesystem MCP");
  const [transport, setTransport] = useState("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [executableAllowlist, setExecutableAllowlist] = useState("");
  const [domainAllowlist, setDomainAllowlist] = useState("");
  const [allowedTools, setAllowedTools] = useState("read_file,list_directory");
  const [timeoutSeconds, setTimeoutSeconds] = useState("10");
  const [message, setMessage] = useState<string | null>(null);
  const [pluginId, setPluginId] = useState("calendar");
  const [pluginName, setPluginName] = useState("Calendar HTTP");
  const [pluginDescription, setPluginDescription] = useState("");
  const [pluginEnabled, setPluginEnabled] = useState(true);
  const [pluginEndpointUrl, setPluginEndpointUrl] = useState("");
  const [pluginDomainAllowlist, setPluginDomainAllowlist] = useState("");
  const [pluginTimeoutSeconds, setPluginTimeoutSeconds] = useState("10");
  const [pluginCredentialRef, setPluginCredentialRef] = useState("");
  const [pluginCredentialHeader, setPluginCredentialHeader] = useState("X-Plugin-Credential");
  const [pluginCredentialScheme, setPluginCredentialScheme] = useState("Bearer");
  const [pluginCapabilityId, setPluginCapabilityId] = useState("calendar.create_event");
  const [pluginCapabilityAdapter, setPluginCapabilityAdapter] = useState("http_json");
  const [pluginPermissionClass, setPluginPermissionClass] = useState("plugin.use");
  const [pluginSandboxProfile, setPluginSandboxProfile] = useState("remote_connector");
  const [pluginReplaySafe, setPluginReplaySafe] = useState(false);
  const [pluginAliases, setPluginAliases] = useState("");
  const [pluginMessage, setPluginMessage] = useState<string | null>(null);

  async function refreshPluginSurfaces() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["plugins"] }),
      queryClient.invalidateQueries({ queryKey: ["capability-manifest"] }),
    ]);
  }

  const saveServer = useMutation({
    mutationFn: () =>
      api.createMcpServer({
        id: serverId.trim(),
        name: name.trim(),
        transport,
        command: transport === "stdio" ? command.trim() || null : null,
        args: transport === "stdio" ? parseCsv(args) : [],
        url: transport === "stdio" ? null : url.trim() || null,
        executable_allowlist:
          transport === "stdio" ? parseCsv(executableAllowlist || command) : [],
        domain_allowlist: transport === "stdio" ? [] : parseCsv(domainAllowlist),
        allowed_tools: parseCsv(allowedTools),
        timeout_seconds: Number(timeoutSeconds) || 10,
      }),
    onSuccess: async () => {
      setMessage("MCP 配置已保存。运行时只会暴露允许列表里的工具。");
      await queryClient.invalidateQueries({ queryKey: ["mcp"] });
      await queryClient.invalidateQueries({ queryKey: ["capability-manifest"] });
    },
  });
  const deleteServer = useMutation({
    mutationFn: (id: string) => api.deleteMcpServer(id),
    onSuccess: async () => {
      setMessage("MCP 配置已删除。");
      await queryClient.invalidateQueries({ queryKey: ["mcp"] });
      await queryClient.invalidateQueries({ queryKey: ["capability-manifest"] });
    },
  });
  const savePlugin = useMutation({
    mutationFn: () =>
      api.createPlugin({
        id: pluginId.trim(),
        name: pluginName.trim(),
        enabled: pluginEnabled,
        description: pluginDescription.trim() || null,
        version: "local",
        endpoint_url: pluginEndpointUrl.trim() || null,
        domain_allowlist: parseCsv(pluginDomainAllowlist),
        timeout_seconds: Number(pluginTimeoutSeconds) || 10,
        credential_ref: pluginCredentialRef.trim() || null,
        credential_header: pluginCredentialHeader.trim() || "X-Plugin-Credential",
        credential_scheme: pluginCredentialScheme.trim(),
        capabilities: [
          {
            id: pluginCapabilityId.trim(),
            adapter: pluginCapabilityAdapter.trim() || "http_json",
            permission_class: pluginPermissionClass.trim() || "plugin.use",
            sandbox_profile: pluginSandboxProfile.trim() || "remote_connector",
            replay_safe: pluginReplaySafe,
            aliases: parseCsv(pluginAliases),
          },
        ],
      }),
    onSuccess: async () => {
      setPluginMessage("插件配置已保存。运行时能力注册表会重新加载。");
      await refreshPluginSurfaces();
    },
  });
  const pluginLifecycle = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "start" | "stop" | "reload" | "delete" }) => {
      if (action === "start") return api.startPlugin(id);
      if (action === "stop") return api.stopPlugin(id);
      if (action === "reload") return api.reloadPlugin(id);
      return api.deletePlugin(id);
    },
    onSuccess: async (_result, variables) => {
      const messages = {
        start: "插件已启动。",
        stop: "插件已停止。",
        reload: "插件已重载。",
        delete: "插件已删除。",
      };
      setPluginMessage(messages[variables.action]);
      await refreshPluginSurfaces();
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveServer.mutate();
  }

  function submitPlugin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPluginMessage(null);
    savePlugin.mutate();
  }

  function edit(server: McpServer) {
    const next = fillFromServer(server);
    setServerId(next.id);
    setName(next.name);
    setTransport(next.transport);
    setCommand(next.command);
    setArgs(next.args);
    setUrl(next.url);
    setExecutableAllowlist(next.executableAllowlist);
    setDomainAllowlist(next.domainAllowlist);
    setAllowedTools(next.allowedTools);
    setTimeoutSeconds(next.timeoutSeconds);
    setMessage(`已载入 ${server.name}，修改后点击保存。`);
  }

  function editPlugin(plugin: PluginResource) {
    const next = fillFromPlugin(plugin);
    setPluginId(next.id);
    setPluginName(next.name);
    setPluginDescription(next.description);
    setPluginEnabled(next.enabled);
    setPluginEndpointUrl(next.endpointUrl);
    setPluginDomainAllowlist(next.domainAllowlist);
    setPluginTimeoutSeconds(next.timeoutSeconds);
    setPluginCredentialRef(next.credentialRef);
    setPluginCredentialHeader(next.credentialHeader);
    setPluginCredentialScheme(next.credentialScheme);
    setPluginCapabilityId(next.capabilityId);
    setPluginCapabilityAdapter(next.capabilityAdapter);
    setPluginPermissionClass(next.permissionClass);
    setPluginSandboxProfile(next.sandboxProfile);
    setPluginReplaySafe(next.replaySafe);
    setPluginAliases(next.aliases);
    setPluginMessage(`已载入 ${plugin.name}，修改后点击保存。`);
  }

  function confirmDelete(server: McpServer) {
    if (!window.confirm(`确定删除 MCP「${server.name}」吗？删除后 Agent 不能再调用它的工具。`)) return;
    deleteServer.mutate(server.id);
  }

  function confirmPluginDelete(plugin: PluginResource) {
    if (!window.confirm(`确定删除插件「${plugin.name}」吗？删除后 Agent 不能再调用它的能力。`)) return;
    pluginLifecycle.mutate({ id: plugin.id, action: "delete" });
  }

  if (servers.isLoading) return <p>正在加载 MCP 工具...</p>;
  if (servers.isError) {
    return <p role="alert">{formatApiError(servers.error, "MCP 工具加载失败")}</p>;
  }

  const items = servers.data ?? [];
  const pluginItems = canReadCapabilityManifest ? plugins.data ?? [] : [];
  const isStdio = transport === "stdio";

  return (
    <section>
      <p className="eyebrow">MCP governance</p>
      <h2>MCP 工具</h2>
      <p>
        MCP 和插件用来接入文件、浏览器、数据库或外部系统。这里配置的是生产连接参数：
        本地 stdio 需要命令和可执行白名单，远程能力需要 HTTPS URL 和域名白名单。
      </p>

      <section aria-label="运行时能力注册表">
        <h3>运行时能力注册表</h3>
        {!canReadCapabilityManifest ? <p className="field-help">当前账号无权查看运行时能力。</p> : null}
        {capabilityManifest.isLoading ? <p>正在加载运行时能力...</p> : null}
        {capabilityManifest.isError ? (
          <p role="alert">{formatApiError(capabilityManifest.error, "运行时能力加载失败")}</p>
        ) : null}
        {visibleCapabilityManifest && visibleCapabilityManifest.capabilities.length === 0 ? (
          <article>
            <h4>还没有运行时能力</h4>
            <p>当前租户暂未暴露可调用能力；确认运行时网关、Skill 存储和工具注册表配置。</p>
          </article>
        ) : null}
        {visibleCapabilityManifest && visibleCapabilityManifest.capabilities.length > 0 ? (
          <div className="table-shell">
            <table aria-label="运行时能力注册表" className="dense-table">
              <thead>
                <tr>
                  <th>能力</th>
                  <th>类型</th>
                  <th>适配器</th>
                  <th>权限</th>
                  <th>沙箱</th>
                  <th>状态</th>
                  <th>审批</th>
                  <th>别名</th>
                </tr>
              </thead>
              <tbody>
                {visibleCapabilityManifest.capabilities.map((capability) => (
                  <tr key={capability.id}>
                    <td><strong>{capability.id}</strong></td>
                    <td>{capabilityKindLabel(capability.kind)}</td>
                    <td>{capability.adapter}</td>
                    <td>{capability.permission_class}</td>
                    <td>{capability.sandbox_profile}</td>
                    <td>
                      {availabilityLabel(capability)}
                      {capability.availability_reason ? (
                        <p className="field-help">{capability.availability_reason}</p>
                      ) : null}
                    </td>
                    <td>{approvalLabel(capability)}</td>
                    <td>{capability.aliases.join(", ") || "无"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <div className="two-column">
        <form onSubmit={submitPlugin} aria-label="保存插件">
          <h3>新增或更新插件</h3>
          <label htmlFor="plugin-id">插件 ID</label>
          <input
            id="plugin-id"
            value={pluginId}
            onChange={(event) => setPluginId(event.target.value)}
            placeholder="例如 calendar"
            required
          />

          <label htmlFor="plugin-name">插件名称</label>
          <input
            id="plugin-name"
            value={pluginName}
            onChange={(event) => setPluginName(event.target.value)}
            required
          />

          <label htmlFor="plugin-enabled">
            <input
              id="plugin-enabled"
              type="checkbox"
              checked={pluginEnabled}
              onChange={(event) => setPluginEnabled(event.target.checked)}
            />
            启用插件
          </label>

          <label htmlFor="plugin-description">描述</label>
          <textarea
            id="plugin-description"
            value={pluginDescription}
            onChange={(event) => setPluginDescription(event.target.value)}
            placeholder="可选，用来记录插件用途"
          />

          <label htmlFor="plugin-endpoint">HTTP Endpoint</label>
          <input
            id="plugin-endpoint"
            value={pluginEndpointUrl}
            onChange={(event) => setPluginEndpointUrl(event.target.value)}
            placeholder="https://plugins.example/invoke"
          />

          <label htmlFor="plugin-domain-allowlist">允许域名，英文逗号分隔</label>
          <textarea
            id="plugin-domain-allowlist"
            value={pluginDomainAllowlist}
            onChange={(event) => setPluginDomainAllowlist(event.target.value)}
            placeholder="plugins.example"
          />

          <label htmlFor="plugin-timeout">插件调用超时（秒）</label>
          <input
            id="plugin-timeout"
            value={pluginTimeoutSeconds}
            onChange={(event) => setPluginTimeoutSeconds(event.target.value)}
            inputMode="decimal"
          />

          <label htmlFor="plugin-credential-ref">Credential Ref</label>
          <input
            id="plugin-credential-ref"
            value={pluginCredentialRef}
            onChange={(event) => setPluginCredentialRef(event.target.value)}
            placeholder="secret://plugin-token"
          />

          <label htmlFor="plugin-credential-header">Credential Header</label>
          <input
            id="plugin-credential-header"
            value={pluginCredentialHeader}
            onChange={(event) => setPluginCredentialHeader(event.target.value)}
            placeholder="X-Plugin-Credential"
          />

          <label htmlFor="plugin-credential-scheme">Credential Scheme</label>
          <input
            id="plugin-credential-scheme"
            value={pluginCredentialScheme}
            onChange={(event) => setPluginCredentialScheme(event.target.value)}
            placeholder="Bearer；留空表示直接写入 header 值"
          />

          <label htmlFor="plugin-capability-id">能力 ID</label>
          <input
            id="plugin-capability-id"
            value={pluginCapabilityId}
            onChange={(event) => setPluginCapabilityId(event.target.value)}
            placeholder="calendar.create_event"
            required
          />

          <label htmlFor="plugin-capability-adapter">能力适配器</label>
          <input
            id="plugin-capability-adapter"
            value={pluginCapabilityAdapter}
            onChange={(event) => setPluginCapabilityAdapter(event.target.value)}
            placeholder="http_json"
          />

          <label htmlFor="plugin-permission-class">权限类</label>
          <input
            id="plugin-permission-class"
            value={pluginPermissionClass}
            onChange={(event) => setPluginPermissionClass(event.target.value)}
            placeholder="plugin.use"
          />

          <label htmlFor="plugin-sandbox-profile">沙箱 Profile</label>
          <input
            id="plugin-sandbox-profile"
            value={pluginSandboxProfile}
            onChange={(event) => setPluginSandboxProfile(event.target.value)}
            placeholder="remote_connector"
          />

          <label htmlFor="plugin-replay-safe">
            <input
              id="plugin-replay-safe"
              type="checkbox"
              checked={pluginReplaySafe}
              onChange={(event) => setPluginReplaySafe(event.target.checked)}
            />
            可安全重放
          </label>

          <label htmlFor="plugin-aliases">能力别名，英文逗号分隔</label>
          <textarea
            id="plugin-aliases"
            value={pluginAliases}
            onChange={(event) => setPluginAliases(event.target.value)}
            placeholder="calendar_create"
          />

          <button type="submit" disabled={savePlugin.isPending || !canWritePlugins}>
            {savePlugin.isPending ? "正在保存..." : "保存插件"}
          </button>
          {!canWritePlugins ? <p className="field-help">当前账号无权保存插件配置。</p> : null}
          {pluginMessage ? <p role="status">{pluginMessage}</p> : null}
          {savePlugin.isError ? <p role="alert">{formatApiError(savePlugin.error, "插件保存失败")}</p> : null}
          {pluginLifecycle.isError ? (
            <p role="alert">{formatApiError(pluginLifecycle.error, "插件操作失败")}</p>
          ) : null}
        </form>

        <section aria-label="已配置插件">
          <h3>已配置插件</h3>
          {!canReadCapabilityManifest ? <p className="field-help">当前账号无权查看插件配置。</p> : null}
          {plugins.isLoading ? <p>正在加载插件...</p> : null}
          {plugins.isError ? <p role="alert">{formatApiError(plugins.error, "插件加载失败")}</p> : null}
          {canReadCapabilityManifest && pluginItems.length === 0 ? (
            <article>
              <h4>还没有插件</h4>
              <p>添加 HTTP JSON 插件后，它的能力会进入运行时注册表。</p>
            </article>
          ) : null}
          {pluginItems.length > 0 ? (
            <div className="card-grid">
              {pluginItems.map((plugin) => (
                <article key={plugin.id}>
                  <span className="eyebrow">插件 {plugin.health}</span>
                  <h3>{plugin.name}</h3>
                  <p>ID：<span>{plugin.id}</span></p>
                  <p>状态：<span>{plugin.status}</span></p>
                  <p>Endpoint：<span>{plugin.endpoint_url ?? "未填写"}</span></p>
                  <p>允许域名：<span>{plugin.domain_allowlist.join(", ") || "未配置"}</span></p>
                  <p>Credential：<span>{plugin.credential_ref ?? "未配置"}</span></p>
                  <p>Header：<span>{plugin.credential_header}</span></p>
                  <p>能力：<span>{plugin.capabilities.map((capability) => capability.id).join(", ") || "未配置"}</span></p>
                  <button type="button" onClick={() => editPlugin(plugin)}>
                    编辑
                  </button>
                  <button
                    type="button"
                    disabled={pluginLifecycle.isPending || !canWritePlugins}
                    onClick={() => pluginLifecycle.mutate({ id: plugin.id, action: "start" })}
                    aria-label={`启动插件 ${plugin.name}`}
                  >
                    启动
                  </button>
                  <button
                    type="button"
                    disabled={pluginLifecycle.isPending || !canWritePlugins}
                    onClick={() => pluginLifecycle.mutate({ id: plugin.id, action: "stop" })}
                    aria-label={`停止插件 ${plugin.name}`}
                  >
                    停止
                  </button>
                  <button
                    type="button"
                    disabled={pluginLifecycle.isPending || !canWritePlugins}
                    onClick={() => pluginLifecycle.mutate({ id: plugin.id, action: "reload" })}
                    aria-label={`重载插件 ${plugin.name}`}
                  >
                    重载
                  </button>
                  <button
                    type="button"
                    className="danger-action"
                    disabled={pluginLifecycle.isPending || !canWritePlugins}
                    onClick={() => confirmPluginDelete(plugin)}
                    aria-label={`删除插件 ${plugin.name}`}
                  >
                    删除
                  </button>
                </article>
              ))}
            </div>
          ) : null}
        </section>
      </div>

      <div className="two-column">
        <form onSubmit={submit} aria-label="保存 MCP 工具">
          <h3>新增或更新 MCP</h3>
          <label htmlFor="mcp-id">服务 ID</label>
          <input
            id="mcp-id"
            value={serverId}
            onChange={(event) => setServerId(event.target.value)}
            placeholder="例如 filesystem"
            required
          />

          <label htmlFor="mcp-name">显示名称</label>
          <input id="mcp-name" value={name} onChange={(event) => setName(event.target.value)} required />

          <label htmlFor="mcp-transport">连接方式</label>
          <select id="mcp-transport" value={transport} onChange={(event) => setTransport(event.target.value)}>
            <option value="stdio">本地 stdio</option>
            <option value="streamable_http">远程 Streamable HTTP</option>
            <option value="sse">远程 SSE</option>
          </select>

          {isStdio ? (
            <>
              <label htmlFor="mcp-command">启动命令</label>
              <input
                id="mcp-command"
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                placeholder="/usr/bin/node 或 /usr/bin/python3"
              />

              <label htmlFor="mcp-args">启动参数，英文逗号分隔</label>
              <textarea
                id="mcp-args"
                value={args}
                onChange={(event) => setArgs(event.target.value)}
                placeholder="/opt/mcp/server.js,--stdio"
              />

              <label htmlFor="mcp-executable-allowlist">可执行白名单，英文逗号分隔</label>
              <textarea
                id="mcp-executable-allowlist"
                value={executableAllowlist}
                onChange={(event) => setExecutableAllowlist(event.target.value)}
                placeholder="/usr/bin/node,/usr/bin/python3"
              />
            </>
          ) : (
            <>
              <label htmlFor="mcp-url">远程 MCP URL</label>
              <input
                id="mcp-url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://mcp.example.com/mcp"
              />

              <label htmlFor="mcp-domain-allowlist">允许域名，英文逗号分隔</label>
              <textarea
                id="mcp-domain-allowlist"
                value={domainAllowlist}
                onChange={(event) => setDomainAllowlist(event.target.value)}
                placeholder="example.com"
              />
            </>
          )}

          <label htmlFor="mcp-tools">允许暴露给 Agent 的工具，英文逗号分隔</label>
          <textarea
            id="mcp-tools"
            value={allowedTools}
            onChange={(event) => setAllowedTools(event.target.value)}
            placeholder="例如 read_file,list_directory"
          />

          <label htmlFor="mcp-timeout">调用超时（秒）</label>
          <input
            id="mcp-timeout"
            value={timeoutSeconds}
            onChange={(event) => setTimeoutSeconds(event.target.value)}
            inputMode="numeric"
          />

          <button type="submit" disabled={saveServer.isPending}>
            {saveServer.isPending ? "正在保存..." : "保存 MCP"}
          </button>
          {message ? <p role="status">{message}</p> : null}
          {saveServer.isError ? <p role="alert">{formatApiError(saveServer.error, "MCP 保存失败")}</p> : null}
          {deleteServer.isError ? <p role="alert">{formatApiError(deleteServer.error, "MCP 删除失败")}</p> : null}
        </form>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>stdio：填写可执行文件绝对路径，并把同一路径放入可执行白名单。</li>
            <li>远程：必须是 HTTPS，域名必须写入允许域名，避免 SSRF 和误连内网。</li>
            <li>允许工具只填需要给 Agent 用的工具名；写入、删除、外部发送类工具要谨慎。</li>
            <li>保存后先做小任务验证，失败原因会进入“日志 → 主要功能/模式运行”。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已配置 MCP">
        <h3>已配置 MCP</h3>
        {items.length === 0 ? (
          <article>
            <h4>还没有 MCP 工具</h4>
            <p>从上方添加一个 MCP 服务，并限制允许工具清单。</p>
          </article>
        ) : (
          <div className="card-grid">
            {items.map((server) => (
              <article key={server.id}>
                <span className="eyebrow">{server.health}</span>
                <h3>{server.name}</h3>
                <p>ID：{server.id}</p>
                <p>连接：{server.transport}</p>
                <p>地址：{server.transport === "stdio" ? server.command || "未填写" : server.url || "未填写"}</p>
                <p>允许工具：{server.allowed_tools.join(", ") || "未配置"}</p>
                <button type="button" onClick={() => edit(server)}>
                  编辑
                </button>
                <button
                  type="button"
                  className="danger-action"
                  disabled={deleteServer.isPending}
                  onClick={() => confirmDelete(server)}
                >
                  删除
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
