import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import {
  api,
  formatApiError,
  type CapabilityManifestItem,
  type McpServer,
  type PluginAdapterDescriptor,
  type PluginCapability,
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

type PluginCapabilityForm = {
  aliases: string;
  adapter: string;
  id: string;
  inputSchema: string;
  outputSchema: string;
  policyEffect: PluginCapability["policy_effect"];
  permissionClass: string;
  replaySafe: boolean;
  sandboxProfile: string;
};

function createPluginCapabilityForm(
  values: Partial<PluginCapabilityForm> = {},
): PluginCapabilityForm {
  return {
    id: "calendar.create_event",
    adapter: "http_json",
    permissionClass: "plugin.use",
    sandboxProfile: "remote_connector",
    policyEffect: "inherit",
    replaySafe: false,
    aliases: "",
    inputSchema: "",
    outputSchema: "",
    ...values,
  };
}

function formatSchemaText(schema: PluginCapability["input_schema"]) {
  return schema ? JSON.stringify(schema, null, 2) : "";
}

function parseSchemaText(value: string, label: string): PluginCapability["input_schema"] {
  const trimmed = value.trim();
  if (!trimmed) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} 必须是合法 JSON。`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON 对象。`);
  }
  return parsed as PluginCapability["input_schema"];
}

function pluginCapabilityFormFromCapability(capability: PluginCapability): PluginCapabilityForm {
  return createPluginCapabilityForm({
    id: capability.id,
    adapter: capability.adapter,
    permissionClass: capability.permission_class,
    sandboxProfile: capability.sandbox_profile,
    policyEffect: capability.policy_effect,
    replaySafe: capability.replay_safe,
    aliases: capability.aliases.join(","),
    inputSchema: formatSchemaText(capability.input_schema),
    outputSchema: formatSchemaText(capability.output_schema),
  });
}

