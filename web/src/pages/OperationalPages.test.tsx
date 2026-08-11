import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const runId = "22222222-2222-4222-8222-222222222222";
const secondRunId = "33333333-3333-4333-8333-333333333333";

const runListItem = {
  id: runId,
  status: "running",
  mode: "dispatch",
  queue_wait_ms: 120,
  capacity_wait_ms: 40,
  cost_usd: "0.0132",
};

const runDetail = {
  ...runListItem,
  request: "给我做一个短视频脚本方案。",
  events: [
    {
      sequence: 1,
      kind: "queued",
      message: "Run accepted and queued.",
      created_at: "2026-08-07T00:00:00Z",
    },
    {
      sequence: 2,
      kind: "artifact.created",
      message: "artifact.created",
      created_at: "2026-08-07T00:00:01Z",
      actor: "copywriter",
      participants: [],
      tool_name: "artifact_writer",
      step_id: "write-script",
      action: null,
      decision: null,
      payload: {
        summary: "写入短视频脚本",
        api_key: "[redacted]",
      },
    },
    {
      sequence: 3,
      kind: "discussion.completed",
      message: "导演、文案和剪辑师完成讨论，主 Agent 采用可拍摄性最高的方案。",
      created_at: "2026-08-07T00:00:02Z",
      actor: "director",
      participants: ["director", "copywriter", "editor"],
      tool_name: null,
      step_id: null,
      action: null,
      decision: "adopt",
      payload: {
        result: "采用可拍摄性最高的方案",
      },
    },
  ],
  artifacts: [
    {
      id: "artifact-1",
      kind: "markdown",
      title: "短视频脚本",
      text: "这是最终回复正文：导演、文案和剪辑师已经汇总出一个短视频脚本方案。",
    },
  ],
  explicit_details: {
    workflow_id: "short-video-dispatch",
    workflow_adjustment_policy: "ask_before_apply",
    selected_agent_ids: "director,copywriter,editor",
    routing_reason: "workflow selected explicitly",
    conversation_id: "conv-previous",
  },
};

const settings = {
  default_mode: "auto",
  default_workflow_id: null,
  default_agent_ids: [],
  log_level: "warning",
  hermes_enabled: true,
  safe_tools_enabled: true,
  require_approval_for_tools: true,
  allow_main_agent_override: true,
  allow_temporary_agents: true,
  temporary_agent_policy: "全局策略：缺少专业能力时先询问用户，再临时加入子 Agent。",
  channel_entry: "web",
  attachment_retention_days: 7,
  attachment_max_mb: 25,
};

const secondRunListItem = {
  ...runListItem,
  id: secondRunId,
  status: "completed",
  mode: "direct",
};

const hermesInsight = {
  id: "hermes-1",
  outcome: "success",
  lesson: "Use group chat when debate review is required.",
  summary: "Learned success pattern: Use group chat when debate review is required. Tags: debate, review. Weight: 5.",
  run_id: runId,
  conversation_id: "conv-architecture-1",
  confirmed_at: null,
  tags: ["debate", "review"],
  weight: 5,
  created_at: "2026-08-07T00:04:00Z",
};

const secondHermesInsight = {
  ...hermesInsight,
  id: "hermes-2",
  outcome: "failure",
  lesson: "Ask for confirmation before changing the workflow role pool.",
  summary: "Learned failure pattern: Ask for confirmation before changing the workflow role pool. Tags: workflow, approval. Weight: 4.",
  conversation_id: "conv-workflow-2",
  confirmed_at: null,
  tags: ["workflow", "approval"],
  weight: 4,
  created_at: "2026-08-07T00:06:00Z",
};

const agents = [
  {
    id: "director",
    name: "导演",
    enabled: true,
    role: "导演",
    prompt: "负责选题、分镜和最终把关。",
    model: "main",
    skills: [],
  },
  {
    id: "copywriter",
    name: "文案生成",
    enabled: true,
    role: "文案生成",
    prompt: "负责脚本与口播。",
    model: "main",
    skills: [],
  },
  {
    id: "editor",
    name: "剪辑师",
    enabled: true,
    role: "剪辑师",
    prompt: "负责镜头节奏和剪辑建议。",
    model: "main",
    skills: [],
  },
];

