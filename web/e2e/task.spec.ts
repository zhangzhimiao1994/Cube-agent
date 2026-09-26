import { expect, test, type Page } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const runId = "22222222-2222-4222-8222-222222222222";
const codingRunId = "33333333-3333-4333-8333-333333333333";
const codingConversationId = "44444444-4444-4444-8444-444444444444";

function crc32(bytes: Buffer) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function buildStoredZip(files: Record<string, string>) {
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  let offset = 0;

  for (const [name, content] of Object.entries(files)) {
    const nameBytes = Buffer.from(name, "utf8");
    const contentBytes = Buffer.from(content, "utf8");
    const checksum = crc32(contentBytes);

    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(0x04034b50, 0);
    localHeader.writeUInt16LE(20, 4);
    localHeader.writeUInt16LE(0, 6);
    localHeader.writeUInt16LE(0, 8);
    localHeader.writeUInt32LE(checksum, 14);
    localHeader.writeUInt32LE(contentBytes.length, 18);
    localHeader.writeUInt32LE(contentBytes.length, 22);
    localHeader.writeUInt16LE(nameBytes.length, 26);
    localParts.push(localHeader, nameBytes, contentBytes);

    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(0x02014b50, 0);
    centralHeader.writeUInt16LE(20, 4);
    centralHeader.writeUInt16LE(20, 6);
    centralHeader.writeUInt16LE(0, 8);
    centralHeader.writeUInt16LE(0, 10);
    centralHeader.writeUInt32LE(checksum, 16);
    centralHeader.writeUInt32LE(contentBytes.length, 20);
    centralHeader.writeUInt32LE(contentBytes.length, 24);
    centralHeader.writeUInt16LE(nameBytes.length, 28);
    centralHeader.writeUInt32LE(offset, 42);
    centralParts.push(centralHeader, nameBytes);

    offset += localHeader.length + nameBytes.length + contentBytes.length;
  }

  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);

  return Buffer.concat([...localParts, centralDirectory, end]);
}

function readStoredZipEntries(bytes: Buffer) {
  const entries = new Map<string, string>();
  let offset = 0;
  while (offset + 4 <= bytes.length && bytes.readUInt32LE(offset) === 0x04034b50) {
    const method = bytes.readUInt16LE(offset + 8);
    const compressedSize = bytes.readUInt32LE(offset + 18);
    const filenameLength = bytes.readUInt16LE(offset + 26);
    const extraLength = bytes.readUInt16LE(offset + 28);
    const nameStart = offset + 30;
    const contentStart = nameStart + filenameLength + extraLength;
    const contentEnd = contentStart + compressedSize;
    if (method !== 0) throw new Error("test ZIP parser only supports stored entries");
    const name = bytes.subarray(nameStart, nameStart + filenameLength).toString("utf8");
    entries.set(name, bytes.subarray(contentStart, contentEnd).toString("utf8"));
    offset = contentEnd;
  }
  return entries;
}

async function extractStoredZipEntries(bytes: Buffer, destination: string) {
  const entries = readStoredZipEntries(bytes);
  for (const [name, content] of entries) {
    const outputPath = join(destination, name);
    await mkdir(dirname(outputPath), { recursive: true });
    await writeFile(outputPath, content, "utf8");
  }
  return entries;
}

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
  await expect(page.getByText("终端").first()).toBeVisible();
  await expect(page.getByText("命令非零退出").first()).toBeVisible();
  await expect(page.getByText("run_safe_command")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "故障诊断" })).toBeVisible();
  await expect(page.getByText("Readiness report")).toBeVisible();
  await page.getByRole("button", { name: "取消" }).click();
  await expect(page.getByRole("heading", { name: "已取消" })).toBeVisible();
});

