import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api, type Conversation, type RunDetail } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { RunsPage } from "./RunsPage";

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function conversation(id: string, project = "bound-project", workspace = "bound-session"): Conversation {
  return { conversation_id: id, title: id, project_id: project, workspace_path: workspace, runs: [] };
}

async function mount(initialId: string | null, listed: Conversation[] = []) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const detail = deferred<Conversation>();
  vi.spyOn(api, "me").mockResolvedValue({
    tenant_id: "tenant", user_id: "ordinary", username: "ordinary", role: "operator",
    permissions: ["run:create", "run:read", "config:read"],
  });
  vi.spyOn(api, "runs").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue([]);
  vi.spyOn(api, "conversations").mockImplementation(async (archived) => archived ? [] : listed);
  vi.spyOn(api, "conversation").mockImplementation(() => detail.promise);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
  vi.spyOn(api, "executionBackends").mockResolvedValue([]);
  vi.spyOn(api, "settings").mockImplementation(() => new Promise(() => {}));
  vi.spyOn(api, "mainAgent").mockImplementation(() => new Promise(() => {}));
  const files = vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  const fetch = vi.fn(() => Promise.reject(new Error("Unexpected HTTP request")));
  vi.stubGlobal("fetch", fetch);
  let navigate!: ReturnType<typeof useNavigate>;
  function Page() {
    navigate = useNavigate();
    return <RunsPage />;
  }
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={[initialId ? `/?conversation=${initialId}` : "/"]}><Page /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await screen.findByRole("form", { name: "发送消息" });
  if (initialId) await waitFor(() => expect(api.conversation).toHaveBeenCalledWith(initialId));
  await act(async () => {});
  return { client, detail, files, fetch, navigate: (id: string) => navigate(`/?conversation=${id}`) };
}

it("waits for a complete bound scope instead of requesting default/<conversation>/files", async () => {
  const { detail, files, fetch } = await mount("deep-link");
  expect(files).not.toHaveBeenCalled();
  await act(async () => detail.resolve(conversation("deep-link")));
  await waitFor(() => expect(files).toHaveBeenCalledWith("bound-project", "bound-session"));
  expect(files.mock.calls.every(([project, session]) => project === "bound-project" && session === "bound-session")).toBe(true);
  expect(fetch).not.toHaveBeenCalled();
});

it.each([
  { project: "bound-project", workspace: "" },
  { project: "", workspace: "bound-session" },
])("does not combine incomplete metadata with local fallbacks ($project/$workspace)", async ({ project, workspace }) => {
  const { detail, files } = await mount("partial");
  await act(async () => detail.resolve(conversation("partial", project, workspace)));
  await waitFor(() => expect(screen.getByRole("form", { name: "发送消息" })).toBeTruthy());
  expect(files).not.toHaveBeenCalled();
});

it("uses an existing project's complete list metadata while detail is pending", async () => {
  const { files } = await mount("existing", [conversation("existing", "existing-project", "existing-session")]);
  await waitFor(() => expect(files).toHaveBeenCalledWith("existing-project", "existing-session"));
  expect(files.mock.calls.every(([project, session]) => project === "existing-project" && session === "existing-session")).toBe(true);
});

it("does not borrow a project from list metadata to complete a partial detail scope", async () => {
  const { detail, files } = await mount("split", [conversation("split", "list-project", "")]);
  await act(async () => detail.resolve(conversation("split", "", "detail-session")));
  expect(files).not.toHaveBeenCalled();
});

it("rejects detail metadata belonging to another conversation", async () => {
  const { detail, files } = await mount("requested");
  await act(async () => detail.resolve(conversation("foreign")));
  expect(files).not.toHaveBeenCalled();
});

