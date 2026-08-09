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
        if (path === "/api/v1/admin/runs") {
          return jsonResponse([]);
        }
        if (path.startsWith("/api/v1/admin/logs")) {
          return jsonResponse([]);
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a polished operations console shell", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByText("Agent 编排控制台")).not.toBeNull();
    expect(screen.getByText("控制中枢")).not.toBeNull();
    expect(screen.getByText("实时调度")).not.toBeNull();
    expect(screen.getByText("工具防护")).not.toBeNull();
    expect(screen.getByRole("link", { name: "对话任务" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "记忆" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "技能" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "通道连接" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "日志" })).not.toBeNull();
    expect(screen.queryByRole("link", { name: "审计日志" })).toBeNull();
    expect(screen.getAllByText("Hermes 学习").length).toBeGreaterThan(0);
  });
});
