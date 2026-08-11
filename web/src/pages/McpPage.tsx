import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError, type McpServer } from "../api/client";

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

export function McpPage() {
  const queryClient = useQueryClient();
  const servers = useQuery({ queryKey: ["mcp"], queryFn: () => api.mcpServers() });
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
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveServer.mutate();
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

  if (servers.isLoading) return <p>正在加载 MCP 工具...</p>;
  if (servers.isError) {
    return <p role="alert">{formatApiError(servers.error, "MCP 工具加载失败")}</p>;
  }

  const items = servers.data ?? [];
  const isStdio = transport === "stdio";

  return (
    <section>
      <p className="eyebrow">MCP governance</p>
      <h2>MCP 工具</h2>
      <p>
        MCP 用来接入文件、浏览器、数据库或外部系统。这里配置的是生产连接参数：本地
        stdio 需要命令和可执行白名单，远程 MCP 需要 HTTPS URL 和域名白名单。
      </p>

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
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
