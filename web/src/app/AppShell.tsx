import { NavLink, Outlet } from "react-router-dom";

import { MODULE_GROUPS } from "./navigation";
import { useAuth } from "../auth/AuthProvider";

export function AppShell() {
  const auth = useAuth();
  const visibleGroups = MODULE_GROUPS.map((group) => ({
    ...group,
    modules: group.modules.filter((module) => hasPermission(auth.user?.permissions ?? [], module.permission)),
  })).filter((group) => group.modules.length > 0);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-card">
          <span className="eyebrow">Agent Hub</span>
          <h1>控制中枢</h1>
          <p>面向生产环境的多 Agent 调度与配置控制台</p>
        </div>
        <nav aria-label="Main navigation" className="nav-list">
          {visibleGroups.map((item) => (
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
        <main className="page-surface">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function hasPermission(grants: string[], permission: string): boolean {
  if (grants.includes("*") || grants.includes(permission)) return true;
  const [namespace] = permission.split(":");
  return grants.includes(`${namespace}:*`);
}
