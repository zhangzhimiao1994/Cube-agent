import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError, type MemoryRecord } from "../api/client";

export function MemoryPage() {
  const queryClient = useQueryClient();
  const memory = useQuery({ queryKey: ["memory"], queryFn: () => api.memory() });
  const update = useMutation({
    mutationFn: ({ id, value }: MemoryRecord) => api.updateMemory(id, value),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
  const forget = useMutation({
    mutationFn: (id: string) => api.forgetMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  if (memory.isLoading) return <p>Loading memory...</p>;
  if (memory.isError) {
    return <p role="alert">{formatApiError(memory.error, "Failed to load memory")}</p>;
  }
  return (
    <section>
      <h2>Memory management</h2>
      {update.isError ? (
        <p role="alert">{formatApiError(update.error, "Memory update failed")}</p>
      ) : null}
      {forget.isError ? (
        <p role="alert">{formatApiError(forget.error, "Memory deletion failed")}</p>
      ) : null}
      {memory.data?.map((record) => (
        <article key={record.id}>
          <h3>{record.id}</h3>
          <p>Scope: {record.scope}</p>
          <label>
            Value
            <textarea
              aria-label={`Memory value ${record.id}`}
              defaultValue={record.value}
              onBlur={(event) =>
                update.mutate({ ...record, value: event.currentTarget.value })
              }
            />
          </label>
          <button type="button" onClick={() => forget.mutate(record.id)}>
            Forget
          </button>
        </article>
      ))}
    </section>
  );
}
