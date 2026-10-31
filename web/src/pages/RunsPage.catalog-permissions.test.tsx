import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type CurrentUser } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { RunsPage } from "./RunsPage";

// The ordinary operator grants from auth/models.py deliberately exclude agent:read.
const operatorPermissions = [
  "run:create", "run:read", "run:pause", "run:resume", "run:cancel", "config:read",
  "skill:read", "skill:use", "mcp:read", "mcp:use", "plugin:read", "plugin:use",
];
const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("does not expose inherited legacy or foreign-actor admin catalogs to an operator", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const privateAgent = { id: "private-agent", name: "ADMIN_ONLY_AGENT", enabled: true };
  const privateWorkflow = { id: "private-workflow", name: "ADMIN_ONLY_WORKFLOW", enabled: true };
  // An inherited client may contain legacy unscoped entries before AuthProvider
  // has registered its first actor. Also retain the former admin's scoped entries.
  for (const scope of [[], ["other-tenant", "former-admin", "admin"]]) {
    client.setQueryData(["agents", ...scope], [privateAgent]);
    client.setQueryData(["workflows", ...scope], [privateWorkflow]);
  }
  vi.spyOn(api, "me").mockResolvedValue({
    tenant_id: "tenant", user_id: "ordinary-user", username: "ordinary", role: "operator",
    permissions: operatorPermissions,
  });
  vi.spyOn(api, "runs").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue([]);
  vi.spyOn(api, "conversations").mockResolvedValue([]);
  vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
  vi.spyOn(api, "executionBackends").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  vi.spyOn(api, "settings").mockImplementation(() => new Promise(() => {}));
  vi.spyOn(api, "mainAgent").mockImplementation(() => new Promise(() => {}));
  const fetch = vi.fn(() => Promise.reject(new Error("Unexpected HTTP request")));
  vi.stubGlobal("fetch", fetch);
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter><RunsPage /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await screen.findByRole("form", { name: "发送消息" });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "更多消息工具" }));
  await user.click(screen.getByRole("menuitem", { name: "运行设置" }));
  expect(screen.queryByRole("option", { name: "ADMIN_ONLY_WORKFLOW" })).toBeNull();
  const observedCatalogs = client.getQueryCache().getAll().filter((query) =>
    ["agents", "workflows"].includes(String(query.queryKey[0])) && query.getObserversCount() > 0);
  expect(observedCatalogs).toHaveLength(2);
  expect(JSON.stringify(observedCatalogs.map((query) => query.state.data))).not.toContain("ADMIN_ONLY_");
  expect(fetch).not.toHaveBeenCalled();
});

it.each([
  { role: "operator", permissions: operatorPermissions, allowed: false },
  { role: "admin", permissions: [...operatorPermissions, "agent:*"], allowed: true },
  { role: "super_admin", permissions: ["*"], allowed: true },
])("gates agent and workflow HTTP reads for $role without blocking the composer", async ({ role, permissions, allowed }) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  vi.spyOn(api, "me").mockResolvedValue({
    tenant_id: "tenant", user_id: "ordinary-user", username: "ordinary", role, permissions,
  } as CurrentUser);
  vi.spyOn(api, "runs").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue([]);
  vi.spyOn(api, "conversations").mockResolvedValue([]);
  vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
  vi.spyOn(api, "executionBackends").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  // These unrelated reads may still be loading when the composer is displayed.
  vi.spyOn(api, "settings").mockImplementation(() => new Promise(() => {}));
  vi.spyOn(api, "mainAgent").mockImplementation(() => new Promise(() => {}));
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://localhost").pathname;
    requests.push(path);
    if (!["/api/v1/admin/agents", "/api/v1/admin/workflows"].includes(path)) {
      throw new Error(`Unexpected HTTP request: ${path}`);
    }
    return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter><RunsPage /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await screen.findByRole("form", { name: "发送消息" });
  await waitFor(() => {
    expect(client.getQueryState(["agents", "tenant", "ordinary-user", role])?.fetchStatus).toBe("idle");
    expect(client.getQueryState(["workflows", "tenant", "ordinary-user", role])?.fetchStatus).toBe("idle");
  });
  if (allowed) {
    expect(requests).toContain("/api/v1/admin/agents");
    expect(requests).toContain("/api/v1/admin/workflows");
  } else {
    expect(requests).toEqual([]);
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["agents"] });
      await client.invalidateQueries({ queryKey: ["workflows"] });
    });
    expect(requests).toEqual([]);
  }
});
