import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError, type LogEntry } from "../api/client";

const LOG_MODULES = [
  {
    path: "audit",
    category: "audit",
    title: "审计日志",
    description: "配置发布、Skill 审批、运行控制等必须留痕的安全审计记录。",
  },
  {
    path: "model",
    category: "model_error",
    title: "模型配置与调用错误",
    description: "模型配置校验、可用性测试、上游状态码、API Base、模型名和运行调用错误。",
  },
  {
    path: "mode",
    category: "mode_error",
    title: "模式运行错误",
    description: "直接、派单、讨论、混合等模式执行失败记录。",
  },
  {
    path: "feature",
    category: "feature_error",
    title: "主要功能错误",
    description: "Skill、MCP、记忆、Hermes 等主要功能的 warning/error。",
  },
  {
    path: "agent",
    category: "agent_error",
    title: "Agent 角色错误",
    description: "角色配置、模型绑定、提示词缺失等 Agent 配置错误。",
  },
  {
    path: "channel",
    category: "channel_error",
    title: "通道连接错误",
    description: "飞书、企业微信、钉钉、Telegram、Slack、QQ 等通道配置缺失或连接异常。",
  },
] as const;

function moduleByPath(path: string | undefined) {
  return LOG_MODULES.find((item) => item.path === path);
}

function exportLogs(moduleTitle: string, entries: LogEntry[]) {
  const blob = new Blob([JSON.stringify(entries, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `agent-hub-${moduleTitle}-${new Date().toISOString().slice(0, 10)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function logMatchesFilters(entry: LogEntry, searchTerm: string, levelFilter: "all" | LogEntry["level"]) {
  if (levelFilter !== "all" && entry.level !== levelFilter) return false;
  const query = searchTerm.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    entry.id,
    entry.category,
    entry.level,
    entry.title,
    entry.message,
    entry.source,
    entry.created_at,
    ...Object.entries(entry.details).flatMap(([key, value]) => [key, value]),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

export function LogsPage() {
  const { module } = useParams();
  const selected = moduleByPath(module);

  if (!selected) {
    return (
      <section>
        <p className="eyebrow">Logs center</p>
        <h2>日志</h2>
        <p>正常运行流水不会收集；除审计留痕外，这里主要展示 warning/error，方便排查生产问题。</p>
        <div className="log-module-grid">
          {LOG_MODULES.map((item) => (
            <Link key={item.path} to={`/logs/${item.path}`} className="log-module-card">
              <span className="eyebrow">{item.category}</span>
              <h3>{item.title}</h3>
              <p>{item.description}</p>
            </Link>
          ))}
        </div>
      </section>
    );
  }

  return <LogModulePage module={selected} />;
}

function LogModulePage({ module }: { module: (typeof LOG_MODULES)[number] }) {
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [levelFilter, setLevelFilter] = useState<"all" | LogEntry["level"]>("all");
  const logs = useQuery({
    queryKey: ["logs", module.category],
    queryFn: () => api.logs(module.category),
  });
  const entries = logs.data ?? [];
  const visibleEntries = useMemo(
    () => entries.filter((entry) => logMatchesFilters(entry, searchTerm, levelFilter)),
    [entries, searchTerm, levelFilter],
  );
  const selectedEntries = visibleEntries.filter((entry) => selectedIds.includes(entry.id));
  const allSelected = visibleEntries.length > 0 && visibleEntries.every((entry) => selectedIds.includes(entry.id));

  if (logs.isLoading) return <p>正在加载{module.title}...</p>;
  if (logs.isError) return <p role="alert">{formatApiError(logs.error, `${module.title}加载失败`)}</p>;

  function toggleLog(id: string) {
    setSelectedIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  function toggleAllLogs() {
    const visibleIds = visibleEntries.map((entry) => entry.id);
    setSelectedIds((current) => {
      if (allSelected) return current.filter((id) => !visibleIds.includes(id));
      return Array.from(new Set([...current, ...visibleIds]));
    });
  }

  return (
    <section>
      <p className="eyebrow">Logs center</p>
      <h2>{module.title}</h2>
      <p>{module.description}</p>
      <div className="toolbar">
        <Link to="/logs" className="button-link">
          返回日志入口
        </Link>
        <button type="button" onClick={() => exportLogs(module.category, entries)} disabled={entries.length === 0}>
          导出安全 JSON
        </button>
      </div>
      <div className="toolbar">
        <label>
          搜索日志
          <input
            type="search"
            aria-label="搜索日志"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            placeholder="搜索标题、消息、来源或详情"
          />
        </label>
        <label>
          日志级别
          <select
            aria-label="日志级别"
            value={levelFilter}
            onChange={(event) => setLevelFilter(event.target.value as typeof levelFilter)}
          >
            <option value="all">全部级别</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </select>
        </label>
      </div>

      {visibleEntries.length === 0 ? (
        <article>
          <h3>暂无日志</h3>
          <p>当前筛选条件下没有匹配日志。</p>
        </article>
      ) : (
        <>
          <div className="bulk-action-bar">
            <label className="inline-check compact-check">
              <input
                type="checkbox"
                aria-label="Select all logs in current module"
                checked={allSelected}
                onChange={toggleAllLogs}
              />
              全选当前日志
            </label>
            <button
              type="button"
              className="secondary-action"
              disabled={selectedEntries.length === 0}
              onClick={() => exportLogs(`${module.category}-selected`, selectedEntries)}
            >
              导出已选 JSON
            </button>
            <small>已选 {selectedEntries.length}</small>
          </div>
          <div className="log-list">
            {visibleEntries.map((entry) => (
              <article key={entry.id} className="log-entry">
                <div className="log-entry-header">
                  <input
                    type="checkbox"
                    aria-label={`Select log ${entry.id}`}
                    checked={selectedIds.includes(entry.id)}
                    onChange={() => toggleLog(entry.id)}
                  />
                  <span className={`level-pill level-${entry.level}`}>{entry.level}</span>
                  <div>
                    <h3>{entry.title}</h3>
                    <p>{entry.message}</p>
                  </div>
                </div>
                <dl className="diagnostic-grid">
                  <div className="diagnostic-row">
                    <dt>来源</dt>
                    <dd>{entry.source}</dd>
                  </div>
                  <div className="diagnostic-row">
                    <dt>时间</dt>
                    <dd>
                      <time dateTime={entry.created_at}>{entry.created_at}</time>
                    </dd>
                  </div>
                  {Object.entries(entry.details).map(([key, value]) => (
                    <div key={key} className="diagnostic-row">
                      <dt>{key}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
