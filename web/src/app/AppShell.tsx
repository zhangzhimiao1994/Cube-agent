import { Link, Outlet } from "react-router-dom";

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

export function AppShell() {
  const auth = useAuth();
  return (
    <div>
      <header>
        <h1>Agent Hub</h1>
        <p>{auth.user?.username}</p>
        <button type="button" onClick={() => void auth.logout()}>
          Logout
        </button>
      </header>
      <nav aria-label="Main navigation">
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
