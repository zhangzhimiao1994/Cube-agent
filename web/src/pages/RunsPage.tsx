import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, formatApiError } from "../api/client";

export function RunsPage() {
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.runs() });
  if (runs.isLoading) return <p>Loading runs...</p>;
  if (runs.isError) return <p role="alert">{formatApiError(runs.error, "Failed to load runs")}</p>;
  return (
    <section>
      <h2>Run operations</h2>
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Status</th>
            <th>Mode</th>
            <th>Queue wait</th>
            <th>Capacity wait</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {runs.data?.map((run) => (
            <tr key={run.id}>
              <td>
                <Link to={`/runs/${run.id}`}>{run.id}</Link>
              </td>
              <td>{run.status}</td>
              <td>{run.mode}</td>
              <td>{run.queue_wait_ms} ms</td>
              <td>{run.capacity_wait_ms} ms</td>
              <td>${run.cost_usd}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