function fillFromPlugin(plugin: PluginResource) {
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
    capabilities: plugin.capabilities.length > 0
      ? plugin.capabilities.map(pluginCapabilityFormFromCapability)
      : [createPluginCapabilityForm({ id: "" })],
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

function policyEffectLabel(effect: PluginCapability["policy_effect"]) {
  if (effect === "allow") return "允许";
  if (effect === "require_approval") return "需要审批";
  if (effect === "deny") return "拒绝";
  return "继承";
}

function schemaRequiredFields(schema: PluginAdapterDescriptor["resource_schema"]) {
  const required = schema.required;
  return Array.isArray(required) ? required.filter((field): field is string => typeof field === "string") : [];
}

function schemaPropertyNames(schema: PluginAdapterDescriptor["resource_schema"]) {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return [];
  return Object.keys(properties);
}

function schemaProperties(schema: PluginAdapterDescriptor["resource_schema"]) {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return {};
  return properties as Record<string, unknown>;
}

function schemaDefault(schema: PluginAdapterDescriptor["resource_schema"], property: string) {
  const field = schemaProperties(schema)[property];
  if (!field || typeof field !== "object" || Array.isArray(field)) return undefined;
  return (field as { default?: unknown }).default;
}

function capabilityDefaultsForAdapter(
  adapter: PluginAdapterDescriptor | undefined,
): Partial<PluginCapabilityForm> {
  if (!adapter) return {};
  const permissionClass = schemaDefault(adapter.capability_schema, "permission_class");
  const sandboxProfile = schemaDefault(adapter.capability_schema, "sandbox_profile");
  const policyEffect = schemaDefault(adapter.capability_schema, "policy_effect");
  const replaySafe = schemaDefault(adapter.capability_schema, "replay_safe");
  return {
    adapter: adapter.id,
    ...(typeof permissionClass === "string" ? { permissionClass } : {}),
    ...(typeof sandboxProfile === "string" ? { sandboxProfile } : {}),
    ...(isPolicyEffect(policyEffect) ? { policyEffect } : {}),
    ...(typeof replaySafe === "boolean" ? { replaySafe } : {}),
  };
}

function isPolicyEffect(value: unknown): value is PluginCapability["policy_effect"] {
  return (
    value === "inherit" ||
    value === "allow" ||
    value === "require_approval" ||
    value === "deny"
  );
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
  const pluginAdapters = useQuery({
    queryKey: [
      "plugin-adapters",
      auth.user?.tenant_id,
      auth.user?.user_id,
      auth.user?.role,
      canReadCapabilityManifest,
    ],
    queryFn: () => api.pluginAdapters(),
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
  const [pluginCapabilities, setPluginCapabilities] = useState<PluginCapabilityForm[]>([
    createPluginCapabilityForm(),
  ]);
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
        capabilities: pluginCapabilities.map((capability, index) => ({
          id: capability.id.trim(),
          adapter: capability.adapter.trim() || "http_json",
          permission_class: capability.permissionClass.trim() || "plugin.use",
          sandbox_profile: capability.sandboxProfile.trim() || "remote_connector",
          policy_effect: capability.policyEffect,
          replay_safe: capability.replaySafe,
          aliases: parseCsv(capability.aliases),
          input_schema: parseSchemaText(capability.inputSchema, `Input Schema ${index + 1}`),
          output_schema: parseSchemaText(capability.outputSchema, `Output Schema ${index + 1}`),
        })),
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
    setPluginCapabilities(next.capabilities);
    setPluginMessage(`已载入 ${plugin.name}，修改后点击保存。`);
  }

  function updatePluginCapability(
    index: number,
    patch: Partial<PluginCapabilityForm>,
  ) {
    setPluginCapabilities((capabilities) =>
      capabilities.map((capability, capabilityIndex) =>
        capabilityIndex === index ? { ...capability, ...patch } : capability,
      ),
    );
  }

  function addPluginCapability() {
    const firstAdapter = adapterItems[0];
    setPluginCapabilities((capabilities) => [
      ...capabilities,
      createPluginCapabilityForm({
        id: "",
        permissionClass: "plugin.use",
        aliases: "",
        ...capabilityDefaultsForAdapter(firstAdapter),
      }),
    ]);
  }

  function removePluginCapability(index: number) {
    setPluginCapabilities((capabilities) =>
      capabilities.length === 1
        ? capabilities
        : capabilities.filter((_capability, capabilityIndex) => capabilityIndex !== index),
    );
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
  const adapterItems = canReadCapabilityManifest ? pluginAdapters.data ?? [] : [];
  const adapterById = new Map(adapterItems.map((adapter) => [adapter.id, adapter]));
  const isStdio = transport === "stdio";

  function updatePluginCapabilityAdapter(index: number, adapterId: string) {
    updatePluginCapability(index, {
      adapter: adapterId,
      ...capabilityDefaultsForAdapter(adapterById.get(adapterId)),
    });
  }

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
                  <th>策略</th>
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
                    <td>{policyEffectLabel(capability.policy_effect)}</td>
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

          <fieldset>
            <legend>插件能力</legend>
            {pluginCapabilities.map((capability, index) => {
              const capabilityNumber = index + 1;
              return (
                <fieldset key={index}>
                  <legend>能力 {capabilityNumber}</legend>

                  <label htmlFor={`plugin-capability-id-${index}`}>能力 ID {capabilityNumber}</label>
                  <input
                    id={`plugin-capability-id-${index}`}
                    value={capability.id}
                    onChange={(event) => updatePluginCapability(index, { id: event.target.value })}
                    placeholder="calendar.create_event"
                    required
                  />

                  <label htmlFor={`plugin-capability-adapter-${index}`}>能力适配器 {capabilityNumber}</label>
                  {adapterItems.length > 0 ? (
                    <select
                      id={`plugin-capability-adapter-${index}`}
                      value={capability.adapter}
                      onChange={(event) => updatePluginCapabilityAdapter(index, event.target.value)}
                    >
                      {adapterItems.map((adapter) => (
                        <option key={adapter.id} value={adapter.id}>
                          {adapter.name}
                        </option>
                      ))}
                      {!adapterById.has(capability.adapter) ? (
                        <option value={capability.adapter}>{capability.adapter}</option>
                      ) : null}
                    </select>
                  ) : (
                    <input
                      id={`plugin-capability-adapter-${index}`}
                      value={capability.adapter}
                      onChange={(event) => updatePluginCapability(index, { adapter: event.target.value })}
                      placeholder="http_json"
                    />
                  )}

                  <label htmlFor={`plugin-permission-class-${index}`}>权限类 {capabilityNumber}</label>
                  <input
                    id={`plugin-permission-class-${index}`}
                    value={capability.permissionClass}
                    onChange={(event) => updatePluginCapability(index, { permissionClass: event.target.value })}
                    placeholder="plugin.use"
                  />

                  <label htmlFor={`plugin-sandbox-profile-${index}`}>沙箱 Profile {capabilityNumber}</label>
                  <input
                    id={`plugin-sandbox-profile-${index}`}
                    value={capability.sandboxProfile}
                    onChange={(event) => updatePluginCapability(index, { sandboxProfile: event.target.value })}
                    placeholder="remote_connector"
                  />

                  <label htmlFor={`plugin-policy-effect-${index}`}>权限策略 {capabilityNumber}</label>
                  <select
                    id={`plugin-policy-effect-${index}`}
                    value={capability.policyEffect}
                    onChange={(event) =>
                      updatePluginCapability(index, {
                        policyEffect: event.target.value as PluginCapability["policy_effect"],
                      })
                    }
                  >
                    <option value="inherit">继承</option>
                    <option value="allow">允许</option>
                    <option value="require_approval">需要审批</option>
                    <option value="deny">拒绝</option>
                  </select>

                  <label htmlFor={`plugin-replay-safe-${index}`}>
                    <input
                      id={`plugin-replay-safe-${index}`}
                      type="checkbox"
                      checked={capability.replaySafe}
                      onChange={(event) => updatePluginCapability(index, { replaySafe: event.target.checked })}
                    />
                    可安全重放 {capabilityNumber}
                  </label>

                  <label htmlFor={`plugin-aliases-${index}`}>能力别名 {capabilityNumber}，英文逗号分隔</label>
                  <textarea
                    id={`plugin-aliases-${index}`}
                    value={capability.aliases}
                    onChange={(event) => updatePluginCapability(index, { aliases: event.target.value })}
                    placeholder="calendar_create"
                  />

                  <label htmlFor={`plugin-input-schema-${index}`}>Input Schema {capabilityNumber}</label>
                  <textarea
                    id={`plugin-input-schema-${index}`}
                    value={capability.inputSchema}
                    onChange={(event) => updatePluginCapability(index, { inputSchema: event.target.value })}
                    placeholder='{"type":"object","properties":{}}'
                  />

                  <label htmlFor={`plugin-output-schema-${index}`}>Output Schema {capabilityNumber}</label>
                  <textarea
                    id={`plugin-output-schema-${index}`}
                    value={capability.outputSchema}
                    onChange={(event) => updatePluginCapability(index, { outputSchema: event.target.value })}
                    placeholder='{"type":"object","properties":{}}'
                  />

                  {pluginCapabilities.length > 1 ? (
                    <button
                      type="button"
                      className="danger-action"
                      onClick={() => removePluginCapability(index)}
                      aria-label={`删除能力 ${capabilityNumber}`}
                    >
                      删除能力
                    </button>
                  ) : null}
                </fieldset>
              );
            })}
            <button type="button" onClick={addPluginCapability}>
              添加能力
            </button>
          </fieldset>

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

        <div>
          <section aria-label="插件适配器目录">
            <h3>插件适配器目录</h3>
            {!canReadCapabilityManifest ? <p className="field-help">当前账号无权查看插件适配器。</p> : null}
            {pluginAdapters.isLoading ? <p>正在加载插件适配器...</p> : null}
            {pluginAdapters.isError ? (
              <p role="alert">{formatApiError(pluginAdapters.error, "插件适配器加载失败")}</p>
            ) : null}
            {canReadCapabilityManifest && adapterItems.length === 0 ? (
              <article>
                <h4>还没有适配器</h4>
                <p>运行时暂未暴露可插拔适配器目录。</p>
              </article>
            ) : null}
            {adapterItems.length > 0 ? (
              <div className="card-grid compact">
                {adapterItems.map((adapter) => (
                  <article key={adapter.id}>
                    <span className="eyebrow">{adapter.id}</span>
                    <h3>{adapter.name}</h3>
                    {adapter.description ? <p>{adapter.description}</p> : null}
                    <p>必填资源字段：<span>{schemaRequiredFields(adapter.resource_schema).join(", ") || "无"}</span></p>
                    <p>资源字段：<span>{schemaPropertyNames(adapter.resource_schema).join(", ") || "无"}</span></p>
                    <p>能力字段：</p>
                    <div className="toolbar">
                      {schemaPropertyNames(adapter.capability_schema).map((field) => (
                        <span key={field} className="status-chip">{field}</span>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            ) : null}
          </section>

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
                    <p>
                      策略：
                      <span>
                        {plugin.capabilities
                          .map((capability) => `${capability.id}:${policyEffectLabel(capability.policy_effect)}`)
                          .join(", ") || "未配置"}
                      </span>
                    </p>
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
