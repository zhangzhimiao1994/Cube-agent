import { render, screen } from "@testing-library/react";
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
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/users") {
          return jsonResponse([
            {
              id: "11111111-1111-4111-8111-111111111111",
              username: "owner",
              role: "super_admin",
              disabled: false,
              feishu_open_id: "ou_owner",
            },
          ]);
        }
        if (path.endsWith("/role") && init?.method === "PATCH") {
          return jsonResponse({
            id: "11111111-1111-4111-8111-111111111111",
            username: "owner",
            role: "admin",
            disabled: false,
            feishu_open_id: "ou_owner",
          });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("lists users and submits role changes with bearer authentication", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    expect(await screen.findByRole("heading", { name: "用户管理" })).not.toBeNull();
    expect(await screen.findByText("owner")).not.toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("修改 owner 的角色"), "admin");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/11111111-1111-4111-8111-111111111111/role",
      expect.objectContaining({
        method: "PATCH",
        credentials: "include",
        headers: expect.objectContaining({ Authorization: "Bearer owner-token" }),
      }),
    );
  });
});
