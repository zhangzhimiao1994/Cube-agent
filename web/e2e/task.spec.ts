import { expect, test, type Page } from "@playwright/test";

const runId = "22222222-2222-4222-8222-222222222222";
const codingRunId = "33333333-3333-4333-8333-333333333333";
const codingConversationId = "44444444-4444-4444-8444-444444444444";

async function mockRunApi(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({
        json: {
          user_id: "11111111-1111-4111-8111-111111111111",
          tenant_id: "00000000-0000-4000-8000-000000000001",
          role: "super_admin",
        },
      });
      return;
    }
    if (path === "/api/v1/admin/runs") {
      await route.fulfill({
        json: [
          {
            id: runId,
            status: "running",
            mode: "dispatch",
            queue_wait_ms: 120,
            capacity_wait_ms: 40,
            cost_usd: "0.0132",
          },
        ],
      });
      return;
    }
    if (path === `/api/v1/admin/runs/${runId}`) {
      await route.fulfill({
        json: {
          id: runId,
          status: "running",
          mode: "dispatch",
          queue_wait_ms: 120,
          capacity_wait_ms: 40,
          cost_usd: "0.0132",
          request: "Summarize current deployment readiness.",
          events: [
            {
              sequence: 1,
              kind: "queued",
              message: "Run accepted and queued.",
              created_at: "2026-08-07T00:00:00Z",
            },
            {
              sequence: 2,
              kind: "tool.started",
              message: "tool.started",
              summary: "terminal check started",
              created_at: "2026-08-07T00:00:01Z",
              actor: "engineer",
              tool_name: "run_safe_command",
              tool_call_id: "call_terminal",
              step_id: "verify",
              payload: {
                status: "started",
                operation_kind: "terminal",
                argument_bytes: 36,
              },
            },
            {
              sequence: 3,
              kind: "tool.failed",
              message: "tool.failed",
              summary: "terminal check failed",
              created_at: "2026-08-07T00:00:02Z",
              actor: "engineer",
              tool_name: "run_safe_command",
              tool_call_id: "call_terminal",
              step_id: "verify",
              payload: {
                status: "failed",
                operation_kind: "terminal",
                output_bytes: 96,
                exit_code: 1,
                failure_kind: "nonzero_exit",
              },
            },
          ],
          artifacts: [{ id: "artifact-1", kind: "markdown", title: "Readiness report" }],
          explicit_details: {
            routing_reason: "dispatch mode selected explicitly",
            harness_provider: "openai",
            harness_logical_model: "vibe_engineer",
          },
          failure_diagnostics: [
            {
              category: "tool",
              stage: "tool.failed",
              reason: "terminal command failed",
              recommendation: "Review the tool lifecycle before retrying.",
              sequence: 3,
              actor: "engineer",
              step_id: "verify",
              tool_name: "run_safe_command",
              tool_call_id: "call_terminal",
              failure_kind: "nonzero_exit",
              status_code: null,
              logical_model: null,
              approval_id: null,
              action: null,
              wrapped_by: null,
            },
          ],
        },
      });
      return;
    }
    if (path === `/api/v1/admin/runs/${runId}/cancel`) {
      await route.fulfill({
        json: {
          id: runId,
          status: "cancelled",
          mode: "dispatch",
          queue_wait_ms: 120,
          capacity_wait_ms: 40,
          cost_usd: "0.0132",
          request: "Summarize current deployment readiness.",
          events: [],
          artifacts: [],
          explicit_details: {},
          failure_diagnostics: [],
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("operator inspects run detail and cancels safely", async ({ page }) => {
  await mockRunApi(page);
  await page.goto(`/runs/${runId}`);
  await expect(page.getByText("排队等待")).toBeVisible();
  await expect(page.getByRole("heading", { name: "120 ms" })).toBeVisible();
  await expect(page.getByRole("status", { name: /任务态势，执行异常/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "工具链路" })).toBeVisible();
  await expect(page.getByText("run_safe_command").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "故障诊断" })).toBeVisible();
  await expect(page.getByText("Readiness report")).toBeVisible();
  await page.getByRole("button", { name: "取消" }).click();
  await expect(page.getByRole("heading", { name: "已取消" })).toBeVisible();
});

async function mockCodingRunApi(page: Page) {
  const finalArtifactId = "55555555-5555-4555-8555-555555555555";
  const intermediateArtifactId = "66666666-6666-4666-8666-666666666666";
  const finalDownloadPath = `/api/v1/admin/runs/${codingRunId}/artifacts/${finalArtifactId}/download`;
  const intermediateDownloadPath = `/api/v1/admin/runs/${codingRunId}/artifacts/${intermediateArtifactId}/download`;
  const runDetail = {
    id: codingRunId,
    status: "completed",
    mode: "dispatch",
    conversation_id: codingConversationId,
    request: "生成一个最简单的 hello world 项目。",
    created_at: "2026-08-31T00:00:00Z",
    queue_wait_ms: 10,
    capacity_wait_ms: 5,
    cost_usd: "0.0001",
    events: [
      {
        sequence: 1,
        kind: "step.started",
        message: "step.started",
        summary: "主 Agent 已拆分编码任务。",
        created_at: "2026-08-31T00:00:00Z",
        actor: "main_agent",
        step_id: "main_agent_plan",
        payload: {
          roles: [
            {
              id: "engineer",
              name: "陆微",
              role: "工程师",
              logical_model: "vibe-engineer",
              tools: ["terminal.run", "file.write"],
            },
          ],
          steps: [
            {
              id: "create_project",
              title: "创建项目",
              agent: "engineer",
              summary: "创建 hello world 项目",
              depends_on: [],
            },
          ],
        },
      },
      {
        sequence: 2,
        kind: "step.started",
        message: "step.started",
        summary: "工程师开始创建最小项目。",
        created_at: "2026-08-31T00:00:01Z",
        actor: "engineer",
        step_id: "create_project",
        payload: {
          role: "工程师",
          logical_model: "vibe-engineer",
          task: "创建 hello world 项目",
        },
      },
      {
        sequence: 3,
        kind: "artifact.created",
        message: "artifact.created",
        summary: "生成中间项目文件。",
        created_at: "2026-08-31T00:00:02Z",
        actor: "engineer",
        step_id: "create_project",
        payload: { artifact_id: intermediateArtifactId },
        artifact: {
          id: intermediateArtifactId,
          kind: "zip",
          title: "工程师",
          filename: "hello-world-source.zip",
          mime_type: "application/zip",
          size_bytes: 22,
          sha256: "b".repeat(64),
          download_url: intermediateDownloadPath,
          presentation: "step_detail",
        },
      },
      {
        sequence: 4,
        kind: "step.completed",
        message: "step.completed",
        summary: "工程师完成最小项目。",
        created_at: "2026-08-31T00:00:02Z",
        actor: "engineer",
        step_id: "create_project",
        payload: {
          role: "工程师",
          logical_model: "vibe-engineer",
          artifact_id: finalArtifactId,
        },
      },
      {
        sequence: 5,
        kind: "runtime.completed",
        message: "runtime.completed",
        summary: "项目已生成并打包。",
        created_at: "2026-08-31T00:00:03Z",
        actor: "main",
        payload: {},
      },
    ],
    artifacts: [
      {
        id: "reply",
        kind: "markdown",
        title: "main",
        text: "已生成一个最小 hello world 项目，并附上可下载压缩包。",
        filename: null,
        mime_type: null,
        size_bytes: null,
        sha256: null,
        download_url: null,
        presentation: null,
      },
      {
        id: finalArtifactId,
        kind: "zip",
        title: "final_synthesizer",
        text: null,
        filename: "hello-world.zip",
        mime_type: "application/zip",
        size_bytes: 22,
        sha256: "a".repeat(64),
        download_url: finalDownloadPath,
        presentation: "final_attachment",
      },
    ],
    explicit_details: {
      conversation_id: codingConversationId,
    },
    failure_diagnostics: [],
    tool_lifecycle: [],
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({
        json: {
          user_id: "11111111-1111-4111-8111-111111111111",
          tenant_id: "00000000-0000-4000-8000-000000000001",
          role: "super_admin",
        },
      });
      return;
    }
    if (path === "/api/v1/admin/runs") {
      await route.fulfill({ json: [runDetail] });
      return;
    }
    if (path === "/api/v1/admin/agents") {
      await route.fulfill({
        json: [{ id: "engineer", name: "陆微", enabled: true, role: "工程师", model: "vibe-engineer", skills: [] }],
      });
      return;
    }
    if (path === "/api/v1/admin/models") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/admin/workflows") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/admin/settings") {
      await route.fulfill({
        json: {
          default_mode: "dispatch",
          default_workflow_id: null,
          default_agent_ids: [],
          log_level: "warning",
          hermes_enabled: false,
          safe_tools_enabled: true,
          require_approval_for_tools: true,
          allow_main_agent_override: false,
          allow_temporary_agents: false,
          vibe_coding_enabled: true,
          channel_entry: "",
          attachment_retention_days: 7,
          attachment_max_mb: 10,
        },
      });
      return;
    }
    if (path === "/api/v1/admin/main-agent") {
      await route.fulfill({
        json: {
          model: {
            provider: "openai",
            api_base: "https://api.openai.com/v1",
            api_protocol: "openai_compatible",
            upstream_model: "gpt-5.6",
            credential_ref: "secret:model",
            capabilities: ["tool_calling"],
            max_concurrency: 1,
          },
          control_mode: "autonomous",
          decision_policy: "ship working code",
          operating_style: "control the room",
          direct_answerer: "main_agent",
          hermes_policy: "observe",
          max_review_rounds: 1,
        },
      });
      return;
    }
    if (path === "/api/v1/runs" && request.method() === "POST") {
      await route.fulfill({
        json: {
          id: codingRunId,
          tenant_id: "00000000-0000-4000-8000-000000000001",
          status: "completed",
          mode: "dispatch",
          decision_token: null,
          version: 1,
          clarification_reason: null,
          conversation_id: codingConversationId,
        },
      });
      return;
    }
    if (path === `/api/v1/admin/runs/${codingRunId}`) {
      await route.fulfill({ json: runDetail });
      return;
    }
    if (path === `/api/v1/admin/conversations/${codingConversationId}`) {
      await route.fulfill({ json: { conversation_id: codingConversationId, runs: [runDetail] } });
      return;
    }
    if (path === finalDownloadPath || path === intermediateDownloadPath) {
      const filename = path === finalDownloadPath ? "hello-world.zip" : "hello-world-source.zip";
      await route.fulfill({
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": `attachment; filename="${filename}"`,
        },
        body: "zip-bytes",
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("operator validates a simple coding run and downloads final and intermediate artifacts", async ({ page }) => {
  await mockCodingRunApi(page);

  await page.goto("/");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("已生成一个最小 hello world 项目，并附上可下载压缩包。")).toBeVisible();
  await expect(page.getByRole("link", { name: /下载 hello-world\.zip/ })).toBeVisible();
  await expect(page.getByText("hello-world-source.zip")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "助手派单状态" })).toContainText("陆微");
  await expect(page.getByRole("region", { name: "Agent 集群动作" })).toContainText("生成中间项目文件。");
  await expect(page.getByRole("region", { name: "Agent 集群动作" })).not.toContainText("create_project");

  const finalDownload = page.waitForEvent("download");
  await page.getByRole("link", { name: /下载 hello-world\.zip/ }).click();
  expect((await finalDownload).suggestedFilename()).toBe("hello-world.zip");

  await page.getByRole("button", { name: /生成中间项目文件。/ }).click();
  await expect(page.getByRole("dialog", { name: "运行过程详情" })).toBeVisible();
  const intermediateDownload = page.waitForEvent("download");
  await page.getByRole("link", { name: /下载 hello-world-source\.zip/ }).click();
  expect((await intermediateDownload).suggestedFilename()).toBe("hello-world-source.zip");
  await page.locator(".process-drawer-backdrop").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "运行过程详情" })).toHaveCount(0);
});
