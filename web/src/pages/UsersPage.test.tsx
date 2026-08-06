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
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/me") {
          return jsonResponse({
            username: "owner",
            role: "super_admin",
            permissions: ["*", "user:read"],
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
        return new Response(null, { status: 204 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists users and submits role changes", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/users" />);

    expect(await screen.findByRole("heading", { name: "用户管理" })).not.toBeNull();
    expect((await screen.findAllByText("owner")).length).toBeGreaterThan(1);
    await userEvent.click(screen.getByRole("button", { name: "设为管理员" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/users/11111111-1111-4111-8111-111111111111/role",
      expect.objectContaining({ method: "PATCH", credentials: "include" }),
    );
  });
});
