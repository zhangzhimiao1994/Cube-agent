import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ModelsPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/me") {
          return jsonResponse({
            username: "owner",
            role: "super_admin",
            permissions: ["*", "config:read", "agent:read"],
          });
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse([
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider: "deepseek",
              api_base: "https://api.deepseek.example/v1",
              logical_model: "planner",
              capabilities: ["text"],
              credential_ref: "secret_1",
              quota_scope: "deepseek_account_1",
              max_concurrency: 1,
              target_utilization: 0.8,
              reserved_capacity: 0,
              rpm: 60,
              tpm: 100000,
              queue_timeout_seconds: 60,
              fallback: null,
              weight: 100,
              effective_slots: 1,
              saturation_policy: "queue_first_then_fallback",
            },
          ]);
        }
        return jsonResponse([]);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows that a serial key contributes one slot", async () => {
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByText("最大并发：1")).not.toBeNull();
    expect(screen.getByText("满载策略：先排队，超时后降级")).not.toBeNull();
    expect(
      screen.getByText("同一供应商账号的多个 Key 可能共享配额，不能重复计算容量。"),
    ).not.toBeNull();
  });
});
