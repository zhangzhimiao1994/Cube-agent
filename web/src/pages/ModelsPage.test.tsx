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

describe("ModelsPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let failModelSave = false;

  beforeEach(() => {
    requests.length = 0;
    failModelSave = false;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/models" && method === "GET") {
          return jsonResponse([
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider: "deepseek",
              api_base: "https://api.deepseek.example/v1",
              upstream_model: "deepseek-chat",
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
        if (path === "/api/v1/admin/secrets" && method === "POST") {
          return jsonResponse({ ref: "secret_created", last_four: "1234" });
        }
        if (path === "/api/v1/admin/models" && method === "POST") {
          if (failModelSave) {
            return jsonResponse(
              {
                error: {
                  code: "model_unavailable",
                  message: "model availability check failed: status=401",
                },
              },
              { status: 422 },
            );
          }
          const body = JSON.parse(String(init?.body));
          return jsonResponse({
            id: "22222222-2222-4222-8222-222222222222",
            ...body,
            effective_slots: body.max_concurrency,
            saturation_policy: "queue_first_then_fallback",
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

  it("shows existing model concurrency and quota policy", async () => {
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByRole("heading", { name: "已保存模型" })).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "逻辑模型" })).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "服务商" })).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "上游模型" })).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "有效并发" })).not.toBeNull();
    expect(screen.getByText("planner")).not.toBeNull();
    expect(screen.getByText("deepseek")).not.toBeNull();
    expect(screen.getByText("deepseek-chat")).not.toBeNull();
    expect(screen.getByText("1")).not.toBeNull();
    expect(screen.getByText("先排队，超时后降级")).not.toBeNull();
    expect(screen.getByText("同一服务商账号下的多个 Key 可能共享配额，不要把并发设置到跑满额度。")).not.toBeNull();
  });

  it("limits the model dropdown to the selected provider and saves the api key as a secret", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "deepseek");

    const modelSelect = screen.getByLabelText("模型") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((option) => option.value);
    expect(optionValues).toContain("deepseek-chat");
    expect(optionValues).toContain("__custom_model__");
    expect(optionValues).not.toContain("qwen-plus");
    expect(optionValues).not.toContain("kimi-k2-turbo-preview");
    expect(modelSelect.value).toBe("deepseek-chat");
    expect((screen.getByLabelText("API Base") as HTMLInputElement).value).toBe(
      "https://api.deepseek.com/v1",
    );

    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "planner");
    await user.clear(screen.getByLabelText("API Key"));
    await user.type(screen.getByLabelText("API Key"), "sk-deepseek-1234");
    await user.clear(screen.getByLabelText("Quota Scope"));
    await user.type(screen.getByLabelText("Quota Scope"), "deepseek-account");
    await user.clear(screen.getByLabelText("最大并发"));
    await user.type(screen.getByLabelText("最大并发"), "4");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");

    expect(requests[0]).toEqual({
      path: "/api/v1/admin/secrets",
      method: "POST",
      body: { label: "planner deepseek", value: "sk-deepseek-1234" },
    });
    expect(requests[1]).toEqual({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "deepseek",
        api_base: "https://api.deepseek.com/v1",
        upstream_model: "deepseek-chat",
        logical_model: "planner",
        capabilities: ["text", "tool_calling"],
        credential_ref: "secret_created",
        quota_scope: "deepseek-account",
        max_concurrency: 4,
        target_utilization: 0.8,
        reserved_capacity: 0,
        rpm: 60,
        tpm: 100000,
        queue_timeout_seconds: 60,
        fallback: null,
        weight: 100,
      },
    });
  });

  it("shows the backend model check failure reason", async () => {
    failModelSave = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.type(screen.getByLabelText("API Key"), "sk-bad-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("model availability check failed: status=401");
    expect(alert.textContent).toContain("model_unavailable");
  });

  it("allows a custom model under any selected provider", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "kimi");
    await user.selectOptions(screen.getByLabelText("模型"), "__custom_model__");

    expect(screen.getByLabelText("自定义模型")).not.toBeNull();
    await user.type(screen.getByLabelText("自定义模型"), "kimi-custom-routed-model");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "custom-main");
    await user.type(screen.getByLabelText("API Key"), "sk-custom-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");

    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "kimi",
        api_base: "https://api.moonshot.cn/v1",
        upstream_model: "kimi-custom-routed-model",
        logical_model: "custom-main",
      },
    });
  });

  it("allows custom provider and custom model values", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "custom");
    await user.type(screen.getByLabelText("自定义服务商"), "my-proxy");
    await user.type(screen.getByLabelText("自定义模型"), "my-model-v1");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://proxy.example.com/v1");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "custom-main");
    await user.type(screen.getByLabelText("API Key"), "sk-custom-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");

    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "my-proxy",
        api_base: "https://proxy.example.com/v1",
        upstream_model: "my-model-v1",
        logical_model: "custom-main",
      },
    });
  });
});
