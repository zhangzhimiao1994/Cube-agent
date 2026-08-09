import { useQuery } from "@tanstack/react-query";
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
    title: "大模型错误",
    description: "模型可用性测试、上游状态码、API Base、模型名等诊断信息。",
  },
  {
    path: "mode",
    category: "mode_error",
    title: "模式运行错误",
    description: "direct、dispatch、discuss、hybrid 等模式执行失败记录。",
  },
  {
    path: "feature",
    category: "feature_error",
    title: "主要功能运行错误",
    description: "Skill、MCP、记忆、Hermes 等主要功能的 warning/error。",
  },
  {
    path: "agent",
    category: "agent_error",
    title: "Agent 角色",
    description: "角色配置、模型绑定、提示词缺失等 Agent 配置错误。",
  },
  {
    path: "channel",
    category: "channel_error",
    title: "通道连接",
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
  const logs = useQuery({
    queryKey: ["logs", module.category],
    queryFn: () => api.logs(module.category),
  });

  if (logs.isLoading) return <p>正在加载{module.title}...</p>;
  if (logs.isError) return <p role="alert">{formatApiError(logs.error, `${module.title}加载失败`)}</p>;

  const entries = logs.data ?? [];
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

      {entries.length === 0 ? (
        <article>
          <h3>暂无日志</h3>
          <p>当前模块没有 warning/error；正常运行流水不会写入这里。</p>
        </article>
      ) : (
        <div className="log-list">
          {entries.map((entry) => (
            <article key={entry.id} className="log-entry">
              <div className="log-entry-header">
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
      )}
    </section>
  );
}
