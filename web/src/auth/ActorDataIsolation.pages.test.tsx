import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type Conversation, type CurrentUser, type RunDetail } from "../api/client";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsPage } from "../pages/RunsPage";
import { AuthProvider, RequireAuth, useAuth } from "./AuthProvider";

const actor: CurrentUser = { tenant_id: "tenant-a", user_id: "actor-a", role: "operator", username: "a", permissions: ["*"] };
const secretRun: RunDetail = {
  id: "private-run", conversation_id: "private-conversation", request: "A_PRIVATE_REQUEST",
  status: "waiting_approval", mode: "direct", version: 7, created_at: "2026-10-24T00:00:00Z",
  cost_usd: "0", queue_wait_ms: 0, capacity_wait_ms: 0, events: [], artifacts: [], tool_lifecycle: [],
  explicit_details: { project_id: "private-project", approval_kind: "capability_tool", approval_id: "A_PRIVATE_APPROVAL" },
  failure_diagnostics: [{ category: "approval", stage: "approval.requested", approval_id: "A_PRIVATE_APPROVAL",
    reason: "A_PRIVATE_DIAGNOSTIC", recommendation: "A_PRIVATE_RECOMMENDATION", sequence: 1 }],
  orchestration_protocol_summary: null, model_outcome_summary: { completion_count: 0, fallback_used: false,
    fallback_attempt_count: 0, requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
};
const conversation: Conversation = {
  conversation_id: "private-conversation", title: "A_PRIVATE_CONVERSATION", project_id: "private-project",
  project_label: "A_PRIVATE_PROJECT", workspace_path: "main", archived_at: null,
  created_at: secretRun.created_at, updated_at: secretRun.created_at, runs: [secretRun],
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  void promise.catch(() => {});
  return { promise, resolve, reject };
}
const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); });

function assertNoPrivateContent() {
  expect(document.body.textContent).not.toMatch(/A_PRIVATE_|A_LATE_PRIVATE_/);
  expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
}

it.each([401, 403, 404])("does not restore denied details from a late control response: %s", async (status) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  let denied = false;
  const running = { ...secretRun, status: "running" };
  const pendingControl = deferred<RunDetail>();
  vi.spyOn(api, "me").mockResolvedValue(actor);
  const read = vi.spyOn(api, "run").mockImplementation(async () => {
    if (denied) throw new ApiError("Access revoked", status);
    return running;
  });
  const pause = vi.spyOn(api, "pauseRun").mockReturnValue(pendingControl.promise);
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/runs/private-run"]}>
    <AuthProvider><Routes><Route path="/runs/:runId" element={<RunDetailPage />} /></Routes></AuthProvider>
  </MemoryRouter></QueryClientProvider>);
  await screen.findAllByText("A_PRIVATE_REQUEST");
  await userEvent.click(screen.getByRole("button", { name: "暂停" }));
  await waitFor(() => expect(pause).toHaveBeenCalledOnce());
  denied = true;
  await act(async () => { await client.refetchQueries({ queryKey: ["run", secretRun.id] }); });
  await screen.findByRole("alert");
  assertNoPrivateContent();
  await act(async () => pendingControl.resolve({ ...running, status: "paused" }));
  assertNoPrivateContent();
  expect(client.getQueryCache().find({ queryKey: ["run", secretRun.id], exact: false })?.state.data).toBeUndefined();
  expect(read).toHaveBeenCalledTimes(2);
  denied = false;
  await act(async () => { await client.refetchQueries({ queryKey: ["run", secretRun.id] }); });
  await screen.findAllByText("A_PRIVATE_REQUEST");
});

