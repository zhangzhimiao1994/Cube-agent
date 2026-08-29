import { expect, test, type Page } from "@playwright/test";

const runId = "22222222-2222-4222-8222-222222222222";
const now = "2026-08-29T00:00:00Z";

const currentUser = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "00000000-0000-4000-8000-000000000001",
  username: "super-admin",
  role: "super_admin",
  permissions: ["*"],
};

const model = {
  id: "model-main",
  provider: "openai-compatible",
  api_base: "https://relay.example.com/v1",
  api_protocol: "openai_compatible",
  upstream_model: "gpt-5.6-long-context-production-model",
  logical_model: "main-agent-production-primary",
  capabilities: ["text", "tool_calling", "vision"],
  credential_ref: "secret:model-main",
  quota_scope: "main-agent-production-primary",
  max_concurrency: 4,
  target_utilization: 0.75,
  reserved_capacity: 1,
  rpm: 120,
  tpm: 200000,
  queue_timeout_seconds: 60,
  fallback: null,
  weight: 1,
  effective_slots: 3,
  saturation_policy: "queue",
};

const run = {
  id: runId,
  status: "running",
  mode: "dispatch",
  conversation_id: "conversation-main",
  request: "Review production deployment readiness and summarize remaining operational risk.",
  created_at: now,
  queue_wait_ms: 120,
  capacity_wait_ms: 40,
  cost_usd: "0.0132",
  events: [
    {
      sequence: 1,
      kind: "queued",
      message: "Run accepted and queued.",
      created_at: now,
      participants: [],
      payload: {},
    },
  ],
  artifacts: [{ id: "artifact-1", kind: "markdown", title: "Readiness report" }],
  explicit_details: { routing: "dispatch mode selected explicitly" },
};

const workflow = {
  id: "short-video-dispatch",
  name: "Short video dispatch workflow",
  enabled: true,
  role: null,
  prompt: null,
  model: null,
  skills: [],
  mode: "dispatch",
  task_type: "short-video",
  allow_main_agent_override: true,
  allow_temporary_agents: true,
  temporary_agent_policy: "Ask before creating temporary roles.",
  role_selection_policy: "Choose director, writer, editor, reviewer, and operations only when needed.",
  agent_ids: ["director", "writer"],
  objective: "Produce scripts, plans, and review notes for production work.",
  steps: ["Plan", "Draft", "Review"],
  deliverables: ["Plan", "Final answer"],
  decision_policy: "Reviewer resolves conflicts before delivery.",
};

const agent = {
  id: "director",
  name: "Director",
  enabled: true,
  role: "director",
  prompt: "Coordinate production work and review role handoffs.",
  model: "main-agent-production-primary",
  skills: ["research"],
};

const hermesInsight = {
  id: "hermes-1",
  category: "conversation",
  outcome: "success",
  lesson: "Use dispatch mode when the request has clear deliverables.",
  summary: "Matched dispatch mode for concrete deliverables.",
  run_id: runId,
  conversation_id: "conversation-main",
  confirmed_at: null,
  tags: ["dispatch"],
  weight: 3,
  created_at: now,
};

