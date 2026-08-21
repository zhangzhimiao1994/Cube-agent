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
  temporary_agent_policy: "主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
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

describe("OpenClawPage", () => {
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
        if (init?.body) requests.push({ path, method, body: JSON.parse(String(init.body)) });
        if (path === "/api/v1/auth/me") return jsonResponse(principal);
        if (path === "/api/v1/admin/settings") {
          if (method === "PUT") return jsonResponse(JSON.parse(String(init?.body)));
          return jsonResponse(settings);
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
              description: "Runs exact allowlisted argv commands on the 魔方 agent Linux server after approval.",
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
        if (path === "/api/v1/admin/openclaw/sessions" && method === "GET") return jsonResponse(openClawSessions);
        if (path === "/api/v1/admin/openclaw/sessions" && method === "POST") {
          const body = JSON.parse(String(init?.body));
          const session = {
            id: "openclaw_session_page_test",
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
        if (path === "/api/v1/admin/openclaw/operations" && method === "POST") {
          lastOpenClawOperationBody = JSON.parse(String(init?.body));
          return jsonResponse(
            {
              id: "openclaw_page_test",
              status: "waiting_user_approval",
              approval_id: "openclaw_page_test_approval",
              requires_user_approval: true,
              platform: "linux",
              kind: "server_command",
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
        if (path === "/api/v1/admin/openclaw/operations/openclaw_page_test" && method === "PATCH") {
          return jsonResponse({
            id: "openclaw_page_test",
            status: "approved",
            approval_id: "openclaw_page_test_approval",
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
        if (path === "/api/v1/admin/openclaw/operations/openclaw_page_test/execute" && method === "POST") {
          return jsonResponse({
            operation: {
              id: "openclaw_page_test",
              status: "executed",
              approval_id: "openclaw_page_test_approval",
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
                stdout: "openclaw-page-ok\n",
                stderr: "",
                truncated: false,
                executed_by: principal.user_id,
                executed_at: "2026-08-13T00:02:00Z",
              },
            },
            exit_code: 0,
            stdout: "openclaw-page-ok\n",
            stderr: "",
            truncated: false,
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

  it("exposes OpenClaw as a standalone navigation module", async () => {
    render(<TestApp initialPath="/system" />);

    const links = await screen.findAllByRole("link", { name: /OpenClaw 控制/ });
    expect(links.some((link) => link.getAttribute("href") === "/openclaw")).toBe(true);
  });

  it("saves OpenClaw settings and remote adapters from the dedicated page", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/openclaw" />);

    expect(await screen.findByRole("heading", { name: "OpenClaw 控制" })).not.toBeNull();
    await user.click(screen.getByTestId("openclaw-page-toggle"));
    await user.selectOptions(screen.getByLabelText("权限模式"), "read_only");
    fireEvent.change(screen.getByTestId("openclaw-page-allowed-commands"), {
      target: { value: `[["python","-c","print('openclaw-page-ok')"]]` },
    });
    fireEvent.change(screen.getByTestId("openclaw-page-adapter-target"), { target: { value: "reporting-pc" } });
    fireEvent.change(screen.getByTestId("openclaw-page-adapter-credential-ref"), {
      target: { value: "secret://openclaw-reporting-pc" },
    });
    await user.click(screen.getByTestId("openclaw-page-add-remote-adapter"));
    await user.click(screen.getByTestId("save-openclaw-settings"));

    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
        method: "PUT",
        body: expect.objectContaining({
          openclaw_enabled: true,
          openclaw_mode: "read_only",
          openclaw_allowed_commands: [["python", "-c", "print('openclaw-page-ok')"]],
          openclaw_remote_adapters: [
            expect.objectContaining({
              platform: "windows",
              target_type: "computer",
              target: "reporting-pc",
              credential_ref: "secret://openclaw-reporting-pc",
            }),
          ],
        }),
      });
    });
    expect(await screen.findByText("OpenClaw 设置已保存")).not.toBeNull();
  });

  it("explains approval modes and lets operators curate the command allowlist", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/openclaw" />);

    expect(await screen.findByRole("region", { name: "OpenClaw 审批策略预览" })).not.toBeNull();
    expect(screen.getByText("默认审核")).not.toBeNull();
    expect(screen.getByText(/所有操作先进入待审批/)).not.toBeNull();

    await user.selectOptions(screen.getByLabelText("权限模式"), "auto_review");
    expect(screen.getByText("自动审核")).not.toBeNull();
    expect(screen.getByText("当前没有 allowlist，自动审核不会放行任何命令。")).not.toBeNull();

    fireEvent.change(screen.getByTestId("openclaw-page-operation-argv"), {
      target: { value: `["python","--version"]` },
    });
    await user.click(screen.getByRole("button", { name: "添加当前控制台命令" }));
    expect(screen.getByText("python --version")).not.toBeNull();

    await user.click(screen.getByTestId("save-openclaw-settings"));

    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
        method: "PUT",
        body: expect.objectContaining({
          openclaw_mode: "auto_review",
          openclaw_allowed_commands: [["python", "--version"]],
        }),
      });
    });
  });
  it("creates non-Linux read operations from the dedicated console", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/openclaw" />);

    expect(await screen.findByRole("heading", { name: "OpenClaw 控制" })).not.toBeNull();
    await user.selectOptions(screen.getByLabelText("操作平台"), "windows");
    await user.selectOptions(screen.getByLabelText("操作类型"), "file_read");
    await user.clear(screen.getByLabelText("操作目标"));
    await user.type(screen.getByLabelText("操作目标"), "C:\\Reports\\daily.txt");
    fireEvent.change(screen.getByTestId("openclaw-page-operation-argv"), { target: { value: "[]" } });

    await user.click(screen.getByTestId("openclaw-page-create-operation"));
    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/openclaw/operations")).toMatchObject({
        method: "POST",
        body: expect.objectContaining({
          platform: "windows",
          kind: "file_read",
          target: "C:\\Reports\\daily.txt",
          argv: [],
        }),
      });
    });
  });
  it("creates selected Windows control sessions from the dedicated page", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/openclaw" />);

    expect(await screen.findByRole("heading", { name: "OpenClaw 控制" })).not.toBeNull();
    await user.selectOptions(screen.getByLabelText("会话平台"), "windows");
    await user.selectOptions(screen.getByLabelText("会话目标类型"), "computer");
    await user.clear(screen.getByLabelText("会话目标"));
    await user.type(screen.getByLabelText("会话目标"), "office-windows-pc");
    await user.clear(screen.getByLabelText("会话用途"));
    await user.type(screen.getByLabelText("会话用途"), "远程审批后接管 Windows 桌面执行报表任务");

    await user.click(screen.getByTestId("openclaw-page-create-session"));
    await waitFor(() => {
      expect(requests.find((request) => request.path === "/api/v1/admin/openclaw/sessions")).toMatchObject({
        method: "POST",
        body: expect.objectContaining({
          platform: "windows",
          target_type: "computer",
          target: "office-windows-pc",
          purpose: "远程审批后接管 Windows 桌面执行报表任务",
        }),
      });
    });
  });
  it("runs the OpenClaw approval and execution chain from the dedicated page", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/openclaw" />);

    await user.click(await screen.findByTestId("openclaw-page-create-session"));
    expect((await screen.findAllByText(/openclaw_session_page_test/)).length).toBeGreaterThan(0);
    expect(screen.getByTestId("openclaw-page-operation-session")).toHaveProperty("value", "openclaw_session_page_test");

    fireEvent.change(screen.getByTestId("openclaw-page-operation-argv"), {
      target: { value: `[["broken"]]` },
    });
    fireEvent.change(screen.getByTestId("openclaw-page-operation-argv"), {
      target: { value: `["python","-c","print('openclaw-page-ok')"]` },
    });
    await user.click(screen.getByTestId("openclaw-page-create-operation"));
    expect(await screen.findByText(/waiting_user_approval/)).not.toBeNull();

    await user.click(screen.getByTestId("openclaw-page-approve-operation"));
    expect(await screen.findByText(/approved/)).not.toBeNull();
    await user.click(screen.getByTestId("openclaw-page-execute-operation"));

    expect((await screen.findByTestId("openclaw-page-execution-output")).textContent).toContain("openclaw-page-ok");
    expect(requests.find((request) => request.path === "/api/v1/admin/openclaw/operations")).toMatchObject({
      method: "POST",
      body: expect.objectContaining({ session_id: "openclaw_session_page_test" }),
    });
  });
});
