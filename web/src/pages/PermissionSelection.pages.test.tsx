import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../api/client";
import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { RunsPage } from "./RunsPage";

const clients: QueryClient[] = [];
const defaults = {
  default_permission_mode: "default", default_sandbox_profile: "workspace_write",
  supported_permission_modes: ["default", "auto_review", "read_only"],
  default_execution_backend: "systemd", default_agent_ids: [], default_workflow_id: null,
  tool_approval_mode: "auto_review",
};

afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); });

async function mount(settings = defaults, active = false, confirmed = true) {
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: {
    queries: { enabled: false, retry: false, staleTime: Infinity }, mutations: { retry: false },
  } });
  clients.push(client);
  client.setQueryData(["settings"], settings);
  if (confirmed) client.setQueryData(["settings", "tenant", "actor", "operator"], settings);
  vi.spyOn(api, "settings").mockImplementation(() => new Promise(() => {}));
  client.setQueryData(["runs"], []);
  const run = { id: "run-current", conversation_id: "conv-current", request: "Existing task", status: "running", mode: "direct",
    version: 1, created_at: "2026-10-23T00:00:00Z", cost_usd: "0", queue_wait_ms: 0, capacity_wait_ms: 0,
    events: [], artifacts: [], explicit_details: {}, failure_diagnostics: [], tool_lifecycle: [], orchestration_protocol_summary: null,
    model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0, requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] } };
  const conversation = { conversation_id: "conv-current", title: "Current", project_id: "default", project_label: "Default",
    workspace_path: "main", archived_at: null, created_at: run.created_at, updated_at: run.created_at,
    runs: active ? [run] : [] };
  client.setQueryData(["conversations", false], [conversation]);
  client.setQueryData(["conversations", true], []);
  client.setQueryData(["project-workspaces"], []);
  client.setQueryData(["conversation", "conv-current"], conversation);
  vi.spyOn(api, "conversation").mockImplementation(async (id) => ({ ...conversation, conversation_id: id }));
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  const create = vi.spyOn(api, "createRun").mockRejectedValue(new Error("submission boundary reached"));
  const queue = vi.spyOn(api, "queueConversationMessage").mockRejectedValue(new Error("submission boundary reached"));
  vi.spyOn(api, "me").mockResolvedValue({ tenant_id: "tenant", user_id: "actor", username: "tester", role: "operator", permissions: ["*"] });
  vi.spyOn(api, "login").mockResolvedValue({ tenant_id: "tenant-new", user_id: "actor-new", username: "second", role: "operator", permissions: ["*"] });
  let navigate!: ReturnType<typeof useNavigate>;
  let auth!: ReturnType<typeof useAuth>;
  function Controller() { navigate = useNavigate(); auth = useAuth(); return <RunsPage />; }
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={["/?conversation=conv-current"]}><Controller /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await screen.findByRole("navigation", { name: "项目与会话管理" });
  return { user, client, create, queue,
    go: async (id: string) => { await act(async () => navigate(`/?conversation=${id}`)); },
    changeIdentity: async () => { await act(async () => auth.login("second", "test-password")); },
  };
}