it("waits on a switched conversation and then reads only its own scope", async () => {
  const { client, files, navigate } = await mount("first", [conversation("first", "first-project", "first-session")]);
  await waitFor(() => expect(files).toHaveBeenCalledWith("first-project", "first-session"));
  files.mockClear();
  await act(async () => navigate("second"));
  await waitFor(() => expect(api.conversation).toHaveBeenCalledWith("second"));
  expect(files).not.toHaveBeenCalled();
  await act(async () => { client.setQueryData(["conversation", "second"], conversation("second", "second-project", "second-session")); });
  await waitFor(() => expect(files).toHaveBeenCalledWith("second-project", "second-session"));
  expect(files.mock.calls.every(([project, session]) => project === "second-project" && session === "second-session")).toBe(true);
});

it("enables a newly created conversation immediately from its complete cached metadata", async () => {
  const { client, files, navigate } = await mount(null);
  expect(files).not.toHaveBeenCalled();
  await act(async () => {
    client.setQueryData(["conversation", "new-conversation"], conversation("new-conversation", "new-project", "new-session"));
    navigate("new-conversation");
  });
  await waitFor(() => expect(files).toHaveBeenCalledWith("new-project", "new-session"));
});

it("removes cached conversation history and open minutes when access is revoked", async () => {
  const user = userEvent.setup();
  const { client, detail } = await mount("revoked");
  const createdAt = "2026-11-09T00:00:00Z";
  const request = "Previously visible conversation request";
  const run: RunDetail = {
    id: "revoked-run", conversation_id: "revoked", status: "completed", mode: "discuss",
    request, created_at: createdAt, version: 1, queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
    explicit_details: {}, artifacts: [], failure_diagnostics: [], tool_lifecycle: [],
    model_outcome_summary: {
      completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [],
    },
    orchestration_protocol_summary: null,
    events: ["engineer", "reviewer"].map((actor, index) => ({
      kind: "message.created", actor, sequence: index + 1, created_at: createdAt,
      message: "Previously visible role statement", participants: [],
      payload: { role_contribution: {
        type: "role_contribution", schema_version: 1, status: "proposed", actor,
        source_sequence: index + 1, position: "approve", claims: ["Previously visible role statement"],
      } },
    })),
  };
  const original = { ...conversation("revoked"), runs: [run] };
  await act(async () => detail.resolve(original));
  await user.click(await screen.findByRole("button", { name: /Agent 工作席/ }));
  expect(screen.getByRole("dialog", { name: "Agent 工作席详情" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "会议纪要" })).toBeTruthy();
  expect(screen.getByText(request)).toBeTruthy();

  const refresh = async (status: number, message: string) => {
    vi.mocked(api.conversation).mockRejectedValueOnce(new ApiError(message, status));
    await act(async () => { await client.refetchQueries({ queryKey: ["conversation", "revoked"], exact: true }); });
    await screen.findByText(message, { exact: false });
  };
  await refresh(503, "Temporary read failure");
  expect(screen.getByRole("region", { name: "会议纪要" })).toBeTruthy();

  await refresh(403, "Conversation access revoked");
  expect(screen.queryByRole("dialog", { name: "Agent 工作席详情" })).toBeNull();
  expect(screen.queryByText(request)).toBeNull();
  expect(screen.queryByText("Previously visible role statement")).toBeNull();

  await act(async () => { client.setQueryData(["conversation", "revoked"], original); });
  await refresh(503, "Still waiting for an authorized read");
  expect(screen.queryByText(request)).toBeNull();

  await act(async () => { await client.refetchQueries({ queryKey: ["conversation", "revoked"], exact: true }); });
  expect(await screen.findByText(request)).toBeTruthy();
  expect(screen.queryByRole("dialog", { name: "Agent 工作席详情" })).toBeNull();

  // An explicit run denial must also win over the cached copy in a readable conversation.
  await user.click(screen.getByRole("button", { name: /Agent 工作席/ }));
  vi.spyOn(api, "run").mockRejectedValueOnce(new ApiError("Run no longer visible", 404));
  await act(async () => {
    await expect(client.fetchQuery({
      queryKey: ["run", run.id], queryFn: () => api.run(run.id),
    })).rejects.toMatchObject({ status: 404 });
  });
  await waitFor(() => expect(screen.queryByText(request)).toBeNull());
  expect(screen.queryByRole("dialog", { name: "Agent 工作席详情" })).toBeNull();
});
