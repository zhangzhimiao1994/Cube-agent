import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, formatApiError } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("formatApiError", () => {
  it("includes safe structured details without dumping unrelated values", () => {
    const error = new ApiError("model transport failed", 503, "model_check_failed", "err-123", {
      logical_model: "main_agent",
      provider: "litellm",
      upstream_model: "qwen-max",
      status_code: "503",
      hint: "Check model config and rate limits.",
      credential_ref: "secret://private-key",
      traceback: "private stack",
    });

    const formatted = formatApiError(error, "模型测试失败");

    expect(formatted).toContain("模型测试失败: model transport failed");
    expect(formatted).toContain("model_check_failed");
    expect(formatted).toContain("HTTP 503");
    expect(formatted).toContain("error err-123");
    expect(formatted).toContain("logical_model=main_agent");
    expect(formatted).toContain("provider=litellm");
    expect(formatted).toContain("upstream_model=qwen-max");
    expect(formatted).toContain("status_code=503");
    expect(formatted).toContain("hint=Check model config and rate limits.");
    expect(formatted).not.toContain("secret://private-key");
    expect(formatted).not.toContain("private stack");
  });
});

describe("api client transport", () => {
  it("disables browser caching for API reads used by live run surfaces", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.runs();

    expect(fetchMock.mock.calls[0]?.[0]).toMatch(/^\/api\/v1\/admin\/runs\?_=/);
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ cache: "no-store" }));
  });

  it("keeps legacy settings responses on manual tool approval by default", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          default_mode: "auto",
          default_workflow_id: null,
          default_agent_ids: [],
          log_level: "warning",
          hermes_enabled: true,
          safe_tools_enabled: true,
          require_approval_for_tools: true,
          channel_entry: "web",
          attachment_retention_days: 7,
          attachment_max_mb: 25,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const settings = await api.settings();

    expect(settings.tool_approval_mode).toBe("ask");
  });

  it("preserves nested model execution plans on run detail events", async () => {
    const modelExecutionPlan = {
      schema_version: 1,
      main_agent: {
        logical_model: "main",
        selection_source: "harness_decision",
        harness_constrained: true,
        selected_provider: "deepseek",
        selected_model: "deepseek-chat",
        fallback_policy: "disabled_for_harness_selection",
      },
      role_model_assignments: [
        {
          role_id: "copywriter",
          purpose: "execute",
          logical_model: "creative",
        },
      ],
    };
    const capabilityExecutionPlan = {
      schema_version: 1,
      permission_boundary: "runtime_capability_gateway",
      role_capability_assignments: [
        {
          role_id: "copywriter",
          capabilities: [
            {
              name: "read_context",
              replay_safe: true,
              approval_policy: "not_required",
            },
            {
              name: "docx",
              replay_safe: false,
              approval_policy: "runtime_policy",
            },
          ],
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run_1",
          status: "running",
          mode: "dispatch",
          version: 1,
          request: "Draft copy.",
          created_at: "2026-09-06T13:50:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0",
          events: [
            {
              sequence: 1,
              kind: "step.started",
              message: "main_agent_plan",
              created_at: "2026-09-06T13:50:01Z",
              actor: "main_agent",
              participants: [],
              step_id: "main_agent_plan",
              payload: {
                model_execution_plan: modelExecutionPlan,
                capability_execution_plan: capabilityExecutionPlan,
              },
            },
          ],
          artifacts: [],
          explicit_details: {},
          failure_diagnostics: [],
          tool_lifecycle: [],
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const run = await api.run("run_1");

    expect(run.events[0]?.payload.model_execution_plan).toEqual(modelExecutionPlan);
    expect(run.events[0]?.payload.capability_execution_plan).toEqual(
      capabilityExecutionPlan,
    );
  });

  it("loads the runtime capability manifest without browser cache", async () => {
    const manifest = {
      schema_version: 1,
      capabilities: [
        {
          id: "calculator.evaluate",
          kind: "builtin",
          adapter: "runtime_builtin",
          permission_class: "calculator.evaluate",
          sandbox_profile: "in_process",
          available: true,
          availability_reason: null,
          replay_safe: true,
          aliases: ["calculator"],
        },
        {
          id: "workspace.read",
          kind: "builtin",
          adapter: "runtime_builtin",
          permission_class: "file.read",
          sandbox_profile: "workspace_read",
          available: false,
          availability_reason: "workspace_root_not_configured",
          replay_safe: true,
          aliases: ["workspace_read"],
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(manifest), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.capabilityManifest();

    expect(fetchMock.mock.calls[0]?.[0]).toMatch(
      /^\/api\/v1\/admin\/capabilities\/manifest\?_=/,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ cache: "no-store" }));
    expect(result.capabilities[1]).toEqual({
      id: "workspace.read",
      kind: "builtin",
      adapter: "runtime_builtin",
      permission_class: "file.read",
      sandbox_profile: "workspace_read",
      available: false,
      availability_reason: "workspace_root_not_configured",
      replay_safe: true,
      aliases: ["workspace_read"],
    });
  });
});
