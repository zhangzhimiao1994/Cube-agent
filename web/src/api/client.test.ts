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
    expect(formatted).toContain("模型检测失败");
    expect(formatted).not.toContain("model_check_failed");
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

  it("labels invalid backend response error codes", () => {
    const error = new ApiError("invalid backend response", 502, "invalid_error_response");

    const formatted = formatApiError(error, "请求失败");

    expect(formatted).toContain("错误响应格式无效");
    expect(formatted).not.toContain("invalid_error_response");
  });
});

describe("api client transport", () => {
  it("accepts metadata-only responses when creating and updating conversations", async () => {
    const created = {
      conversation_id: "conv-project",
      title: "项目讨论",
      project_id: "cube-agent",
      project_label: "魔方 Agent",
      workspace_path: "projects/cube-agent/session-a",
      archived_at: null,
      created_at: "2026-09-26T08:00:00Z",
      updated_at: "2026-09-26T08:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(created), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...created, title: "项目讨论第二版" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify([created]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.createConversation({
      conversation_id: "conv-project",
      title: "项目讨论",
      project_id: "cube-agent",
      project_label: "魔方 Agent",
      workspace_path: "projects/cube-agent/session-a",
    });
    const updated = await api.updateConversation("conv-project", { title: "项目讨论第二版" });
    const conversations = await api.conversations();

    expect(result).toEqual(created);
    expect(updated.title).toBe("项目讨论第二版");
    expect(conversations).toEqual([created]);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/conversations");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/admin/conversations/conv-project");
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(expect.objectContaining({ method: "PATCH" }));
    expect(String(fetchMock.mock.calls[2]?.[0])).toContain("/api/v1/admin/conversations?archived=false");
  });

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
    expect(settings.plugin_package_subprocess_registration_status).toBeNull();
  });

  it("parses plugin package subprocess registration status on settings responses", async () => {
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
          tool_approval_mode: "auto_review",
          allow_main_agent_override: false,
          allow_temporary_agents: false,
          vibe_coding_enabled: false,
          multimedia_generation_enabled: false,
          openclaw_enabled: false,
          openclaw_mode: "ask",
          openclaw_allowed_commands: [],
          openclaw_remote_adapters: [],
          temporary_agent_policy: "policy",
          channel_entry: "web",
          attachment_retention_days: 7,
          attachment_max_mb: 25,
          plugin_package_subprocess_registration_status: "launcher_not_found",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const settings = await api.settings();

    expect(settings.plugin_package_subprocess_registration_status).toBe("launcher_not_found");
  });

  it("drops unknown plugin package subprocess registration status on settings responses", async () => {
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
          tool_approval_mode: "auto_review",
          allow_main_agent_override: false,
          allow_temporary_agents: false,
          vibe_coding_enabled: false,
          multimedia_generation_enabled: false,
          openclaw_enabled: false,
          openclaw_mode: "ask",
          openclaw_allowed_commands: [],
          openclaw_remote_adapters: [],
          temporary_agent_policy: "policy",
          channel_entry: "web",
          attachment_retention_days: 7,
          attachment_max_mb: 25,
          plugin_package_subprocess_registration_status: "unexpected_raw_status",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const settings = await api.settings();

    expect(settings.plugin_package_subprocess_registration_status).toBeNull();
  });

  it("preserves plugin package dependency policy locks on capability manifests", async () => {
    const dependencyLock = {
      status: "unsupported",
      install_policy: "offline_cache",
      cache_status: "present",
      allowlist_status: "allowed",
      sha256: "a".repeat(64),
      dependency_count: 1,
      dependencies: [
        {
          kind: "python",
          source: "pypi",
          name: "requests",
          version: "2.32.0",
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          capabilities: [
            {
              id: "calendar.create_event",
              kind: "plugin",
              adapter: "calendar_python",
              permission_class: "calendar.write",
              sandbox_profile: "local_process",
              policy_effect: "inherit",
              available: false,
              availability_reason: "plugin_package_dependencies_unsupported",
              replay_safe: false,
              aliases: [],
              package_dependency_lock: dependencyLock,
            },
          ],
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const manifest = await api.capabilityManifest();

    expect(manifest.capabilities[0]?.package_dependency_lock).toEqual(dependencyLock);
  });

  it("rejects malformed plugin package dependency locks on capability manifests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          capabilities: [
            {
              id: "calendar.create_event",
              kind: "plugin",
              adapter: "calendar_python",
              permission_class: "calendar.write",
              sandbox_profile: "local_process",
              policy_effect: "inherit",
              available: false,
              availability_reason: "plugin_package_dependencies_unsupported",
              replay_safe: false,
              aliases: [],
              package_dependency_lock: {
                status: "unsupported",
                install_policy: "not_configured",
                cache_status: "missing",
                allowlist_status: "missing",
                sha256: "not-a-sha",
                dependency_count: 1,
                dependencies: [
                  {
                    kind: "python",
                    source: "pypi",
                    name: "requests",
                    version: "2.32.0",
                  },
                ],
              },
            },
          ],
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.capabilityManifest()).rejects.toThrow();
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

  it("preserves model outcome summaries on run details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run_1",
          status: "completed",
          mode: "dispatch",
          version: 1,
          request: "Draft copy.",
          created_at: "2026-09-06T13:50:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0",
          events: [],
          artifacts: [],
          explicit_details: {},
          failure_diagnostics: [],
          tool_lifecycle: [],
          model_outcome_summary: {
            completion_count: 2,
            fallback_used: true,
            fallback_attempt_count: 1,
            requested_logical_models: ["planner", "main"],
            actual_logical_models: ["planner", "backup"],
            attempted_logical_models: ["planner", "main", "backup"],
            provider_ids: ["deepseek", "openai"],
            last_requested_logical_model: "main",
            last_logical_model: "backup",
            last_provider_id: "openai",
          },
          orchestration_protocol_summary: {
            protocol: "role_handoff_contract_v1",
            status: "blocked",
            role_count: 3,
            handoff_count: 2,
            contract_count: 2,
            blocked_contract_count: 1,
            truncated: false,
          },
          model_capability_negotiation_summary: {
            role_count: 3,
            satisfied_count: 1,
            missing_count: 2,
            unknown_count: 0,
            missing_capability_counts: {
              structured_output: 1,
              tool_calling: 1,
            },
            truncated: true,
          },
          runtime_recovery_summary: {
            recovery_count: 1,
            last_completed_steps: 2,
            last_total_steps: 5,
            model_status_counts: { failed: 1, succeeded: 2 },
            tool_status_counts: { running: 1 },
            review_artifacts: 1,
          },
          self_repair_recovery_summary: {
            status: "active",
            recovery_strategy: "retry_blocked_contract_chain_after_replanning",
            orchestration_recovery_hint: "retry_blocked_contract_chain",
            replan_scope: "blocked_contract_chain",
            reuse_completed_artifacts: true,
            retry_blocked_contracts_only: true,
            automatic_execution: false,
          },
          capability_execution_summary: {
            permission_boundary: "runtime_capability_gateway",
            role_count: 2,
            capability_count: 3,
            inventory_count: 4,
            failure_code_count: 3,
            failure_code_counts: {
              "mcp.server_failed": 1,
              "plugin.invalid_arguments": 1,
              "plugin.timeout": 2,
            },
            truncated: true,
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const run = await api.run("run_1");

    expect(run.model_outcome_summary).toEqual({
      completion_count: 2,
      fallback_used: true,
      fallback_attempt_count: 1,
      requested_logical_models: ["planner", "main"],
      actual_logical_models: ["planner", "backup"],
      attempted_logical_models: ["planner", "main", "backup"],
      provider_ids: ["deepseek", "openai"],
      last_requested_logical_model: "main",
      last_logical_model: "backup",
      last_provider_id: "openai",
    });
    expect(run.orchestration_protocol_summary).toEqual({
      protocol: "role_handoff_contract_v1",
      status: "blocked",
      role_count: 3,
      handoff_count: 2,
      contract_count: 2,
      blocked_contract_count: 1,
      truncated: false,
    });
    expect(run.model_capability_negotiation_summary).toEqual({
      role_count: 3,
      satisfied_count: 1,
      missing_count: 2,
      unknown_count: 0,
      missing_capability_counts: {
        structured_output: 1,
        tool_calling: 1,
      },
      truncated: true,
    });
    expect(run.runtime_recovery_summary).toEqual({
      recovery_count: 1,
      last_completed_steps: 2,
      last_total_steps: 5,
      model_status_counts: { failed: 1, succeeded: 2 },
      tool_status_counts: { running: 1 },
      review_artifacts: 1,
    });
    expect(run.self_repair_recovery_summary).toEqual({
      status: "active",
      recovery_strategy: "retry_blocked_contract_chain_after_replanning",
      orchestration_recovery_hint: "retry_blocked_contract_chain",
      replan_scope: "blocked_contract_chain",
      reuse_completed_artifacts: true,
      retry_blocked_contracts_only: true,
      automatic_execution: false,
    });
    expect(run.capability_execution_summary).toEqual({
      permission_boundary: "runtime_capability_gateway",
      role_count: 2,
      capability_count: 3,
      inventory_count: 4,
      failure_code_count: 3,
      failure_code_counts: {
        "mcp.server_failed": 1,
        "plugin.invalid_arguments": 1,
        "plugin.timeout": 2,
      },
      truncated: true,
    });
  });

  it("accepts self-repair recovery summaries for model, plugin, and MCP runtime scopes", async () => {
    const summaries = [
      {
        status: "active",
        recovery_strategy: "switch_to_available_model_and_retry",
        replan_scope: "model_capability_roles",
        reuse_completed_artifacts: true,
        retry_blocked_contracts_only: false,
        automatic_execution: false,
      },
      {
        status: "active",
        recovery_strategy: "repair_plugin_endpoint_or_adapter_and_retry",
        replan_scope: "plugin_runtime",
        reuse_completed_artifacts: false,
        retry_blocked_contracts_only: false,
        automatic_execution: false,
      },
      {
        status: "active",
        recovery_strategy: "repair_mcp_server_or_adapter_and_retry",
        replan_scope: "mcp_runtime",
        reuse_completed_artifacts: false,
        retry_blocked_contracts_only: false,
        automatic_execution: true,
      },
    ] as const;

    for (const [index, summary] of summaries.entries()) {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            id: `run_${index}`,
            status: "completed",
            mode: "dispatch",
            version: 1,
            request: "Repair summary.",
            created_at: "2026-09-06T13:50:00Z",
            queue_wait_ms: 0,
            capacity_wait_ms: 0,
            cost_usd: "0",
            events: [],
            artifacts: [],
            explicit_details: {},
            failure_diagnostics: [],
            tool_lifecycle: [],
            self_repair_recovery_summary: summary,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);

      const run = await api.run(`run_${index}`);

      expect(run.self_repair_recovery_summary).toEqual(summary);
    }
  });

  it("accepts a zero-count runtime recovery summary without checkpoint internals", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run_1",
          status: "completed",
          mode: "dispatch",
          version: 1,
          request: "Draft copy.",
          created_at: "2026-09-06T13:50:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0",
          events: [],
          artifacts: [],
          explicit_details: {},
          failure_diagnostics: [],
          tool_lifecycle: [],
          runtime_recovery_summary: {
            recovery_count: 0,
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const run = await api.run("run_1");

    expect(run.runtime_recovery_summary).toEqual({
      recovery_count: 0,
      last_completed_steps: 0,
      last_total_steps: 0,
      model_status_counts: {},
      tool_status_counts: {},
      review_artifacts: 0,
    });
  });

  it("preserves safe recovery metadata on self-repair proposals", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run_1",
          status: "failed",
          mode: "dispatch",
          version: 1,
          request: "Recover run.",
          created_at: "2026-09-06T13:50:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0",
          events: [],
          artifacts: [],
          explicit_details: {},
          failure_diagnostics: [],
          tool_lifecycle: [],
          repair_proposal: {
            kind: "self_repair",
            title: "受控自修复建议",
            summary: "运行失败已分类，可在审批后创建一次受控修复重试。",
            repair_action: "draft_repair_proposal",
            failure_kind: "capacity_pressure",
            source_run_id: "run_1",
            source_event_sequence: 2,
            attempt: 1,
            max_attempts: 1,
            instruction: "只执行一次受控修复。",
            requires_approval: true,
            replay_safe: false,
            automatic_execution: false,
            fingerprint: "a".repeat(64),
            recovery_strategy: "switch_to_available_model_and_retry",
            orchestration_recovery_hint: "retry_blocked_contract_chain",
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const run = await api.run("run_1");

    expect(run.repair_proposal?.recovery_strategy).toBe("switch_to_available_model_and_retry");
    expect(run.repair_proposal?.orchestration_recovery_hint).toBe("retry_blocked_contract_chain");
  });

  it("defaults missing model outcome summaries on legacy run details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run_1",
          status: "completed",
          mode: "dispatch",
          version: 1,
          request: "Draft copy.",
          created_at: "2026-09-06T13:50:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0",
          events: [],
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

    expect(run.model_outcome_summary).toEqual({
      completion_count: 0,
      fallback_used: false,
      fallback_attempt_count: 0,
      requested_logical_models: [],
      actual_logical_models: [],
      attempted_logical_models: [],
      provider_ids: [],
    });
    expect(run.orchestration_protocol_summary).toBeNull();
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
      source_filename: null,
      content_sha256: null,
      package_metadata: null,
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
      source_filename: null,
      content_sha256: null,
      package_metadata: null,
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
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(plugin), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
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
        failure_codes: ["plugin.timeout", "plugin.endpoint_unavailable"],
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
      source_filename: "calendar plugin.zip",
      content_sha256: "abc123",
      package_metadata: null,
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
      source_filename: "calendar plugin.zip",
      content_sha256: "abc123",
      package_metadata: {
        schema_version: 1,
        kind: "adapter_package",
        package_version: "1.2.3",
        provenance: {
          source: "marketplace",
          source_id: "calendar/plugin",
          source_url: "https://plugins.example/marketplace/calendar",
          publisher: "Calendar Labs",
          description: "Reviewed marketplace package",
        },
        adapter_id: "calendar_python",
        sdk_api_version: "1.0",
        signature: {
          algorithm: "ed25519",
          key_id: "calendar-prod",
          value: "A".repeat(86),
        },
        signature_verification: "verified",
        verified_public_key_sha256: "a".repeat(64),
        signature_trust_expires_at: null,
        approval_state: "pending",
        approval_reason: "adapter package requires plugin approval before activation",
        approved_by: null,
        approved_at: null,
        activation_state: "blocked_pending_approval",
        activation_reason: "adapter package requires plugin approval before activation",
        runtime: "python",
        entrypoint: "adapter/main.py",
        isolation: "local_process",
        install_mode: "scan_only",
        dependencies: [],
        artifact: {
          storage_key:
            "00000000-0000-4000-8000-000000000001/calendar/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          content_sha256: "a".repeat(64),
          file_count: 2,
          total_size_bytes: 56,
          stored_at: "2026-09-09T04:00:00Z",
          quarantine_state: "stored",
        },
      },
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

    const result = await api.uploadPluginArchive(
      new File(["plugin-bytes"], "calendar plugin.zip", { type: "application/zip" }),
    );
    expect(result).toEqual(response);
    expect(result.plugin.package_metadata?.provenance?.source).toBe("marketplace");
    expect(result.plugin.package_metadata?.provenance?.source_url).toBe(
      "https://plugins.example/marketplace/calendar",
    );

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

  it("installs trusted capabilities through the capability installer endpoint", async () => {
    const installResponse = {
      plan: {
        id: "cap-install-1234",
        status: "installed",
        entry_id: "office_doc_search",
        name_cn: "Office 文档搜索",
        summary_cn: "读取授权范围内的 Office 文档索引。",
        query: "读取 Office 文档并搜索",
        plugin_id: "office-doc-search",
        capabilities: ["office.search_documents"],
        risks: ["read_only"],
        permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
        rollback_strategy: "restore_previous_plugin_or_delete_installed_plugin",
        requires_confirmation: true,
        plugin_request: {
          id: "office-doc-search",
          name: "Office 文档搜索",
          enabled: true,
          capabilities: [
            {
              id: "office.search_documents",
              adapter: "http_json",
              permission_class: "file.read",
              sandbox_profile: "remote_connector",
              policy_effect: "require_approval",
              replay_safe: true,
              aliases: ["office_doc_search"],
              capability_config: {},
              input_schema: null,
              output_schema: null,
            },
          ],
        },
      },
      plugin: {
        id: "office-doc-search",
        name: "Office 文档搜索",
        enabled: true,
        description: null,
        version: "1.0.0",
        endpoint_url: "https://plugins.example/office-doc-search/invoke",
        domain_allowlist: ["plugins.example"],
        resource_config: {},
        timeout_seconds: 10,
        credential_ref: null,
        credential_header: "X-Plugin-Credential",
        credential_scheme: "Bearer",
        capabilities: [
          {
            id: "office.search_documents",
            adapter: "http_json",
            permission_class: "file.read",
            sandbox_profile: "remote_connector",
            policy_effect: "require_approval",
            replay_safe: true,
            aliases: ["office_doc_search"],
            capability_config: {},
            input_schema: null,
            output_schema: null,
          },
        ],
        source_filename: null,
        content_sha256: null,
        package_metadata: null,
        status: "running",
        health: "healthy",
        last_error_type: null,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(installResponse), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.installCapability({
      entry_id: "office_doc_search",
      query: "读取 Office 文档并搜索",
      plan_id: "cap-install-1234",
      confirm: true,
    });

    expect(result.plan.name_cn).toBe("Office 文档搜索");
    expect(result.plugin.status).toBe("running");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/capability-installer/install");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      entry_id: "office_doc_search",
      query: "读取 Office 文档并搜索",
      plan_id: "cap-install-1234",
      confirm: true,
    });
  });

  it("preserves capability install proposals on run detail responses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "55555555-5555-4555-8555-555555555555",
          tenant_id: "33333333-3333-4333-8333-333333333333",
          status: "failed",
          mode: "dispatch",
          decision_token: null,
          version: 1,
          clarification_reason: null,
          conversation_id: "conv-capability-install",
          request: "读取 Office 文档并搜索",
          created_at: "2026-09-25T00:00:00Z",
          queue_wait_ms: 0,
          capacity_wait_ms: 0,
          cost_usd: "0.0000",
          events: [],
          artifacts: [],
          explicit_details: {},
          capability_install_proposal: {
            entry_id: "office_doc_search",
            plan_id: "capability-plan-office_doc_search-1234",
            name_cn: "Office 文档搜索",
            summary_cn: "安装只读 Office 文档索引与搜索能力。",
            query: "读取 Office 文档并搜索",
            plugin_id: "office-doc-search",
            capabilities: ["office.search_documents"],
            risks: ["read_only"],
            permission_summary: ["读取用户选择的文档目录"],
            requires_confirmation: true,
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.run("55555555-5555-4555-8555-555555555555");

    expect(result.capability_install_proposal?.name_cn).toBe("Office 文档搜索");
    expect(result.capability_install_proposal?.capabilities).toEqual(["office.search_documents"]);
  });

  it("updates plugin package approval state", async () => {
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
      source_filename: "calendar plugin.zip",
      content_sha256: "abc123",
      package_metadata: {
        schema_version: 1,
        kind: "adapter_package",
        package_version: "1.2.3",
        adapter_id: "calendar_python",
        sdk_api_version: "1.0",
        signature: null,
        signature_verification: "not_provided",
        verified_public_key_sha256: null,
        signature_trust_expires_at: "2026-10-09T04:00:00Z",
        approval_state: "approved",
        approval_reason: "reviewed",
        approved_by: "11111111-1111-4111-8111-111111111111",
        approved_at: "2026-09-09T04:00:00Z",
        activation_state: "verified_scan_only",
        activation_reason: "package signature is verified, but install_mode=scan_only prevents activation",
        runtime: "python",
        entrypoint: "adapter/main.py",
        isolation: "local_process",
        install_mode: "scan_only",
        dependencies: [],
        artifact: null,
      },
      status: "stopped",
      health: "stopped",
      last_error_type: null,
    };
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(plugin), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.approvePluginPackage("calendar", { reason: "reviewed" })).resolves.toEqual(plugin);
    await expect(api.rejectPluginPackage("calendar", { reason: "requires isolation" })).resolves.toEqual(plugin);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/plugins/calendar/package/approve");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "reviewed" }) }),
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/admin/plugins/calendar/package/reject");
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "requires isolation" }) }),
    );
  });

  it("parses runtime registered plugin package metadata", async () => {
    const plugin = {
      id: "calendar",
      name: "Calendar Python",
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
      source_filename: "calendar plugin.zip",
      content_sha256: "abc123",
      package_metadata: {
        schema_version: 1,
        kind: "adapter_package",
        package_version: "1.2.3",
        adapter_id: "calendar_python",
        sdk_api_version: "1.0",
        signature: {
          algorithm: "ed25519",
          key_id: "calendar-prod",
          value: "A".repeat(86),
        },
        signature_verification: "verified",
        verified_public_key_sha256: "a".repeat(64),
        signature_trust_expires_at: "2026-10-09T04:00:00Z",
        approval_state: "approved",
        approval_reason: "reviewed",
        approved_by: "11111111-1111-4111-8111-111111111111",
        approved_at: "2026-09-09T04:00:00Z",
        activation_state: "eligible",
        activation_reason: "package signature, approval, SDK, adapter, and isolation policy allow execution",
        runtime: "python",
        entrypoint: "adapter/main.py",
        isolation: "in_process",
        install_mode: "runtime_registered",
        dependencies: [
          {
            kind: "python",
            source: "pypi",
            name: "requests",
            version: "2.31.0",
          },
        ],
        artifact: {
          storage_key:
            "00000000-0000-4000-8000-000000000001/calendar/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          content_sha256: "a".repeat(64),
          file_count: 2,
          total_size_bytes: 56,
          stored_at: "2026-09-09T04:00:00Z",
          quarantine_state: "stored",
        },
      },
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

    expect(result[0]?.package_metadata?.install_mode).toBe("runtime_registered");
    expect(result[0]?.package_metadata?.activation_state).toBe("eligible");
    expect(result[0]?.package_metadata?.signature_trust_expires_at).toBe("2026-10-09T04:00:00Z");
    expect(result[0]?.package_metadata?.dependencies).toEqual([
      {
        kind: "python",
        source: "pypi",
        name: "requests",
        version: "2.31.0",
      },
    ]);
    expect(result[0]?.package_metadata?.artifact?.quarantine_state).toBe("stored");
  });

  it("manages trusted plugin signing keys", async () => {
    const signingKey = {
      key_id: "calendar-prod",
      algorithm: "ed25519",
      public_key: "A".repeat(43),
      trusted: true,
      not_before: "2026-09-08T00:00:00Z",
      not_after: "2026-10-08T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify([signingKey]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(signingKey), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.pluginSigningKeys()).resolves.toEqual([signingKey]);
    await expect(
      api.upsertPluginSigningKey({
        key_id: "calendar-prod",
        algorithm: "ed25519",
        public_key: "A".repeat(43),
        not_before: "2026-09-08T00:00:00Z",
        not_after: "2026-10-08T00:00:00Z",
      }),
    ).resolves.toEqual(signingKey);

    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(
      /^\/api\/v1\/admin\/plugins\/signing-keys\?_/,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: "GET" }));
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/admin/plugins/signing-keys");
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          key_id: "calendar-prod",
          algorithm: "ed25519",
          public_key: "A".repeat(43),
          not_before: "2026-09-08T00:00:00Z",
          not_after: "2026-10-08T00:00:00Z",
        }),
      }),
    );
  });

  it("deletes trusted plugin signing keys", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "deleted" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.deletePluginSigningKey("calendar-prod")).resolves.toEqual({
      status: "deleted",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/v1/admin/plugins/signing-keys/calendar-prod",
    );
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: "DELETE" }));
  });

  it("encodes plugin signing key ids in delete paths", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "deleted" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.deletePluginSigningKey("calendar:prod.v1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/v1/admin/plugins/signing-keys/calendar%3Aprod.v1",
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
