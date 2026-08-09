import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError } from "../api/client";

export function AuditPage() {
  const [action, setAction] = useState("");
  const audit = useQuery({
    queryKey: ["audit", action],
    queryFn: () => api.audit(action || undefined),
  });

  function exportAudit() {
    const safeRows = audit.data ?? [];
    const blob = new Blob([JSON.stringify(safeRows, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `agent-hub-audit-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  if (audit.isLoading) return <p>正在加载审计日志...</p>;
  if (audit.isError) {
    return <p role="alert">{formatApiError(audit.error, "审计日志加载失败")}</p>;
  }

  const events = audit.data ?? [];

  return (
    <section>
      <p className="eyebrow">Audit trail</p>
      <h2>审计日志</h2>
      <p>审计日志用于排查生产环境配置变更、记忆更新、Skill 审批和运行控制。</p>

      <div className="toolbar">
        <label>
          操作过滤
          <input
            value={action}
            onChange={(event) => setAction(event.currentTarget.value)}
            placeholder="例如 config.publish"
          />
        </label>
        <button type="button" onClick={exportAudit} disabled={events.length === 0}>
          导出安全 JSON
        </button>
      </div>

      {events.length === 0 ? (
        <article>
          <h3>没有匹配日志</h3>
          <p>调整过滤条件，或先执行一次配置保存、任务控制等操作。</p>
        </article>
      ) : (
        <div className="card-grid">
          {events.map((event) => (
            <article key={event.id}>
              <span className="eyebrow">{event.action}</span>
              <h3>{event.resource}</h3>
              <p>操作者：{event.actor}</p>
              <time dateTime={event.created_at}>{event.created_at}</time>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
