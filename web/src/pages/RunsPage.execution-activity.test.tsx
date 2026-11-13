import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type RunDetail, type SystemSettings } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { mergeConversationRuns, runExecutionDeadlineElapsed, runExecutionIsActive, RunsPage } from "./RunsPage";

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach(c => c.clear()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const expired = { is_active: false, reason: "deadline_elapsed" as const, deadline_at: "2026-11-12T21:50:39.884148Z", evaluated_at: "2026-11-13T04:00:00Z" };
function historicalRun(): RunDetail {
  return Object.assign({
    id: "historical", status: "waiting_approval", mode: "discuss", version: 15,
    conversation_id: "existing", request: "Retained old request", created_at: "2026-11-12T20:50:39Z",
    queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0", events: [], artifacts: [],
    explicit_details: { approval_kind: "capability_tool", approval_id: "old-approval" },
    failure_diagnostics: [{ category: "approval", stage: "approval.requested", approval_id: "old-approval",
      reason: "Retained genuine pending action", recommendation: "Only this invocation", sequence: 1 }],
    tool_lifecycle: [], orchestration_protocol_summary: null,
    model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
  } as RunDetail, { execution_activity: expired });
}

it("keeps authoritative activity through the real client Zod reader", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([historicalRun()]), { status: 200 })));
  const [run] = await api.runs();
  expect((run as unknown as { execution_activity: unknown }).execution_activity).toEqual(expired);
});

it("refreshes activity at the same historical run version without losing progress", () => {
  const next = historicalRun();
  const previous = Object.assign({}, next, { execution_activity: { ...expired, is_active: true, reason: "active", evaluated_at: "2026-11-12T21:40:00Z" } });
  const history = [previous];
  const merged = mergeConversationRuns(history, [next]);
  expect(merged).not.toBe(history);
  expect((merged[0] as typeof next).execution_activity).toEqual(expired);
  expect(merged[0].status).toBe("waiting_approval");
  expect(merged[0].version).toBe(15);
});

it("keeps the known inactive projection when an older or legacy response arrives", () => {
  const current = historicalRun();
  const { execution_activity: _activity, ...legacy } = current;
  const stale = { ...current, execution_activity: { ...expired, is_active: true, reason: "active" as const, evaluated_at: "2026-11-12T21:40:00Z" } };
  expect(runExecutionIsActive(mergeConversationRuns([current], [legacy])[0])).toBe(false);
  expect(runExecutionIsActive(mergeConversationRuns([current], [stale])[0])).toBe(false);
});

it("does not update React history merely because evaluated_at advanced", () => {
  const current = historicalRun(); const history = [current];
  expect(mergeConversationRuns(history, [{ ...current, execution_activity: { ...expired, evaluated_at: "2026-11-13T04:01:00Z" } }])).toBe(history);
});

it("binds activity to the newer run version even when an old response was evaluated later", () => {
  const old = historicalRun();
  const held = { ...old, version: 16, execution_activity: { ...expired, is_active: true,
    reason: "worker_not_released" as const, evaluated_at: "2026-11-13T03:30:00Z" } };
  expect(mergeConversationRuns([held], [old])[0]).toBe(held);
  expect(runExecutionIsActive(mergeConversationRuns([held], [old])[0])).toBe(true);
  const legacyNewer = { ...held, execution_activity: undefined };
  expect(runExecutionIsActive(mergeConversationRuns([legacyNewer], [old])[0])).toBe(true);
});

it("keeps legacy waiting conservative and does not label a terminal repair as expired", () => {
  const { execution_activity: _activity, ...legacy } = historicalRun();
  expect(runExecutionIsActive(legacy)).toBe(true);
  expect(runExecutionDeadlineElapsed({ ...historicalRun(), status: "failed", execution_activity: { ...expired, reason: "terminal" } })).toBe(false);
});

