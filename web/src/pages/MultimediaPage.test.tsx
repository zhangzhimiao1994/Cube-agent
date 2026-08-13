import { render, screen, waitFor } from "@testing-library/react";
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

describe("MultimediaPage", () => {
  const requests: Array<{ path: string; method: string; body: unknown }> = [];

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
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
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
            allow_main_agent_override: false,
            allow_temporary_agents: false,
            vibe_coding_enabled: false,
            multimedia_generation_enabled: true,
            openclaw_enabled: false,
            openclaw_mode: "ask",
            openclaw_allowed_commands: [],
            temporary_agent_policy: "ask before adding temporary agents",
            channel_entry: "web",
            attachment_retention_days: 7,
            attachment_max_mb: 25,
          });
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse([
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider: "deepseek",
              api_base: "https://api.deepseek.com/v1",
              api_protocol: "openai_compatible",
              upstream_model: "deepseek-v4-flash",
              logical_model: "text_primary",
              capabilities: ["text"],
              credential_ref: "secret://text",
              quota_scope: "deepseek",
              max_concurrency: 1,
              target_utilization: 0.8,
              reserved_capacity: 0,
              rpm: null,
              tpm: null,
              queue_timeout_seconds: 60,
              fallback: null,
              weight: 100,
              effective_slots: 1,
              saturation_policy: "queue_first_then_fallback",
            },
            {
              id: "22222222-2222-4222-8222-222222222222",
              provider: "minimax",
              api_base: "https://api.minimax.chat/v1",
              api_protocol: "openai_compatible",
              upstream_model: "MiniMax-Hailuo-02",
              logical_model: "video_primary",
              capabilities: ["text", "video_generation"],
              credential_ref: "secret://video",
              quota_scope: "minimax",
              max_concurrency: 1,
              target_utilization: 0.8,
              reserved_capacity: 0,
              rpm: null,
              tpm: null,
              queue_timeout_seconds: 60,
              fallback: null,
              weight: 100,
              effective_slots: 1,
              saturation_policy: "queue_first_then_fallback",
            },
          ]);
        }
        if (path === "/api/v1/admin/multimedia/generate" && method === "POST") {
          return jsonResponse(
            {
              kind: "video",
              logical_model: "video_primary",
              deployment_id: "media_primary_1",
              text: "artifact://generated-video",
            },
            { status: 202 },
          );
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("submits video generation only to a video capable model", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/multimedia" />);

    await screen.findByRole("heading", { name: "多媒体生成" });
    await user.click(screen.getByRole("radio", { name: "视频" }));
    await user.selectOptions(screen.getByLabelText("模型"), "video_primary");
    await user.type(screen.getByLabelText("提示词"), "make a 5 second launch video");
    await user.click(screen.getByRole("button", { name: "生成" }));

    await screen.findByText("artifact://generated-video");
    await waitFor(() => {
      expect(requests).toContainEqual({
        path: "/api/v1/admin/multimedia/generate",
        method: "POST",
        body: {
          kind: "video",
          logical_model: "video_primary",
          prompt: "make a 5 second launch video",
        },
      });
    });
  });
});
