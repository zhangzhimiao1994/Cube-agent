import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type CurrentUser, type RunDetail } from "../api/client";
import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { RunApprovalHistory } from "./RunApprovalHistory";

const actor: CurrentUser = { tenant_id: "tenant-a", user_id: "actor-a", role: "operator", username: "test", permissions: ["run:read"] };
const uuid = (index: number) => `00000000-0000-4000-8000-${index.toString(16).padStart(12, "0")}`;
const review = (index = 1, patch: Record<string, unknown> = {}) => ({ id: `model-review:${uuid(index)}`,
  created_at: "2026-10-31T01:00:00Z", status: "allow", risk: "low", reason_code: "authorized_action",
  reviewer: "independent_model", tool_name: "workspace.write_text", ...patch });
const run = (id = "run-a"): RunDetail => ({ id, conversation_id: "conversation-a", request: "Task", status: "completed",
  mode: "direct", version: 1, created_at: "2026-10-31T01:00:00Z", queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
  events: [], artifacts: [], tool_lifecycle: [], failure_diagnostics: [], orchestration_protocol_summary: null,
  explicit_details: {}, model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
    requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] } });
const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function mount(read: (url: URL, init?: RequestInit) => Response | Promise<Response>, runs = [run()], identity = actor) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  vi.spyOn(api, "me").mockResolvedValue(identity);
  vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ items: [], ready: false, conversation_approval_available: false });
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => read(new URL(String(input), "http://fixture.test"), init)));
  let auth!: ReturnType<typeof useAuth>;
  let activeClient = client;
  function Content() { activeClient = useQueryClient(); auth = useAuth(); return <>{runs.map((item) => <RunApprovalHistory key={item.id} run={item} />)}</>; }
  const tree = () => <QueryClientProvider client={client}><AuthProvider><Content /></AuthProvider></QueryClientProvider>;
  const view = render(tree());
  return { client, getClient: () => activeClient, view, refresh: () => view.rerender(tree()), login: () => auth.login("second", "test-password") };
}
const page = (items: unknown[], next_cursor: string | null = null) => new Response(JSON.stringify({ items, next_cursor }));

