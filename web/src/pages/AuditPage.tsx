import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError } from "../api/client";

export function AuditPage() {
  const [action, setAction] = useState("");
  const audit = useQuery({
    queryKey: ["audit", action],
    queryFn: () => api.audit(action || undefined),
  });
  const exportAudit = () => {
    const safeRows = audit.data ?? [];
    const blob = new Blob([JSON.stringify(safeRows, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    URL.revokeObjectURL(url);
  };

  if (audit.isLoading) return <p>Loading audit...</p>;
  if (audit.isError) {
    return <p role="alert">{formatApiError(audit.error, "Failed to load audit")}</p>;
  }
  return (
    <section>
      <h2>Audit log</h2>
      <label>
        Action filter
        <input
          value={action}
          onChange={(event) => setAction(event.currentTarget.value)}
          placeholder="config.publish"
        />
      </label>
      <button type="button" onClick={exportAudit}>
        Export safe JSON
      </button>
      {audit.data?.map((event) => (
        <article key={event.id}>
          <h3>{event.action}</h3>
          <p>Actor: {event.actor}</p>
          <p>Resource: {event.resource}</p>
          <time dateTime={event.created_at}>{event.created_at}</time>
        </article>
      ))}
    </section>
  );
}
