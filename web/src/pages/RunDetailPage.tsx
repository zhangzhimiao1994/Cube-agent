import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api } from "../api/client";

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: runId.length > 0,
  });
  const control = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel") => {
      if (action === "pause") return api.pauseRun(runId);
      if (action === "resume") return api.resumeRun(runId);
      return api.cancelRun(runId);
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["run", runId], updated);
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  if (run.isLoading) return <p>Loading run...</p>;
  if (run.isError || !run.data) return <p role="alert">Failed to load run</p>;

  return (
    <section>
      <h2>Run detail</h2>
      <p>Status: {run.data.status}</p>
      <p>Mode: {run.data.mode}</p>
      <p>Queue wait: {run.data.queue_wait_ms} ms</p>
      <p>Capacity wait: {run.data.capacity_wait_ms} ms</p>
      <p>Cost: ${run.data.cost_usd}</p>
      <p>Request: {run.data.request}</p>
      <div>
        <button type="button" onClick={() => control.mutate("pause")}>
          Pause
        </button>
        <button type="button" onClick={() => control.mutate("resume")}>
          Resume
        </button>
        <button type="button" onClick={() => control.mutate("cancel")}>
          Cancel
        </button>
      </div>
      <h3>Events</h3>
      <ol>
        {run.data.events.map((event) => (
          <li key={event.sequence}>
            {event.kind}: {event.message}
          </li>
        ))}
      </ol>
      <h3>Artifacts</h3>
      <ul>
        {run.data.artifacts.map((artifact) => (
          <li key={artifact.id}>
            {artifact.kind}: {artifact.title}
          </li>
        ))}
      </ul>
      <h3>Explicit details</h3>
      <dl>
        {Object.entries(run.data.explicit_details).map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
