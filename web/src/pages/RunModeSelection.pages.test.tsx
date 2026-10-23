import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type SubmittedRun } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { RunsPage } from "./RunsPage";

const clients: QueryClient[] = [];
const defaults = {
  default_permission_mode: "default", default_sandbox_profile: "workspace_write",
  supported_permission_modes: ["default", "auto_review", "read_only"],
  default_execution_backend: "systemd", default_agent_ids: [], default_workflow_id: null,
  tool_approval_mode: "auto_review",
};

afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); });

function submitted(overrides: Partial<SubmittedRun> = {}): SubmittedRun {
  return { id: "run-next", tenant_id: "tenant", status: "completed", mode: "direct", version: 1,
    decision_token: null, clarification_reason: null, conversation_id: "conv-current",
    execution_backend: "systemd", requested_permissions: [], ...overrides };
}

async function mount(active = false) {
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: {
    queries: { enabled: false, retry: false, staleTime: Infinity }, mutations: { retry: false },
  } });
  clients.push(client);
  client.setQueryData(["settings"], defaults);
  client.setQueryData(["settings", "tenant", "actor", "operator"], defaults);
  vi.spyOn(api, "settings").mockImplementation(() => new Promise(() => {}));
  const history = { id: "run-history", conversation_id: "conv-current", request: "Previous Direct task",
    status: active ? "running" : "completed", mode: "direct", version: 1,
    created_at: "2026-10-23T00:00:00Z", cost_usd: "0", queue_wait_ms: 0, capacity_wait_ms: 0,
    events: [], artifacts: [], explicit_details: {}, failure_diagnostics: [], tool_lifecycle: [], orchestration_protocol_summary: null,
    model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] } };
  const conversation = { conversation_id: "conv-current", title: "Current", project_id: "default", project_label: "Default",
    workspace_path: "main", archived_at: null, created_at: history.created_at, updated_at: history.created_at, runs: [history] };
  client.setQueryData(["runs"], [history]);
  client.setQueryData(["conversations", false], [conversation]);
  client.setQueryData(["conversations", true], []);
  client.setQueryData(["project-workspaces"], []);
  client.setQueryData(["conversation", "conv-current"], conversation);
  vi.spyOn(api, "conversation").mockResolvedValue(conversation);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  const create = vi.spyOn(api, "createRun").mockResolvedValue(submitted());
  const queue = vi.spyOn(api, "queueConversationMessage").mockRejectedValue(new Error("submission boundary reached"));
  const choose = vi.spyOn(api, "chooseMode").mockResolvedValue(submitted());
  vi.spyOn(api, "me").mockResolvedValue({ tenant_id: "tenant", user_id: "actor", username: "tester", role: "operator", permissions: ["*"] });
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={["/?conversation=conv-current"]}><RunsPage /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await screen.findByRole("navigation", { name: "项目与会话管理" });
  return { user, create, queue, choose };
}

async function openPicker(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "打开本次运行配置" }));
  const dialog = screen.getByRole("dialog", { name: "本次运行设置" });
  return within(dialog).getByRole("combobox", { name: "运行模式" }) as HTMLSelectElement;
}

async function closeSettings(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "关闭运行设置" }));
}

async function send(user: ReturnType<typeof userEvent.setup>, message: string, button = "发送") {
  const form = within(screen.getByRole("form", { name: "发送消息" }));
  await user.type(form.getByRole("textbox"), message);
  await user.click(form.getByRole("button", { name: button }));
}

describe("explicit next-run mode selection", () => {
  it("keeps ordinary Direct wording and previous Direct history under auto routing", async () => {
    const { user, create } = await mount();
    const picker = await openPicker(user);
    expect(picker.value).toBe("auto");
    await closeSettings(user);
    await send(user, "Explicit Direct please, directly answer this question");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ mode: "auto", direct_model: null,
      agent_ids: [], allow_workflow_adjustment: false, sandbox_profile: "workspace_write" });
  });

  it("submits explicitly selected Direct once and resets the next message to auto", async () => {
    const { user, create } = await mount();
    await user.selectOptions(await openPicker(user), "direct");
    await closeSettings(user);
    await send(user, "Build a project");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ mode: "direct", direct_model: null, agent_ids: [] });
    await waitFor(() => expect((within(screen.getByRole("form", { name: "发送消息" })).getByRole("textbox") as HTMLTextAreaElement).value).toBe(""));
    expect((await openPicker(user)).value).toBe("auto");
    await closeSettings(user);
    await send(user, "Next ordinary message");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1][0].mode).toBe("auto");
  });

  it("uses the submitted Direct snapshot if a response still requires mode confirmation", async () => {
    const { user, create, choose } = await mount();
    let resolve!: (run: SubmittedRun) => void;
    create.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    await user.selectOptions(await openPicker(user), "direct");
    await closeSettings(user);
    await send(user, "Build with the explicit mode");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect((await openPicker(user)).disabled).toBe(true);
    await closeSettings(user);
    await act(async () => resolve(submitted({ status: "waiting_user_mode", mode: null,
      decision_token: "exact-mode-decision", version: 7 })));
    await waitFor(() => expect(choose).toHaveBeenCalledWith("run-next", expect.objectContaining({
      mode: "direct", decision_token: "exact-mode-decision", version: 7,
    })));
    expect((await openPicker(user)).value).toBe("auto");
  });

  it.each(["button", "chat"])("does not copy a %s mode-confirmation decision into the next message", async (entry) => {
    const { user, create, choose } = await mount();
    create.mockResolvedValueOnce(submitted({ status: "waiting_user_mode", mode: null,
      decision_token: "confirm-this-run", version: 3 }));
    await send(user, "Let the main agent decide");
    const prompt = await screen.findByRole("article", { name: "运行模式确认" });
    if (entry === "button") await user.click(within(prompt).getByRole("button", { name: /^直连/ }));
    else await send(user, "直连", "确认");
    await waitFor(() => expect(choose).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("article", { name: "运行模式确认" })).toBeNull());
    expect((await openPicker(user)).value).toBe("auto");
    await closeSettings(user);
    await send(user, "Follow up normally");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1][0].mode).toBe("auto");
  });

  it("disables the picker during a running task and keeps queue submissions auto", async () => {
    const { user, create, queue } = await mount(true);
    const picker = await openPicker(user);
    expect(picker.disabled).toBe(true);
    await closeSettings(user);
    await send(user, "Queued task", "排队");
    await waitFor(() => expect(queue).toHaveBeenCalledTimes(1));
    expect(queue.mock.calls[0][2].mode).toBe("auto");
    expect(create).not.toHaveBeenCalled();
  });

  it("keeps retry explicitly auto even when a next-run Direct draft is selected", async () => {
    const { user, create } = await mount();
    await user.selectOptions(await openPicker(user), "direct");
    await closeSettings(user);
    await send(user, "/retry");
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ mode: "auto", message: "Previous Direct task" });
  });

  it("exposes a labelled native select inside the modal and restores focus on Escape", async () => {
    const { user, create } = await mount();
    await send(user, "/mode");
    const dialog = screen.getByRole("dialog", { name: "本次运行设置" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const picker = within(dialog).getByRole("combobox", { name: "运行模式" });
    expect(within(picker).getAllByRole("option").map((option) => (option as HTMLOptionElement).value))
      .toEqual(["auto", "direct", "dispatch", "discuss", "hybrid"]);
    picker.focus();
    expect(document.activeElement).toBe(picker);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "本次运行设置" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "打开本次运行配置" })));
    expect(create).not.toHaveBeenCalled();
  });
});
