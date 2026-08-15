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
  let failMainAgentUpdate = false;

  beforeEach(() => {
    requests.length = 0;
    currentMainAgent = mainAgent;
    failMainAgentUpdate = false;
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
            if (failMainAgentUpdate) {
              return jsonResponse(
                {
                  error: {
                    code: "model_unavailable",
                    message: "model availability check failed: provider returned status=401",
                    details: {
                      stage: "model_availability_check",
                      provider: "claude-code-relay",
                      api_base: "https://bad-relay.example/v1/messages",
                      logical_model: "main_agent",
                      upstream_model: "claude-sonnet-4-6",
                      status_code: "401",
                      reason: "provider returned status=401",
                      hint: "检查 API Key、API Base、模型名和中转站协议。",
                    },
                  },
                },
                { status: 422 },
              );
            }
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
    expect(screen.getByTestId("main-agent-provider")).toBeInstanceOf(HTMLSelectElement);
    expect(screen.queryByTestId("main-agent-credential-ref")).toBeNull();
    await user.selectOptions(screen.getByTestId("main-agent-provider"), "claude-code-relay");
    expect((screen.getByTestId("main-agent-api-protocol") as HTMLSelectElement).value).toBe("anthropic_messages");
    await user.clear(screen.getByTestId("main-agent-api-base"));
    await user.type(screen.getByTestId("main-agent-api-base"), "https://toapis.com/v1");
    await user.type(screen.getByTestId("main-agent-custom-model"), "claude-sonnet-4-6");
    await user.type(screen.getByTestId("main-agent-api-key"), "sk-test-main-agent-live");
    await user.clear(screen.getByTestId("main-agent-max-concurrency"));
    await user.type(screen.getByTestId("main-agent-max-concurrency"), "3");
    expect(screen.getByText(/实际有效并发槽 2 个/)).not.toBeNull();
    expect(screen.getByText("实际最大并发：2")).not.toBeNull();
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
            max_concurrency: 3,
          },
          control_mode: "supervisor",
          hermes_policy: "confirm_before_apply",
          decision_policy: "choose workflow, select role pool, then make the final decision",
          max_review_rounds: 2,
        },
      }),
    );
    expect(await screen.findByText("claude-code-relay")).not.toBeNull();
    expect(screen.getByText("claude-sonnet-4-6")).not.toBeNull();
    expect(screen.getAllByText("https://toapis.com/v1/messages").length).toBeGreaterThan(0);
  });

  it("lets the main agent use catalog presets or custom providers like model registration", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByTestId("main-agent-page")).not.toBeNull();
    await user.selectOptions(screen.getByTestId("main-agent-provider"), "deepseek");

    expect(screen.getByTestId("main-agent-model")).toBeInstanceOf(HTMLSelectElement);
    expect((screen.getByTestId("main-agent-model") as HTMLSelectElement).value).toBe("deepseek-v4-flash");
    expect((screen.getByTestId("main-agent-api-protocol") as HTMLSelectElement).value).toBe("openai_compatible");
    expect((screen.getByTestId("main-agent-api-base") as HTMLInputElement).value).toBe("https://api.deepseek.com/v1");

    await user.selectOptions(screen.getByTestId("main-agent-model"), "__custom_model__");
    expect(screen.getByTestId("main-agent-custom-model")).not.toBeNull();

    await user.selectOptions(screen.getByTestId("main-agent-provider"), "custom");
    expect(screen.getByTestId("main-agent-custom-provider")).not.toBeNull();
    expect(screen.getByTestId("main-agent-custom-model")).not.toBeNull();
  });

  it("shows current main agent model and only requires a new key when provider connection changes", async () => {
    const user = userEvent.setup();
    currentMainAgent = {
      ...mainAgent,
      model: {
        provider: "deepseek",
        api_base: "https://api.deepseek.com/v1",
        api_protocol: "openai_compatible",
        upstream_model: "deepseek-v4-flash",
        credential_ref: "secret://saved-main-agent",
        capabilities: ["text", "tool_calling"],
        max_concurrency: 3,
      },
    };
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByRole("heading", { name: "当前主 Agent 模型情况" })).not.toBeNull();
    expect(screen.getAllByText("deepseek").length).toBeGreaterThan(0);
    expect(screen.getAllByText("https://api.deepseek.com/v1").length).toBeGreaterThan(0);
    expect(screen.getByText("2 / 3")).not.toBeNull();
    expect(screen.getByText("可沿用当前已保存 Key")).not.toBeNull();

    await user.selectOptions(screen.getByTestId("main-agent-model"), "deepseek-v4-pro");
    expect(screen.getByText("可沿用当前已保存 Key")).not.toBeNull();
    expect((screen.getByTestId("main-agent-save") as HTMLButtonElement).disabled).toBe(false);

    await user.selectOptions(screen.getByTestId("main-agent-provider"), "kimi");
    expect(screen.getByText("服务商、接口类型或 API Base 已变化，需要填写新的 API Key。")).not.toBeNull();
    expect((screen.getByTestId("main-agent-save") as HTMLButtonElement).disabled).toBe(true);

    await user.type(screen.getByTestId("main-agent-api-key"), "sk-new-kimi-main-agent");
    expect((screen.getByTestId("main-agent-save") as HTMLButtonElement).disabled).toBe(false);
  });

  it("removes the dedicated main agent model without deleting control policy", async () => {
    const user = userEvent.setup();
    currentMainAgent = {
      ...mainAgent,
      model: {
        provider: "deepseek",
        api_base: "https://api.deepseek.com/v1",
        api_protocol: "openai_compatible",
        upstream_model: "deepseek-v4-flash",
        credential_ref: "secret://saved-main-agent",
        capabilities: ["text", "tool_calling"],
        max_concurrency: 3,
      },
    };
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByTestId("main-agent-delete-model")).not.toBeNull();
    await user.click(screen.getByTestId("main-agent-delete-model"));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/main-agent")).toMatchObject({
        method: "PUT",
        body: {
          model: null,
          control_mode: "supervisor",
          decision_policy: "choose mode first, then roles; main agent makes the final decision",
          hermes_policy: "observe",
          max_review_rounds: 2,
        },
      }),
    );
  });

  it("shows main agent model diagnostics and points to model logs when availability fails", async () => {
    failMainAgentUpdate = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/main-agent" />);

    expect(await screen.findByTestId("main-agent-page")).not.toBeNull();
    await user.selectOptions(screen.getByTestId("main-agent-provider"), "claude-code-relay");
    await user.type(screen.getByTestId("main-agent-custom-model"), "claude-sonnet-4-6");
    await user.clear(screen.getByTestId("main-agent-api-base"));
    await user.type(screen.getByTestId("main-agent-api-base"), "https://bad-relay.example/v1");
    await user.type(screen.getByTestId("main-agent-api-key"), "sk-bad-main-agent");
    await user.click(screen.getByTestId("main-agent-save"));

    expect(await screen.findByRole("heading", { name: "主 Agent 模型配置错误日志" })).not.toBeNull();
    expect(screen.getByText("claude-code-relay")).not.toBeNull();
    expect(screen.getByText("https://bad-relay.example/v1/messages")).not.toBeNull();
    expect(screen.getByText("provider returned status=401")).not.toBeNull();
    expect(screen.getByRole("link", { name: "查看模型日志" })).not.toBeNull();
  });
});
