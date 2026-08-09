import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError } from "../api/client";

function parseTools(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function McpPage() {
  const queryClient = useQueryClient();
  const servers = useQuery({ queryKey: ["mcp"], queryFn: () => api.mcpServers() });
  const [serverId, setServerId] = useState("filesystem");
  const [name, setName] = useState("Filesystem MCP");
  const [allowedTools, setAllowedTools] = useState("read_file,list_directory");
  const [message, setMessage] = useState<string | null>(null);

  const saveServer = useMutation({
    mutationFn: () =>
      api.createMcpServer({
        id: serverId.trim(),
        name: name.trim(),
        allowed_tools: parseTools(allowedTools),
      }),
    onSuccess: async () => {
      setMessage("MCP 工具配置已保存。");
      await queryClient.invalidateQueries({ queryKey: ["mcp"] });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    saveServer.mutate();
  }

  if (servers.isLoading) return <p>正在加载 MCP 工具...</p>;
  if (servers.isError) {
    return <p role="alert">{formatApiError(servers.error, "MCP 工具加载失败")}</p>;
  }

  const items = servers.data ?? [];

  return (
    <section>
      <p className="eyebrow">MCP governance</p>
      <h2>MCP 工具</h2>
      <p>
        MCP 用于接入文件、浏览器、数据库或外部系统等工具。这里配置的是主 Agent 和子 Agent
        可见的工具边界，工具必须在允许列表内才会被调度。
      </p>

      <div className="two-column">
        <form onSubmit={submit} aria-label="保存 MCP 工具">
          <h3>新增或更新 MCP</h3>
          <label htmlFor="mcp-id">服务器 ID</label>
          <input
            id="mcp-id"
            value={serverId}
            onChange={(event) => setServerId(event.target.value)}
            placeholder="例如 filesystem"
            required
          />

          <label htmlFor="mcp-name">显示名称</label>
          <input id="mcp-name" value={name} onChange={(event) => setName(event.target.value)} required />

          <label htmlFor="mcp-tools">允许工具，多个用英文逗号分隔</label>
          <textarea
            id="mcp-tools"
            value={allowedTools}
            onChange={(event) => setAllowedTools(event.target.value)}
            placeholder="例如 read_file,list_directory"
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
            <li>只把非危险或经过审批的工具放入允许列表。</li>
            <li>读文件、查状态等低风险工具可以默认开放；写入、删除、外部发送类工具要谨慎。</li>
            <li>如果工具调用失败，运行详情和审计日志会显示错误原因，便于排查。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已配置 MCP">
        <h3>已配置 MCP</h3>
        {items.length === 0 ? (
          <article>
            <h4>还没有 MCP 工具</h4>
            <p>从上方添加一个工具服务器，并限制允许工具清单。</p>
          </article>
        ) : (
          <div className="card-grid">
            {items.map((server) => (
              <article key={server.id}>
                <span className="eyebrow">{server.health}</span>
                <h3>{server.name}</h3>
                <p>ID：{server.id}</p>
                <p>允许工具：{server.allowed_tools.join(", ") || "未配置"}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
