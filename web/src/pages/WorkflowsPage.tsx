import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

export function WorkflowsPage() {
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });
  return (
    <section>
      <h2>Workflow</h2>
      {(workflows.data ?? []).map((workflow) => (
        <p key={workflow.id}>{workflow.name}</p>
      ))}
    </section>
  );
}