async function mockCodingRunApi(
  page: Page,
  options: {
    liveRefresh?: boolean;
    fullOutputSentinel?: string;
    largeWorkbench?: boolean;
    longWorkspacePreview?: boolean;
    multiTurnHistory?: boolean;
  } = {},
) {
  const finalArtifactId = "55555555-5555-4555-8555-555555555555";
  const intermediateArtifactId = "66666666-6666-4666-8666-666666666666";
  const finalDownloadPath = `/api/v1/runs/${codingRunId}/artifacts/${finalArtifactId}/download`;
  const intermediateDownloadPath = `/api/v1/runs/${codingRunId}/artifacts/${intermediateArtifactId}/download`;
  const longPreviewSentinel = "README_SCROLL_SENTINEL_末尾内容必须可以通过拖动看到";
  const longPreviewText = [
    "# Enterprise Portfolio OS",
    "",
    ...Array.from({ length: 90 }, (_item, index) => `第 ${index + 1} 行：移动端文件预览需要保持可拖动，不能被抽屉或浏览器底栏截断。`),
    longPreviewSentinel,
  ].join("\n");
  let detailRequests = 0;
  const projectWorkspaces = [
    {
      project_id: "default",
      label: "默认项目",
      workspace_path: "main",
      created_at: "2026-08-31T00:00:00Z",
      updated_at: "2026-08-31T00:00:00Z",
    },
  ];
  const plannedRoles = options.largeWorkbench
    ? [
        {
          id: "engineer",
          name: "陆微",
          role: "工程师",
          logical_model: "vibe-engineer",
          tools: ["terminal.run", "file.write"],
        },
        { id: "planner", name: "规划助手", role: "规划助手", logical_model: "deepseek-chat", tools: [] },
        { id: "reviewer", name: "审查助手", role: "审查助手", logical_model: "qwen-max", tools: [] },
        { id: "researcher", name: "资料助手", role: "资料助手", logical_model: "kimi-k2", tools: [] },
        { id: "designer", name: "视觉助手", role: "视觉助手", logical_model: "minimax-m3", tools: [] },
        { id: "operator", name: "运营助手", role: "运营助手", logical_model: "gpt-5.6", tools: [] },
        { id: "publisher", name: "发布助手", role: "发布助手", logical_model: "qwen-plus", tools: [] },
      ]
    : [
        {
          id: "engineer",
          name: "陆微",
          role: "工程师",
          logical_model: "vibe-engineer",
          tools: ["terminal.run", "file.write"],
        },
      ];
  const plannedSteps = [
    {
      id: "create_project",
      title: "创建项目",
      agent: "engineer",
      summary: "创建 hello world 项目",
      depends_on: [],
    },
  ];
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
          roles: plannedRoles,
          steps: plannedSteps,
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
        payload: {
          artifact_id: intermediateArtifactId,
          ...(options.fullOutputSentinel ? { output: options.fullOutputSentinel } : {}),
        },
        artifact: {
          id: intermediateArtifactId,
          kind: "zip",
          title: "工程师",
          text: options.fullOutputSentinel ?? null,
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
  const refreshedRunDetail = {
    ...runDetail,
    events: [
      ...runDetail.events,
      {
        sequence: 6,
        kind: "step.started",
        message: "step.started",
        summary: "工程师刷新后继续执行验收。",
        created_at: "2026-08-31T00:00:04Z",
        actor: "engineer",
        step_id: "create_project",
        payload: {
          role: "工程师",
          logical_model: "vibe-engineer",
          task: "刷新后继续验收 hello world 项目",
        },
      },
    ],
  };
  const followUpRunDetail = {
    ...runDetail,
    id: "77777777-7777-4777-8777-777777777777",
    request: "继续优化 UI 交互，重点检查文件预览和配置页面。",
    created_at: "2026-08-31T00:03:00Z",
    events: [
      {
        sequence: 1,
        kind: "runtime.completed",
        message: "runtime.completed",
        summary: "已完成 UI 复核。",
        created_at: "2026-08-31T00:03:02Z",
        actor: "main",
        payload: {},
      },
    ],
    artifacts: [
      {
        id: "reply-follow-up",
        kind: "markdown",
        title: "main",
        text: "已复核文件预览和配置页面交互。",
        filename: null,
        mime_type: null,
        size_bytes: null,
        sha256: null,
        download_url: null,
        presentation: null,
      },
    ],
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
        json: plannedRoles.map((role) => ({
          id: role.id,
          name: role.name,
          enabled: true,
          role: role.role,
          model: role.logical_model,
          skills: [],
        })),
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
    if (path === "/api/v1/admin/execution-backends") {
      await route.fulfill({
        json: [
          {
            id: "systemd",
            name: "本机 systemd 隔离",
            adapter: "SystemdSkillSandbox",
            description: "本机隔离执行",
            isolation: "DynamicUser + 私有网络",
            cost: "本机资源",
            available: true,
            reason: null,
            supported_sandbox_profiles: ["read_only", "restricted", "workspace_write"],
          },
        ],
      });
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
    if (path === "/api/v1/admin/conversations" && request.method() === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/admin/project-workspaces" && request.method() === "GET") {
      await route.fulfill({ json: projectWorkspaces });
      return;
    }
    if (path === "/api/v1/admin/project-workspaces" && request.method() === "POST") {
      const payload = request.postDataJSON() as {
        project_id: string;
        label: string;
        workspace_path: string;
      };
      const created = {
        ...payload,
        created_at: "2026-08-31T00:00:00Z",
        updated_at: "2026-08-31T00:00:00Z",
      };
      projectWorkspaces.unshift(created);
      await route.fulfill({ status: 201, json: created });
      return;
    }
    if (path === "/api/v1/admin/conversations" && request.method() === "POST") {
      const payload = request.postDataJSON() as {
        conversation_id: string;
        title?: string;
        project_id: string;
        project_label?: string | null;
        workspace_path: string;
      };
      await route.fulfill({
        json: {
          ...payload,
          title: payload.title ?? null,
          project_label: payload.project_label ?? null,
          archived_at: null,
          created_at: "2026-08-31T00:00:00Z",
          updated_at: "2026-08-31T00:00:00Z",
        },
      });
      return;
    }
    if (/^\/api\/v1\/admin\/conversations\/[^/]+\/queue$/.test(path) && request.method() === "GET") {
      await route.fulfill({ json: [] });
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
      detailRequests += 1;
      await route.fulfill({
        json: options.liveRefresh && detailRequests > 1 ? refreshedRunDetail : runDetail,
      });
      return;
    }
    if (path === `/api/v1/admin/conversations/${codingConversationId}`) {
      await route.fulfill({
        json: {
          conversation_id: codingConversationId,
          runs: options.multiTurnHistory ? [runDetail, followUpRunDetail] : [runDetail],
        },
      });
      return;
    }
    if (path === `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/files`) {
      await route.fulfill({
        json: {
          items: [
            ...(options.longWorkspacePreview
              ? [
                  {
                    path: "README.md",
                    filename: "README.md",
                    mime_type: "text/markdown",
                    size_bytes: longPreviewText.length,
                    sha256: "e".repeat(64),
                    download_url: `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/files/download?path=README.md`,
                  },
                ]
              : []),
            {
              path: "plan.md",
              filename: "plan.md",
              mime_type: "text/markdown",
              size_bytes: 512,
              sha256: "c".repeat(64),
              download_url: `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/files/download?path=plan.md`,
            },
            {
              path: "src/index.mjs",
              filename: "index.mjs",
              mime_type: "text/javascript",
              size_bytes: 28,
              sha256: "d".repeat(64),
              download_url: `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/files/download?path=src%2Findex.mjs`,
            },
          ],
          bundle_download_url: `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/bundle/download`,
        },
      });
      return;
    }
    if (path === `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/files/download`) {
      const requestedPath = new URL(request.url()).searchParams.get("path");
      if (requestedPath === "README.md") {
        await route.fulfill({
          status: 200,
          headers: { "Content-Type": "text/markdown" },
          body: longPreviewText,
        });
        return;
      }
      if (requestedPath === "plan.md") {
        await route.fulfill({
          status: 200,
          headers: { "Content-Type": "text/markdown" },
          body: "# Plan\n\nCreate a hello world project.\n",
        });
        return;
      }
      if (requestedPath === "src/index.mjs") {
        await route.fulfill({
          status: 200,
          headers: { "Content-Type": "text/javascript" },
          body: "console.log('hello world')\n",
        });
        return;
      }
    }
    const workspaceBundlePath = `/api/v1/workspaces/projects/default/sessions/${codingConversationId}/bundle/download`;
    if (path === finalDownloadPath || path === workspaceBundlePath || path === intermediateDownloadPath) {
      const filename = path === intermediateDownloadPath ? "hello-world-source.zip" : "workspace.zip";
      const body =
        path === intermediateDownloadPath
          ? buildStoredZip({
              "source/index.mjs": "console.log('hello world')\n",
              "source/package.json": "{\"type\":\"module\",\"scripts\":{\"start\":\"node index.mjs\"}}\n",
              "source/README.md": "# Source Package\n\nRun with `node index.mjs`.\n",
            })
          : buildStoredZip({
              "hello-world/index.mjs": "console.log('hello world')\n",
              "hello-world/package.json": "{\"type\":\"module\",\"scripts\":{\"start\":\"node index.mjs\"}}\n",
              "hello-world/README.md": "# Hello World\n\nRun with `node index.mjs`.\n",
            });
      await route.fulfill({
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": `attachment; filename="${filename}"`,
        },
        body,
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("project conversation and run settings dialogs stay separate and usable on desktop and mobile", async ({ page }, testInfo) => {
  await mockCodingRunApi(page);

  for (const viewport of [
    { name: "desktop", width: 1280, height: 900 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/");

    const initialConversationDialog = page.getByRole("dialog", { name: "新建会话" });
    await expect(initialConversationDialog).toBeVisible();
    await expect(initialConversationDialog.getByLabel("所属项目")).toHaveValue("default");
    await page.keyboard.press("Escape");
    await expect(initialConversationDialog).toHaveCount(0);

    await page.getByRole("button", { name: "新建项目工作区" }).click();
    const projectDialog = page.getByRole("dialog", { name: "新建项目工作区" });
    await expect(projectDialog).toBeVisible();
    await expect(projectDialog.getByLabel("项目名称")).toBeVisible();
    await expect(projectDialog.getByLabel("会话标题")).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("hidden");
    await projectDialog.getByText("项目名称").click();
    await expect(projectDialog).toBeVisible();
    await page.locator(".conversation-dialog-backdrop").click({ position: { x: 5, y: 5 } });
    await expect(projectDialog).toHaveCount(0);

    await page.getByRole("button", { name: "新建对话" }).click();
    const conversationDialog = page.getByRole("dialog", { name: "新建会话" });
    await expect(conversationDialog).toBeVisible();
    await expect(conversationDialog.getByLabel("会话标题")).toBeVisible();
    await expect(conversationDialog.getByLabel("所属项目")).toHaveValue("default");
    await expect(conversationDialog.getByLabel("共享工作区名称")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(conversationDialog).toHaveCount(0);

    await page.getByRole("button", { name: "打开本次运行配置" }).click();
    const settingsDialog = page.getByRole("dialog", { name: "本次运行设置" });
    await expect(settingsDialog).toBeVisible();
    const closeSettings = settingsDialog.getByRole("button", { name: "关闭运行设置" });
    const settingsSummary = settingsDialog.locator("summary");
    await expect(closeSettings).toBeVisible();
    await expect(closeSettings).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(settingsSummary).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(closeSettings).toBeFocused();
    const layout = await page.evaluate(() => ({
      bodyWidth: document.body.scrollWidth,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }));
    expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    await page.screenshot({ path: testInfo.outputPath(`project-conversation-settings-${viewport.name}.png`) });
    await page.locator(".composer-settings-backdrop").click({ position: { x: 5, y: 5 } });
    await expect(settingsDialog).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("");
  }
});

test("operator validates a simple coding run and downloads final and intermediate artifacts", async ({ page }, testInfo) => {
  await mockCodingRunApi(page);

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("已生成一个最小 hello world 项目，并附上可下载压缩包。")).toBeVisible();
  const resultMessage = page
    .locator("article.chat-message.assistant")
    .filter({ hasText: "已生成一个最小 hello world 项目" });
  const deliverableFiles = resultMessage.getByRole("region", { name: "交付文件" });
  await expect(deliverableFiles).toBeVisible();
  await expect(deliverableFiles.getByRole("button", { name: "下载 workspace.zip" })).toBeVisible();
  await expect(deliverableFiles.getByRole("button", { name: "下载 plan.md" })).toBeVisible();
  await expect(deliverableFiles.getByRole("button", { name: "下载 index.mjs" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Agent 工作席 1 个 Agent/ })).toBeVisible();
  await page.getByRole("button", { name: /Agent 工作席 1 个 Agent/ }).click();
  const workbenchDrawer = page.getByRole("dialog", { name: "Agent 工作席详情" });
  await expect(workbenchDrawer).toContainText("实现");
  await expect(workbenchDrawer).toContainText("vibe-engineer");
  await expect(workbenchDrawer).not.toContainText("create_project");
  await workbenchDrawer.getByRole("button", { name: "关闭" }).click();
  await expect(workbenchDrawer).toHaveCount(0);

  const finalDownload = page.waitForEvent("download");
  await deliverableFiles.getByRole("button", { name: "下载 workspace.zip" }).click();
  const finalArchive = await finalDownload;
  expect(finalArchive.suggestedFilename()).toBe("workspace.zip");
  const finalArchivePath = testInfo.outputPath("hello-world.zip");
  await finalArchive.saveAs(finalArchivePath);
  const finalArchiveBytes = await readFile(finalArchivePath);
  expect(finalArchiveBytes.subarray(0, 4).toString("binary")).toBe("PK\u0003\u0004");
  const finalEntries = readStoredZipEntries(finalArchiveBytes);
  expect([...finalEntries.keys()].sort()).toEqual([
    "hello-world/README.md",
    "hello-world/index.mjs",
    "hello-world/package.json",
  ]);
  expect(finalEntries.get("hello-world/index.mjs")).toBe("console.log('hello world')\n");
  expect(JSON.parse(finalEntries.get("hello-world/package.json") ?? "{}")).toMatchObject({
    scripts: { start: "node index.mjs" },
  });
  const extractedFinalDir = testInfo.outputPath("final-extracted");
  await extractStoredZipEntries(finalArchiveBytes, extractedFinalDir);
  const runResult = spawnSync(process.execPath, ["index.mjs"], {
    cwd: join(extractedFinalDir, "hello-world"),
    encoding: "utf8",
  });
  expect(runResult.status).toBe(0);
  expect(runResult.stdout.trim()).toBe("hello world");

  await page.getByRole("button", { name: /Agent 工作席 1 个 Agent/ }).click();
  await page.getByRole("button", { name: /打开.*实现.*工作调度/ }).click();
  await page.getByRole("button", { name: /生成中间项目文件。/ }).click();
  const drawer = page.getByRole("dialog", { name: "运行过程详情" });
  await expect(drawer).toBeVisible();
  const intermediateDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: /下载 hello-world-source\.zip/ }).click();
  const intermediateArchive = await intermediateDownload;
  expect(intermediateArchive.suggestedFilename()).toBe("hello-world-source.zip");
  const intermediateArchivePath = testInfo.outputPath("hello-world-source.zip");
  await intermediateArchive.saveAs(intermediateArchivePath);
  const intermediateArchiveBytes = await readFile(intermediateArchivePath);
  expect(intermediateArchiveBytes.subarray(0, 4).toString("binary")).toBe("PK\u0003\u0004");
  const intermediateEntries = readStoredZipEntries(intermediateArchiveBytes);
  expect([...intermediateEntries.keys()].sort()).toEqual([
    "source/README.md",
    "source/index.mjs",
    "source/package.json",
  ]);
  expect(intermediateEntries.get("source/README.md")).toContain("node index.mjs");
  await drawer.getByRole("button", { name: "关闭" }).click();
  await expect(page.getByRole("dialog", { name: "运行过程详情" })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "Agent 工作席详情" })).toBeVisible();
});

test("conversation checkpoints jump between user questions", async ({ page }) => {
  await mockCodingRunApi(page, { multiTurnHistory: true });

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  const checkpoints = page.getByRole("navigation", { name: "对话检查点" });
  await expect(checkpoints).toBeVisible();
  await expect(checkpoints.getByRole("button", { name: /^1 生成一个最简单的 hello world 项目/ })).toBeVisible();
  await expect(checkpoints.getByRole("button", { name: /^2 继续优化 UI 交互/ })).toBeVisible();

  await page.evaluate(() => {
    const testWindow = window as unknown as { __lastConversationCheckpointTarget: string };
    testWindow.__lastConversationCheckpointTarget = "";
    HTMLElement.prototype.scrollIntoView = function () {
      testWindow.__lastConversationCheckpointTarget = this.id;
    };
  });

  await checkpoints.getByRole("button", { name: /^2 继续优化 UI 交互/ }).click();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const testWindow = window as unknown as { __lastConversationCheckpointTarget: string };
        return testWindow.__lastConversationCheckpointTarget;
      }),
    )
    .toBe("chat-message-77777777-7777-4777-8777-777777777777-request");
});

test("agent workbench keeps subagent scheduling compact on mobile", async ({ page }) => {
  await mockCodingRunApi(page, { largeWorkbench: true });
  await page.setViewportSize({ width: 390, height: 844 });

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  const workbench = page.getByRole("button", { name: /Agent 工作席 7 个 Agent/ });
  await expect(workbench).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Agent 工作席详情" })).toHaveCount(0);
  await expect(page.getByText("工程师开始创建最小项目。")).toHaveCount(0);
  const collapsedBox = await workbench.boundingBox();
  expect(collapsedBox).not.toBeNull();
  expect(collapsedBox!.x).toBeGreaterThanOrEqual(0);
  expect(collapsedBox!.x + collapsedBox!.width).toBeLessThanOrEqual(390);
  expect(collapsedBox!.height).toBeLessThanOrEqual(48);

  await workbench.click();
  const detail = page.getByRole("dialog", { name: "Agent 工作席详情" });
  await expect(detail).toContainText("实现");
  await expect(detail).toContainText("vibe-engineer");
  await expect(detail).not.toContainText("工程师开始创建最小项目。");
  await detail.getByRole("button", { name: /打开.*实现.*工作调度/ }).click();
  await expect(detail).toContainText("工程师开始创建最小项目。");
  const layout = await page.evaluate(() => ({
    bodyScrollWidth: document.body.scrollWidth,
    docScrollWidth: document.documentElement.scrollWidth,
    innerWidth,
  }));
  expect(layout.bodyScrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
  expect(layout.docScrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
});

test("mobile conversation file preview scrolls long files to the end", async ({ page }) => {
  await mockCodingRunApi(page, { longWorkspacePreview: true });
  await page.setViewportSize({ width: 390, height: 844 });

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  await page.getByRole("button", { name: "预览文件 README.md" }).click();
  const drawer = page.getByRole("dialog", { name: "文件内容预览" });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("README.md");

  const previewBody = drawer.locator(".agent-workbench-file-preview");
  const codeBlock = drawer.locator(".agent-workbench-file-code");
  await expect(codeBlock).toContainText("# Enterprise Portfolio OS");
  await expect
    .poll(() =>
      previewBody.evaluate((element) => {
        const style = window.getComputedStyle(element);
        return style.overflowY;
      }),
    )
    .toBe("auto");
  await expect
    .poll(() =>
      codeBlock.evaluate((element) => ({
        canScroll: element.scrollHeight > element.clientHeight,
        overflowY: window.getComputedStyle(element).overflowY,
      })),
    )
    .toEqual({ canScroll: true, overflowY: "auto" });

  await previewBody.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await codeBlock.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect
    .poll(() =>
      codeBlock.evaluate((element) => ({
        atEnd: element.scrollTop + element.clientHeight >= element.scrollHeight - 1,
        text: element.textContent,
      })),
    )
    .toMatchObject({
      atEnd: true,
      text: expect.stringContaining("README_SCROLL_SENTINEL_末尾内容必须可以通过拖动看到"),
    });
  const layout = await page.evaluate(() => ({
    bodyScrollWidth: document.body.scrollWidth,
    docScrollWidth: document.documentElement.scrollWidth,
    innerWidth,
  }));
  expect(layout.bodyScrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
  expect(layout.docScrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
});

test("opened agent process drawer refreshes when new step events arrive", async ({ page }) => {
  await mockCodingRunApi(page, { liveRefresh: true });

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  await page.getByRole("button", { name: /Agent 工作席/ }).click();
  await page.getByRole("button", { name: /打开.*实现.*工作调度/ }).click();
  await page.getByRole("button", { name: /工程师开始创建最小项目。/ }).click();
  const drawer = page.getByRole("dialog", { name: "运行过程详情" });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("工程师刷新后继续执行验收。", { timeout: 5000 });
  await expect(drawer).toBeVisible();
});

test("process drawer keeps long fields behind summary detail cards", async ({ page }) => {
  const fullOutputSentinel = Array.from(
    { length: 30 },
    (_item, index) => `第 ${index + 1} 段：完整输出字段应该只在二级详情中出现，不能直接铺在抽屉正文里。alpha beta gamma delta epsilon.`,
  ).join("\n");
  await mockCodingRunApi(page, { fullOutputSentinel });

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("发送消息").getByPlaceholder(/输入消息，继续当前对话/).fill("生成一个最简单的 hello world 项目。");
  await page.getByRole("button", { name: "发送" }).click();

  await page.getByRole("button", { name: /Agent 工作席/ }).click();
  await page.getByRole("button", { name: /打开.*实现.*工作调度/ }).click();
  await page.getByRole("button", { name: /生成中间项目文件。/ }).click();
  const drawer = page.getByRole("dialog", { name: "运行过程详情" });
  await expect(drawer).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("hidden");
  await expect(drawer).not.toContainText(fullOutputSentinel);

  await drawer.getByRole("button", { name: /产物：/ }).click();
  const modal = page.getByRole("dialog", { name: "产物详情" });
  await expect(modal).toContainText(fullOutputSentinel);
  const boundedBlock = modal.locator(".bounded-text-block").first();
  await expect(boundedBlock).toBeVisible();
  await expect
    .poll(() =>
      boundedBlock.evaluate((element) => {
        const border = window.getComputedStyle(element, "::after");
        return {
          top: border.borderTopWidth,
          right: border.borderRightWidth,
          bottom: border.borderBottomWidth,
          left: border.borderLeftWidth,
        };
      }),
    )
    .toEqual({ top: "1px", right: "1px", bottom: "1px", left: "1px" });

  await boundedBlock.getByRole("button", { name: /展开/ }).click();
  await expect(boundedBlock).toHaveClass(/is-expanded/);
  await expect
    .poll(() =>
      boundedBlock.evaluate((element) => {
        const border = window.getComputedStyle(element, "::after");
        const rect = element.getBoundingClientRect();
        return {
          borders: [
            border.borderTopWidth,
            border.borderRightWidth,
            border.borderBottomWidth,
            border.borderLeftWidth,
          ],
          insideViewport: rect.left >= 0 && rect.right <= window.innerWidth,
        };
      }),
    )
    .toEqual({ borders: ["1px", "1px", "1px", "1px"], insideViewport: true });

  await page.locator(".process-detail-modal-backdrop").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "产物详情" })).toHaveCount(0);
  await expect(drawer).toBeVisible();
});
