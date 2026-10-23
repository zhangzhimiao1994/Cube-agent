import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type CapabilityApprovalEvent, type CurrentUser, type RunDetail } from "../api/client";
import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { RunApprovalHistory } from "./RunApprovalHistory";

const actor: CurrentUser = {
  tenant_id: "tenant-a", user_id: "actor-a", username: "test", role: "operator", permissions: ["run:read"],
};
const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function run(id = "run-a"): RunDetail {
  return {
    id, conversation_id: "conv-a", request: "Create a project", status: "cancelled", mode: "direct",
    created_at: "2026-10-24T00:00:00Z", version: 7, queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
    events: [], artifacts: [], tool_lifecycle: [], failure_diagnostics: [], orchestration_protocol_summary: null,
    explicit_details: {}, model_outcome_summary: { completion_count: 0, fallback_used: false,
      fallback_attempt_count: 0, requested_logical_models: [], actual_logical_models: [],
      attempted_logical_models: [], provider_ids: [] },
  };
}

function events(runId = "run-a", approvalId = "approval-a"): CapabilityApprovalEvent[] {
  return [
    { kind: "approval.requested", sequence: 4, run_id: runId, approval_id: approvalId,
      action: "capability_tool", created_at: "2026-10-24T00:00:00Z", payload: {} },
    { kind: "approval.resolved", sequence: 5, run_id: runId, approval_id: approvalId,
      decision: "approved", created_at: "2026-10-24T00:01:00Z", payload: {} },
  ];
}

function mount(runs: RunDetail[], identity = actor) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  vi.spyOn(api, "me").mockResolvedValue(identity);
  let auth!: ReturnType<typeof useAuth>;
  function Content() {
    auth = useAuth();
    return <>{runs.map((item) => <RunApprovalHistory key={item.id} run={item} />)}</>;
  }
  const tree = () => <QueryClientProvider client={client}><AuthProvider><Content /></AuthProvider></QueryClientProvider>;
  const view = render(tree());
  return { client, view, refresh: () => view.rerender(tree()), login: () => auth.login("test", "test-password") };
}

