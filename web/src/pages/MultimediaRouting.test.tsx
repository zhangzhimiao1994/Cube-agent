import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("multimedia generation routing", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        expect((init?.headers as Record<string, string> | undefined)?.Authorization).toBe("Bearer owner-token");
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "owner",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === "/api/v1/admin/settings") {
          return jsonResponse({
            default_mode: "auto",
            default_workflow_id: null,
            default_agent_ids: [],
            log_level: "warning",
            hermes_enabled: true,
            safe_tools_enabled: true,
            require_approval_for_tools: true,
            multimedia_generation_enabled: true,
            channel_entry: "web",
          });
        }
        if (path === "/api/v1/admin/models") return jsonResponse([]);
        if (path === "/api/v1/admin/runs") return jsonResponse([]);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("does not expose multimedia generation as a standalone resource module", async () => {
    render(<TestApp initialPath="/resources" />);

    const moduleGrid = await screen.findByRole("list", { name: "资源模块" });

    expect(within(moduleGrid).getByRole("link", { name: /模型与 API/ })).not.toBeNull();
    expect(within(moduleGrid).queryByRole("link", { name: /多媒体生成/ })).toBeNull();
  });

  it("redirects the old multimedia path to model configuration instead of showing a generation form", async () => {
    render(<TestApp initialPath="/multimedia" />);

    expect(await screen.findByRole("heading", { name: "模型与 API" })).not.toBeNull();
    await waitFor(() => expect(screen.queryByRole("form", { name: "多媒体生成表单" })).toBeNull());
    expect(screen.queryByRole("button", { name: "生成" })).toBeNull();
  });
});
