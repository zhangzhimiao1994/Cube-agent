import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

export function AgentsPage() {
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.agents() });
  return (
    <section>
      <h2>Agent 角色</h2>
      {(agents.data ?? []).map((agent) => (
        <p key={agent.id}>{agent.name}</p>
      ))}
    </section>
  );
}
