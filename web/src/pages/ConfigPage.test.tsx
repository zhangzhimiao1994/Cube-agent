import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

const settings = {
  default_mode: "auto",
  default_workflow_id: null,
  default_agent_ids: [],
  log_level: "warning",
  hermes_enabled: true,
  safe_tools_enabled: true,
  require_approval_for_tools: true,
  allow_main_agent_override: false,
  allow_temporary_agents: false,
  vibe_coding_enabled: false,
  multimedia_generation_enabled: false,
  openclaw_enabled: false,
  openclaw_mode: "ask",
  openclaw_allowed_commands: [],
  openclaw_remote_adapters: [],
  temporary_agent_policy:
    "主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
  channel_entry: "web",
  attachment_retention_days: 7,
  attachment_max_mb: 25,
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ConfigPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let openClawSessions: Array<Record<string, unknown>> = [];
  let lastOpenClawOperationBody: Record<string, unknown> = {};

  beforeEach(() => {
    requests.length = 0;
    openClawSessions = [];
    lastOpenClawOperationBody = {};
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/settings") {
          if (method === "PUT") return jsonResponse(JSON.parse(String(init?.body)));
          return jsonResponse(settings);
        }
        if (path === "/api/v1/admin/openclaw/operations" && method === "POST") {
          const body = JSON.parse(String(init?.body));
          lastOpenClawOperationBody = body;
          return jsonResponse(
            {
              id: "openclaw_ui_test",
              status: "waiting_user_approval",
              approval_id: "openclaw_ui_test_approval",
              requires_user_approval: true,
              platform: body.platform,
              kind: body.kind,
              operation: lastOpenClawOperationBody,
              approval_summary: "OpenClaw linux server_command on agent-hub-server",
              requested_by: principal.user_id,
              created_at: "2026-08-13T00:00:00Z",
              resolved_by: null,
              resolved_at: null,
              execution: null,
            },
            { status: 202 },
          );
        }
        if (path === "/api/v1/admin/openclaw/operations/openclaw_ui_test" && method === "PATCH") {
          return jsonResponse({
            id: "openclaw_ui_test",
            status: "approved",
            approval_id: "openclaw_ui_test_approval",
            requires_user_approval: false,
            platform: "linux",
            kind: "server_command",
            operation: lastOpenClawOperationBody,
            approval_summary: "OpenClaw linux server_command on agent-hub-server",
            requested_by: principal.user_id,
            created_at: "2026-08-13T00:00:00Z",
            resolved_by: principal.user_id,
            resolved_at: "2026-08-13T00:01:00Z",
            execution: null,
          });
        }
        if (path === "/api/v1/admin/openclaw/operations/openclaw_ui_test/execute" && method === "POST") {
          return jsonResponse({
            operation: {
              id: "openclaw_ui_test",
              status: "executed",
              approval_id: "openclaw_ui_test_approval",
              requires_user_approval: false,
              platform: "linux",
              kind: "server_command",
              operation: lastOpenClawOperationBody,
              approval_summary: "OpenClaw linux server_command on agent-hub-server",
              requested_by: principal.user_id,
              created_at: "2026-08-13T00:00:00Z",
              resolved_by: principal.user_id,
              resolved_at: "2026-08-13T00:01:00Z",
              execution: {
                exit_code: 0,
                stdout: "ui-openclaw-ok\n",
                stderr: "",
                truncated: false,
                executed_by: principal.user_id,
                executed_at: "2026-08-13T00:02:00Z",
              },
            },
            exit_code: 0,
            stdout: "ui-openclaw-ok\n",
            stderr: "",
            truncated: false,
          });
        }
        if (path === "/api/v1/admin/openclaw/adapters") {
          return jsonResponse([
            {
              platform: "linux",
              kind: "server_command",
              target_type: "server",
              status: "available",
              execution_host: "agent-hub-server",
              requires_user_approval: true,
              supports_read_only: false,
              description: "Runs exact allowlisted argv commands on the 魔方agent Linux server after approval.",
            },
            {
              platform: "windows",
              kind: "server_command",
              target_type: "computer",
              status: "adapter_unavailable",
              execution_host: "remote-windows-host",
              requires_user_approval: true,
              supports_read_only: false,
              description: "Requires a connected Windows OpenClaw adapter before execution.",
            },
          ]);
        }
        if (path === "/api/v1/admin/openclaw/sessions" && method === "GET") {
          return jsonResponse(openClawSessions);
        }
        if (path === "/api/v1/admin/openclaw/sessions" && method === "POST") {
          const body = JSON.parse(String(init?.body));
          const session = {
            id: "openclaw_session_ui_test",
            status: "active",
            adapter_status: "available",
            mode: "ask",
            platform: body.platform,
            target_type: body.target_type,
            target: body.target,
            purpose: body.purpose,
            execution_host: "agent-hub-server",
            requested_by: principal.user_id,
            created_at: "2026-08-13T00:00:00Z",
            updated_at: "2026-08-13T00:00:00Z",
            stopped_at: null,
            operation_ids: [],
          };
          openClawSessions = [session];
          return jsonResponse(session, { status: 201 });
        }
        if (path === "/api/v1/admin/openclaw/sessions/openclaw_session_ui_test" && method === "PATCH") {
          const body = JSON.parse(String(init?.body));
          const status = body.action === "pause" ? "paused" : body.action === "stop" ? "stopped" : "active";
          openClawSessions = openClawSessions.map((session) =>
            session.id === "openclaw_session_ui_test"
              ? { ...session, status, updated_at: "2026-08-13T00:01:00Z" }
              : session,
          );
          return jsonResponse(openClawSessions[0]);
        }
        if (path === "/api/v1/admin/agents") {
          return jsonResponse([
            {
              id: "director",
              name: "导演",
              enabled: true,
              role: "导演",
              prompt: "负责选题、分镜和最终把关。",
              model: "main",
              skills: [],
            },
          ]);
        }
        if (path === "/api/v1/admin/workflows") {
          return jsonResponse([
            {
              id: "short-video-dispatch",
              name: "短视频派单",
              enabled: true,
              mode: "dispatch",
              task_type: "短视频",
              role_selection_policy: "按任务类型选择导演、文案、剪辑师。",
              agent_ids: ["director"],
              objective: "生产短视频方案",
              steps: ["拆解需求", "分派角色", "汇总产物"],
              deliverables: ["脚本", "分镜"],
              decision_policy: "主 Agent 汇总裁决",
            },
          ]);
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse([]);
        }
        if (path === "/api/v1/config/current") {
          return jsonResponse({
            id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            version: 3,
            status: "published",
            document: {
              models: {
                main: {
                  deployments: [{ provider: "deepseek", model: "deepseek-v4-flash" }],
                },
              },
              agents: [],
            },
            created_by: principal.user_id,
            created_at: "2026-08-08T00:00:00Z",
          });
        }
        if (path === "/api/v1/config/drafts" && method === "POST") {
          return jsonResponse(
            {
              id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              version: 4,
              status: "draft",
              document: JSON.parse(String(init?.body)),
              created_by: principal.user_id,
              created_at: "2026-08-08T00:01:00Z",
            },
            { status: 201 },
          );
        }
        if (path === "/api/v1/config/drafts/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/publish") {
          return jsonResponse({
            id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            version: 4,
            status: "published",
            document: requests.at(-1)?.body,
            created_by: principal.user_id,
            created_at: "2026-08-08T00:01:00Z",
            notification_status: "sent",
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

  it("loads system settings and saves production defaults through dedicated controls", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/config" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByText("版本 3")).not.toBeNull();
    expect(view.container.querySelectorAll(".settings-shortcut-card")).toHaveLength(5);

    await user.selectOptions(screen.getByLabelText("默认运行模式"), "dispatch");
    await user.selectOptions(screen.getByLabelText("默认工作流"), "short-video-dispatch");
    await user.click(screen.getByLabelText(/导演/));
    await user.click(screen.getByLabelText("允许主 Agent 提出临场调整，执行前必须向用户核对"));
    await user.click(screen.getByLabelText("允许主 Agent 在能力不足时申请临时子 Agent"));
    await user.click(screen.getByTestId("vibe-coding-toggle"));
    await user.click(screen.getByTestId("multimedia-generation-toggle"));
    await user.click(screen.getByTestId("openclaw-toggle"));
    await user.selectOptions(screen.getByLabelText("OpenClaw 权限模式"), "read_only");
    fireEvent.change(screen.getByLabelText(/临时 Agent 补位规则/), {
      target: { value: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。" },
    });
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));

    expect((await screen.findByRole("status")).textContent).toContain("系统设置已保存");
    expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
      method: "PUT",
      body: {
        ...settings,
        default_mode: "dispatch",
        default_workflow_id: "short-video-dispatch",
        default_agent_ids: ["director"],
        allow_main_agent_override: true,
        allow_temporary_agents: true,
        vibe_coding_enabled: true,
        multimedia_generation_enabled: true,
        openclaw_enabled: true,
        openclaw_mode: "read_only",
        openclaw_allowed_commands: [],
        temporary_agent_policy: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。",
      },
    });
  });

  it("keeps advanced JSON publishing available with detailed parse errors", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    await user.click(await screen.findByText("高级：直接编辑生产配置 JSON"));
    const editor = screen.getByLabelText("配置 JSON") as HTMLTextAreaElement;
    expect(editor.value).toContain('"models"');

    fireEvent.change(editor, { target: { value: "{broken" } });
    await user.click(screen.getByRole("button", { name: "创建草稿并发布" }));

    expect((await screen.findByRole("alert")).textContent).toContain("JSON 解析失败");
  });
  it("runs the OpenClaw approval and execution console from settings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    const allowedCommands = await screen.findByTestId("openclaw-allowed-commands");
    fireEvent.change(allowedCommands, { target: { value: `[["python","-c","print('ui-openclaw-ok')"]]` } });
    await user.click(screen.getByTestId("save-system-settings"));

    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
        method: "PUT",
        body: {
          ...settings,
          openclaw_allowed_commands: [["python", "-c", "print('ui-openclaw-ok')"]],
        },
      });
    });

    fireEvent.change(screen.getByTestId("openclaw-operation-argv"), {
      target: { value: `["python","-c","print('ui-openclaw-ok')"]` },
    });
    await user.click(screen.getByTestId("openclaw-create-operation"));
    expect(await screen.findByText(/waiting_user_approval/)).not.toBeNull();

    await user.click(screen.getByTestId("openclaw-approve-operation"));
    expect(await screen.findByText(/approved/)).not.toBeNull();

    await user.click(screen.getByTestId("openclaw-execute-operation"));
    expect((await screen.findByTestId("openclaw-execution-output")).textContent).toContain("ui-openclaw-ok");
  });

  it("saves configured OpenClaw remote adapters as secret references", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    const remoteAdapters = [
      {
        platform: "windows",
        target_type: "server",
        target: "desktop",
        base_url: "http://127.0.0.1:8765",
        credential_ref: "secret://openclaw-adapter",
      },
    ];
    fireEvent.change(await screen.findByTestId("openclaw-remote-adapters"), {
      target: { value: JSON.stringify(remoteAdapters) },
    });
    await user.click(screen.getByTestId("save-system-settings"));

    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
        method: "PUT",
        body: expect.objectContaining({ openclaw_remote_adapters: remoteAdapters }),
      });
    });
  });

  it("shows OpenClaw adapter availability in settings", async () => {
    render(<TestApp initialPath="/config" />);

    expect(await screen.findByText("linux server_command")).not.toBeNull();
    expect(screen.getByText("available")).not.toBeNull();
    expect(screen.getByText("windows server_command")).not.toBeNull();
    expect(screen.getByText("adapter_unavailable")).not.toBeNull();
    expect(screen.getByText(/remote-windows-host/)).not.toBeNull();
  });

  it("binds OpenClaw operation requests to the selected active session", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    await user.click(await screen.findByTestId("openclaw-create-session"));
    expect((await screen.findAllByText(/openclaw_session_ui_test/)).length).toBeGreaterThan(0);
    expect(screen.getByTestId("openclaw-operation-session")).toHaveProperty("value", "openclaw_session_ui_test");

    const allowedCommands = screen.getByTestId("openclaw-allowed-commands");
    fireEvent.change(allowedCommands, { target: { value: `[["python","-c","print('ui-openclaw-ok')"]]` } });
    await user.click(screen.getByText("保存系统设置"));

    fireEvent.change(screen.getByTestId("openclaw-operation-argv"), {
      target: { value: `["python","-c","print('ui-openclaw-ok')"]` },
    });
    await user.click(screen.getByTestId("openclaw-create-operation"));

    expect(requests.find((request) => request.path === "/api/v1/admin/openclaw/operations")).toMatchObject({
      method: "POST",
      body: expect.objectContaining({ session_id: "openclaw_session_ui_test" }),
    });
  });

  it("manages OpenClaw control sessions from settings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    await user.click(await screen.findByTestId("openclaw-create-session"));
    expect((await screen.findAllByText(/openclaw_session_ui_test/)).length).toBeGreaterThan(0);
    expect(screen.getByText("active")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/openclaw/sessions")).toMatchObject({
      method: "POST",
      body: {
        platform: "linux",
        target_type: "server",
        target: "agent-hub-server",
        purpose: "Keep a bounded OpenClaw control session for server maintenance",
      },
    });

    await user.click(screen.getByTestId("openclaw-pause-session-openclaw_session_ui_test"));
    expect(await screen.findByText("paused")).not.toBeNull();

    await user.click(screen.getByTestId("openclaw-resume-session-openclaw_session_ui_test"));
    expect(await screen.findByText("active")).not.toBeNull();

    await user.click(screen.getByTestId("openclaw-stop-session-openclaw_session_ui_test"));
    expect(await screen.findByText("stopped")).not.toBeNull();
  });
});
