import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const mainAgent = {
  model: null,
  control_mode: "supervisor",
  decision_policy: "choose mode first, then roles; main agent makes the final decision",
  hermes_policy: "observe",
  max_review_rounds: 2,
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("MainAgentPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let currentMainAgent: typeof mainAgent | unknown = mainAgent;

  beforeEach(() => {
    requests.length = 0;
    currentMainAgent = mainAgent;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (init?.body && typeof init.body === "string") {
          requests.push({ path, method, body: JSON.parse(init.body) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/main-agent") {
          if (method === "PUT") {
            currentMainAgent = JSON.parse(String(init?.body));
            return jsonResponse(currentMainAgent);
          }
          return jsonResponse(currentMainAgent);
        }
        if (path === "/api/v1/admin/secrets") {
          return jsonResponse({ ref: "secret://generated-main-agent", last_four: "live" });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("configures the main agent with its own model api and control policy", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByTestId("main-agent-page")).not.toBeNull();
    await user.selectOptions(screen.getByTestId("main-agent-api-protocol"), "anthropic_messages");
    await user.clear(screen.getByTestId("main-agent-provider"));
    await user.type(screen.getByTestId("main-agent-provider"), "claude-code-relay");
    await user.clear(screen.getByTestId("main-agent-api-base"));
    await user.type(screen.getByTestId("main-agent-api-base"), "https://toapis.com/v1");
    await user.clear(screen.getByTestId("main-agent-upstream-model"));
    await user.type(screen.getByTestId("main-agent-upstream-model"), "claude-sonnet-4-6");
    await user.clear(screen.getByTestId("main-agent-credential-ref"));
    await user.type(screen.getByTestId("main-agent-api-key"), "sk-test-main-agent-live");
    await user.selectOptions(screen.getByTestId("main-agent-control-mode"), "supervisor");
    await user.selectOptions(screen.getByTestId("main-agent-hermes-policy"), "confirm_before_apply");
    await user.clear(screen.getByTestId("main-agent-decision-policy"));
    await user.type(
      screen.getByTestId("main-agent-decision-policy"),
      "choose workflow, select role pool, then make the final decision",
    );
    await user.click(screen.getByTestId("main-agent-save"));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/main-agent")).toMatchObject({
        method: "PUT",
        body: {
          model: {
            provider: "claude-code-relay",
            api_base: "https://toapis.com/v1/messages",
            api_protocol: "anthropic_messages",
            upstream_model: "claude-sonnet-4-6",
            credential_ref: "secret://generated-main-agent",
            capabilities: ["text", "tool_calling"],
          },
          control_mode: "supervisor",
          hermes_policy: "confirm_before_apply",
          decision_policy: "choose workflow, select role pool, then make the final decision",
          max_review_rounds: 2,
        },
      }),
    );
    expect(await screen.findByText("claude-code-relay · claude-sonnet-4-6")).not.toBeNull();
    expect(screen.getByText("https://toapis.com/v1/messages")).not.toBeNull();
  });
});
