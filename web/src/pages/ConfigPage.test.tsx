import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { TestApp } from "../app/router";
import { api } from "../api/client";
import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { ConfigPage } from "./ConfigPage";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

const settings = {
  default_mode: "auto",
  default_workflow_id: null,
  default_execution_backend: "systemd",
  default_permission_mode: "default",
  default_sandbox_profile: "workspace_write",
  supported_permission_modes: ["default", "auto_review", "read_only"],
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
  temporary_agent_policy:
    "主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
  channel_entry: "web",
  attachment_retention_days: 7,
  attachment_max_mb: 25,
  plugin_package_subprocess_registration_status: "launcher_not_found",
};
let currentSettings = { ...settings };

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function IdentityControls() {
  const { login } = useAuth();
  return <button onClick={() => void login("next", "password")}>切换设置身份</button>;
}

function renderScopedConfig(client: QueryClient) {
  return render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter>
    <IdentityControls /><ConfigPage />
  </MemoryRouter></AuthProvider></QueryClientProvider>);
}

describe("ConfigPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let openClawSessions: Array<Record<string, unknown>> = [];
  let lastOpenClawOperationBody: Record<string, unknown> = {};
  let executionBackendsShouldFail = false;

  beforeEach(() => {
    requests.length = 0;
    openClawSessions = [];
    lastOpenClawOperationBody = {};
    executionBackendsShouldFail = false;
    currentSettings = { ...settings };
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        const method = init?.method ?? "GET";
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/settings") {
          if (method === "PUT") return jsonResponse({
            ...JSON.parse(String(init?.body)),
            supported_permission_modes: currentSettings.supported_permission_modes,
          });
          return jsonResponse(currentSettings);
        }
        if (path === "/api/v1/admin/execution-backends") {
          if (executionBackendsShouldFail) {
            return jsonResponse(
              { error: { code: "execution_backend_probe_failed", message: "probe unavailable" } },
              { status: 503 },
            );
          }
          return jsonResponse([
            {
              id: "systemd",
              name: "本机 systemd 隔离",
              adapter: "SystemdSkillSandbox",
              description: "在当前 Linux 服务器上通过 systemd transient unit 运行技能。",
              isolation: "DynamicUser + 只读系统 + 私有网络",
              cost: "本机资源",
              available: true,
              reason: null,
              supported_sandbox_profiles: ["read_only", "restricted", "workspace_write"],
            },
            {
              id: "docker",
              name: "Docker 容器隔离",
              adapter: "DockerSkillSandbox",
              description: "在临时容器中运行技能。",
              isolation: "容器隔离",
              cost: "本机容器资源",
              available: false,
              reason: "docker_cli_not_found",
              supported_sandbox_profiles: ["read_only", "restricted", "workspace_write"],
            },
          ]);
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
    vi.restoreAllMocks();
  });

  it("does not read or expose settings until the authenticated identity is loaded", async () => {
    const identity = await api.me();
    let resolveIdentity!: (value: typeof identity) => void;
    vi.spyOn(api, "me").mockReturnValue(new Promise((resolve) => { resolveIdentity = resolve; }));
    const read = vi.spyOn(api, "settings");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderScopedConfig(client);
    await act(async () => {});
    expect(read).not.toHaveBeenCalled();
    expect(screen.queryByRole("form", { name: "保存系统设置" })).toBeNull();
    await act(async () => resolveIdentity(identity));
    expect(await screen.findByRole("group", { name: "沙箱权限" })).toBeTruthy();
  });

  it.each(["tenant_id", "user_id", "role"] as const)("isolates cached settings and pending drafts when %s changes", async (field) => {
    const identity = await api.me();
    const nextIdentity = { ...identity, [field]: field === "role" ? "admin" : "22222222-2222-4222-8222-222222222222" };
    vi.spyOn(api, "login").mockResolvedValue(nextIdentity as typeof identity);
    const original = api.settings;
    let resolveSettings!: (value: Awaited<ReturnType<typeof api.settings>>) => void;
    const nextSettings = await original();
    let secondIdentity = false;
    const read = vi.spyOn(api, "settings").mockImplementation(() => secondIdentity
      ? new Promise((resolve) => { resolveSettings = resolve; }) : original());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(["settings"], { ...nextSettings, default_permission_mode: "read_only" });
    const user = userEvent.setup();
    renderScopedConfig(client);
    const mode = await screen.findByLabelText("默认权限模式");
    expect((mode as HTMLSelectElement).value).toBe("default");
    await user.selectOptions(mode, "read_only");
    secondIdentity = true;
    await user.click(screen.getByRole("button", { name: "切换设置身份" }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("form", { name: "保存系统设置" })).toBeNull();
    expect(screen.queryByLabelText("默认权限模式")).toBeNull();
    await act(async () => resolveSettings({ ...nextSettings, default_permission_mode: "default", default_sandbox_profile: "restricted" }));
    expect((await screen.findByLabelText("默认权限模式") as HTMLSelectElement).value).toBe("default");
    expect((screen.getByLabelText("默认沙箱边界") as HTMLSelectElement).value).toBe("restricted");
    expect(client.getQueryData(["settings", nextIdentity.tenant_id, nextIdentity.user_id, nextIdentity.role])).toBeTruthy();
  });

  it("does not permit editing or saving stale settings after a failed scoped refresh", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    renderScopedConfig(client);
    await screen.findByRole("group", { name: "沙箱权限" });
    vi.spyOn(api, "settings").mockRejectedValue(new Error("settings unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["settings"] }); });
    expect((await screen.findByRole("alert")).textContent).toContain("系统设置加载失败");
    expect(screen.queryByRole("button", { name: "保存系统设置" })).toBeNull();
    expect(screen.queryByLabelText("默认权限模式")).toBeNull();
    expect(requests.some((request) => request.path === "/api/v1/admin/settings")).toBe(false);
    await user.keyboard("{Enter}");
    expect(requests.some((request) => request.path === "/api/v1/admin/settings")).toBe(false);
  });

  it("loads system settings and saves production defaults through dedicated controls", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/config" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByText("版本 3")).not.toBeNull();
    expect(screen.queryByText("Vibe Coding")).toBeNull();
    expect(screen.queryByTestId("vibe-coding-toggle")).toBeNull();
    expect(view.container.querySelectorAll(".settings-shortcut-card")).toHaveLength(7);
    expect(screen.getByLabelText(/^默认执行环境/)).not.toBeNull();

    await user.selectOptions(screen.getByLabelText("默认运行模式"), "dispatch");
    await user.selectOptions(screen.getByLabelText("默认工作流"), "short-video-dispatch");
    await user.selectOptions(screen.getByLabelText("工具审批模式"), "ask");
    await user.click(screen.getByLabelText(/导演/));
    await user.click(screen.getByLabelText("允许主 Agent 提出临场调整，执行前必须向用户核对"));
    await user.click(screen.getByLabelText("允许主 Agent 在能力不足时申请临时子 Agent"));
    await user.click(screen.getByTestId("multimedia-generation-toggle"));
    fireEvent.change(screen.getByLabelText(/临时 Agent 补位规则/), {
      target: { value: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。" },
    });
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));

    expect((await screen.findByRole("status")).textContent).toContain("系统设置已保存");
    expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
      method: "PUT",
      body: {
        ...Object.fromEntries(Object.entries(settings).filter(([key]) => key !== "supported_permission_modes")),
        default_mode: "dispatch",
        default_workflow_id: "short-video-dispatch",
        default_agent_ids: ["director"],
        tool_approval_mode: "ask",
        allow_main_agent_override: true,
        allow_temporary_agents: true,
        multimedia_generation_enabled: true,
        openclaw_allowed_commands: [],
        temporary_agent_policy: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。",
      },
    });
  });

  it("normalizes read-only mode before saving and strips server capability metadata", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);
    const group = await screen.findByRole("group", { name: "沙箱权限" });
    await user.selectOptions(within(group).getByLabelText("默认权限模式"), "read_only");
    expect((within(group).getByLabelText("默认沙箱边界") as HTMLSelectElement).value).toBe("read_only");
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));
    await waitFor(() => expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toBeTruthy());
    const body = requests.find((request) => request.path === "/api/v1/admin/settings")!.body;
    expect(body).toMatchObject({ default_permission_mode: "read_only", default_sandbox_profile: "read_only" });
    expect(body).not.toHaveProperty("supported_permission_modes");
  });

  it("normalizes an inconsistent read-only response at the save boundary", async () => {
    currentSettings.default_permission_mode = "read_only";
    currentSettings.default_sandbox_profile = "workspace_write";
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);
    await screen.findByRole("group", { name: "沙箱权限" });
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));
    await waitFor(() => expect(requests.some((request) => request.path === "/api/v1/admin/settings")).toBe(true));
    expect(requests.find((request) => request.path === "/api/v1/admin/settings")!.body).toMatchObject({
      default_permission_mode: "read_only", default_sandbox_profile: "read_only",
    });
  });

  it("keeps a read-only boundary paired with read-only mode and can leave read-only", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);
    const group = await screen.findByRole("group", { name: "沙箱权限" });
    await user.selectOptions(within(group).getByLabelText("默认沙箱边界"), "read_only");
    expect((within(group).getByLabelText("默认权限模式") as HTMLSelectElement).value).toBe("read_only");
    await user.selectOptions(within(group).getByLabelText("默认权限模式"), "default");
    expect((within(group).getByLabelText("默认沙箱边界") as HTMLSelectElement).value).toBe("workspace_write");
  });

  it("requires both server support and global rule review to choose auto review", async () => {
    currentSettings.tool_approval_mode = "ask";
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);
    const group = await screen.findByRole("group", { name: "沙箱权限" });
    const mode = within(group).getByLabelText("默认权限模式");
    const auto = within(mode).getByRole("option", { name: /自动审查/ }) as HTMLOptionElement;
    expect(auto.disabled).toBe(true);
    await user.selectOptions(screen.getByLabelText("工具审批模式"), "auto_review");
    expect(auto.disabled).toBe(false);
    await user.selectOptions(mode, "auto_review");
    await user.selectOptions(screen.getByLabelText("工具审批模式"), "ask");
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));
    expect(await screen.findByText(/自动审查需要先启用全局规则审查/)).toBeTruthy();
    expect(requests.some((request) => request.path === "/api/v1/admin/settings")).toBe(false);
  });

  it("disables an unadvertised mode even when global rule review is enabled", async () => {
    currentSettings.supported_permission_modes = ["default", "read_only"];
    render(<TestApp initialPath="/config" />);
    const group = await screen.findByRole("group", { name: "沙箱权限" });
    expect((within(group).getByRole("option", { name: /自动审查/ }) as HTMLOptionElement).disabled).toBe(true);
  });

  it("disables new defaults for old services and saves only the legacy payload", async () => {
    const { supported_permission_modes: _supported, default_permission_mode: _mode, default_sandbox_profile: _profile, ...legacy } = settings;
    currentSettings = legacy as typeof settings;
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);
    const group = await screen.findByRole("group", { name: "沙箱权限" });
    expect((within(group).getByLabelText("默认权限模式") as HTMLSelectElement).disabled).toBe(true);
    expect((within(group).getByLabelText("默认沙箱边界") as HTMLSelectElement).disabled).toBe(true);
    expect(within(group).getByText(/服务需更新/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));
    await waitFor(() => expect(requests.some((request) => request.path === "/api/v1/admin/settings")).toBe(true));
    const body = requests.find((request) => request.path === "/api/v1/admin/settings")!.body;
    expect(body).not.toHaveProperty("supported_permission_modes");
    expect(body).not.toHaveProperty("default_permission_mode");
    expect(body).not.toHaveProperty("default_sandbox_profile");
    expect(screen.queryByText(/沙箱权限.*已保存/)).toBeNull();
  });

  it("defaults absent permission fields in legacy API responses", async () => {
    const { supported_permission_modes: _supported, default_permission_mode: _mode, default_sandbox_profile: _profile, ...legacy } = settings;
    currentSettings = legacy as typeof settings;
    const parsed = await api.settings();
    expect(parsed).toMatchObject({
      supported_permission_modes: [], default_permission_mode: "default", default_sandbox_profile: "workspace_write",
    });
  });

  it.each([
    ["default_permission_mode", "full_access"],
    ["default_sandbox_profile", "full_access"],
    ["supported_permission_modes", ["full_access"]],
  ])("rejects unsupported permission protocol values for %s", async (key, value) => {
    currentSettings = { ...settings, [key]: value };
    await expect(api.settings()).rejects.toThrow();
  });

  it("keeps other system settings available when execution backend probing fails", async () => {
    executionBackendsShouldFail = true;
    render(<TestApp initialPath="/config" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByLabelText("默认运行模式")).not.toBeNull();
    expect((screen.getByRole("button", { name: "保存系统设置" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText("默认执行环境") as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByText(/执行环境探测失败/)).not.toBeNull();
    expect(screen.getByText(/其他设置仍可正常使用/)).not.toBeNull();
  });

  it("shows only real backend adapters and explains unavailable environments", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/execution-environments" />);

    expect(await screen.findByRole("heading", { name: "执行环境" })).not.toBeNull();
    expect(screen.getByText("本机 systemd 隔离")).not.toBeNull();
    expect(screen.getByText("Docker 容器隔离")).not.toBeNull();
    expect(screen.getByText("服务器未安装 Docker CLI。")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "重新探测" }));
    await waitFor(() => expect(screen.getByText("当前可用")).not.toBeNull());
  });

  it("keeps long execution backend statuses inside mobile cards", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toContain(".execution-environment-heading {\n  align-items: start;\n  display: flex;\n  flex-wrap: wrap;");
    expect(stylesCss).toContain(".execution-environment-card .status-pill {\n  max-width: 100%;\n  min-width: 0;");
    expect(stylesCss).toContain("overflow-wrap: anywhere;");
    expect(stylesCss).toContain("white-space: normal;");
  });

  it("shows package subprocess registration status as runtime state", async () => {
    render(<TestApp initialPath="/config" />);

    expect(await screen.findByText("本地进程插件")).not.toBeNull();
    expect(screen.getByText("启动器文件不可用，未注册本地进程适配器。")).not.toBeNull();
  });

  it("anchors workflow configuration as a third-level system settings section", async () => {
    render(<TestApp initialPath="/config?section=workflows" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    await waitFor(() => {
      expect(document.querySelector('[data-nav-section="workflows"]')?.getAttribute("data-nav-active")).toBe("true");
    });
    expect(screen.getByRole("link", { name: "配置工作流" }).getAttribute("href")).toBe("/workflows");
  });

  it.each([
    ["ready", "隔离启动器就绪，可注册本地进程适配器。"],
    ["disabled", "本地进程插件运行器未启用。"],
    ["no_adapter_ids", "未配置本地进程适配器白名单。"],
    ["unsupported_isolation_backend", "隔离后端未启用 bubblewrap。"],
    ["missing_isolation_launcher", "未配置隔离启动器。"],
    ["launcher_path_not_absolute", "启动器路径不是绝对路径。"],
    ["launcher_not_found", "启动器文件不可用，未注册本地进程适配器。"],
    ["launcher_probe_failed", "隔离启动器无法创建安全沙箱，请检查 AppArmor 或用户命名空间配置。"],
    ["launcher_not_executable", "启动器不可执行，未注册本地进程适配器。"],
  ])("renders package subprocess registration status %s", async (status, text) => {
    currentSettings = {
      ...settings,
      plugin_package_subprocess_registration_status: status,
    };

    render(<TestApp initialPath="/config" />);

    expect(await screen.findByText(text)).not.toBeNull();
  });

  it("keeps advanced JSON publishing available with detailed parse errors", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<TestApp initialPath="/config" />);

    await user.click(await screen.findByText("高级：直接编辑生产配置 JSON"));
    const editor = screen.getByLabelText("配置 JSON") as HTMLTextAreaElement;
    expect(editor.value).toContain('"models"');

    fireEvent.change(editor, { target: { value: "{broken" } });
    await user.click(screen.getByRole("button", { name: "创建草稿并发布" }));

    expect((await screen.findByRole("alert")).textContent).toContain("JSON 解析失败");
  });
  it("keeps OpenClaw management in the dedicated control page", async () => {
    render(<TestApp initialPath="/config" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByRole("region", { name: "OpenClaw 配置入口" })).not.toBeNull();
    expect(screen.getByText(/allowlist 0 条，远程适配器 0 个/)).not.toBeNull();
    expect(screen.getByRole("link", { name: "打开 OpenClaw 控制" }).getAttribute("href")).toBe("/openclaw");
    expect(screen.queryByTestId("openclaw-create-operation")).toBeNull();
    expect(screen.queryByTestId("openclaw-allowed-commands")).toBeNull();
  });
});
