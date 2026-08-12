import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useMemo, useState } from "react";

import { MODULE_GROUPS } from "./navigation";
import { useAuth } from "../auth/AuthProvider";

export function AppShell() {
  const auth = useAuth();
  const location = useLocation();
  const [hoveredGroupId, setHoveredGroupId] = useState<string | null>(null);
  const visibleGroups = MODULE_GROUPS.map((group) => ({
    ...group,
    modules: group.modules.filter((module) => hasPermission(auth.user?.permissions ?? [], module.permission)),
  })).filter((group) => group.modules.length > 0);
  const activeGroup = useMemo(
    () =>
      visibleGroups.find(
        (group) =>
          location.pathname === group.to ||
          group.modules.some((module) => module.to === location.pathname || location.pathname.startsWith(`${module.to}/`)),
      ) ?? visibleGroups[0],
    [location.pathname, visibleGroups],
  );
  const drawerGroup = visibleGroups.find((group) => group.id === hoveredGroupId) ?? activeGroup;

  return (
    <div className="app-shell">
      <aside className="sidebar floating-sidebar" onMouseLeave={() => setHoveredGroupId(null)}>
        <div className="floating-nav-rail">
          <div className="brand-card compact-brand-card">
            <span className="eyebrow">Agent Hub</span>
            <h1>控制台</h1>
          </div>
          <nav aria-label="Main navigation" className="nav-list">
            {visibleGroups.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                onFocus={() => setHoveredGroupId(item.id)}
                onMouseEnter={() => setHoveredGroupId(item.id)}
                className={({ isActive }) =>
                  `nav-link${isActive || item.id === activeGroup?.id ? " nav-link-active" : ""}`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
        {drawerGroup ? (
          <section className={`nav-drawer nav-drawer-${drawerGroup.tone}`} aria-label={`${drawerGroup.label}二级导航`}>
            <div>
              <p className="eyebrow">{drawerGroup.eyebrow}</p>
              <strong className="nav-drawer-title">{drawerGroup.label}</strong>
              <p>{drawerGroup.description}</p>
            </div>
            <div className="nav-drawer-links" role="list">
              {drawerGroup.modules.map((module) => (
                <Link key={module.to} to={module.to} className="nav-drawer-link">
                  <strong>{module.label}</strong>
                  <span>{module.description}</span>
                </Link>
              ))}
            </div>
          </section>
        ) : null}
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
