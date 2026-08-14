import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, formatApiError, type LogEntry } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

const LOG_MODULES = [
  {
    path: "audit",
    category: "audit",
    title: "审计日志",
    description: "配置发布、Skill 审批、运行控制和用户对话提交等必须留痕的安全审计记录。",
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

function logDetailsText(entry: LogEntry) {
  return Object.entries(entry.details).map(([key, value]) => `${key}: ${value}`).join("; ");
}

function auditDisplayMessage(entry: LogEntry) {
  if (entry.category !== "audit") return entry.message;
  if (entry.details.action === "run.submit" || entry.message === "run.submit") return "对话提交";
  return entry.message;
}

function auditConversationSummary(entry: LogEntry) {
  if (entry.category !== "audit" || (entry.details.action !== "run.submit" && entry.message !== "run.submit")) return null;
  return [
    `用户 ${entry.details.user_id ?? entry.details.actor ?? "未知"}`,
    `对话 ${entry.details.conversation_id ?? "未关联"}`,
    `运行 ${entry.details.run_id ?? entry.details.resource ?? "未知"}`,
    `模式 ${entry.details.accepted_mode ?? entry.details.mode ?? "未知"}`,
  ].join(" / ");
}

function logMatchesFilters(entry: LogEntry, searchTerm: string, levelFilter: "all" | LogEntry["level"]) {
  if (levelFilter !== "all" && entry.level !== levelFilter) return false;
  return textContains(
    [
      entry.id,
      entry.category,
      entry.level,
      entry.title,
      auditDisplayMessage(entry),
      auditConversationSummary(entry) ?? "",
      entry.source,
      entry.created_at,
      logDetailsText(entry),
    ].join(" "),
    searchTerm,
  );
}

type LogSortKey = "level" | "title" | "source" | "time" | "details";

type LogColumnFilters = {
  details: string;
  level: "all" | LogEntry["level"];
  source: string;
  time: string;
  title: string;
};

const EMPTY_LOG_FILTERS: LogColumnFilters = {
  details: "",
  level: "all",
  source: "",
  time: "",
  title: "",
};

function matchesLogColumns(entry: LogEntry, filters: LogColumnFilters) {
  return (
    (filters.level === "all" || entry.level === filters.level) &&
    textContains(`${entry.title} ${auditDisplayMessage(entry)} ${auditConversationSummary(entry) ?? ""}`, filters.title) &&
    textContains(entry.source, filters.source) &&
    textContains(entry.created_at, filters.time) &&
    textContains(logDetailsText(entry), filters.details)
  );
}

function logSortValue(entry: LogEntry, key: LogSortKey) {
  if (key === "level") return entry.level;
  if (key === "title") return `${entry.title} ${auditDisplayMessage(entry)} ${auditConversationSummary(entry) ?? ""}`;
  if (key === "source") return entry.source;
  if (key === "time") return entry.created_at;
  return logDetailsText(entry);
}

function sortedLogs(entries: LogEntry[], sort: SortState<LogSortKey>) {
  return [...entries].sort((left, right) => compareText(logSortValue(left, sort.key), logSortValue(right, sort.key), sort.direction));
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
  const [columnFilters, setColumnFilters] = useState<LogColumnFilters>(EMPTY_LOG_FILTERS);
  const [sort, setSort] = useState<SortState<LogSortKey>>({ key: "time", direction: "desc" });
  const logs = useQuery({
    queryKey: ["logs", module.category],
    queryFn: () => api.logs(module.category),
  });
  const entries = logs.data ?? [];
  const visibleEntries = useMemo(
    () => sortedLogs(entries.filter((entry) => logMatchesFilters(entry, searchTerm, levelFilter) && matchesLogColumns(entry, columnFilters)), sort),
    [columnFilters, entries, searchTerm, levelFilter, sort],
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

  function updateColumnFilter<Key extends keyof LogColumnFilters>(key: Key, value: LogColumnFilters[Key]) {
    setColumnFilters((current) => ({ ...current, [key]: value }));
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
      <div className="list-toolbar">
        <label>
          搜索日志
          <input
            type="search"
            aria-label="搜索日志"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            placeholder={module.category === "audit" ? "搜索用户、对话、操作、来源或详情" : "搜索标题、消息、来源或详情"}
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
        <button type="button" className="secondary-action" onClick={() => { setSearchTerm(""); setLevelFilter("all"); setColumnFilters(EMPTY_LOG_FILTERS); }}>
          清空筛选
        </button>
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
            <small>当前结果已选 {selectedEntries.length}</small>
          </div>
          <table aria-label={`${module.title}列表`} className="dense-table">
            <thead>
              <tr>
                <th>选择</th>
                <th><SortHeader column="level" label="级别" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>级别</SortHeader></th>
                <th><SortHeader column="title" label="标题与消息" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>标题与消息</SortHeader></th>
                <th><SortHeader column="source" label="来源" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>来源</SortHeader></th>
                <th><SortHeader column="time" label="时间" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>时间</SortHeader></th>
                <th><SortHeader column="details" label="详情" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>详情</SortHeader></th>
              </tr>
              <tr className="table-filter-row">
                <th aria-label="日志选择筛选占位" />
                <th>
                  <select aria-label="按日志级别筛选" value={columnFilters.level} onChange={(event) => updateColumnFilter("level", event.currentTarget.value as LogColumnFilters["level"])}>
                    <option value="all">全部</option>
                    <option value="info">info</option>
                    <option value="warning">warning</option>
                    <option value="error">error</option>
                  </select>
                </th>
                <th><input aria-label="按日志标题筛选" value={columnFilters.title} onChange={(event) => updateColumnFilter("title", event.currentTarget.value)} placeholder={module.category === "audit" ? "操作或对话" : "标题或消息"} /></th>
                <th><input aria-label="按日志来源筛选" value={columnFilters.source} onChange={(event) => updateColumnFilter("source", event.currentTarget.value)} placeholder="来源" /></th>
                <th><input aria-label="按日志时间筛选" value={columnFilters.time} onChange={(event) => updateColumnFilter("time", event.currentTarget.value)} placeholder="时间" /></th>
                <th><input aria-label="按日志详情筛选" value={columnFilters.details} onChange={(event) => updateColumnFilter("details", event.currentTarget.value)} placeholder={module.category === "audit" ? "用户、对话或运行" : "详情键或值"} /></th>
              </tr>
            </thead>
            <tbody>
              {visibleEntries.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select log ${entry.id}`}
                      checked={selectedIds.includes(entry.id)}
                      onChange={() => toggleLog(entry.id)}
                    />
                  </td>
                  <td><span className={`level-pill level-${entry.level}`}>{entry.level}</span></td>
                  <td>
                    <strong>{entry.title}</strong>
                    <br />
                    <span>{auditDisplayMessage(entry)}</span>
                    {auditConversationSummary(entry) ? (
                      <>
                        <br />
                        <small>{auditConversationSummary(entry)}</small>
                      </>
                    ) : null}
                  </td>
                  <td>{entry.source}</td>
                  <td><time dateTime={entry.created_at}>{entry.created_at}</time></td>
                  <td>{logDetailsText(entry)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
