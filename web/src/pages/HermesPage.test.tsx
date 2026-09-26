import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("HermesPage", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === "/api/v1/admin/hermes") {
          return jsonResponse([
            {
              id: "hermes_conversation_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              category: "conversation",
              outcome: "success",
              lesson: "Run completed with mode=hybrid, workflow=quality-review.",
              summary: "Hermes recorded reusable conversation memory from conv-cleared-after-chat.",
              user_summary: "对话记忆记录了一条可复用经验：quality-review 工作流以 hybrid 模式成功完成。",
              run_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              conversation_id: "conv-cleared-after-chat",
              confirmed_at: null,
              tags: ["completed", "hybrid", "quality-review"],
              weight: 4,
              created_at: "2026-09-02T00:00:01Z",
            },
            {
              id: "hermes_scheduler_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              category: "scheduler",
              outcome: "failure",
              lesson: "UI expansion drawers should open as full reading sheets on mobile.",
              summary: "Hermes captured a UI lesson from a failed mobile verification.",
              user_summary: "移动端展开抽屉需要全屏阅读态，避免内容挤在小框里。",
              run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              conversation_id: "conv-ui-polish",
              confirmed_at: "2026-09-02T00:10:00Z",
              tags: ["ui", "mobile", "drawer"],
              weight: 7,
              created_at: "2026-09-02T00:05:01Z",
            },
          ]);
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
            tool_approval_mode: "auto_review",
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
    cleanup();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it("shows pending conversation ledger entries with their Chinese learning summary", async () => {
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("heading", { name: "Hermes 学习" })).not.toBeNull();
    const ledger = await screen.findByRole("table", { name: "Hermes 学习台账" });
    const row = within(ledger).getByRole("row", {
      name: /conv-cleared-after-chat/,
    });

    expect(within(row).getByText("对话记忆")).not.toBeNull();
    expect(within(row).getByText("conv-cleared-after-chat")).not.toBeNull();
    expect(
      within(row).getByText("对话记忆记录了一条可复用经验：quality-review 工作流以 hybrid 模式成功完成。"),
    ).not.toBeNull();
    expect(within(row).getByText("待确认")).not.toBeNull();
  });

  it("renders a journey timeline for confirmed and pending learning", async () => {
    render(<TestApp initialPath="/hermes" />);

    const journey = await screen.findByRole("region", { name: "Hermes 学习旅程" });

    expect(within(journey).getByText("学习旅程")).not.toBeNull();
    expect(within(journey).getByText("待确认 1")).not.toBeNull();
    expect(within(journey).getByText("已确认 1")).not.toBeNull();
    expect(within(journey).getByText("移动端展开抽屉需要全屏阅读态，避免内容挤在小框里。")).not.toBeNull();
    expect(within(journey).getByRole("link", { name: /查看 conv-ui-polish 的学习详情/ })).not.toBeNull();
  });
});
