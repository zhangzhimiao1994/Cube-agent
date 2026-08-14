import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError, type AuditEvent } from "../api/client";

function matchesAuditSearch(event: AuditEvent, query: string) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [event.id, event.action, event.resource, event.actor, event.created_at]
    .join(" ")
    .toLowerCase()
    .includes(normalized);
}

export function AuditPage() {
  const [action, setAction] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const audit = useQuery({
    queryKey: ["audit", action],
    queryFn: () => api.audit(action || undefined),
  });

  const events = audit.data ?? [];
  const visibleEvents = events.filter((event) => matchesAuditSearch(event, searchTerm));

  function exportAudit() {
    const blob = new Blob([JSON.stringify(visibleEvents, null, 2)], {
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
        <label>
          搜索审计日志
          <input
            type="search"
            aria-label="搜索审计日志"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.currentTarget.value)}
            placeholder="按资源、操作者或时间搜索"
          />
        </label>
        <button type="button" onClick={exportAudit} disabled={visibleEvents.length === 0}>
          导出当前结果
        </button>
        <small>
          显示 {visibleEvents.length} / {events.length}
        </small>
      </div>

      {visibleEvents.length === 0 ? (
        <article>
          <h3>没有匹配日志</h3>
          <p>调整过滤或搜索条件，或先执行一次配置保存、任务控制等操作。</p>
        </article>
      ) : (
        <table className="dense-table">
          <thead>
            <tr>
              <th>操作</th>
              <th>资源</th>
              <th>操作者</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {visibleEvents.map((event) => (
              <tr key={event.id}>
                <td>{event.action}</td>
                <td>{event.resource}</td>
                <td>{event.actor}</td>
                <td>
                  <time dateTime={event.created_at}>{event.created_at}</time>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}