async function mount(run = historicalRun()) {
  const conversation = { conversation_id: "existing", title: "Existing", project_id: "project",
    workspace_path: "session", created_at: "2026-11-12T20:00:00Z", runs: [run] };
  vi.spyOn(api, "me").mockResolvedValue({ tenant_id: "tenant", user_id: "operator", username: "operator", role: "operator", permissions: ["run:create", "run:read", "config:read"] });
  vi.spyOn(api, "runs").mockResolvedValue([run]);
  vi.spyOn(api, "conversation").mockResolvedValue(conversation);
  vi.spyOn(api, "run").mockResolvedValue(run);
  vi.spyOn(api, "conversations").mockResolvedValue([conversation]);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue([]);
  vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
  vi.spyOn(api, "executionBackends").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  vi.spyOn(api, "settings").mockResolvedValue({ default_mode: "auto", default_workflow_id: null,
    default_agent_ids: [], default_permission_mode: "default", default_sandbox_profile: "workspace_write",
    supported_permission_modes: ["default"], tool_approval_mode: "ask", log_level: "warning",
    hermes_enabled: false, safe_tools_enabled: true, require_approval_for_tools: true,
    channel_entry: "web", attachment_retention_days: 7, attachment_max_mb: 10,
    default_execution_backend: "systemd", allow_main_agent_override: false, allow_temporary_agents: false,
    temporary_agent_policy: "Ask before adding a role", vibe_coding_enabled: false,
    multimedia_generation_enabled: false, openclaw_enabled: false, openclaw_mode: "ask",
    openclaw_allowed_commands: [], openclaw_remote_adapters: [], plugin_package_subprocess_registration_status: null,
  } satisfies SystemSettings);
  vi.spyOn(api, "mainAgent").mockImplementation(() => new Promise(() => {}));
  vi.spyOn(api, "capabilityApprovals").mockResolvedValue({
    ready: true, conversation_approval_available: true, conversation_approval_id: "old-approval", conversation_approval_version: 15,
    items: [{ kind: "approval.requested", sequence: 1, run_id: run.id, approval_id: "old-approval", created_at: run.created_at!, payload: {} }],
  });
  const create = vi.spyOn(api, "createRun").mockImplementation(() => new Promise(() => {}));
  const queue = vi.spyOn(api, "queueConversationMessage").mockImplementation(() => new Promise(() => {}));
  const approve = vi.spyOn(api, "approveCapability");
  const choose = vi.spyOn(api, "chooseMode");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={["/?conversation=existing"]}><RunsPage /></MemoryRouter></AuthProvider></QueryClientProvider>);
  await waitFor(() => expect(api.conversation).toHaveBeenCalledWith("existing"));
  return { client, create, queue, approve, choose };
}

it("stops fast polling an expired conversation and its queue without losing invalidation", async () => {
  const { client } = await mount();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  const conversationReads = vi.mocked(api.conversation).mock.calls.length;
  const queueReads = vi.mocked(api.conversationQueue).mock.calls.length;
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1200)); });
  expect(api.conversation).toHaveBeenCalledTimes(conversationReads);
  expect(api.conversationQueue).toHaveBeenCalledTimes(queueReads);
  await act(async () => { await client.invalidateQueries({ queryKey: ["conversation", "existing"] }); });
  expect(vi.mocked(api.conversation).mock.calls.length).toBeGreaterThan(conversationReads);
});

it("enables mode and sends a new run instead of queueing or approving expired history", async () => {
  const run = historicalRun(); const { create, queue, approve } = await mount(run);
  const mode = await screen.findByRole("button", { name: "模式：自动" });
  await waitFor(() => expect((mode as HTMLButtonElement).disabled).toBe(false));
  const user = userEvent.setup(); await user.click(mode);
  await user.click(screen.getByRole("menuitemradio", { name: "讨论" }));
  await user.type(screen.getByRole("textbox", { name: "消息" }), "Build the new deliverable");
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  expect(create.mock.calls[0][0]).toMatchObject({ conversation_id: "existing", mode: "discuss" });
  expect(queue).not.toHaveBeenCalled(); expect(approve).not.toHaveBeenCalled();
  expect(run.status).toBe("waiting_approval"); expect(run.version).toBe(15);
  expect(screen.getByText("Retained old request")).toBeTruthy();
});

it("invalidates an already selected mode proposal when conversation expires but selectedRun stays stale", async () => {
  const { client, create, queue, approve, choose } = await mount();
  const selected: RunDetail = { ...historicalRun(), id: "selected-mode", status: "waiting_user_mode",
    mode: "auto", decision_token: "genuine-mode-token", explicit_details: {},
    created_at: "2026-11-13T03:00:00Z",
    execution_activity: { ...expired, is_active: true, reason: "active", evaluated_at: "2026-11-13T03:30:00Z" } };
  vi.mocked(api.run).mockResolvedValue(selected);
  create.mockResolvedValueOnce({ id: selected.id, tenant_id: "tenant", conversation_id: "existing",
    status: "waiting_user_mode", mode: "auto", version: 15, decision_token: "genuine-mode-token",
    clarification_reason: "Select a mode", execution_backend: "systemd", requested_permissions: [] });
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox", { name: "消息" }), "First new request");
  await user.click(within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: "发送" }));
  await screen.findByRole("button", { name: "确认" });
  const elapsed = { ...selected, execution_activity: expired };
  vi.mocked(api.conversation).mockResolvedValue({ conversation_id: "existing", title: "Existing",
    project_id: "project", workspace_path: "session", created_at: "2026-11-12T20:00:00Z", runs: [historicalRun(), elapsed] });
  // Intentionally leave api.run at its older active projection.
  await act(async () => { await client.refetchQueries({ queryKey: ["conversation", "existing"] }); });
  await waitFor(() => expect(within(screen.getByRole("form", { name: "发送消息" })).queryByRole("button", { name: "确认" })).toBeNull());
  await user.type(screen.getByRole("textbox", { name: "消息" }), "A new independent task");
  await user.click(within(screen.getByRole("form", { name: "发送消息" })).getByRole("button", { name: "发送" }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(choose).not.toHaveBeenCalled(); expect(queue).not.toHaveBeenCalled(); expect(approve).not.toHaveBeenCalled();
  expect(selected.decision_token).toBe("genuine-mode-token"); expect(selected.version).toBe(15);
});
