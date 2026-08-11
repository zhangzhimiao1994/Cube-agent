import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const runId = "22222222-2222-4222-8222-222222222222";

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
  channel_entry: "web",
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
    allow_main_agent_override: true,
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
  let deletedRunIds = new Set<string>();

  beforeEach(() => {
    requests.length = 0;
    visibleRunListItem = runListItem;
    visibleRunDetail = runDetail;
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
          return jsonResponse(deletedRunIds.has(runId) ? [] : [visibleRunListItem]);
        }
        if (path === `/api/v1/admin/runs/${runId}` && method === "DELETE") {
          deletedRunIds.add(runId);
          return jsonResponse({ id: runId, deleted: true });
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          if (deletedRunIds.has(runId)) {
            return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
          }
          return jsonResponse(visibleRunDetail);
        }
        if (path === "/api/v1/admin/conversations/conv-previous") {
          return jsonResponse({ conversation_id: "conv-previous", runs: [runDetail] });
        }
        if (path === `/api/v1/admin/runs/${runId}/pause`) {
          return jsonResponse({ ...runDetail, status: "paused" });
        }
        if (path === "/api/v1/runs" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          const message = String(body.message ?? "");
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
        if (path === "/api/v1/admin/workflows") {
          return jsonResponse(workflows);
        }
        if (path === "/api/v1/admin/hermes") {
          return jsonResponse([hermesInsight]);
        }
        if (path === "/api/v1/admin/hermes/hermes-1") {
          return jsonResponse(hermesInsight);
        }
        if (path === "/api/v1/admin/hermes/hermes-1/confirm" && method === "POST") {
          return jsonResponse({ ...hermesInsight, confirmed_at: "2026-08-07T00:05:00Z" });
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
    expect(screen.getByText(/执行前必须向你核对/)).not.toBeNull();
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
    await userEvent.click(screen.getByRole("button", { name: /派单式/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/这是最终回复正文/)).not.toBeNull();
    expect(within(stream).queryByText("产物：短视频脚本")).toBeNull();
  });

  it("collapses run process events behind a compact execution summary", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话任务" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /派单式/ }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/这是最终回复正文/)).not.toBeNull();
    expect(within(stream).queryByText("Run accepted and queued.")).toBeNull();

    expect(within(stream).queryByText("正在实时刷新运行状态")).toBeNull();
    await user.click(within(stream).getByRole("button", { name: /执行 2 条步骤/ }));
    expect(within(stream).getByText("Run accepted and queued.")).not.toBeNull();
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

  it("deletes a finished conversation from the conversation list", async () => {
    const user = userEvent.setup();
    visibleRunListItem = { ...runListItem, status: "cancelled" };
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