describe("read-only model review history", () => {
  it("renders a separate light record without approval actions or execution success claims", async () => {
    const approve = vi.spyOn(api, "approveCapability");
    mount(() => page([review(1, { rationale: "PRIVATE_RATIONALE", raw_verdict: "PRIVATE_VERDICT", action: "PRIVATE_ACTION",
      args: "PRIVATE_ARGS", auth: "PRIVATE_AUTH", privatebody: "PRIVATE_BODY" })]));
    const card = await screen.findByRole("article", { name: "模型自动审查" });
    expect(card.classList.contains("run-approval-result")).toBe(true);
    expect(card.classList.contains("chat-message")).toBe(false);
    expect(card.textContent).toContain("审查通过");
    expect(card.textContent).toContain("不代表动作执行成功");
    expect(card.textContent).toContain("符合已授权范围");
    expect(card.textContent).not.toMatch(/PRIVATE_|independent_model|workspace.write_text|model-review:/);
    expect(within(card).queryByRole("button")).toBeNull();
    expect(approve).not.toHaveBeenCalled();
  });

  it("keeps loading cursor pages beyond 128 records and reloads from durable history", async () => {
    const cursors: Array<string | null> = [];
    const { view } = mount((url) => {
      const cursor = url.searchParams.get("cursor"); cursors.push(cursor);
      const offset = cursor === uuid(50) ? 50 : cursor === uuid(100) ? 100 : 0;
      const count = offset === 100 ? 30 : 50;
      return page(Array.from({ length: count }, (_, i) => review(offset + i + 1)), offset < 100 ? uuid(offset + 50) : null);
    });
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getAllByRole("article", { name: "模型自动审查" })).toHaveLength(50));
    await user.click(screen.getByRole("button", { name: "继续加载模型审查记录" }));
    await waitFor(() => expect(screen.getAllByRole("article", { name: "模型自动审查" })).toHaveLength(100));
    await user.click(screen.getByRole("button", { name: "继续加载模型审查记录" }));
    await waitFor(() => expect(screen.getAllByRole("article", { name: "模型自动审查" })).toHaveLength(130));
    expect(screen.queryByRole("button", { name: "继续加载模型审查记录" })).toBeNull();
    expect(cursors).toEqual([null, uuid(50), uuid(100)]);
    view.unmount();
    mount(() => page([review()]));
    expect(await screen.findByRole("article", { name: "模型自动审查" })).toBeTruthy();
  });

  it.each(["pending", "deny", "needs_human", "unknown"])("labels %s with fixed public copy", async (status) => {
    mount(() => page([review(1, { status, risk: null, reason_code: "missing_context" })]));
    const card = await screen.findByRole("article", { name: "模型自动审查" });
    expect(card.textContent).toContain("缺少必要上下文");
    expect(card.textContent).not.toMatch(/PRIVATE_|已批准|写入成功/);
  });

  it.each([
    ["accounting_unknown", "用量记账状态未知"], ["review_timeout", "模型审查超时"],
    ["review_unavailable", "模型审查服务不可用"], ["review_invalid_response", "模型审查响应无效"],
    ["review_unknown", "模型审查状态未知"], ["review_cancelled", "模型审查已取消"],
  ])("renders fixed storage reason %s without raw code", async (reason_code, label) => {
    mount(() => page([review(1, { status: "unknown", risk: null, reason_code })]));
    const card = await screen.findByRole("article", { name: "模型自动审查" });
    expect(card.textContent).toContain(label);
    expect(card.textContent).not.toContain(reason_code);
  });

  it.each([401, 403, 404])("clears cached records on a later HTTP %s scope denial", async (status) => {
    const { getClient } = mount((url) => url.searchParams.has("cursor")
      ? new Response(JSON.stringify({ error: { code: "permission_denied", message: "PRIVATE_DENIAL" } }), { status })
      : page([review()], uuid(1)));
    await screen.findByRole("article", { name: "模型自动审查" });
    expect(JSON.stringify(getClient().getQueryCache().findAll({ queryKey: ["model-review-history"] }).map((query) => query.state.data))).toContain("model-review:");
    await userEvent.setup().click(screen.getByRole("button", { name: "继续加载模型审查记录" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: "模型自动审查" })).toBeNull());
    const data = getClient().getQueryCache().findAll({ queryKey: ["model-review-history"] }).map((query) => query.state.data);
    expect(JSON.stringify(data)).not.toContain("model-review:");
    expect(document.body.textContent).not.toContain("PRIVATE_DENIAL");
  });

  it("treats an old-server 404 as an empty optional history", async () => {
    const { client } = mount(() => new Response("not found", { status: 404 }));
    await waitFor(() => expect(client.getQueryCache().findAll({ queryKey: ["model-review-history"] }).some((query) => query.state.status === "error")).toBe(true));
    expect(screen.queryByRole("article", { name: "模型自动审查" })).toBeNull();
    expect(screen.queryByText("模型审查记录暂时无法读取")).toBeNull();
  });

  it("hides malformed payloads and provides only a fixed retry message", async () => {
    let valid = false;
    mount(() => page([review(1, { reason_code: valid ? "authorized_action" : "PRIVATE_BODY" })]));
    await screen.findByText("模型审查记录暂时无法读取");
    expect(document.body.textContent).not.toContain("PRIVATE_BODY");
    expect(screen.queryByRole("article", { name: "模型自动审查" })).toBeNull();
    valid = true; await userEvent.setup().click(screen.getByRole("button", { name: "重试读取模型审查记录" }));
    await screen.findByRole("article", { name: "模型自动审查" });
  });

  it("isolates actors and runs, and aborts a read when the surface unmounts", async () => {
    const signals: AbortSignal[] = [];
    const { view } = mount((_url, init) => { signals.push(init!.signal as AbortSignal); return new Promise(() => {}); }, [run(), run("run-b")]);
    await waitFor(() => expect(signals).toHaveLength(2));
    view.unmount(); expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it("does not keep another actor's model records after identity changes", async () => {
    let first = true;
    const { login } = mount(() => { const result = first ? page([review()]) : page([]); first = false; return result; });
    vi.spyOn(api, "login").mockResolvedValue({ ...actor, user_id: "actor-b" });
    await screen.findByRole("article", { name: "模型自动审查" });
    await act(async () => { await login(); });
    await waitFor(() => expect(screen.queryByRole("article", { name: "模型自动审查" })).toBeNull());
  });

  it("performs a final durable read when a pending run becomes terminal", async () => {
    let reads = 0;
    const item = { ...run(), status: "running" };
    const { refresh } = mount(() => page([review(1, { status: ++reads === 1 ? "pending" : "allow", reason_code: null })]), [item]);
    expect((await screen.findByRole("article", { name: "模型自动审查" })).textContent).toContain("审查中");
    item.status = "completed"; refresh();
    await waitFor(() => expect(screen.getByRole("article", { name: "模型自动审查" }).textContent).toContain("审查通过"));
    expect(reads).toBe(2);
  });
});
