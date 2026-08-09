import { render, screen } from "@testing-library/react";
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
            role: "super_admin",
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
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders the operations shell without global capability cards above every page", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    expect(screen.getByText("控制中枢")).not.toBeNull();
    expect(screen.getByRole("link", { name: "对话" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "设置" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "Hermes 学习" })).not.toBeNull();
    expect(screen.queryByText("实时调度")).toBeNull();
    expect(screen.queryByText("工具防护")).toBeNull();
    expect(screen.queryByText("沉淀经验，但不绕过审批")).toBeNull();
  });
});
