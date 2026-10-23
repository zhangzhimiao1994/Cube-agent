import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type RunDetail } from "../api/client";
import { useRunCapabilityApprovals } from "./useRunCapabilityApprovals";

const authorization = vi.hoisted(() => ({ permissions: ["run:read", "run:create"] }));
vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({
    user: { tenant_id: "tenant", user_id: "actor", role: "operator" },
    hasPermission: (permission: string) => authorization.permissions.includes(permission),
  }),
}));

const clients: QueryClient[] = [];
afterEach(() => {
  authorization.permissions = ["run:read", "run:create"];
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function pending(approvalId = "approval-A", version = 7): RunDetail {
  return {
    id: "run", conversation_id: "conversation", request: "Synthetic approval", mode: "direct",
    status: "waiting_approval", version, created_at: "2026-10-24T00:00:00Z", events: [], artifacts: [],
    tool_lifecycle: [], failure_diagnostics: [], queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
    explicit_details: { approval_kind: "capability_tool", approval_id: approvalId },
    model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
    orchestration_protocol_summary: null,
  };
}

function receipt(approvalId: string | null = "approval-A", version: number | null = 7) {
  return {
    ready: true, conversation_approval_available: true,
    conversation_approval_id: approvalId, conversation_approval_version: version,
    // Request sequence is historical, not the current run/checkpoint version.
    items: [{ kind: "approval.requested" as const, run_id: "run", approval_id: approvalId ?? "approval-A",
      sequence: 3, created_at: "2026-10-24T00:00:00Z", payload: {} }],
  };
}

function mount(run = pending()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(({ detail }) => useRunCapabilityApprovals(detail), { initialProps: { detail: run }, wrapper });
}

describe("session approval requires a current exact receipt", () => {
  it("keeps history readable but never enables session approval with run:read alone", async () => {
    authorization.permissions = ["run:read"];
    const read = vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt());
    const { result } = mount();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(read).toHaveBeenCalledTimes(1);
    expect(result.current.sessionAvailable).toBe(false);
    expect(result.current.sessionUnavailableReason).toContain("权限");
  });

  it("revokes session approval immediately when the same actor loses run:create", async () => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt());
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.sessionAvailable).toBe(true));
    authorization.permissions = ["run:read"];
    await act(async () => rerender({ detail: pending() }));
    expect(result.current.sessionAvailable).toBe(false);
  });

  it("accepts matching ID/current version without using the historical event sequence", async () => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt());
    const { result } = mount();
    await waitFor(() => expect(result.current.sessionAvailable).toBe(true));
  });

  it.each([
    ["different approval", "approval-B", 7], ["newer version", "approval-A", 8],
    ["older version", "approval-A", 6], ["missing ID", null, 7], ["missing version", "approval-A", null],
  ] as const)("does not borrow readiness for %s", async (_label, id, version) => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt(id, version));
    const { result } = mount();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.sessionAvailable).toBe(false);
  });

  it("fails closed for old APIs that return only availability booleans", async () => {
    const { conversation_approval_id: _id, conversation_approval_version: _version, ...legacy } = receipt();
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(legacy);
    const { result } = mount();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.sessionAvailable).toBe(false);
  });

  it.each(["not ready", "unsupported"])("does not manufacture session support when %s", async (state) => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ ...receipt(),
      ready: state !== "not ready", conversation_approval_available: state !== "unsupported" });
    const { result } = mount();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.sessionAvailable).toBe(false);
  });

  it("invalidates the receipt when approval ID changes even before the version catches up", async () => {
    const read = vi.spyOn(api, "capabilityApprovals").mockResolvedValueOnce(receipt())
      .mockImplementation(() => new Promise(() => {}));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.sessionAvailable).toBe(true));
    await act(async () => rerender({ detail: pending("approval-B", 7) }));
    expect(result.current.sessionAvailable).toBe(false);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });

  it("uses a valid explicit_details version only when the top-level version is absent", async () => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt());
    const detail = { ...pending(), version: undefined, explicit_details: {
      ...pending().explicit_details, version: "7",
    } };
    const { result } = mount(detail);
    await waitFor(() => expect(result.current.sessionAvailable).toBe(true));
  });

  it("fails closed without a valid target version", async () => {
    vi.spyOn(api, "capabilityApprovals").mockResolvedValue(receipt());
    const { result } = mount({ ...pending(), version: undefined });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.sessionAvailable).toBe(false);
  });
});

describe("session receipt API schema", () => {
  it("preserves the exact pending receipt", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(receipt()), { status: 200 })));
    expect(await api.capabilityApprovals("run")).toMatchObject({
      conversation_approval_id: "approval-A", conversation_approval_version: 7,
    });
  });

  it.each([0, -1, 1.5, "7", true])("rejects invalid receipt version %s", async (version) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ...receipt(), conversation_approval_version: version,
    }), { status: 200 })));
    await expect(api.capabilityApprovals("run")).rejects.toMatchObject({ code: "invalid_response" });
  });
});
