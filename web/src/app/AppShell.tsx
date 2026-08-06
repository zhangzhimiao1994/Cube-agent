import { Link, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

const NAVIGATION = [
  { to: "/", label: "运行概览", permission: "run:read" },
  { to: "/config", label: "配置", permission: "config:read" },
  { to: "/skills", label: "Skills", permission: "skill:read" },
  { to: "/mcp", label: "MCP", permission: "mcp:read" },
  { to: "/audit", label: "审计", permission: "audit:read" },
];

export function AppShell() {
  const auth = useAuth();
  return (
    <div>
      <header>
        <h1>Agent Hub</h1>
        <p>{auth.user?.username}</p>
        <button type="button" onClick={() => void auth.logout()}>
          退出
        </button>
      </header>
      <nav aria-label="主导航">
        {NAVIGATION.filter((item) => auth.hasPermission(item.permission)).map((item) => (
          <Link key={item.to} to={item.to}>
            {item.label}
          </Link>
        ))}
      </nav>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
