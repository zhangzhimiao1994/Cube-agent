import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ConfigPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];

  beforeEach(() => {
    requests.length = 0;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/config/current") {
          return jsonResponse({
            id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            version: 3,
            status: "published",
            document: {
              models: {
                main: {
                  deployments: [
                    {
                      provider: "deepseek",
                      model: "deepseek-chat",
                      credential_ref: "secret://live",
                      quota_scope_id: "deepseek-account",
                    },
                  ],
                },
              },
              agents: [],
            },
            created_by: principal.user_id,
            created_at: "2026-08-08T00:00:00Z",
          });
        }
        if (path === "/api/v1/config/drafts" && method === "POST") {
          return jsonResponse(
            {
              id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              version: 4,
              status: "draft",
              document: JSON.parse(String(init?.body)),
              created_by: principal.user_id,
              created_at: "2026-08-08T00:01:00Z",
            },
            { status: 201 },
          );
        }
        if (path === "/api/v1/config/drafts/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/publish") {
          return jsonResponse({
            id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            version: 4,
            status: "published",
            document: requests[0]?.body,
            created_by: principal.user_id,
            created_at: "2026-08-08T00:01:00Z",
            notification_status: "sent",
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

  it("loads the current production config and publishes a validated draft", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    expect(await screen.findByText("生产配置中心")).not.toBeNull();
    expect(screen.getByText("当前发布版本：3")).not.toBeNull();
    const editor = screen.getByLabelText("配置 JSON") as HTMLTextAreaElement;
    expect(editor.value).toContain('"models"');

    fireEvent.change(editor, { target: { value: '{"models":{},"agents":[]}' } });
    await user.click(screen.getByRole("button", { name: "创建草稿并发布" }));

    expect((await screen.findByRole("status")).textContent).toContain("已发布配置版本 4");
    expect(requests[0]).toEqual({
      path: "/api/v1/config/drafts",
      method: "POST",
      body: { models: {}, agents: [] },
    });
  });

  it("shows a detailed parse error instead of a generic failure", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    const editor = (await screen.findByLabelText("配置 JSON")) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "{broken" } });
    await user.click(screen.getByRole("button", { name: "创建草稿并发布" }));

    expect((await screen.findByRole("alert")).textContent).toContain("JSON 解析失败");
  });
});
