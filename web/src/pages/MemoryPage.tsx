import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError, type MemoryRecord } from "../api/client";

export function MemoryPage() {
  const queryClient = useQueryClient();
  const memory = useQuery({ queryKey: ["memory"], queryFn: () => api.memory() });
  const [memoryId, setMemoryId] = useState("project-policy");
  const [scope, setScope] = useState("tenant");
  const [value, setValue] = useState("Only non-dangerous operations may run without approval.");
  const [message, setMessage] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.createMemory({ id: memoryId.trim(), scope: scope.trim(), value: value.trim() }),
    onSuccess: async () => {
      setMessage("记忆已保存。");
      await queryClient.invalidateQueries({ queryKey: ["memory"] });
    },
  });
  const update = useMutation({
    mutationFn: ({ id, value: nextValue }: MemoryRecord) => api.updateMemory(id, nextValue),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
  const forget = useMutation({
    mutationFn: (id: string) => api.forgetMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
  const lock = useMutation({
    mutationFn: (id: string) => api.lockMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
  const unlock = useMutation({
    mutationFn: (id: string) => api.unlockMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    create.mutate();
  }

  if (memory.isLoading) return <p>正在加载记忆...</p>;
  if (memory.isError) {
    return <p role="alert">{formatApiError(memory.error, "记忆加载失败")}</p>;
  }

  const records = memory.data ?? [];

  return (
    <section>
      <p className="eyebrow">Memory control</p>
      <h2>记忆管理</h2>
      <p>
        记忆用于保存长期策略、偏好和安全边界。Hermes 负责经验学习，记忆负责明确规则；
        子 Agent 使用前仍受主 Agent 调度和权限控制。
      </p>

      <div className="two-column">
        <form onSubmit={submit} aria-label="新增记忆">
          <h3>新增或覆盖记忆</h3>
          <label htmlFor="memory-id">记忆 ID</label>
          <input
            id="memory-id"
            value={memoryId}
            onChange={(event) => setMemoryId(event.target.value)}
            placeholder="例如 project-policy"
            required
          />

          <label htmlFor="memory-scope">作用域</label>
          <input id="memory-scope" value={scope} onChange={(event) => setScope(event.target.value)} required />

          <label htmlFor="memory-value">内容</label>
          <textarea id="memory-value" value={value} onChange={(event) => setValue(event.target.value)} required />

          <button type="submit" disabled={create.isPending || value.trim().length === 0}>
            {create.isPending ? "正在保存..." : "保存记忆"}
          </button>
          {message ? <p role="status">{message}</p> : null}
          {create.isError ? <p role="alert">{formatApiError(create.error, "记忆保存失败")}</p> : null}
        </form>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>适合保存“默认日志级别 warning”“不自动执行危险操作”等长期规则。</li>
            <li>不要写入 API Key、密码、Token 等敏感信息。</li>
            <li>修改现有记忆后会立即持久化；删除前请确认该规则不再需要。</li>
          </ol>
        </article>
      </div>

      {update.isError ? <p role="alert">{formatApiError(update.error, "记忆更新失败")}</p> : null}
      {forget.isError ? <p role="alert">{formatApiError(forget.error, "记忆删除失败")}</p> : null}
      {lock.isError ? <p role="alert">{formatApiError(lock.error, "记忆锁定失败")}</p> : null}
      {unlock.isError ? <p role="alert">{formatApiError(unlock.error, "记忆解锁失败")}</p> : null}

      <section aria-label="已保存记忆">
        <h3>已保存记忆</h3>
        {records.length === 0 ? (
          <article>
            <h4>还没有记忆</h4>
            <p>从上方新增第一条长期规则。</p>
          </article>
        ) : (
          <div className="card-grid">
            {records.map((record) => (
              <article key={record.id}>
                <span className="eyebrow">{record.scope}</span>
                <h3>{record.id}</h3>
                <div className="inline-status-list">
                  <span>热度 {record.heat.toFixed(2)}</span>
                  <span>{record.locked ? "已锁定" : "未锁定"}</span>
                  {record.project_id ? <span>项目 {record.project_id}</span> : null}
                  {record.conversation_id ? <span>对话 {record.conversation_id}</span> : null}
                  {record.summary_period !== "none" ? <span>摘要 {record.summary_period}</span> : null}
                  <span>召回 {record.recall_count} 次</span>
                </div>
                <label>
                  内容
                  <textarea
                    aria-label={`Memory value ${record.id}`}
                    defaultValue={record.value}
                    onBlur={(event) =>
                      update.mutate({ ...record, value: event.currentTarget.value })
                    }
                  />
                </label>
                <button
                  type="button"
                  onClick={() =>
                    record.locked ? unlock.mutate(record.id) : lock.mutate(record.id)
                  }
                  disabled={lock.isPending || unlock.isPending}
                >
                  {record.locked ? "解除锁定" : "锁定记忆"}
                </button>
                <button type="button" onClick={() => forget.mutate(record.id)}>
                  删除记忆
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
