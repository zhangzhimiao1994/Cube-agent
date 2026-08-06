import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const owner = {
  username: "owner",
  role: "super_admin",
  permissions: ["*"],
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/me") {
          return jsonResponse({ error: "unauthenticated" }, { status: 401 });
        }
        if (path === "/api/v1/auth/login") {
          const body = JSON.parse(String(init?.body));
          if (body.username === "owner" && body.password === "correct horse battery staple") {
            return jsonResponse(owner);
          }
          return jsonResponse({ error: "invalid_credentials" }, { status: 401 });
        }
        if (path === "/api/v1/setup") {
          return jsonResponse(owner);
        }
        if (path === "/api/v1/auth/logout") {
          return new Response(null, { status: 204 });
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("logs in and opens the dashboard", async () => {
    render(<TestApp initialPath="/login" />);
    await userEvent.type(screen.getByLabelText("用户名"), "owner");
    await userEvent.type(screen.getByLabelText("密码"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("heading", { name: "运行概览" })).not.toBeNull();
  });

  it("shows invalid credentials without storing browser tokens", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<TestApp initialPath="/login" />);
    await userEvent.type(screen.getByLabelText("用户名"), "owner");
    await userEvent.type(screen.getByLabelText("密码"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect((await screen.findByRole("alert")).textContent).toBe("用户名或密码错误");
    expect(setItem).not.toHaveBeenCalled();
  });

  it("creates the first admin through setup", async () => {
    render(<TestApp initialPath="/setup" />);
    await userEvent.type(screen.getByLabelText("初始化码"), "setup-code");
    await userEvent.type(screen.getByLabelText("用户名"), "owner");
    await userEvent.type(screen.getByLabelText("密码"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    expect(await screen.findByRole("heading", { name: "运行概览" })).not.toBeNull();
  });

  it("protects the dashboard while session is missing", async () => {
    render(<TestApp initialPath="/" />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "登录" })).not.toBeNull());
  });

  it("shows navigation only for returned permissions", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/me") {
          return jsonResponse({
            username: "viewer",
            role: "viewer",
            permissions: ["run:read", "config:read"],
          });
        }
        return new Response(null, { status: 204 });
      }),
    );

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("link", { name: "运行概览" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "配置" })).not.toBeNull();
    expect(screen.queryByRole("link", { name: "Skills" })).toBeNull();
  });
});
