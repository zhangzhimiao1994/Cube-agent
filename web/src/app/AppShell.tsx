import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";

import { APP_BRAND_LOGO_SRC, APP_BRAND_NAME } from "./brand";
import { MODULE_GROUPS } from "./navigation";
import { useAuth } from "../auth/AuthProvider";

export function AppShell() {
  const auth = useAuth();
  const location = useLocation();
  const [hoveredGroupId, setHoveredGroupId] = useState<string | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [expandedMobileGroupId, setExpandedMobileGroupId] = useState<string | null>(null);
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

  useEffect(() => {
    setMobileNavOpen(false);
    setExpandedMobileGroupId(activeGroup?.id ?? null);
  }, [activeGroup?.id, location.pathname]);

  return (
    <div className={`app-shell nav-floating${mobileNavOpen ? " mobile-nav-open" : ""}`}>
      <aside className="sidebar floating-sidebar">
        <div className="mobile-nav-bar">
          <button
            type="button"
            className="mobile-nav-trigger"
            aria-label={mobileNavOpen ? "关闭导航栏" : "打开导航栏"}
            aria-expanded={mobileNavOpen}
            onClick={() => setMobileNavOpen((open) => !open)}
          >
            <span className="mobile-nav-trigger-icon" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          </button>
          <div className="mobile-nav-title">
            <span>{APP_BRAND_NAME}</span>
            <strong>控制台</strong>
          </div>
        </div>
        <button
          type="button"
          className="mobile-nav-backdrop"
          aria-label="关闭导航栏"
          onClick={() => setMobileNavOpen(false)}
        />
        <div className="floating-nav-panel" onMouseLeave={() => setHoveredGroupId(null)}>
          <div className="floating-nav-rail">
            <div className="mobile-drawer-header">
              <span className="brand-lockup">
                <img src={APP_BRAND_LOGO_SRC} alt={APP_BRAND_NAME} />
                <strong>{APP_BRAND_NAME}</strong>
              </span>
              <button type="button" aria-label="关闭导航栏" onClick={() => setMobileNavOpen(false)}>
                ×
              </button>
            </div>
            <div className="brand-card compact-brand-card">
              <img src={APP_BRAND_LOGO_SRC} alt={APP_BRAND_NAME} />
              <span className="eyebrow">{APP_BRAND_NAME}</span>
              <h1>控制台</h1>
            </div>
            <nav aria-label="Main navigation" className="nav-list">
              {visibleGroups.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.modules[0]?.to ?? item.to}
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
            <nav aria-label="手机版主导航" className="mobile-nav-groups">
              {visibleGroups.map((group) => {
                const expanded = expandedMobileGroupId === group.id;
                const submenuId = `mobile-nav-submenu-${group.id}`;
                return (
                  <section key={group.id} className="mobile-nav-group">
                    <button
                      type="button"
                      className={`mobile-nav-group-trigger${group.id === activeGroup?.id ? " mobile-nav-group-active" : ""}`}
                      aria-expanded={expanded}
                      aria-controls={submenuId}
                      aria-label={`${expanded ? "收起" : "展开"}${group.label}二级导航`}
                      onClick={() => setExpandedMobileGroupId(expanded ? null : group.id)}
                    >
                      <span>{group.label}</span>
                      <span className="mobile-nav-chevron" aria-hidden="true">
                        ›
                      </span>
                    </button>
                    <div id={submenuId} className="mobile-nav-submenu" hidden={!expanded}>
                      {group.modules.map((module) => (
                        <Link
                          key={module.to}
                          to={module.to}
                          className="mobile-nav-submenu-link"
                          onClick={() => setMobileNavOpen(false)}
                        >
                          <strong>{module.label}</strong>
                          <span>{module.description}</span>
                        </Link>
                      ))}
                    </div>
                  </section>
                );
              })}
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
        </div>
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
