import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
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
        if (path === "/api/v1/admin/evolution-runs") return jsonResponse([]);
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
        if (path === "/api/v1/admin/main-agent") {
          return jsonResponse({
            model: null,
            control_mode: "supervisor",
            decision_policy: "choose mode first, then roles; main agent makes the final decision",
            hermes_policy: "observe",
            max_review_rounds: 2,
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

    expect(await screen.findAllByText("魔方agent")).not.toHaveLength(0);
    expect(screen.getAllByAltText("魔方agent")).not.toHaveLength(0);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    expect(screen.getAllByText("工作台").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "对话与进化" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "编排" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "系统" })).not.toBeNull();
    const accountNavigation = screen.getByRole("navigation", { name: "账号操作" });
    expect(within(accountNavigation).getByRole("button", { name: "退出当前账号" })).not.toBeNull();
    expect(screen.queryByText("实时调度")).toBeNull();
    expect(screen.queryByText("工具防护")).toBeNull();
    expect(screen.queryByText("沉淀经验，但不绕过审核")).toBeNull();
  });

  it("groups navigation into six module hubs with colored module cards", async () => {
    render(<TestApp initialPath="/orchestration" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(6);
    expect(within(navigation).getByRole("link", { name: "对话与进化" })).not.toBeNull();
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

  it("shows tertiary navigation under module drawers without adding top-level entries", async () => {
    render(<TestApp initialPath="/evolution" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(6);

    const workspaceDrawer = screen.getByLabelText("对话与进化二级导航");
    expect(within(workspaceDrawer).getByRole("link", { name: "Skill 进化" }).getAttribute("href")).toBe("/evolution?type=skill");
    expect(within(workspaceDrawer).getByRole("link", { name: "调度策略进化" }).getAttribute("href")).toBe("/evolution?type=scheduler-policy");
  });

  it("activates the matching page section after a third-level menu click", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    const drawer = screen.getByLabelText("编排二级导航");
    await user.click(within(drawer).getByRole("link", { name: "调度策略" }));

    await waitFor(() => {
      expect(document.querySelector('[data-nav-section="scheduler"]')?.getAttribute("data-nav-active")).toBe("true");
    });
  });
  it("makes top-level navigation enter the default module directly while keeping drawer links", async () => {
    render(<TestApp initialPath="/skills" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getByRole("link", { name: "对话与进化" }).getAttribute("href")).toBe("/");
    expect(within(navigation).getByRole("link", { name: "编排" }).getAttribute("href")).toBe("/main-agent");
    expect(within(navigation).getByRole("link", { name: "资源" }).getAttribute("href")).toBe("/models");
    expect(within(navigation).getByRole("link", { name: "工具" }).getAttribute("href")).toBe("/skills");
    expect(within(navigation).getByRole("link", { name: "通道" }).getAttribute("href")).toBe("/channels");
    expect(within(navigation).getByRole("link", { name: "系统" }).getAttribute("href")).toBe("/config");
    expect(screen.getByLabelText("工具二级导航")).not.toBeNull();
  });

  it("does not expose fixed navigation controls", async () => {
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    expect(document.querySelector(".app-shell")?.className).toContain("nav-floating");
    expect(document.querySelector(".app-shell")?.className).not.toContain("nav-pinned");
    expect(screen.queryByRole("button", { name: "固定导航栏" })).toBeNull();
    expect(screen.queryByRole("button", { name: "悬浮导航栏" })).toBeNull();
    expect(screen.queryByRole("button", { name: "固定" })).toBeNull();
  });

  it("opens the floating navigation as a mobile drawer with expandable second-level modules", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    const shell = document.querySelector(".app-shell");
    const mobileTrigger = screen.getByRole("button", { name: "打开导航栏" });

    await user.click(mobileTrigger);

    await waitFor(() => expect(shell?.className).toContain("mobile-nav-open"));
    const mobileNavigation = screen.getByRole("navigation", { name: "手机版主导航" });
    const orchestrationTrigger = within(mobileNavigation).getByRole("button", { name: "展开编排二级导航" });

    await user.click(orchestrationTrigger);

    expect(orchestrationTrigger.getAttribute("aria-expanded")).toBe("true");
    expect(within(mobileNavigation).getByRole("link", { name: /主 Agent/ })).not.toBeNull();
    expect(within(mobileNavigation).getByRole("link", { name: /工作流配置/ })).not.toBeNull();

    await user.click(screen.getAllByRole("button", { name: "关闭导航栏" })[0]);

    await waitFor(() => expect(shell?.className).not.toContain("mobile-nav-open"));
  });

  it("keeps mobile floating navigation as an overlay drawer", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");
    expect(stylesCss).not.toContain("nav-pinned");
    expect(stylesCss).toMatch(/@media \(max-width: 980px\)[\s\S]*\.nav-floating \.floating-nav-panel\s*{[\s\S]*position:\s*fixed;[\s\S]*transform:\s*translateX\(-105%\);/);
    expect(stylesCss).toMatch(/@media \(max-width: 980px\)[\s\S]*\.nav-floating \.nav-list,[\s\S]*\.nav-floating \.nav-drawer\s*{[\s\S]*display:\s*none;/);
    expect(stylesCss).toMatch(/@media \(max-width: 980px\)[\s\S]*\.nav-floating \.mobile-nav-groups\s*{[\s\S]*display:\s*grid;/);
    expect(stylesCss).toMatch(/\.history-drawer-open \.chat-panel\s*{[\s\S]*opacity:\s*0\.38;[\s\S]*pointer-events:\s*none;/);
    expect(stylesCss).toContain("P3 conversation history drawer compactness");
    expect(stylesCss).toMatch(/\.conversation-list\s*{[\s\S]*border-radius:\s*8px;[\s\S]*width:\s*min\(360px, calc\(100vw - 1\.5rem\)\);/);
    expect(stylesCss).toMatch(/\.conversation-title-text\s*{[\s\S]*text-overflow:\s*ellipsis;[\s\S]*white-space:\s*nowrap;/);
    expect(stylesCss).toMatch(/@media \(max-width: 640px\)[\s\S]*\.conversation-list\s*{[\s\S]*width:\s*min\(86vw, 360px\);/);
  });
});
