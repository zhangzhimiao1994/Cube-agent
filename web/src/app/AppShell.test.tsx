import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "./router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("AppShell presentation", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "owner",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === "/api/v1/admin/runs") return jsonResponse([]);
        if (path === "/api/v1/admin/agents") return jsonResponse([]);
        if (path === "/api/v1/admin/workflows") return jsonResponse([]);
        if (path === "/api/v1/admin/settings") {
          return jsonResponse({
            default_mode: "auto",
            default_workflow_id: null,
            default_agent_ids: [],
            log_level: "warning",
            hermes_enabled: true,
            safe_tools_enabled: true,
            require_approval_for_tools: true,
            channel_entry: "web",
          });
        }
        if (path.startsWith("/api/v1/admin/logs")) return jsonResponse([]);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders the operations shell without global capability cards above every page", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    expect(screen.getByText("控制台")).not.toBeNull();
    expect(screen.getByRole("link", { name: "对话" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "编排" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "系统" })).not.toBeNull();
    expect(screen.queryByText("实时调度")).toBeNull();
    expect(screen.queryByText("工具防护")).toBeNull();
    expect(screen.queryByText("沉淀经验，但不绕过审核")).toBeNull();
  });

  it("groups navigation into six module hubs with colored module cards", async () => {
    render(<TestApp initialPath="/orchestration" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(6);
    expect(within(navigation).getByRole("link", { name: "对话" })).not.toBeNull();
    expect(within(navigation).getByRole("link", { name: "编排" })).not.toBeNull();
    expect(within(navigation).getByRole("link", { name: "资源" })).not.toBeNull();
    expect(within(navigation).getByRole("link", { name: "工具" })).not.toBeNull();
    expect(within(navigation).getByRole("link", { name: "通道" })).not.toBeNull();
    expect(within(navigation).getByRole("link", { name: "系统" })).not.toBeNull();

    const moduleGrid = screen.getByRole("list", { name: "编排模块" });
    expect(within(moduleGrid).getByRole("link", { name: /主 Agent/ })).not.toBeNull();
    expect(within(moduleGrid).getByRole("link", { name: /Agent 角色/ })).not.toBeNull();
    expect(within(moduleGrid).getByRole("link", { name: /工作流配置/ })).not.toBeNull();
    expect(within(moduleGrid).getByRole("link", { name: /Hermes 学习/ })).not.toBeNull();

    const drawer = screen.getByLabelText("编排二级导航");
    expect(within(drawer).getByRole("link", { name: /主 Agent/ })).not.toBeNull();
    expect(within(drawer).getByRole("link", { name: /工作流配置/ })).not.toBeNull();
  });

  it("makes top-level navigation enter the default module directly while keeping drawer links", async () => {
    render(<TestApp initialPath="/skills" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getByRole("link", { name: "对话" }).getAttribute("href")).toBe("/");
    expect(within(navigation).getByRole("link", { name: "编排" }).getAttribute("href")).toBe("/main-agent");
    expect(within(navigation).getByRole("link", { name: "资源" }).getAttribute("href")).toBe("/models");
    expect(within(navigation).getByRole("link", { name: "工具" }).getAttribute("href")).toBe("/skills");
    expect(within(navigation).getByRole("link", { name: "通道" }).getAttribute("href")).toBe("/channels");
    expect(within(navigation).getByRole("link", { name: "系统" }).getAttribute("href")).toBe("/config");
    expect(screen.getByLabelText("工具二级导航")).not.toBeNull();
  });

  it("lets operators switch the navigation between floating and pinned layouts", async () => {
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    const toggle = screen.getByRole("button", { name: "固定导航栏" });
    expect(document.querySelector(".app-shell")?.className).toContain("nav-floating");

    fireEvent.click(toggle);

    expect(document.querySelector(".app-shell")?.className).toContain("nav-pinned");
    expect(window.localStorage.getItem("agent_hub_nav_layout")).toBe("pinned");
    expect(screen.getByRole("button", { name: "悬浮导航栏" })).not.toBeNull();
  });
});
