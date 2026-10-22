import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type Conversation, type CurrentUser, type RunDetail, type SubmittedRun } from "../api/client";
import { AuthProvider, RequireAuth, useAuth } from "../auth/AuthProvider";
import { RunDetailPage } from "./RunDetailPage";
import { RunsPage } from "./RunsPage";

const initialActor: CurrentUser = {
  tenant_id: "tenant-alpha", user_id: "actor-alpha", username: "alpha", role: "operator", permissions: ["*"],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function approvalRun(name: string, approvalId = `approval-${name}`, version = 7): RunDetail {
  return {
    id: `run-${name}`, conversation_id: `conv-${name}`, request: `Approval ${name}`,
    status: "waiting_approval", mode: "dispatch", version,
    created_at: "2026-10-22T00:00:00Z", queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
    events: [], artifacts: [], tool_lifecycle: [],
    failure_diagnostics: [{
      category: "approval", stage: "approval.requested", approval_id: approvalId,
      reason: `Confirm ${approvalId}`, recommendation: "Allow this invocation only", sequence: 1,
    }],
    explicit_details: { approval_kind: "capability_tool", approval_id: approvalId, project_id: "default" },
    model_outcome_summary: {
      completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [],
    },
    orchestration_protocol_summary: null,
  };
}

function accepted(run: RunDetail): SubmittedRun {
  return {
    id: run.id, conversation_id: run.conversation_id, tenant_id: "tenant", status: "queued",
    mode: run.mode, version: (run.version ?? 0) + 1, decision_token: null, clarification_reason: null,
    execution_backend: "systemd", requested_permissions: [],
  };
}

async function publishRun(client: QueryClient, run: RunDetail) {
  await act(async () => {
    client.setQueryData(["run", run.id], run);
    // React Query batches observer notifications on the next timer turn.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

async function mountPage(page: "runs" | "detail") {
  const user = userEvent.setup();
  const alpha = approvalRun("alpha");
  const beta = approvalRun("beta");
  const client = new QueryClient({
    defaultOptions: { queries: { enabled: false, retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  clients.push(client);
  const conversations = [alpha, beta].map((run): Conversation => ({
    conversation_id: run.conversation_id!, title: run.request, project_id: "default", project_label: "Default",
    workspace_path: run.conversation_id!, archived_at: null,
    created_at: run.created_at, updated_at: run.created_at, runs: [run],
  }));
  function seedClient(target: QueryClient) {
    target.setQueryData(["runs"], [alpha, beta]);
    target.setQueryData(["conversations", false], conversations);
    target.setQueryData(["conversations", true], []);
    target.setQueryData(["project-workspaces"], []);
    for (const run of [alpha, beta]) target.setQueryData(["run", run.id], run);
    for (const conversation of conversations) {
      target.setQueryData(["conversation", conversation.conversation_id], conversation);
      target.setQueryData(["conversation-queue", conversation.conversation_id], []);
      target.setQueryData(["workspace-files", "default", conversation.workspace_path], { items: [], bundle_download_url: "" });
    }
  }
  seedClient(client);
  let currentActor = initialActor;
  vi.spyOn(api, "me").mockImplementation(async () => currentActor);
  vi.spyOn(api, "login").mockImplementation(async () => currentActor);
  let auth!: ReturnType<typeof useAuth>;
  function IdentityController() {
    auth = useAuth();
    return null;
  }
  async function changeIdentity(next: CurrentUser) {
    currentActor = next;
    await act(async () => auth.login("test", "test-password"));
  }
  const read = vi.spyOn(api, "run").mockImplementation(async (id) => id === alpha.id ? alpha : beta);
  vi.spyOn(api, "conversation").mockImplementation(async (id) => conversations.find((item) => item.conversation_id === id)!);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  const approve = vi.spyOn(api, "approveCapability");
  const reject = vi.spyOn(api, "rejectCapability");
  let navigate!: ReturnType<typeof useNavigate>;
  function Page() {
    navigate = useNavigate();
    return page === "runs" ? <RunsPage /> : <RunDetailPage />;
  }
  function renderPage(target: QueryClient) {
    return render(<QueryClientProvider client={target}>
      <AuthProvider>
      <MemoryRouter initialEntries={[page === "runs" ? "/" : "/runs/run-alpha"]}>
        <IdentityController />
        <Routes><Route path={page === "runs" ? "/" : "/runs/:runId"} element={<RequireAuth><Page /></RequireAuth>} /></Routes>
      </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>);
  }
  let view = renderPage(client);
  async function open(name: string) {
    if (page === "detail") {
      await act(async () => navigate(`/runs/run-${name}`));
    } else {
      await user.click(screen.getByRole("button", { name: new RegExp(`^进入会话 Approval ${name}`) }));
    }
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
  async function select(name: string) {
    await open(name);
    return screen.findByRole("status", { name: "沙箱权限确认" });
  }
  async function remount(nextPage = page, target = client) {
    view.unmount();
    page = nextPage;
    view = renderPage(target);
    if (page === "runs") {
      await screen.findByRole("navigation", { name: "项目与会话管理" });
      await open("alpha");
    }
    else await screen.findByRole("heading", { name: "运行详情" });
  }
  function isolatedClient() {
    const isolated = new QueryClient({
      defaultOptions: { queries: { enabled: false, retry: false, staleTime: Infinity }, mutations: { retry: false } },
    });
    clients.push(isolated);
    seedClient(isolated);
    return isolated;
  }
  if (page === "runs") {
    await screen.findByRole("navigation", { name: "项目与会话管理" });
    await select("alpha");
  }
  const card = await screen.findByRole("status", { name: "沙箱权限确认" });
  return { user, alpha, beta, client, read, approve, reject, open, select, card, remount, isolatedClient, changeIdentity, unmount: () => view.unmount() };
}

describe.each(["runs", "detail"] as const)("%s capability approval component", (page) => {
  it.each(["user_id", "tenant_id", "role"] as const)("does not inherit a success when %s changes on the same QueryClient", async (field) => {
    const { user, alpha, read, approve, card, changeIdentity } = await mountPage(page);
    approve.mockResolvedValue(accepted(alpha));
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    await changeIdentity({ ...initialActor, [field]: "other" });
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect(within(current).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    expect(within(current).queryByRole("alert")).toBeNull();
    await changeIdentity(initialActor);
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
  });

  it.each([
    ["user_id", "success"], ["user_id", "failure"],
    ["tenant_id", "success"], ["tenant_id", "failure"],
  ] as const)("isolates a late %s identity's %s from the new actor's pending decision", async (field, result) => {
    const { user, alpha, client, read, approve, reject, card, changeIdentity } = await mountPage(page);
    const first = deferred<SubmittedRun>();
    const second = deferred<SubmittedRun>();
    approve.mockReturnValue(first.promise);
    reject.mockReturnValue(second.promise);
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await changeIdentity({ ...initialActor, [field]: "other" });
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect(within(current).getByRole("button", { name: "拒绝" }).hasAttribute("disabled")).toBe(false);
    await user.click(within(current).getByRole("button", { name: "拒绝" }));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await act(async () => result === "success" ? first.resolve(accepted(alpha)) : first.reject(new Error("old identity failed")));
    expect(within(current).getByRole("button", { name: "拒绝中..." }).hasAttribute("disabled")).toBe(true);
    expect(within(current).queryByRole("alert")).toBeNull();
    expect(invalidate).not.toHaveBeenCalled();
    expect(approve).toHaveBeenCalledTimes(1);
    expect(reject).toHaveBeenCalledTimes(1);
    await act(async () => second.resolve(accepted(alpha)));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    await changeIdentity(initialActor);
    if (result === "success") expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    else expect(screen.getByRole("status", { name: "沙箱权限确认" }).textContent).toContain("old identity failed");
  });

  it.each(["success", "failure"] as const)("does not apply an unmounted actor's late %s to another actor", async (result) => {
    const { user, alpha, client, read, approve, card, changeIdentity, unmount, remount } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValue(response.promise);
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    unmount();
    await changeIdentity({ ...initialActor, user_id: "other" });
    await remount(page === "runs" ? "detail" : "runs");
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect(within(current).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await act(async () => result === "success" ? response.resolve(accepted(alpha)) : response.reject(new Error("old identity failed")));
    expect(within(current).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    expect(within(current).queryByRole("alert")).toBeNull();
    expect(invalidate).not.toHaveBeenCalled();
  });

  it.each(["same", "other"] as const)("shares pending across unmount and %s-page remount", async (destination) => {
    const { user, alpha, read, approve, reject, card, remount } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValue(response.promise);
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await remount(destination === "same" ? page : page === "runs" ? "detail" : "runs");
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect(within(current).getByRole("button", { name: "允许中..." }).hasAttribute("disabled")).toBe(true);
    for (const button of within(current).getAllByRole("button")) await user.click(button);
    expect(approve).toHaveBeenCalledExactlyOnceWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
    expect(reject).not.toHaveBeenCalled();
    await act(async () => response.resolve(accepted(alpha)));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
  });

  it.each(["same", "other"] as const)("shares success across unmount and %s-page remount while run data stays old", async (destination) => {
    const { user, alpha, read, approve, card, remount } = await mountPage(page);
    approve.mockResolvedValue(accepted(alpha));
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    await remount(destination === "same" ? page : page === "runs" ? "detail" : "runs");
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    expect(approve).toHaveBeenCalledTimes(1);
  });

  it.each(["success", "failure"] as const)("retains a %s received while unmounted when opening the other page", async (result) => {
    const { user, alpha, read, approve, reject, card, unmount, remount } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValue(response.promise);
    reject.mockResolvedValue(accepted(alpha));
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    unmount();
    await act(async () => result === "success" ? response.resolve(accepted(alpha)) : response.reject(new Error("unmounted request failed")));
    await remount(page === "runs" ? "detail" : "runs");
    if (result === "success") {
      expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    } else {
      const current = screen.getByRole("status", { name: "沙箱权限确认" });
      expect(within(current).getByRole("alert").textContent).toContain("unmounted request failed");
      await user.click(within(current).getByRole("button", { name: "拒绝" }));
      await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
      expect(reject).toHaveBeenCalledExactlyOnceWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
    }
  });

  it.each(["pending", "success"] as const)("does not share %s with a different QueryClient", async (status) => {
    const { user, alpha, read, approve, card, isolatedClient, remount } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValueOnce(response.promise).mockResolvedValueOnce(accepted(alpha));
    read.mockReturnValue(deferred<RunDetail>().promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    if (status === "success") {
      await act(async () => response.resolve(accepted(alpha)));
      await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    }
    await remount(page, isolatedClient());
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect(within(current).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    await user.click(within(current).getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    expect(approve).toHaveBeenCalledTimes(2);
    if (status === "pending") await act(async () => response.resolve(accepted(alpha)));
  });

  it.each(["approve", "reject"] as const)("keeps the same ID locked during %s when polling changes its version", async (decision) => {
    const { user, alpha, client, read, approve, reject, card } = await mountPage(page);
    read.mockImplementation(async (id) => client.getQueryData<RunDetail>(["run", id])!);
    const response = deferred<SubmittedRun>();
    const action = decision === "approve" ? approve : reject;
    const label = decision === "approve" ? "允许一次" : "拒绝";
    const pendingLabel = decision === "approve" ? "允许中..." : "拒绝中...";
    action.mockReturnValue(response.promise);
    await user.click(within(card).getByRole("button", { name: label }));
    for (const version of [8, 9]) {
      await publishRun(client, { ...alpha, version });
      const current = screen.getByRole("status", { name: "沙箱权限确认" });
      expect(within(current).getByRole("button", { name: pendingLabel }).hasAttribute("disabled")).toBe(true);
      for (const button of within(current).getAllByRole("button")) {
        expect(button.hasAttribute("disabled")).toBe(true);
        await user.click(button);
      }
    }
    expect(action).toHaveBeenCalledExactlyOnceWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
    expect(decision === "approve" ? reject : approve).not.toHaveBeenCalled();
    await act(async () => response.resolve(accepted(alpha)));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
  });

  it.each(["approve", "reject"] as const)("releases the same ID lock after %s fails and retries with the polled version", async (decision) => {
    const { user, alpha, client, read, approve, reject, card } = await mountPage(page);
    read.mockImplementation(async (id) => client.getQueryData<RunDetail>(["run", id])!);
    const response = deferred<SubmittedRun>();
    const action = decision === "approve" ? approve : reject;
    const label = decision === "approve" ? "允许一次" : "拒绝";
    action.mockReturnValueOnce(response.promise).mockResolvedValueOnce(accepted(alpha));
    await user.click(within(card).getByRole("button", { name: label }));
    await publishRun(client, { ...alpha, version: 9 });
    await act(async () => response.reject(new Error("approval request failed")));
    const current = screen.getByRole("status", { name: "沙箱权限确认" });
    expect((await within(current).findByRole("alert")).textContent).toContain("approval request failed");
    expect(within(current).getByRole("button", { name: label }).hasAttribute("disabled")).toBe(false);
    await user.click(within(current).getByRole("button", { name: label }));
    expect(action).toHaveBeenNthCalledWith(2, "run-alpha", { approval_id: "approval-alpha", version: 9 });
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
  });

  it("does not display another session's pending card when switching to a run without approval", async () => {
    const { user, alpha, beta, client, read, approve, open, select, card } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValue(response.promise);
    const idle = { ...beta, status: "running", explicit_details: { project_id: "default" } };
    await publishRun(client, idle);
    read.mockImplementation(async (id) => id === alpha.id ? alpha : idle);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await open("beta");
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    const original = await select("alpha");
    expect(within(original).getByRole("button", { name: "允许中..." }).hasAttribute("disabled")).toBe(true);
    await act(async () => response.resolve(accepted(alpha)));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
  });

  it("shows each new action separately without approving it automatically", async () => {
    const { user, alpha, client, approve, card } = await mountPage(page);
    approve.mockResolvedValue(accepted(alpha));
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    for (const [index, action] of ["workspace.write_text", "project.verify", "workspace.bundle"].entries()) {
      const nextRun = approvalRun("alpha", `approval-${action}`, 9 + index * 2);
      nextRun.failure_diagnostics[0].reason = "工程准入确认";
      nextRun.failure_diagnostics[0].tool_name = action;
      await publishRun(client, nextRun);
      const next = await screen.findByRole("status", { name: "沙箱权限确认" });
      expect(within(next).getByText("工程准入确认")).toBeTruthy();
      expect(within(next).getByText(action)).toBeTruthy();
      expect(within(next).getByText(`审批 approval-${action}`)).toBeTruthy();
      expect(approve).toHaveBeenCalledTimes(index + 1);
      await user.click(within(next).getByRole("button", { name: "允许一次" }));
      await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
      expect(approve).toHaveBeenLastCalledWith("run-alpha", { approval_id: `approval-${action}`, version: 9 + index * 2 });
    }
  });

  it("does not revive a successful approval ID when the run version changes", async () => {
    const { user, alpha, client, read, approve, card } = await mountPage(page);
    read.mockImplementation(async (id) => client.getQueryData<RunDetail>(["run", id])!);
    approve.mockResolvedValue(accepted(alpha));
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    await publishRun(client, { ...alpha, version: 8 });
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
  });

  it("keeps the clicked version fixed when a new approval arrives before its response", async () => {
    const { user, alpha, client, read, approve, card } = await mountPage(page);
    read.mockImplementation(async (id) => client.getQueryData<RunDetail>(["run", id])!);
    const response = deferred<SubmittedRun>();
    approve.mockReturnValue(response.promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    await publishRun(client, approvalRun("alpha", "approval-new", 11));
    const next = await screen.findByRole("status", { name: "沙箱权限确认" });
    await waitFor(() => expect(within(next).getByText("审批 approval-new")).toBeTruthy());
    expect(within(next).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    await act(async () => response.resolve(accepted(alpha)));
    expect(within(next).getByText("Confirm approval-new")).toBeTruthy();
    expect(within(next).queryByRole("alert")).toBeNull();
    expect(approve).toHaveBeenCalledExactlyOnceWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
  });

  it.each(["approve", "reject"] as const)("does not resurrect a successful %s while old detail refreshes slowly", async (decision) => {
    const { user, alpha, client, read, approve, reject, card } = await mountPage(page);
    const response = deferred<SubmittedRun>();
    const refresh = deferred<RunDetail>();
    const action = decision === "approve" ? approve : reject;
    action.mockReturnValue(response.promise);
    read.mockReturnValue(refresh.promise);
    await user.click(within(card).getByRole("button", { name: decision === "approve" ? "允许一次" : "拒绝" }));
    expect(within(card).getByRole("button", { name: decision === "approve" ? "允许中..." : "拒绝中..." }).hasAttribute("disabled")).toBe(true);
    expect(action).toHaveBeenCalledWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
    await act(async () => response.resolve(accepted(alpha)));
    await waitFor(() => expect(read).toHaveBeenCalled());
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    await act(async () => refresh.resolve({ ...alpha }));
    await publishRun(client, { ...alpha, cost_usd: "0.01" });
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    await publishRun(client, approvalRun("alpha", "approval-next", 9));
    const next = await screen.findByRole("status", { name: "沙箱权限确认" });
    expect(within(next).getByText("审批 approval-next")).toBeTruthy();
    expect(within(next).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    expect(action).toHaveBeenCalledTimes(1);
  });

  it.each(["approve", "reject"] as const)("keeps a failed %s visible with an error inside that card and allows retry", async (decision) => {
    const { user, alpha, approve, reject, card } = await mountPage(page);
    const action = decision === "approve" ? approve : reject;
    action.mockRejectedValueOnce(new Error("approval service unavailable")).mockResolvedValueOnce(accepted(alpha));
    const label = decision === "approve" ? "允许一次" : "拒绝";
    await user.click(within(card).getByRole("button", { name: label }));
    const error = await within(card).findByRole("alert");
    expect(error.textContent).toContain("approval service unavailable");
    expect(within(card).getByRole("button", { name: label }).hasAttribute("disabled")).toBe(false);
    await user.click(within(card).getByRole("button", { name: label }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    expect(action).toHaveBeenCalledTimes(2);
  });

  it.each(["success", "failure"] as const)("isolates a late %s from a new request after switching sessions", async (result) => {
    const { user, alpha, beta, approve, open, select, card } = await mountPage(page);
    const first = deferred<SubmittedRun>();
    const second = deferred<SubmittedRun>();
    approve.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await user.click(within(card).getByRole("button", { name: "允许一次" }));
    const next = await select("beta");
    await waitFor(() => expect(within(next).getByText("审批 approval-beta")).toBeTruthy());
    expect(within(next).getByRole("button", { name: "允许一次" }).hasAttribute("disabled")).toBe(false);
    await user.click(within(next).getByRole("button", { name: "允许一次" }));
    await act(async () => result === "success" ? first.resolve(accepted(alpha)) : first.reject(new Error("alpha failed")));
    expect(within(screen.getByRole("status", { name: "沙箱权限确认" })).getByText("审批 approval-beta")).toBeTruthy();
    expect(within(next).getByRole("button", { name: "允许中..." }).hasAttribute("disabled")).toBe(true);
    expect(within(next).queryByRole("alert")).toBeNull();
    expect(approve).toHaveBeenNthCalledWith(2, "run-beta", { approval_id: "approval-beta", version: 7 });
    await act(async () => second.resolve(accepted(beta)));
    await waitFor(() => expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull());
    if (result === "failure") {
      await select("alpha");
      expect((await screen.findByRole("status", { name: "沙箱权限确认" })).textContent).toContain("alpha failed");
    } else {
      await open("alpha");
      expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
    }
  });
});

it("atomically claims one decision when both pages click the same approval together", async () => {
  const { alpha, client, approve, reject, card } = await mountPage("runs");
  const response = deferred<SubmittedRun>();
  approve.mockReturnValue(response.promise);
  reject.mockResolvedValue(accepted(alpha));
  const detail = render(<QueryClientProvider client={client}>
    <AuthProvider>
    <MemoryRouter initialEntries={["/runs/run-alpha"]}>
      <Routes><Route path="/runs/:runId" element={<RequireAuth><RunDetailPage /></RequireAuth>} /></Routes>
    </MemoryRouter>
    </AuthProvider>
  </QueryClientProvider>);
  const otherCard = await within(detail.container).findByRole("status", { name: "沙箱权限确认" });
  const allow = within(card).getByRole("button", { name: "允许一次" });
  const deny = within(otherCard).getByRole("button", { name: "拒绝" });
  await act(async () => { allow.click(); deny.click(); });
  expect(approve).toHaveBeenCalledExactlyOnceWith("run-alpha", { approval_id: "approval-alpha", version: 7 });
  expect(reject).not.toHaveBeenCalled();
  expect(within(otherCard).getByRole("button", { name: "允许中..." }).hasAttribute("disabled")).toBe(true);
  await act(async () => response.resolve(accepted(alpha)));
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "沙箱权限确认" })).toHaveLength(0));
});
