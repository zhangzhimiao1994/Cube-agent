import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

const NAVIGATION = [
  { to: "/", label: "对话", permission: "run:read" },
  { to: "/config", label: "设置", permission: "config:read" },
  { to: "/models", label: "模型与 API", permission: "config:read" },
  { to: "/agents", label: "Agent 角色", permission: "agent:read" },
  { to: "/workflows", label: "工作流配置", permission: "agent:read" },
  { to: "/skills", label: "技能", permission: "skill:read" },
  { to: "/mcp", label: "MCP 工具", permission: "mcp:read" },
  { to: "/channels", label: "通道连接", permission: "config:read" },
  { to: "/memory", label: "记忆", permission: "memory:read" },
  { to: "/hermes", label: "Hermes 学习", permission: "hermes:read" },
  { to: "/users", label: "用户", permission: "user:read" },
  { to: "/logs", label: "日志", permission: "audit:read" },
];

const HEALTH_CARDS = [
  ["实时调度", "自动、直接、派单、讨论、混合模式"],
  ["工具防护", "Skill 与 MCP 均经过权限边界"],
  ["Hermes 学习", "沉淀经验，但不绕过审批"],
] as const;

export function AppShell() {
  const auth = useAuth();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-card">
          <span className="eyebrow">Agent Hub</span>
          <h1>控制中枢</h1>
          <p>面向生产环境的多 Agent 调度与配置控制台</p>
        </div>
        <nav aria-label="Main navigation" className="nav-list">
          {NAVIGATION.filter((item) => auth.hasPermission(item.permission)).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link${isActive ? " nav-link-active" : ""}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Production management</p>
            <h2>Agent 编排控制台</h2>
          </div>
          <div className="user-chip">
            <span>{auth.user?.username}</span>
            <button type="button" onClick={() => void auth.logout()}>
              退出
            </button>
          </div>
        </header>
        <section className="status-grid" aria-label="Console status">
          {HEALTH_CARDS.map(([title, detail]) => (
            <article className="status-card" key={title}>
              <span>{title}</span>
              <p>{detail}</p>
            </article>
          ))}
        </section>
        <main className="page-surface">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
