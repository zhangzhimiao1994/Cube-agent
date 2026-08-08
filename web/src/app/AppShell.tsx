import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

const NAVIGATION = [
  { to: "/", label: "Runs", permission: "run:read" },
  { to: "/config", label: "Config", permission: "config:read" },
  { to: "/models", label: "Models", permission: "config:read" },
  { to: "/agents", label: "Agents", permission: "agent:read" },
  { to: "/workflows", label: "Workflows", permission: "agent:read" },
  { to: "/skills", label: "Skills", permission: "skill:read" },
  { to: "/mcp", label: "MCP", permission: "mcp:read" },
  { to: "/memory", label: "Memory", permission: "memory:read" },
  { to: "/hermes", label: "Hermes", permission: "hermes:read" },
  { to: "/users", label: "Users", permission: "user:read" },
  { to: "/audit", label: "Audit", permission: "audit:read" },
];

const HEALTH_CARDS = [
  ["Live routing", "Auto, dispatch, discuss, hybrid"],
  ["Guarded tools", "Skills and MCP approval gates"],
  ["Hermes learning", "Experience feedback without bypassing approvals"],
] as const;

export function AppShell() {
  const auth = useAuth();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-card">
          <span className="eyebrow">Agent Hub</span>
          <h1>Control console</h1>
          <p>Operations console for multi-agent workflows</p>
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
            <h2>Agent orchestration control plane</h2>
          </div>
          <div className="user-chip">
            <span>{auth.user?.username}</span>
            <button type="button" onClick={() => void auth.logout()}>
              Logout
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
