import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  username: "owner",
  role: "super_admin",
  permissions: ["*"],
};

const ownerToken = {
  access_token: "owner-token",
  token_type: "bearer",
  principal,
};

const runs = [
  {
    id: "22222222-2222-4222-8222-222222222222",
    status: "running",
    mode: "dispatch",
    queue_wait_ms: 120,
    capacity_wait_ms: 40,
    cost_usd: "0.0132",
  },
];

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("LoginPage", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/auth/me") {
          return jsonResponse({ error: "unauthenticated" }, { status: 401 });
        }
        if (path === "/api/v1/auth/login") {
          const body = JSON.parse(String(init?.body));
          if (
            (body.tenant_id === principal.tenant_id || body.tenant_id === undefined) &&
            body.username === "owner" &&
            body.password === "correct horse battery staple"
          ) {
            return jsonResponse(ownerToken);
          }
          return jsonResponse(
            { error: { code: "invalid_credentials", message: "invalid credentials" } },
            { status: 401 },
          );
        }
        if (path === "/api/v1/setup") {
          const body = JSON.parse(String(init?.body));
          if (body.code !== "setup-code") {
            return jsonResponse(
              { error: { code: "invalid_bootstrap", message: "invalid bootstrap code" } },
              { status: 401 },
            );
          }
          return jsonResponse(ownerToken);
        }
        if (path === "/api/v1/admin/runs") {
          if (init?.headers instanceof Headers) {
            expect(init.headers.get("Authorization")).toBe("Bearer owner-token");
          } else {
            expect((init?.headers as Record<string, string>).Authorization).toBe(
              "Bearer owner-token",
            );
          }
          return jsonResponse(runs);
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("logs in and opens the run dashboard", async () => {
    render(<TestApp initialPath="/login" />);
    expect(await screen.findByRole("heading", { name: "魔方agent" })).not.toBeNull();
    expect(screen.getByRole("heading", { name: "登录工作台" })).not.toBeNull();
    expect(screen.getByText("把对话、模型、工具和自动化任务放进同一个工作台，关键操作可追踪、可审批、可恢复。")).not.toBeNull();
    expect(screen.getByText("继续处理对话、计划任务、工具审批和系统配置。")).not.toBeNull();
    expect(screen.getByAltText("魔方agent")).not.toBeNull();
    expect(screen.queryByText(/Agent Hub/i)).toBeNull();
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
  });

  it("shows invalid credentials without storing browser tokens", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<TestApp initialPath="/login" />);
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect((await screen.findByRole("alert")).textContent).toBe("账号或密码不对，请再检查一次。");
    expect(setItem).not.toHaveBeenCalled();
  });

  it("creates the first admin through setup", async () => {
    render(<TestApp initialPath="/setup" />);
    expect(await screen.findByText("魔方agent")).not.toBeNull();
    expect(screen.getByAltText("魔方agent")).not.toBeNull();
    expect(screen.getByText("创建第一个管理员后，就可以接入模型、通道、Skill 和 OpenClaw 工具。")).not.toBeNull();
    expect(screen.getByText("设置码只用于首次启用，使用后立即失效。")).not.toBeNull();
    expect(screen.queryByText(/Agent Hub/i)).toBeNull();
    expect(screen.getByText("安装依赖")).not.toBeNull();
    expect(screen.getByText("部署版本")).not.toBeNull();
    expect(screen.getByText("执行迁移")).not.toBeNull();
    expect(screen.getByText("启动服务")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "创建第一个账号" })).not.toBeNull();
    await userEvent.type(screen.getByLabelText("Setup code"), "setup-code");
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
  });

  it("shows the setup failure reason returned by the API", async () => {
    render(<TestApp initialPath="/setup" />);
    await userEvent.type(await screen.findByLabelText("Setup code"), "wrong-code");
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "初始化失败: invalid bootstrap code (invalid_bootstrap, HTTP 401)",
    );
  });

  it("protects the dashboard while session is missing", async () => {
    render(<TestApp initialPath="/" />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "魔方agent" })).not.toBeNull());
    expect(screen.getByAltText("魔方agent")).not.toBeNull();
  });

  it("keeps module groups visible and lets viewers use installed tool capabilities", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "44444444-4444-4444-8444-444444444444",
            tenant_id: principal.tenant_id,
            username: "viewer",
            role: "viewer",
            permissions: [
              "run:read",
              "agent:read",
              "config:read",
              "skill:read",
              "mcp:read",
              "audit:read",
            ],
          });
        }
        if (String(input) === "/api/v1/admin/runs") {
          return jsonResponse(runs);
        }
        return new Response(null, { status: 204 });
      }),
    );

    render(<TestApp initialPath="/extensions" />);

    expect(await screen.findByRole("link", { name: "对话与进化" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "编排" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "资源" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "工具" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "通道" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "系统" })).not.toBeNull();
    expect(screen.getAllByRole("link", { name: /Skill/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /MCP/ }).length).toBeGreaterThan(0);
  });
});
