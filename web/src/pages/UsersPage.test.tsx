import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const owner = {
  id: "11111111-1111-4111-8111-111111111111",
  username: "owner",
  role: "super_admin",
  disabled: false,
  feishu_open_id: "ou_owner",
  protected: true,
};

const operator = {
  id: "22222222-2222-4222-8222-222222222222",
  username: "ops-user",
  role: "operator",
  disabled: false,
  feishu_open_id: null,
  protected: false,
};

describe("UsersPage", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: owner.id,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/users" && (!init?.method || init.method === "GET")) {
          return jsonResponse([owner, operator]);
        }
        if (path === "/api/v1/users" && init?.method === "POST") {
          return jsonResponse({
            id: "44444444-4444-4444-8444-444444444444",
            username: "new-ops",
            role: "operator",
            disabled: false,
            feishu_open_id: null,
            protected: false,
          });
        }
        if (path.endsWith("/role") && init?.method === "PATCH") {
          return jsonResponse({ ...operator, role: "admin" });
        }
        if (path.endsWith("/disabled") && init?.method === "PATCH") {
          return jsonResponse({ ...operator, disabled: true });
        }
        if (path === `/api/v1/users/${operator.id}` && init?.method === "PATCH") {
          return jsonResponse({
            ...operator,
            username: "ops-renamed",
            role: "admin",
            disabled: true,
          });
        }
        if (path.endsWith("/password") && init?.method === "PATCH") {
          return jsonResponse(operator);
        }
        if (path.includes(operator.id) && init?.method === "DELETE") {
          return new Response(null, { status: 204 });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("lists users and submits role changes with bearer authentication", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    expect(await screen.findByRole("heading", { name: "用户管理" })).not.toBeNull();
    expect(await screen.findByText("owner")).not.toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("修改 ops-user 的角色"), "admin");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/22222222-2222-4222-8222-222222222222/role",
      expect.objectContaining({
        method: "PATCH",
        credentials: "include",
        headers: expect.objectContaining({ Authorization: "Bearer owner-token" }),
      }),
    );
  });

  it("filters users by Feishu binding and keeps row actions aligned", async () => {
    render(<TestApp initialPath="/users" />);

    const table = await screen.findByRole("table", { name: "用户列表" });
    await userEvent.type(screen.getByRole("textbox", { name: "按飞书绑定筛选" }), "ou_owner");

    expect(within(table).getByText("owner")).not.toBeNull();
    expect(within(table).queryByText("ops-user")).toBeNull();
  });
  it("can create, disable, and delete manageable users", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    await screen.findByRole("heading", { name: "用户管理" });
    await userEvent.type(screen.getByPlaceholderText("ops-user"), "new-ops");
    await userEvent.type(screen.getByPlaceholderText("至少 12 位"), "valid password 456");
    await userEvent.click(screen.getByRole("button", { name: "创建用户" }));
    const opsRow = screen.getByRole("row", { name: /ops-user/ });
    await userEvent.click(within(opsRow).getByRole("button", { name: "禁用" }));
    await userEvent.click(within(opsRow).getByRole("button", { name: "删除" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/22222222-2222-4222-8222-222222222222/disabled",
      expect.objectContaining({ method: "PATCH" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/22222222-2222-4222-8222-222222222222",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("can reset a manageable user's password without exposing the password in the table", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    await screen.findByRole("heading", { name: "用户管理" });
    await userEvent.click(within(screen.getByRole("row", { name: /ops-user/ })).getByRole("button", { name: "重置密码" }));
    await userEvent.type(screen.getByLabelText("新密码"), "new valid password 789");
    await userEvent.click(screen.getByRole("button", { name: "保存新密码" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/22222222-2222-4222-8222-222222222222/password",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ password: "new valid password 789" }),
      }),
    );
    expect(screen.queryByText("new valid password 789")).toBeNull();
  });

  it("opens an explicit edit panel and saves username, role, and disabled state together", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    await screen.findByRole("heading", { name: "用户管理" });
    await userEvent.click(within(screen.getByRole("row", { name: /ops-user/ })).getByRole("button", { name: "编辑" }));
    expect(screen.getByRole("heading", { name: "编辑用户" })).not.toBeNull();

    const usernameInput = screen.getByLabelText("用户名（编辑）");
    await userEvent.clear(usernameInput);
    await userEvent.type(usernameInput, "ops-renamed");
    await userEvent.selectOptions(screen.getByLabelText("角色（编辑）"), "admin");
    await userEvent.click(screen.getByLabelText("禁用该用户"));
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/22222222-2222-4222-8222-222222222222",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ username: "ops-renamed", role: "admin", disabled: true }),
      }),
    );
  });
});