describe("durable per-run approval history", () => {
  it("reads every run independently, including terminal runs with no worker approval events", async () => {
    const read = vi.spyOn(api, "capabilityApprovals").mockImplementation(async (id) => ({
      items: events(id, `approval-${id}`), ready: false,
    }));
    mount([run(), run("run-b")]);
    await waitFor(() => expect(screen.getAllByRole("article", { name: "权限审批结果" })).toHaveLength(2));
    expect(read.mock.calls.map(([id]) => id).sort()).toEqual(["run-a", "run-b"]);
    expect(screen.getByText("approval-run-a")).toBeTruthy();
    expect(screen.getByText("approval-run-b")).toBeTruthy();
  });

  it("deduplicates mirrored legacy results without merging independent sequence spaces", async () => {
    const item = run();
    item.events = events().map((event) => ({ ...event, participants: [], message: event.kind, sequence: event.sequence + 90 }));
    const later = events("run-a", "approval-b").map((event) => ({ ...event, sequence: event.sequence + 2,
      created_at: event.kind === "approval.resolved" ? "2026-10-24T00:02:00Z" : event.created_at }));
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ ready: false, items: [...later, ...events(), ...events()] });
    mount([item]);
    await waitFor(() => expect(screen.getAllByRole("article", { name: "权限审批结果" })).toHaveLength(2));
    const records = screen.getAllByRole("article", { name: "权限审批结果" });
    expect(records.map((record) => record.querySelector("code")?.textContent)).toEqual(["approval-a", "approval-b"]);
    expect(records[0].querySelector("time")?.dateTime).toBe("2026-10-24T00:01:00Z");
  });

  it("keeps access-confirmed legacy results during a later read failure without claiming execution success", async () => {
    const item = run();
    item.events = events().map((event) => ({ ...event, participants: [], message: "SECRET_RAW_COMMAND",
      payload: { arguments: { command: "SECRET_RAW_COMMAND" } } }));
    vi.spyOn(api, "capabilityApprovals").mockResolvedValueOnce({ ready: false, items: [] })
      .mockRejectedValue(new Error("offline"));
    const { client } = mount([item]);
    const record = await screen.findByRole("article", { name: "权限审批结果" });
    await act(async () => { await client.invalidateQueries({ queryKey: ["capability-approval-history"] }); });
    expect(record.textContent).toContain("已批准本次操作");
    expect(record.textContent).not.toMatch(/SECRET_RAW_COMMAND|完成|成功|已执行/);
    expect(await screen.findByText("审批记录暂时无法读取")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试读取审批记录" })).toBeTruthy();
  });

  it("does not expose old actor props on a fresh mount while access confirmation is pending", async () => {
    const item = run();
    item.events = events().map((event) => ({ ...event, participants: [], message: event.kind }));
    let deny!: (reason: Error) => void;
    const read = vi.spyOn(api, "capabilityApprovals").mockImplementation(() => new Promise((_resolve, reject) => { deny = reject; }));
    mount([item], { ...actor, user_id: "actor-b" });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("article", { name: "权限审批结果" })).toBeNull();
    await act(async () => { deny(new ApiError("denied", 404)); });
    await screen.findByText("审批记录暂时无法读取");
    expect(screen.queryByRole("article", { name: "权限审批结果" })).toBeNull();
  });

  it("performs a final read when the same mounted run becomes terminal between polls", async () => {
    const read = vi.spyOn(api, "capabilityApprovals").mockResolvedValueOnce({ ready: false, items: [] })
      .mockResolvedValue({ ready: false, items: events() });
    const item = { ...run(), status: "waiting_approval" };
    const { refresh } = mount([item]);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    item.status = "cancelled";
    refresh();
    await screen.findByRole("article", { name: "权限审批结果" });
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("starts a fresh terminal read even while the initial pending request is still in flight", async () => {
    let settle!: (value: Awaited<ReturnType<typeof api.capabilityApprovals>>) => void;
    const read = vi.spyOn(api, "capabilityApprovals")
      .mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }))
      .mockResolvedValue({ ready: false, items: events() });
    const item = { ...run(), status: "waiting_approval" };
    const { refresh } = mount([item]);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    item.status = "cancelled";
    refresh();
    await act(async () => { settle({ ready: false, items: [] }); });
    await screen.findByRole("article", { name: "权限审批结果" });
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("polls a pending run but performs only a read for a terminal run", async () => {
    const read = vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ ready: false, items: events() });
    const item = { ...run(), status: "waiting_approval" };
    const { view } = mount([item]);
    await screen.findByRole("article", { name: "权限审批结果" });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2), { timeout: 2500 });
    view.unmount();
    read.mockClear();
    mount([run()]);
    await screen.findByRole("article", { name: "权限审批结果" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("uses readable capability labels and retries a failed history read", async () => {
    const user = userEvent.setup();
    const items = events();
    items[0].payload = { capability: "file", operation: "write" };
    const read = vi.spyOn(api, "capabilityApprovals").mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ ready: false, items });
    mount([run()]);
    await user.click(await screen.findByRole("button", { name: "重试读取审批记录" }));
    const record = await screen.findByRole("article", { name: "权限审批结果" });
    expect(record.textContent).toContain("写入文件");
    expect(record.textContent).not.toContain("capability_tool");
    expect(record.querySelector("time")?.title).toBe("审批记录时间");
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("discards another run's response instead of showing its approval", async () => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ ready: false, items: events("foreign-run") });
    mount([run()]);
    await waitFor(() => expect(api.capabilityApprovals).toHaveBeenCalled());
    expect(screen.queryByRole("article", { name: "权限审批结果" })).toBeNull();
  });

  it("does not reuse the previous actor's cached result after identity changes", async () => {
    const read = vi.spyOn(api, "capabilityApprovals").mockResolvedValueOnce({ ready: false, items: events() })
      .mockResolvedValue({ ready: false, items: [] });
    vi.spyOn(api, "login").mockResolvedValue({ ...actor, user_id: "actor-b" });
    const item = run();
    item.events = events().map((event) => ({ ...event, participants: [], message: event.kind }));
    const { login, client } = mount([item]);
    await screen.findByRole("article", { name: "权限审批结果" });
    await act(async () => { await login(); });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("article", { name: "权限审批结果" })).toBeNull();
    await waitFor(() => expect(client.getQueryCache().find({
      queryKey: ["capability-approval-history", actor.tenant_id, actor.user_id, actor.role, "run-a"],
    })).toBeUndefined());
  });

  it("aborts an outstanding read when its conversation surface unmounts", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "capabilityApprovals").mockImplementation((_id, pendingSignal) => {
      signal = pendingSignal;
      return new Promise(() => {});
    });
    const { view } = mount([run()]);
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it.each([401, 403, 404])("does not expose legacy props after a scope-denied HTTP %s response", async (status) => {
    const item = run();
    item.events = events().map((event) => ({ ...event, participants: [], message: event.kind }));
    vi.spyOn(api, "capabilityApprovals").mockRejectedValue(new ApiError("denied", status));
    mount([item]);
    await screen.findByText("审批记录暂时无法读取");
    expect(screen.queryByRole("article", { name: "权限审批结果" })).toBeNull();
  });
});

describe("capability approval API projection", () => {
  it("uses the authenticated read API and strips raw payload extensions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ready: false, items: events().map((event) => ({
      ...event, payload: { capability: "file", operation: "write", arguments: { command: "SECRET" } },
    })) }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.capabilityApprovals("run-a");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/runs/run-a/capability-approvals");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "include", cache: "no-store" });
    expect(JSON.stringify(result)).not.toContain("SECRET");
    expect(result.items[0].payload).toEqual({ capability: "file", operation: "write" });
  });

  it.each(["foreign-run", "unknown-decision"])("rejects %s evidence", async (mutation) => {
    const items = events(mutation === "foreign-run" ? "run-b" : "run-a");
    const raw = mutation === "unknown-decision" ? items.map((event) => ({ ...event, decision: "maybe" })) : items;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ready: false, items: raw }), { status: 200 })));
    await expect(api.capabilityApprovals("run-a")).rejects.toMatchObject({ code: "invalid_response" });
  });
});
