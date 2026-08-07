import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

export function McpPage() {
  const servers = useQuery({ queryKey: ["mcp"], queryFn: () => api.mcpServers() });
  if (servers.isLoading) return <p>Loading MCP...</p>;
  if (servers.isError) return <p role="alert">Failed to load MCP</p>;
  return (
    <section>
      <h2>MCP health</h2>
      {servers.data?.map((server) => (
        <article key={server.id}>
          <h3>{server.name}</h3>
          <p>Health: {server.health}</p>
          <p>Allowed tools: {server.allowed_tools.join(", ")}</p>
        </article>
      ))}
    </section>
  );
}