async function mockLayoutApi(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: currentUser });
      return;
    }
    if (path === "/api/v1/admin/models") {
      await route.fulfill({ json: [model] });
      return;
    }
    if (path === "/api/v1/admin/main-agent") {
      await route.fulfill({
        json: {
          model: {
            provider: model.provider,
            api_base: model.api_base,
            api_protocol: model.api_protocol,
            upstream_model: model.upstream_model,
            credential_ref: model.credential_ref,
            capabilities: model.capabilities,
            max_concurrency: 2,
          },
          control_mode: "supervisor",
          decision_policy: "Main agent resolves mode, role, and approval boundaries.",
          operating_style: "Control the room, clarify goals, choose mode and roles, decide conflicts, review failures.",
          direct_answerer: "main_agent",
          hermes_policy: "confirm_before_apply",
          max_review_rounds: 2,
        },
      });
      return;
    }
    if (path === "/api/v1/admin/settings") {
      await route.fulfill({
        json: {
          default_mode: "dispatch",
          default_workflow_id: workflow.id,
          default_agent_ids: [agent.id],
          log_level: "warning",
          hermes_enabled: true,
          safe_tools_enabled: true,
          require_approval_for_tools: true,
          allow_main_agent_override: true,
          allow_temporary_agents: true,
          vibe_coding_enabled: true,
          multimedia_generation_enabled: true,
          openclaw_enabled: true,
          openclaw_mode: "ask",
          openclaw_allowed_commands: [["systemctl", "status", "agent-hub-api"]],
          openclaw_remote_adapters: [
            {
              platform: "windows",
              target_type: "desktop",
              target: "prod-web-01",
              base_url: "https://openclaw.example.com",
              credential_ref: "secret:openclaw",
            },
          ],
          temporary_agent_policy: "Ask before adding a temporary role.",
          channel_entry: "main_agent",
          attachment_retention_days: 7,
          attachment_max_mb: 25,
        },
      });
      return;
    }
    if (path === "/api/v1/config/current") {
      await route.fulfill({
        json: {
          id: "config-current",
          version: 7,
          status: "published",
          document: { models: { [model.logical_model]: model }, agents: [agent] },
          created_by: currentUser.user_id,
          created_at: now,
        },
      });
      return;
    }
    if (path === "/api/v1/admin/agents") {
      await route.fulfill({ json: [agent] });
      return;
    }
    if (path === "/api/v1/admin/workflows") {
      await route.fulfill({ json: [workflow] });
      return;
    }
    if (path === "/api/v1/admin/runs") {
      await route.fulfill({ json: [run] });
      return;
    }
    if (path === `/api/v1/admin/runs/${runId}`) {
      await route.fulfill({ json: run });
      return;
    }
    if (path === "/api/v1/admin/skills") {
      await route.fulfill({
        json: [
          {
            id: "safe-skill",
            name: "safe-skill-with-long-operational-name",
            status: "scanned",
            scan_diff: ["added SKILL.md", "requested filesystem:read"],
            requested_permissions: ["filesystem:read", "network:https"],
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/mcp") {
      await route.fulfill({
        json: [
          {
            id: "filesystem",
            name: "Filesystem MCP",
            health: "healthy",
            allowed_tools: ["read_file", "list_directory"],
            transport: "streamable_http",
            url: "https://mcp.example.com",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/channels" || path === "/api/v1/admin/channels") {
      await route.fulfill({
        json: [
          {
            id: "feishu",
            name: "Feishu",
            status: "missing_configuration",
            transports: ["webhook", "long_connection"],
            webhook_path: "/api/v1/channels/feishu/webhook",
            public_webhook_url: "https://agent.example.com/api/v1/channels/feishu/webhook",
            missing: ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
            configured: ["FEISHU_VERIFICATION_TOKEN"],
            configured_sources: { FEISHU_VERIFICATION_TOKEN: "server" },
            command_aliases: {},
            notes: ["Webhook is mounted on the main API service."],
            runtime: {
              status: "disconnected",
              ready: false,
              connection_attempts: 2,
              reconnects: 1,
              received_events: 4,
              submitted_messages: 3,
              ignored_events: 1,
              failures: 1,
              last_error_type: "missing_secret",
              last_error_message: "Missing app secret.",
            },
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/users" || path === "/api/v1/admin/users") {
      await route.fulfill({
        json: [
          {
            id: currentUser.user_id,
            username: "super-admin",
            role: "super_admin",
            disabled: false,
            feishu_open_id: "ou_long_feishu_identifier_for_layout",
            protected: true,
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/evolution-runs") {
      await route.fulfill({
        json: [
          {
            id: "evolution-1",
            kind: "skill_optimization",
            title: "Improve research skill reliability",
            objective: "Refine skill behavior with score-gated review.",
            mode: "dispatch",
            source_skill_ids: ["safe-skill"],
            source_conversation_id: "conversation-main",
            source_run_id: runId,
            target_artifact_type: "skill",
            baseline_agent_id: agent.id,
            candidate_agent_ids: ["candidate-agent-with-long-id"],
            evaluator_agent_id: "reviewer",
            approval_policy: "ask",
            approval_status: "pending",
            approved_by: null,
            approved_at: null,
            approval_note: "",
            iteration_policy: "score_gated",
            memory_policy: "summarize_between_rounds",
            next_action: "request_approval",
            status: "pending",
            max_rounds: 3,
            min_delta: 0.1,
            budget_tokens: 10000,
            budget_minutes: 30,
            rubric: ["Accuracy", "Coverage"],
            rounds: [],
            created_by: currentUser.user_id,
            created_at: now,
            updated_at: now,
            stop_reason: null,
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/openclaw/adapters") {
      await route.fulfill({
        json: [
          {
            platform: "windows",
            kind: "desktop_action",
            target_type: "desktop",
            status: "available",
            execution_host: "prod-web-01",
            requires_user_approval: true,
            supports_read_only: true,
            description: "Remote desktop adapter for bounded operations.",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/openclaw/sessions") {
      await route.fulfill({
        json: [
          {
            id: "session-1",
            status: "active",
            adapter_status: "available",
            mode: "ask",
            platform: "windows",
            target_type: "desktop",
            target: "prod-web-01",
            purpose: "Verify desktop readiness.",
            execution_host: "prod-web-01",
            requested_by: currentUser.user_id,
            created_at: now,
            updated_at: now,
            stopped_at: null,
            operation_ids: [],
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/openclaw/operations") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/admin/schedules") {
      await route.fulfill({
        json: [
          {
            id: "schedule-1",
            name: "Daily readiness check",
            status: "active",
            kind: "cron",
            mode: "dispatch",
            workflow_id: workflow.id,
            message: "Run readiness check.",
            timezone: "Asia/Shanghai",
            next_fire_at: now,
            run_at: null,
            cron: "0 9 * * *",
            misfire_policy: "fire_once",
            budget: 3,
            metadata: { source: "layout-audit" },
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/runs/attachments") {
      await route.fulfill({
        json: {
          items: [
            {
              id: "attachment-1",
              filename: "very-long-production-readiness-evidence-bundle.zip",
              kind: "archive",
              content_type: "application/zip",
              size_bytes: 2048000,
              sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
              expires_at: now,
            },
          ],
        },
      });
      return;
    }
    if (path === "/api/v1/admin/memory") {
      await route.fulfill({
        json: [
          {
            id: "memory-1",
            scope: "global",
            value: "Prefer production-safe deployment steps and explicit verification.",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/logs") {
      await route.fulfill({
        json: [
          {
            id: "log-1",
            category: url.searchParams.get("category") ?? "audit",
            level: "info",
            title: "config.publish",
            message: "configuration published",
            source: "audit",
            details: { conversation_id: "conversation-main", run_id: runId },
            created_at: now,
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/hermes") {
      await route.fulfill({ json: [hermesInsight] });
      return;
    }

    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

async function assertNoViewportLayoutIssues(page: Page) {
  const result = await page.evaluate(() => {
    function isVisible(element: Element) {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    }

    function hasHorizontalScrollAncestor(element: Element) {
      let parent = element.parentElement;
      while (parent) {
        const style = window.getComputedStyle(parent);
        const canScroll = /(auto|scroll)/.test(style.overflowX);
        if (canScroll && parent.scrollWidth > parent.clientWidth + 2) return true;
        parent = parent.parentElement;
      }
      return false;
    }

    function hasInactiveAncestor(element: Element) {
      let parent: Element | null = element;
      while (parent) {
        const style = window.getComputedStyle(parent);
        if (style.pointerEvents === "none" || parent.getAttribute("aria-hidden") === "true" || parent.hasAttribute("hidden")) {
          return true;
        }
        parent = parent.parentElement;
      }
      return false;
    }

    const viewportWidth = document.documentElement.clientWidth;
    const issues: string[] = [];
    const rootOverflow = document.documentElement.scrollWidth - viewportWidth;
    if (rootOverflow > 2) {
      issues.push(`root horizontal overflow ${rootOverflow}px`);
    }

    const controls = Array.from(document.querySelectorAll("button,input,select,textarea,a,.button-link,[role='button']"));
    for (const element of controls) {
      if (!isVisible(element) || hasInactiveAncestor(element) || hasHorizontalScrollAncestor(element)) continue;
      const rect = element.getBoundingClientRect();
      if (rect.left < -1 || rect.right > viewportWidth + 1) {
        const label =
          element.getAttribute("aria-label") ||
          element.textContent?.trim().replace(/\s+/g, " ").slice(0, 80) ||
          element.tagName.toLowerCase();
        issues.push(`${element.tagName.toLowerCase()} outside viewport: ${label}`);
      }
    }

    return { issues, rootOverflow, viewportWidth };
  });

  expect(result.issues).toEqual([]);
}

const pages = [
  "/",
  `/runs/${runId}`,
  "/models",
  "/main-agent",
  "/agents",
  "/workflows",
  "/skills",
  "/mcp",
  "/channels",
  "/config",
  "/openclaw",
  "/schedules",
  "/evolution",
  "/attachments",
  "/memory",
  "/logs/audit",
  "/hermes",
  "/users",
];

const viewports = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

for (const viewport of viewports) {
  test.describe(`responsive layout audit on ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    for (const path of pages) {
      test(`${path} keeps controls within viewport`, async ({ page }) => {
        await mockLayoutApi(page);
        await page.goto(path);
        await page.waitForLoadState("networkidle");
        await expect(page.getByRole("alert").filter({ hasText: /加载失败|request_failed|invalid_response|not_found/i })).toHaveCount(0);
        await assertNoViewportLayoutIssues(page);
      });
    }
  });
}
