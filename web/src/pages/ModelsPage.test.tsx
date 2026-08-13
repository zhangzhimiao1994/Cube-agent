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
  let modelDeleted = false;

  beforeEach(() => {
    requests.length = 0;
    failModelSave = false;
    modelDeleted = false;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        } else if (method === "DELETE") {
          requests.push({ path, method, body: null });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/models" && method === "GET") {
          return jsonResponse(modelDeleted ? [] : [
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider: "deepseek",
              api_base: "https://api.deepseek.example/v1",
              upstream_model: "deepseek-v4-flash",
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
        if (path === "/api/v1/admin/models/11111111-1111-4111-8111-111111111111" && method === "DELETE") {
          modelDeleted = true;
          return new Response(null, { status: 204 });
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
                  details: {
                    stage: "model_availability_check",
                    provider: "deepseek",
                    api_base: "https://api.deepseek.com/v1",
                    logical_model: "main",
                    upstream_model: "deepseek-v4-flash",
                    status_code: "401",
                    reason: "provider returned status=401",
                    hint: "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。",
                  },
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
        if (path === "/api/v1/admin/models/11111111-1111-4111-8111-111111111111" && method === "PUT") {
          const body = JSON.parse(String(init?.body));
          return jsonResponse({
            id: "11111111-1111-4111-8111-111111111111",
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
    expect(screen.getByText("deepseek-v4-flash")).not.toBeNull();
    expect(screen.getByText("1")).not.toBeNull();
    expect(screen.getByText("先排队，超时后降级")).not.toBeNull();
    expect(screen.getByText("同一服务商账号下的多个 Key 可能共享配额，不要把并发设置到跑满额度。")).not.toBeNull();
  });

  it("deletes an existing model deployment from the saved models table", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByText("planner")).not.toBeNull();
    await user.click(screen.getByTestId("delete-model-11111111-1111-4111-8111-111111111111"));

    expect(requests.find((request) => request.method === "DELETE")).toMatchObject({
      path: "/api/v1/admin/models/11111111-1111-4111-8111-111111111111",
      method: "DELETE",
    });
    expect(await screen.findByText("还没有保存模型")).not.toBeNull();
  });

  it("edits an existing model without requiring a new api key", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    expect(await screen.findByText("planner")).not.toBeNull();
    await user.click(screen.getByTestId("edit-model-11111111-1111-4111-8111-111111111111"));

    expect((screen.getByLabelText("逻辑模型名") as HTMLInputElement).value).toBe("planner");
    expect((screen.getByLabelText("API Base") as HTMLInputElement).value).toBe(
      "https://api.deepseek.example/v1",
    );
    expect((screen.getByLabelText("API Key") as HTMLInputElement).required).toBe(false);

    await user.clear(screen.getByLabelText("最大并发"));
    await user.type(screen.getByLabelText("最大并发"), "3");
    await user.click(screen.getByRole("button", { name: "测试并更新模型" }));

    expect(requests.find((request) => request.method === "PUT")).toMatchObject({
      path: "/api/v1/admin/models/11111111-1111-4111-8111-111111111111",
      method: "PUT",
      body: expect.objectContaining({
        credential_ref: "secret_1",
        max_concurrency: 3,
      }),
    });
  });

  it("limits the model dropdown to the selected provider and saves the api key as a secret", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "deepseek");

    const modelSelect = screen.getByLabelText("模型") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((option) => option.value);
    expect(optionValues).toContain("deepseek-v4-flash");
    expect(optionValues).toContain("__custom_model__");
    expect(optionValues).not.toContain("qwen-plus");
    expect(optionValues).not.toContain("kimi-k2-turbo-preview");
    expect(modelSelect.value).toBe("deepseek-v4-flash");
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
        api_protocol: "openai_compatible",
        upstream_model: "deepseek-v4-flash",
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

  it("includes Anthropic and Claude Code presets with provider-scoped models", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "anthropic");

    const modelSelect = screen.getByLabelText("模型") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((option) => option.value);
    expect(optionValues).toContain("claude-sonnet-5");
    expect(optionValues).toContain("claude-opus-5");
    expect(optionValues).toContain("__custom_model__");
    expect(optionValues).not.toContain("deepseek-chat");
    expect((screen.getByLabelText("API Base") as HTMLInputElement).value).toBe(
      "https://api.anthropic.com/v1/messages",
    );
    expect((screen.getByLabelText("Quota Scope") as HTMLInputElement).value).toBe(
      "anthropic-account",
    );
  });

  it("keeps Claude Code API relay endpoints on the Anthropic Messages protocol", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "claude-code-relay");
    expect(screen.getByLabelText("接口类型")).not.toBeNull();
    await user.type(screen.getByLabelText("中转站模型名"), "claude-sonnet-4-6");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://toapis.com/v1");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "main");
    await user.type(screen.getByLabelText("API Key"), "sk-claude-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");
    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "claude-code-relay",
        api_base: "https://toapis.com/v1/messages",
        api_protocol: "anthropic_messages",
        upstream_model: "claude-sonnet-4-6",
        logical_model: "main",
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
    expect(alert.textContent).toContain("模型配置错误日志");
    expect(alert.textContent).toContain("HTTP 状态");
    expect(alert.textContent).toContain("401");
    expect(alert.textContent).toContain("provider returned status=401");
    expect(alert.textContent).toContain("检查 API Key 是否有效");
    expect(alert.textContent).not.toContain("sk-bad-1234");
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

  it("uses freeform model entry for OpenAI-compatible relay providers", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "openai-compatible");

    expect(screen.queryByLabelText("模型")).toBeNull();
    expect(screen.getByText("中转站通常会混合多个厂商模型，请填写中转站后台显示的完整模型 ID。")).not.toBeNull();
    expect((screen.getByLabelText("API Base") as HTMLInputElement).value).toBe("");

    await user.type(screen.getByLabelText("中转站模型名"), "deepseek/deepseek-v4-flash");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://relay.example.com/v1/chat/completions/");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "relay-main");
    await user.type(screen.getByLabelText("API Key"), "sk-relay-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");

    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "openai-compatible",
        api_base: "https://relay.example.com/v1",
        api_protocol: "openai_compatible",
        upstream_model: "deepseek/deepseek-v4-flash",
        logical_model: "relay-main",
      },
    });
  });

  it("adds /v1 for OpenAI-compatible relay root domains before saving", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "openai-compatible");
    await user.type(screen.getByLabelText("中转站模型名"), "deepseek-v4-flash");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://gsykj.com");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "relay-main");
    await user.type(screen.getByLabelText("API Key"), "sk-relay-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");
    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "openai-compatible",
        api_base: "https://gsykj.com/v1",
        api_protocol: "openai_compatible",
        upstream_model: "deepseek-v4-flash",
      },
    });
  });

  it("allows custom provider and Anthropic Messages relay values", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "custom");
    await user.selectOptions(screen.getByLabelText("接口类型"), "anthropic_messages");
    await user.type(screen.getByLabelText("自定义服务商"), "claude-proxy");
    await user.type(screen.getByLabelText("自定义模型"), "claude-sonnet-4-6");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://proxy.example.com");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "custom-main");
    await user.type(screen.getByLabelText("API Key"), "sk-custom-1234");
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");

    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: {
        provider: "claude-proxy",
        api_base: "https://proxy.example.com/v1/messages",
        api_protocol: "anthropic_messages",
        upstream_model: "claude-sonnet-4-6",
        logical_model: "custom-main",
      },
    });
  });

  it("lets admins declare image and video generation model capabilities", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/models" />);

    await screen.findByText("添加模型配置");
    await user.selectOptions(screen.getByLabelText("服务商"), "custom");
    await user.type(screen.getByLabelText("自定义服务商"), "media-provider");
    await user.type(screen.getByLabelText("自定义模型"), "media-video-1");
    await user.clear(screen.getByLabelText("API Base"));
    await user.type(screen.getByLabelText("API Base"), "https://media.example.com");
    await user.clear(screen.getByLabelText("逻辑模型名"));
    await user.type(screen.getByLabelText("逻辑模型名"), "media_generator");
    await user.type(screen.getByLabelText("API Key"), "sk-media-1234");
    await user.click(screen.getByLabelText("图片生成"));
    await user.click(screen.getByLabelText("视频生成"));
    await user.click(screen.getByRole("button", { name: "测试并保存模型" }));

    await screen.findByText("模型已通过可用性测试并保存，Key 引用：secret_created");
    expect(requests[1]).toMatchObject({
      path: "/api/v1/admin/models",
      method: "POST",
      body: expect.objectContaining({
        logical_model: "media_generator",
        capabilities: ["text", "image_generation", "video_generation"],
      }),
    });
  });
});
