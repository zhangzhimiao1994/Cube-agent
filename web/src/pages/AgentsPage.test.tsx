import { render, screen } from "@testing-library/react";
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

describe("AgentsPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let visibleAgents: unknown[] = [];

  beforeEach(() => {
    requests.length = 0;
    visibleAgents = [];
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        const method = init?.method ?? "GET";
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse([
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider: "deepseek",
              api_base: "https://api.deepseek.com/v1",
              upstream_model: "deepseek-v4-flash",
              logical_model: "main",
              capabilities: ["text", "tool_calling"],
              credential_ref: "secret://live",
              quota_scope: "deepseek-account",
              max_concurrency: 4,
              target_utilization: 0.8,
              reserved_capacity: 0,
              rpm: 60,
              tpm: 100000,
              queue_timeout_seconds: 60,
              fallback: null,
              weight: 100,
              effective_slots: 4,
              saturation_policy: "queue_first_then_fallback",
            },
          ]);
        }
        if (path === "/api/v1/admin/agents" && method === "GET") {
          return jsonResponse(visibleAgents);
        }
        if (path === "/api/v1/admin/agents" && method === "POST") {
          return jsonResponse(JSON.parse(String(init?.body)));
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("creates a production agent from an extensible role template", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents" />);

    expect(await screen.findByText("Agent 角色编排")).not.toBeNull();
    await user.selectOptions(screen.getByLabelText("角色模板"), "director");
    await user.clear(screen.getByLabelText("Agent ID"));
    await user.type(screen.getByLabelText("Agent ID"), "director");
    await user.clear(screen.getByLabelText("系统提示词"));
    await user.type(screen.getByLabelText("系统提示词"), "负责选题、分镜、节奏和最终把关。");
    await user.click(screen.getByRole("button", { name: "保存 Agent" }));

    expect((await screen.findByRole("status")).textContent).toContain("Agent 已保存");
    expect(requests[0]).toEqual({
      path: "/api/v1/admin/agents",
      method: "POST",
      body: {
        id: "director",
        name: "导演",
        enabled: true,
        role: "导演",
        prompt: "负责选题、分镜、节奏和最终把关。",
        model: "main",
        skills: [],
      },
    });
  });

  it("loads an existing agent into the form for editing", async () => {
    const user = userEvent.setup();
    visibleAgents = [
      {
        id: "critic",
        name: "审查员",
        enabled: true,
        role: "审查员",
        prompt: "检查风险和遗漏。",
        model: "main",
        skills: ["risk_review"],
      },
    ];
    render(<TestApp initialPath="/agents" />);

    expect(await screen.findByText("Agent 角色编排")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "编辑 Agent" }));

    expect(screen.getByRole("status").textContent).toContain("已载入 审查员");
    expect((screen.getByLabelText("Agent ID") as HTMLInputElement).value).toBe("critic");
    expect((screen.getByLabelText("允许使用的 Skill ID") as HTMLInputElement).value).toBe("risk_review");

    await user.clear(screen.getByLabelText("系统提示词"));
    await user.type(screen.getByLabelText("系统提示词"), "检查风险、遗漏和执行边界。");
    await user.click(screen.getByRole("button", { name: "保存 Agent" }));

    expect(requests.find((request) => request.method === "POST" && request.path === "/api/v1/admin/agents")).toEqual({
      path: "/api/v1/admin/agents",
      method: "POST",
      body: {
        id: "critic",
        name: "审查员",
        enabled: true,
        role: "审查员",
        prompt: "检查风险、遗漏和执行边界。",
        model: "main",
        skills: ["risk_review"],
      },
    });
  });
});