describe.each(["detail", "conversation"] as const)("actor data boundary on %s", (page) => {
  it.each([401, 403, 404])("removes already cached private content after current access is denied with %s", async (status) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
    clients.push(client);
    let denied = false;
    function value<T>(data: T): Promise<T> {
      return denied ? Promise.reject(new ApiError("Access revoked", status)) : Promise.resolve(data);
    }
    vi.spyOn(api, "me").mockResolvedValue(actor);
    vi.spyOn(api, "run").mockImplementation(() => value(secretRun));
    vi.spyOn(api, "runs").mockImplementation(() => value([secretRun]));
    vi.spyOn(api, "conversation").mockImplementation(() => value(conversation));
    vi.spyOn(api, "conversations").mockImplementation(async (archived) => archived ? [] : [conversation]);
    vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
    vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
    vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ items: [], ready: false,
      conversation_approval_available: false, conversation_approval_id: null, conversation_approval_version: null });
    vi.spyOn(api, "agents").mockResolvedValue([]);
    vi.spyOn(api, "models").mockResolvedValue([]);
    vi.spyOn(api, "workflows").mockResolvedValue([]);
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[
      page === "detail" ? "/runs/private-run" : "/?conversation=private-conversation",
    ]}><AuthProvider><Routes>
      <Route path="/runs/:runId" element={<RunDetailPage />} />
      <Route path="/" element={<RunsPage />} />
    </Routes></AuthProvider></MemoryRouter></QueryClientProvider>);
    await screen.findAllByText("A_PRIVATE_REQUEST");
    denied = true;
    await act(async () => { await client.refetchQueries({ predicate: query =>
      ["run", "runs", "conversation"].includes(String(query.queryKey[0])) }); });
    await screen.findAllByRole("alert");
    assertNoPrivateContent();
  });

  it.each([403, 404])("does not expose A while B is pending or denied with %s, including a late A read", async (status) => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
    clients.push(client);
    let serverActor = "a";
    let refreshA = false;
    const oldRead = deferred<RunDetail>();
    const oldConversation = deferred<Conversation>();
    const newRead = deferred<RunDetail>();
    const newList = deferred<RunDetail[]>();
    const newConversation = deferred<Conversation>();
    vi.spyOn(api, "me").mockResolvedValue(actor);
    vi.spyOn(api, "logout").mockImplementation(async () => { serverActor = "none"; });
    const login = deferred<CurrentUser>();
    vi.spyOn(api, "login").mockImplementation(async () => { const next = await login.promise; serverActor = "b"; return next; });
    const runRead = vi.spyOn(api, "run").mockImplementation(() => serverActor === "a"
      ? refreshA ? oldRead.promise : Promise.resolve(secretRun) : newRead.promise);
    const listRead = vi.spyOn(api, "runs").mockImplementation(() => serverActor === "a" ? Promise.resolve([secretRun]) : newList.promise);
    vi.spyOn(api, "conversations").mockImplementation(async (archived) => serverActor === "a" && !archived ? [conversation] : []);
    const conversationRead = vi.spyOn(api, "conversation").mockImplementation(() => serverActor === "a"
      ? refreshA ? oldConversation.promise : Promise.resolve(conversation) : newConversation.promise);
    vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
    vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
    vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ items: [], ready: false, conversation_approval_available: false });
    vi.spyOn(api, "agents").mockResolvedValue([]);
    vi.spyOn(api, "models").mockResolvedValue([]);
    vi.spyOn(api, "workflows").mockResolvedValue([]);
    let currentClient = client;
    const target = page === "detail" ? "/runs/private-run" : "/?conversation=private-conversation";
    function Controls() {
      const auth = useAuth();
      const navigate = useNavigate();
      currentClient = useQueryClient();
      return <><button onClick={() => void auth.logout()}>Sign out</button>
        <button onClick={() => void auth.login("b", "fixture").then(() => navigate(target))}>Sign in B</button></>;
    }
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[target]}><AuthProvider>
      <Controls /><Routes>
        <Route path="/login" element={<p>Signed out</p>} />
        <Route path="/runs/:runId" element={<RequireAuth><RunDetailPage /></RequireAuth>} />
        <Route path="/" element={<RequireAuth><RunsPage /></RequireAuth>} />
      </Routes>
    </AuthProvider></MemoryRouter></QueryClientProvider>);
    await screen.findAllByText("A_PRIVATE_REQUEST");
    await screen.findByRole("status", { name: "沙箱权限确认" });
    // Keep a real old observer request pending across logout, without relying on cancellation support in the API mock.
    refreshA = true;
    const oldRequest = client.refetchQueries({ queryKey: page === "detail" ? ["run", secretRun.id] : ["conversation", conversation.conversation_id] });
    if (page === "detail") await waitFor(() => expect(runRead.mock.calls.length).toBeGreaterThan(1));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByText("Signed out");
    assertNoPrivateContent();
    await user.click(screen.getByRole("button", { name: "Sign in B" }));
    assertNoPrivateContent();
    await act(async () => login.resolve({ ...actor, user_id: "actor-b", username: "b" }));
    await waitFor(() => expect(page === "detail" ? runRead : listRead).toHaveBeenCalled());
    assertNoPrivateContent();
    await act(async () => {
      newRead.reject(new ApiError("B denied", status));
      newList.reject(new ApiError("B denied", status));
      newConversation.reject(new ApiError("B denied", status));
    });
    await screen.findByRole("alert");
    assertNoPrivateContent();
    await act(async () => {
      oldRead.resolve({ ...secretRun, request: "A_LATE_PRIVATE_REQUEST" });
      oldConversation.resolve({ ...conversation, title: "A_LATE_PRIVATE_CONVERSATION" });
      await oldRequest;
    });
    assertNoPrivateContent();
    expect(JSON.stringify(currentClient.getQueryCache().getAll().map((query) => query.state.data)))
      .not.toMatch(/A_PRIVATE_|A_LATE_PRIVATE_/);
    expect(conversationRead).toBeDefined();
  });
});