describe("independent composer sandbox permissions", () => {
  it("opens permissions without opening or occupying the run settings dialog", async () => {
    const { user } = await mount();
    const tools = screen.getByLabelText("消息工具");
    await user.click(within(tools).getByRole("button", { name: /沙箱权限/ }));
    expect(screen.getByRole("menu", { name: /权限/ })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "本次运行设置" })).toBeNull();
    expect(screen.getByRole("menuitemradio", { name: /完整访问权限/ }).getAttribute("aria-disabled")).toBe("true");
  });

  it("selects read-only and keeps the user's draft selection across settings refetch", async () => {
    const { user, client } = await mount();
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^只读/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /沙箱权限.*只读/ })).toBeTruthy());
    await act(async () => {
      client.setQueryData(["settings", "tenant", "actor", "operator"], { ...defaults, default_permission_mode: "auto_review" });
      client.setQueryData(["settings"], { ...defaults, default_permission_mode: "auto_review" });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.getByRole("button", { name: /沙箱权限.*只读/ })).toBeTruthy();
  });

  it("does not use a legacy unscoped cache while the actual identity's permission settings are loading", async () => {
    const { user, create } = await mount(defaults, false, false);
    await user.type(within(screen.getByRole("form", { name: "发送消息" })).getByRole("textbox"), "Build something");
    const submit = within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: /发送/ });
    expect(submit.hasAttribute("disabled")).toBe(true);
    await user.click(submit);
    expect(create).not.toHaveBeenCalled();
  });

  it("blocks new submissions after an identity change until that identity's policy is loaded", async () => {
    const { user, changeIdentity, create } = await mount();
    await changeIdentity();
    await user.type(within(screen.getByRole("form", { name: "发送消息" })).getByRole("textbox"), "New user request");
    expect(within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: /发送/ }).hasAttribute("disabled")).toBe(true);
    expect(create).not.toHaveBeenCalled();
  });

  it("keeps separate drafts when switching between conversations", async () => {
    const { user, go } = await mount();
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^只读/ }));
    await go("conv-other");
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^默认权限/ }));
    await go("conv-current");
    expect(screen.getByRole("button", { name: /沙箱权限.*只读/ })).toBeTruthy();
  });

  it("retains the queued permission snapshot if the active run finishes before enqueue commits", async () => {
    const { user, create, queue } = await mount(defaults, true);
    let reject!: (error: Error) => void;
    queue.mockImplementation(() => new Promise((_, decline) => { reject = decline; }));
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^只读/ }));
    await user.type(within(screen.getByRole("form", { name: "发送消息" })).getByRole("textbox"), "Queued read-only task");
    await user.click(within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: "排队" }));
    await waitFor(() => expect(queue).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^默认权限/ }));
    await act(async () => reject(new ApiError("conversation has no active run", 409, "conversation_not_active")));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ permission_mode: "read_only", sandbox_profile: "read_only", requested_permissions: ["workspace.read"] });
  });

  it("loads the configured default and does not duplicate the control in plus settings", async () => {
    const { user } = await mount({ ...defaults, default_permission_mode: "read_only", default_sandbox_profile: "read_only" });
    await waitFor(() => expect(screen.getByRole("button", { name: /沙箱权限.*只读/ })).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "打开本次运行配置" }));
    expect(within(screen.getByRole("dialog", { name: "本次运行设置" })).queryByRole("group", { name: "选择本次运行沙箱权限" })).toBeNull();
  });

  it("does not pretend an old server supports runtime reviewer or full access", async () => {
    const { user } = await mount({ ...defaults, supported_permission_modes: [] });
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    expect(screen.getByRole("menuitemradio", { name: /自动审查/ }).getAttribute("aria-disabled")).toBe("true");
  });

  it.each([false, true])("uses the selected immutable read-only permission snapshot when queued=%s", async (active) => {
    const { user, create, queue } = await mount(defaults, active);
    await user.click(screen.getByRole("button", { name: /沙箱权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /^只读/ }));
    await user.type(within(screen.getByRole("form", { name: "发送消息" })).getByRole("textbox"), "Read project only");
    await user.click(within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: /发送|排队/ }));
    if (active) {
      await waitFor(() => expect(queue).toHaveBeenCalledTimes(1));
      expect(queue.mock.calls[0][2]).toMatchObject({ permission_mode: "read_only", sandbox_profile: "read_only", requested_permissions: ["workspace.read"] });
      expect(create).not.toHaveBeenCalled();
    } else {
      await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
      expect(create.mock.calls[0][0]).toMatchObject({ permission_mode: "read_only", sandbox_profile: "read_only", requested_permissions: ["workspace.read"] });
      expect(queue).not.toHaveBeenCalled();
    }
  });
});