const workflows = [
  {
    id: "short-video-dispatch",
    name: "短视频派单",
    enabled: true,
    mode: "dispatch",
    allow_main_agent_override: false,
    allow_temporary_agents: false,
    temporary_agent_policy: "旧工作流内策略应被全局设置取代。",
    task_type: "短视频内容生产",
    role_selection_policy: "导演、文案、剪辑师参与；不默认派给程序员。",
    agent_ids: ["director", "copywriter", "editor"],
    objective: "产出短视频脚本方案",
    steps: ["拆解需求", "角色分工", "汇总产物"],
    deliverables: ["脚本", "分镜", "剪辑建议"],
    decision_policy: "主 Agent 汇总裁决",
  },
];

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("operational management pages", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let visibleRunListItem = runListItem;
  let visibleRunDetail = runDetail;
  let visibleRunListItems = [runListItem];
  let deletedRunIds = new Set<string>();

  beforeEach(() => {
    requests.length = 0;
    visibleRunListItem = runListItem;
    visibleRunDetail = runDetail;
    visibleRunListItems = [visibleRunListItem];
    deletedRunIds = new Set<string>();
    vi.stubGlobal("confirm", vi.fn(() => true));
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (init?.body && typeof init.body === "string") {
          requests.push({ path, method, body: JSON.parse(init.body) });
        } else {
          requests.push({ path, method, body: null });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse(visibleRunListItems.filter((item) => !deletedRunIds.has(item.id)));
        }
        if (path === `/api/v1/admin/runs/${runId}` && method === "DELETE") {
          deletedRunIds.add(runId);
          return jsonResponse({ id: runId, deleted: true });
        }
        if (path === `/api/v1/admin/runs/${secondRunId}` && method === "DELETE") {
          deletedRunIds.add(secondRunId);
          return jsonResponse({ id: secondRunId, deleted: true });
        }
        if (path === "/api/v1/admin/runs/bulk-delete" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          const ids = Array.isArray(body.ids) ? body.ids : [];
          ids.forEach((id: unknown) => deletedRunIds.add(String(id)));
          return jsonResponse({
            deleted: ids.map((id: unknown) => ({ id, deleted: true })),
            failed: [],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          if (deletedRunIds.has(runId)) {
            return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
          }
          return jsonResponse(visibleRunDetail);
        }
        if (path === "/api/v1/admin/conversations/conv-previous") {
          return jsonResponse({ conversation_id: "conv-previous", runs: [visibleRunDetail] });
        }
        if (path === `/api/v1/admin/conversations/${runDetail.explicit_details.conversation_id}`) {
          return jsonResponse({ conversation_id: runDetail.explicit_details.conversation_id, runs: [visibleRunDetail] });
        }
        if (path === `/api/v1/admin/runs/${runId}/pause`) {
          return jsonResponse({ ...runDetail, status: "paused" });
        }
        if (path === "/api/v1/runs" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          const message = String(body.message ?? "");
          if (message.includes("二次确认")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_user_mode",
              mode: null,
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "routing_requires_user_choice",
            });
          }
          if (message.includes("网页") || message.toLowerCase().includes("web page")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_approval",
              mode: "dispatch",
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "temporary_agent_requires_user_approval",
              temporary_agent_proposal: {
                id: "temp-web-engineer",
                name: "Temporary Web Engineer",
                role: "Web Engineer",
                prompt: "把方案落成网页并说明验证步骤。",
                reason: "当前角色池缺少 software_engineering 能力。",
                missing_capability: "software_engineering",
                suggested_skills: ["frontend"],
                permanentizable: true,
              },
            });
          }
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 1,
            clarification_reason: null,
            conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
            reference_conversation_id:
              typeof body.reference_conversation_id === "string" ? body.reference_conversation_id : null,
          });
        }
        if (path === `/api/v1/runs/${runId}/choose-mode` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "discuss",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === `/api/v1/runs/${runId}/approve-temporary-agent` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === `/api/v1/runs/${runId}/revise-temporary-agent` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === "/api/v1/admin/settings") {
          return jsonResponse(settings);
        }
        if (path === "/api/v1/admin/agents" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          return jsonResponse(body);
        }
        if (path === "/api/v1/admin/agents") {
          return jsonResponse(agents);
        }
        if (path === "/api/v1/admin/skills/upload" && method === "POST") {
          return jsonResponse({
            id: "skill-uploaded-from-chat",
            name: "uploaded_skill",
            version: "1.0.0",
            status: "scanned",
            requested_permissions: ["tool:filesystem.read"],
            scan_diff: ["content sha256: abc123", "entry point: main.py"],
          });
        }
        if (path === "/api/v1/admin/skills/skill-uploaded-from-chat/approve" && method === "POST") {
          return jsonResponse({
            id: "skill-uploaded-from-chat",
            name: "uploaded_skill",
            version: "1.0.0",
            status: "enabled",
            requested_permissions: ["tool:filesystem.read"],
            scan_diff: ["content sha256: abc123", "entry point: main.py", "approved by production admin"],
          });
        }
        if (path === "/api/v1/admin/skills") {
          return jsonResponse([]);
        }
        if (path === "/api/v1/runs/attachments/upload" && method === "POST") {
          return jsonResponse({
            id: "att_0123456789abcdef0123456789abcdef",
            filename: "screen.png",
            kind: "image",
            content_type: "image/png",
            size_bytes: 128,
            sha256: "a".repeat(64),
            expires_at: "2026-08-17T00:00:00Z",
          });
        }
        if (path === "/api/v1/admin/workflows") {
          return jsonResponse(workflows);
        }
        if (path === "/api/v1/admin/hermes") {
          return jsonResponse([hermesInsight, secondHermesInsight]);
        }
        if (path === "/api/v1/admin/hermes/hermes-1") {
          return jsonResponse(hermesInsight);
        }
        if (path === "/api/v1/admin/hermes/hermes-1/confirm" && method === "POST") {
          return jsonResponse({ ...hermesInsight, confirmed_at: "2026-08-07T00:05:00Z" });
        }
        if (path === "/api/v1/admin/hermes/hermes-2") {
          return jsonResponse(secondHermesInsight);
        }
        if (path === "/api/v1/admin/hermes/hermes-2/confirm" && method === "POST") {
          return jsonResponse({ ...secondHermesInsight, confirmed_at: "2026-08-07T00:07:00Z" });
        }
        if (path === "/api/v1/admin/hermes/bulk-confirm" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          const ids = Array.isArray(body.ids) ? body.ids : [];
          return jsonResponse({
            confirmed: ids.map((id: unknown) =>
              id === "hermes-2"
                ? { ...secondHermesInsight, confirmed_at: "2026-08-07T00:07:00Z" }
                : { ...hermesInsight, confirmed_at: "2026-08-07T00:05:00Z" },
            ),
            failed: [],
          });
        }
        if (path === "/api/v1/admin/mcp") {
          return jsonResponse([{ id: "filesystem", name: "Filesystem MCP", health: "healthy", allowed_tools: ["read_file"] }]);
        }
        if (path === "/api/v1/admin/memory") {
          return jsonResponse([{ id: "project-policy", scope: "tenant", value: "Only non-dangerous operations may run without approval." }]);
        }
        if (path.startsWith("/api/v1/admin/logs")) {
          const logs = [
            {
              id: "model-error-1",
              category: "model_error",
              level: "error",
              title: "模型配置与调用错误",
              message: "provider returned status=401",
              source: "models.create",
              details: { provider: "deepseek", status_code: "401" },
              created_at: "2026-08-07T00:01:00Z",
            },
            {
              id: "mode-error-1",
              category: "mode_error",
              level: "error",
              title: "模式运行错误",
              message: "dispatch runtime failed",
              source: "runs.execute",
              details: { mode: "dispatch", run_id: runId },
              created_at: "2026-08-07T00:02:00Z",
            },
            {
              id: "audit-1",
              category: "audit",
              level: "info",
              title: "审计日志",
              message: "config.publish",
              source: "admin.audit",
              details: { resource: "configuration", actor: "system" },
              created_at: "2026-08-07T00:03:00Z",
            },
          ];
          const url = new URL(path, "https://agent-hub.test");
          const category = url.searchParams.get("category");
          return jsonResponse(category ? logs.filter((item) => item.category === category) : logs);
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    cleanup();
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  async function openRunConfig(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: /打开本次运行配置|open/i }));
  }

  it("shows run operations and supports pause control on the detail page", async () => {
    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    expect(screen.getByText("running")).not.toBeNull();
    expect(screen.getByText("markdown：短视频脚本")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "暂停" }));

    await waitFor(() => expect(screen.getByText("paused")).not.toBeNull());
  });

  it("keeps run detail access inside the center chat stream and sends selected workflow roles", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    expect(screen.getByText(/工作流配置和工作流使用是分开的/)).not.toBeNull();

    await openRunConfig(user);
    await user.selectOptions(screen.getByLabelText("使用工作流"), "short-video-dispatch");
    expect(screen.getByText(/全局临场策略已开启/)).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入任务/), "给我做一个短视频脚本方案。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const link = await screen.findByRole("link", { name: "查看运行详情" });
    expect(link.getAttribute("href")).toBe(`/runs/${runId}`);
    expect(link.closest(".chat-stream")).not.toBeNull();
    expect(screen.getByText(/本次任务实际使用/)).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "给我做一个短视频脚本方案。",
        mode: "dispatch",
        workflow_id: "short-video-dispatch",
        allow_workflow_adjustment: true,
        agent_ids: ["director", "copywriter", "editor"],
      },
    });
  });

  it("keeps live adjustment and temporary-agent switches out of workflow configuration", async () => {
    render(<TestApp initialPath="/workflows" />);

    expect(await screen.findByRole("heading", { name: "工作流配置" })).not.toBeNull();
    expect(screen.queryByText("允许主 Agent 提出工作流临场调整（执行前必须向用户核对）")).toBeNull();
    expect(screen.queryByText("允许主 Agent 在角色能力不足时申请临时子 Agent")).toBeNull();
    expect(screen.queryByLabelText("临时 Agent 补位规则")).toBeNull();
    expect(screen.getAllByText(/主 Agent 临场调整和临时子 Agent 是全局调度策略/).length).toBeGreaterThan(0);
  });

  it("uses a single direct answerer when direct mode is selected", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await openRunConfig(user);
    await user.selectOptions(screen.getByLabelText("模式"), "direct");
    await user.selectOptions(screen.getByLabelText("直连回答者"), "copywriter");
    await user.type(screen.getByPlaceholderText(/输入任务/), "帮我写一段口播。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "帮我写一段口播。",
        mode: "direct",
        allow_workflow_adjustment: false,
        agent_ids: ["copywriter"],
      },
    });
  });

  it("renders text artifacts as assistant chat replies instead of artifact-only cards", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /进入会话 22222222/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/这是最终回复正文/)).not.toBeNull();
    expect(within(stream).queryByText("产物：短视频脚本")).toBeNull();
  });

  it("opens a historical conversation and continues inside the same conversation id", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /进入会话 22222222/ }));
    await screen.findByText(/当前会话：conv-previous/);
    await user.type(screen.getByPlaceholderText(/输入消息/), "继续优化这个脚本。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.slice().reverse().find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "继续优化这个脚本。",
        conversation_id: "conv-previous",
      },
    });
  });

  it("starts a continuation branch when context is too long", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /进入会话 22222222/ }));
    await screen.findByText(/当前会话：conv-previous/);
    await user.click(screen.getByRole("button", { name: "按照原思路开启新对话" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "接着前面的方向继续。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const request = requests.slice().reverse().find((item) => item.path === "/api/v1/runs");
    expect(request).toMatchObject({
      method: "POST",
      body: {
        message: "接着前面的方向继续。",
        reference_conversation_id: "conv-previous",
      },
    });
    expect((request?.body as { conversation_id?: string }).conversation_id).not.toBe("conv-previous");
  });

  it("keeps partial assistant output visible when a run fails after producing artifacts", async () => {
    visibleRunListItem = { ...runListItem, status: "failed", mode: "hybrid" };
    visibleRunListItems = [visibleRunListItem];
    visibleRunDetail = {
      ...runDetail,
      ...visibleRunListItem,
      events: [
        ...runDetail.events,
        {
          sequence: 4,
          kind: "runtime.failed",
          message: "hybrid discuss failed: model gateway failed: model transport failed",
          created_at: "2026-08-07T00:00:03Z",
        },
      ],
    };

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /进入会话 22222222/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/这是最终回复正文/)).not.toBeNull();
    expect(within(stream).getByText("运行中断")).not.toBeNull();
    expect(within(stream).getByText(/中断前输出已保留/)).not.toBeNull();
    expect(within(stream).getByText(/model transport failed/)).not.toBeNull();
  });

  it("collapses run process events behind a compact execution summary", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /进入会话 22222222/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/这是最终回复正文/)).not.toBeNull();
    expect(within(stream).queryByText("Run accepted and queued.")).toBeNull();

    expect(within(stream).queryByText("正在实时刷新运行状态")).toBeNull();
    await user.click(within(stream).getByRole("button", { name: /已运行 4 个动作/ }));
    expect(within(stream).getByText("任务已进入队列，等待 Worker 调度执行。")).not.toBeNull();
  });

  it("shows localized process summaries with participating roles instead of raw event codes", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /进入会话 22222222/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    await user.click(within(stream).getByRole("button", { name: /已运行 4 个动作/ }));

    expect(within(stream).getByText("参与角色：导演、文案生成、剪辑师")).not.toBeNull();
    expect(within(stream).getByText(/文案生成 生成了结果/)).not.toBeNull();
    expect(within(stream).getByText(/多角色完成讨论/)).not.toBeNull();
    expect(within(stream).getAllByText(/采用可拍摄性最高的方案/).length).toBeGreaterThan(0);
    expect(within(stream).getAllByText("执行者").length).toBeGreaterThan(0);
    expect(within(stream).getByText("文案生成")).not.toBeNull();
    expect(within(stream).getByText("工具")).not.toBeNull();
    expect(within(stream).getByText("artifact_writer")).not.toBeNull();
    expect(within(stream).getByText("参与者")).not.toBeNull();
    expect(within(stream).getByText("导演、文案生成、剪辑师")).not.toBeNull();
    expect(within(stream).getByText("[redacted]")).not.toBeNull();
    expect(within(stream).queryByText("artifact.created")).toBeNull();
  });

  it("selects the chat mode from the compact entry panel before sending", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "进入讨论模式" }));
    await user.type(screen.getByPlaceholderText(/输入任务/), "请让多个角色评审这个方案。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "请让多个角色评审这个方案。",
        mode: "discuss",
      },
    });
  });

  it("continues with the manually selected mode when the backend asks for mode clarification", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "进入讨论模式" }));
    await user.type(screen.getByPlaceholderText(/输入任务/), "这个任务不应该二次确认。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/choose-mode`)).toMatchObject({
        method: "POST",
        body: {
          mode: "discuss",
          decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
          version: 1,
        },
      }),
    );
    expect(screen.queryByRole("dialog", { name: "运行模式确认" })).toBeNull();
  });

  it("uploads a skill zip from the chat composer and requires explicit approval", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    const file = new File(["PK\x03\x04"], "uploaded-skill.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("上传文件或 Skill ZIP"), file);

    expect(await screen.findByText("Skill 包已扫描，等待确认")).not.toBeNull();
    expect(screen.getByText("uploaded_skill")).not.toBeNull();
    expect(screen.getByText("tool:filesystem.read")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/skills/upload")).toMatchObject({
      method: "POST",
    });

    await user.click(screen.getByRole("button", { name: "确认安装 Skill" }));
    expect(await screen.findByText("Skill 已安装并启用")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/skills/skill-uploaded-from-chat/approve")).toMatchObject({
      method: "POST",
    });
  });

  it("uploads an image attachment from chat and submits its attachment id with the run", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    const file = new File(["image-bytes"], "screen.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("上传文件或 Skill ZIP"), file);
    expect(await screen.findByText("图片附件")).not.toBeNull();
    expect(screen.getByText("screen.png")).not.toBeNull();

    await user.type(screen.getByPlaceholderText(/输入任务/), "请根据图片说明问题");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "请根据图片说明问题",
        attachment_ids: ["att_0123456789abcdef0123456789abcdef"],
      },
    });
  });

  it("deletes a finished conversation from the conversation list", async () => {
    const user = userEvent.setup();
    visibleRunListItem = { ...runListItem, status: "cancelled" };
    visibleRunListItems = [visibleRunListItem];
    visibleRunDetail = { ...runDetail, status: "cancelled" };
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("button", { name: /Delete conversation 22222222/i })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /Delete conversation 22222222/i }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/admin/runs/${runId}` && request.method === "DELETE"))
        .toBeTruthy(),
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: /Delete conversation 22222222/i })).toBeNull());
  });

  it("bulk selects finished conversations and deletes them through one batch API call", async () => {
    const user = userEvent.setup();
    visibleRunListItems = [
      { ...runListItem, status: "cancelled" },
      secondRunListItem,
    ];
    visibleRunListItem = visibleRunListItems[0];
    visibleRunDetail = { ...runDetail, status: "cancelled" };
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("checkbox", { name: "Select all deletable conversations" })).not.toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Select all deletable conversations" }));
    await user.click(screen.getByRole("button", { name: "批量删除已选会话" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/runs/bulk-delete")).toMatchObject({
        method: "POST",
        body: { ids: [runId, secondRunId] },
      }),
    );
  });

  it("shows temporary agent approval above the composer and lets the user revise it", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/" />);

    await waitFor(() => expect(view.container.querySelector(".chat-composer")).not.toBeNull());
    await openRunConfig(user);
    await user.selectOptions(screen.getAllByRole("combobox")[1], "short-video-dispatch");
    const composer = view.container.querySelector(".chat-composer") as HTMLFormElement;
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "make this into a web page");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    const dialog = await screen.findByRole("dialog");
    expect(dialog.closest("form")?.className).toContain("chat-composer");
    expect(within(dialog).getByText("Temporary Web Engineer")).not.toBeNull();
    await user.type(within(dialog).getByLabelText(/意见|feedback|opinion/i), "do not add an engineer yet");
    await user.click(within(dialog).getByRole("button", { name: /重规|revise|意见/i }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/revise-temporary-agent`)).toMatchObject({
        method: "POST",
        body: {
          decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
          version: 1,
          feedback: "do not add an engineer yet",
        },
      }),
    );
  });

  it("accepts a temporary agent and can persist it as a normal agent", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/" />);

    await waitFor(() => expect(view.container.querySelector(".chat-composer")).not.toBeNull());
    await openRunConfig(user);
    await user.selectOptions(screen.getAllByRole("combobox")[1], "short-video-dispatch");
    const composer = view.container.querySelector(".chat-composer") as HTMLFormElement;
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "make this into a web page");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /接受|accept|加入/i }));
    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/approve-temporary-agent`)).toBeTruthy(),
    );
    await user.click(within(dialog).getByRole("button", { name: /保存|permanent/i }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/agents" && request.method === "POST")).toMatchObject({
        body: {
          id: "temp-web-engineer",
          name: "Temporary Web Engineer",
          role: "Web Engineer",
          prompt: "把方案落成网页并说明验证步骤。",
          model: null,
          skills: ["frontend"],
        },
      }),
    );
  });

  it("keeps a clear mobile hierarchy for chat sessions, content, and run settings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    expect(screen.getByRole("navigation", { name: "手机版会话导航" })).not.toBeNull();
    expect(screen.getByRole("region", { name: "主对话内容" })).not.toBeNull();
    expect(screen.getByRole("button", { name: /打开本次运行配置/ })).not.toBeNull();
    await openRunConfig(user);
    expect(screen.getByRole("group", { name: "本次运行设置" })).not.toBeNull();
    expect(screen.getByText("本次运行设置")).not.toBeNull();
  });


  it("loads a referenced conversation by id from the chat page", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await openRunConfig(user);
    await user.type(screen.getByLabelText("参考会话 ID"), "conv-previous");
    await user.click(screen.getByRole("button", { name: "读取参考会话" }));

    expect(await screen.findByText("conv-previous")).not.toBeNull();
    expect(screen.getByText(/已读取 1 条运行/)).not.toBeNull();
    expect(screen.getByText(runDetail.request)).not.toBeNull();
  });


  it("shows MCP, memory, and modular log pages", async () => {
    render(<TestApp initialPath="/mcp" />);
    expect(await screen.findByText("Filesystem MCP")).not.toBeNull();
    expect(screen.getByText("healthy")).not.toBeNull();

    cleanup();
    render(<TestApp initialPath="/memory" />);
    expect(await screen.findByText("project-policy")).not.toBeNull();
    expect(screen.getByText("tenant")).not.toBeNull();

    cleanup();
    const logsView = render(<TestApp initialPath="/logs" />);
    expect(await screen.findByRole("heading", { name: "日志" })).not.toBeNull();
    const logsMain = within(logsView.container.querySelector("main") as HTMLElement);
    expect(logsMain.getByRole("link", { name: /审计日志/ })).not.toBeNull();
    expect(logsMain.getByRole("link", { name: /模型配置与调用错误/ })).not.toBeNull();
    expect(logsMain.getByRole("link", { name: /模式运行错误/ })).not.toBeNull();

    cleanup();
    render(<TestApp initialPath="/logs/model" />);
    expect(await screen.findByRole("heading", { name: "模型配置与调用错误", level: 2 })).not.toBeNull();
    expect(await screen.findByText("provider returned status=401")).not.toBeNull();
    expect(screen.queryByText("dispatch runtime failed")).toBeNull();
  });

  it("shows Hermes learning by time and conversation id with detail confirmation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("table", { name: /Hermes/ })).not.toBeNull();
    expect(screen.queryByText("请求 Hermes 推荐")).toBeNull();
    expect(screen.queryByText("推荐结果")).toBeNull();
    expect(screen.getByText("conv-architecture-1")).not.toBeNull();
    expect(screen.getByText("2026-08-07T00:04:00Z")).not.toBeNull();
    await user.click(screen.getByRole("link", { name: /conv-architecture-1/ }));

    expect(await screen.findByText(hermesInsight.summary)).not.toBeNull();
    expect(screen.getByText(hermesInsight.lesson)).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /确认/ }));

    await waitFor(() => expect(screen.getByText("2026-08-07T00:05:00Z")).not.toBeNull());
  });

  it("bulk selects Hermes learning records and confirms them through one batch API call", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("checkbox", { name: "Select all Hermes learning records" })).not.toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Select all Hermes learning records" }));
    await user.click(screen.getByRole("button", { name: "批量确认已选学习" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/bulk-confirm")).toMatchObject({
        method: "POST",
        body: { ids: ["hermes-2", "hermes-1"] },
      }),
    );
  });

  it("shows detailed API errors on run list loading failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse(
            { error: { code: "service_unavailable", message: "database is not ready" } },
            { status: 503, headers: { "X-Error-ID": "err_123" } },
          );
        }
        if (path === "/api/v1/admin/settings") return jsonResponse(settings);
        if (path === "/api/v1/admin/agents") return jsonResponse(agents);
        if (path === "/api/v1/admin/workflows") return jsonResponse(workflows);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath="/" />);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "任务列表加载失败: database is not ready (service_unavailable, HTTP 503, error err_123)",
    );
  });
});
