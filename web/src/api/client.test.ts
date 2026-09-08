import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, formatApiError, type PluginResource } from "./client";

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
      policy_effect: "inherit",
      available: false,
      availability_reason: "workspace_root_not_configured",
      replay_safe: true,
      aliases: ["workspace_read"],
      input_schema: null,
      output_schema: null,
    });
  });

  it("loads plugin resources and preserves HTTP credential metadata", async () => {
    const plugin: PluginResource = {
      id: "calendar",
      name: "Calendar HTTP",
      enabled: true,
      description: "Calendar connector",
      version: "local",
      endpoint_url: "https://plugins.example/invoke",
      domain_allowlist: ["plugins.example"],
      resource_config: { workflow_id: "daily_report" },
      timeout_seconds: 4,
      credential_ref: "secret://calendar",
      credential_header: "X-Plugin-Key",
      credential_scheme: "",
      capabilities: [
        {
          id: "calendar.create_event",
          adapter: "http_json",
          permission_class: "calendar.write",
          sandbox_profile: "remote_connector",
          policy_effect: "require_approval",
          replay_safe: false,
          aliases: ["calendar_create"],
          capability_config: { workflow_stage: "daily" },
          input_schema: {
            type: "object",
            required: ["title"],
            properties: { title: { type: "string" } },
          },
          output_schema: {
            type: "object",
            properties: { remote_id: { type: "string" } },
          },
        },
      ],
      status: "running",
      health: "healthy",
      last_error_type: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([plugin]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.plugins();

    expect(fetchMock.mock.calls[0]?.[0]).toMatch(/^\/api\/v1\/admin\/plugins\?_=/);
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ cache: "no-store" }));
    expect(result[0]).toEqual(plugin);
  });

  it("defaults plugin capability sandbox profiles to remote connector", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "calendar",
            name: "Calendar HTTP",
            enabled: true,
            status: "running",
            health: "healthy",
            capabilities: [
              {
                id: "calendar.create_event",
              },
            ],
          },
        ]),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.plugins();

    expect(result[0]?.capabilities[0]?.sandbox_profile).toBe("remote_connector");
  });

  it("defaults plugin capability policy effects to inherit", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "calendar",
            name: "Calendar HTTP",
            enabled: true,
            status: "running",
            health: "healthy",
            capabilities: [
              {
                id: "calendar.create_event",
              },
            ],
          },
        ]),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.plugins();

    expect(result[0]?.capabilities[0]?.policy_effect).toBe("inherit");
  });

  it("posts plugin resources to the admin API", async () => {
    const plugin: PluginResource = {
      id: "calendar",
      name: "Calendar HTTP",
      enabled: true,
      description: null,
      version: "local",
      endpoint_url: "https://plugins.example/invoke",
      domain_allowlist: ["plugins.example"],
      resource_config: { workflow_id: "daily_report" },
      timeout_seconds: 4,
      credential_ref: "secret://calendar",
      credential_header: "X-Plugin-Credential",
      credential_scheme: "Bearer",
      capabilities: [
        {
          id: "calendar.create_event",
          adapter: "http_json",
          permission_class: "calendar.write",
          sandbox_profile: "remote_connector",
          policy_effect: "require_approval",
          replay_safe: false,
          aliases: ["calendar_create"],
          capability_config: { workflow_stage: "daily" },
          input_schema: {
            type: "object",
            required: ["title"],
            properties: { title: { type: "string" } },
          },
          output_schema: null,
        },
      ],
      status: "stopped",
      health: "stopped",
      last_error_type: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(plugin), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.createPlugin({
      id: plugin.id,
      name: plugin.name,
      enabled: true,
      description: null,
      version: "local",
      resource_config: plugin.resource_config,
      endpoint_url: plugin.endpoint_url,
      domain_allowlist: plugin.domain_allowlist,
      timeout_seconds: plugin.timeout_seconds,
      credential_ref: plugin.credential_ref,
      credential_header: plugin.credential_header,
      credential_scheme: plugin.credential_scheme,
      capabilities: plugin.capabilities,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/plugins");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      id: "calendar",
      name: "Calendar HTTP",
      enabled: true,
      description: null,
      version: "local",
      resource_config: { workflow_id: "daily_report" },
      endpoint_url: "https://plugins.example/invoke",
      domain_allowlist: ["plugins.example"],
      timeout_seconds: 4,
      credential_ref: "secret://calendar",
      credential_header: "X-Plugin-Credential",
      credential_scheme: "Bearer",
      capabilities: [
        {
          id: "calendar.create_event",
          adapter: "http_json",
          permission_class: "calendar.write",
          sandbox_profile: "remote_connector",
          policy_effect: "require_approval",
          replay_safe: false,
          aliases: ["calendar_create"],
          capability_config: { workflow_stage: "daily" },
          input_schema: {
            type: "object",
            required: ["title"],
            properties: { title: { type: "string" } },
          },
          output_schema: null,
        },
      ],
    });
    expect(result.status).toBe("stopped");
  });

  it("loads plugin adapter descriptors", async () => {
    const descriptors = [
      {
        id: "http_json",
        name: "HTTP JSON",
        description: "POSTs plugin invocations to an allowlisted HTTP endpoint.",
        resource_schema: {
          type: "object",
          required: ["endpoint_url", "domain_allowlist"],
          properties: {
            endpoint_url: { type: "string", format: "uri" },
            credential_ref: { type: "string" },
          },
        },
        capability_schema: {
          type: "object",
          required: ["id"],
          properties: {
            input_schema: { type: "object" },
            output_schema: { type: "object" },
          },
        },
        argument_schema: {
          type: "object",
          additionalProperties: true,
        },
        capability_contract: {
          schema_version: 1,
          declared_sandbox_profiles: ["http_read", "local_process"],
          runtime_sandbox_profiles: ["http_read", "remote_connector"],
        },
      },
    ];
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(descriptors), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.pluginAdapters();

    expect(fetchMock.mock.calls[0]?.[0]).toMatch(/^\/api\/v1\/admin\/plugins\/adapters\?_=/);
    expect(result).toEqual(descriptors);
  });

  it("uninstalls plugins through lifecycle endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "uninstalled" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.uninstallPlugin("calendar");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/plugins/calendar/uninstall");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(result).toEqual({ status: "uninstalled" });
  });

  it("enables and disables plugins through lifecycle endpoints", async () => {
    const enabledPlugin: PluginResource = {
      id: "calendar",
      name: "Calendar HTTP",
      enabled: true,
      description: null,
      version: "local",
      endpoint_url: null,
      domain_allowlist: [],
      resource_config: {},
      timeout_seconds: 10,
      credential_ref: null,
      credential_header: "X-Plugin-Credential",
      credential_scheme: "Bearer",
      capabilities: [],
      status: "stopped",
      health: "stopped",
      last_error_type: null,
    };
    const disabledPlugin = {
      ...enabledPlugin,
      enabled: false,
      status: "disabled",
      health: "disabled",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(disabledPlugin), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(enabledPlugin), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.disablePlugin("calendar")).resolves.toEqual(disabledPlugin);
    await expect(api.enablePlugin("calendar")).resolves.toEqual(enabledPlugin);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/plugins/calendar/disable");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/admin/plugins/calendar/enable");
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
  });

  it("uploads plugin archives to the plugin install endpoint", async () => {
    const plugin: PluginResource = {
      id: "calendar",
      name: "Calendar HTTP",
      enabled: true,
      description: null,
      version: "1.0.0",
      endpoint_url: null,
      domain_allowlist: [],
      resource_config: {},
      timeout_seconds: 10,
      credential_ref: null,
      credential_header: "X-Plugin-Credential",
      credential_scheme: "Bearer",
      capabilities: [],
      status: "stopped",
      health: "stopped",
      last_error_type: null,
    };
    const response = {
      filename: "calendar plugin.zip",
      content_sha256: "abc123",
      plugin,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      api.uploadPluginArchive(new File(["plugin-bytes"], "calendar plugin.zip", { type: "application/zip" })),
    ).resolves.toEqual(response);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/plugins/install");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/zip",
          "X-Agent-Hub-Plugin-Filename": "calendar%20plugin.zip",
          "X-Agent-Hub-Plugin-Filename-Encoding": "percent",
        }),
      }),
    );
  });

  it("rejects plugin adapter descriptors with non-object resource schemas", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "array_resource",
            name: "Array Resource",
            description: "Invalid descriptor.",
            resource_schema: { type: "array" },
            capability_schema: { type: "object", additionalProperties: true },
            argument_schema: { type: "object", additionalProperties: true },
          },
        ]),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.pluginAdapters()).rejects.toThrow();
  });
});
