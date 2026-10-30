import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

const cursor = "11111111-1111-4111-8111-111111111111";
const item = { id: `model-review:${cursor}`, created_at: "2026-10-31T01:00:00Z", status: "allow",
  risk: "low", reason_code: "authorized_action", reviewer: "independent_model", tool_name: "workspace.write_text" };
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("public model review history API", () => {
  it("accepts all fixed storage outcome reasons including timeout", async () => {
    const reasons = ["accounting_unknown", "review_timeout", "review_unavailable", "review_invalid_response", "review_unknown", "review_cancelled"];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: reasons.map((reason_code) => ({ ...item,
      status: "unknown", risk: null, reason_code })), next_cursor: null }))));
    expect((await api.modelReviews("run-a")).items.map((record) => record.reason_code)).toEqual(reasons);
  });
  it("uses a read-only cursor request and strips every private extension", async () => {
    const read = vi.fn(async () => new Response(JSON.stringify({ items: [{ ...item,
      rationale: "PRIVATE_RATIONALE", raw_verdict: { decision: "PRIVATE_VERDICT" }, action: "PRIVATE_ACTION",
      args: { text: "PRIVATE_ARGS" }, auth: "PRIVATE_AUTH", privatebody: "PRIVATE_BODY" }],
      next_cursor: cursor, privatebody: "PRIVATE_PAGE" })));
    vi.stubGlobal("fetch", read);
    const controller = new AbortController();
    const page = await api.modelReviews("run-a", { cursor, signal: controller.signal });
    expect(page).toEqual({ items: [item], next_cursor: cursor });
    expect(JSON.stringify(page)).not.toContain("PRIVATE_");
    const [url, init] = read.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toContain(`/api/v1/runs/run-a/model-reviews?limit=50&cursor=${cursor}`);
    expect(init).toMatchObject({ method: "GET", credentials: "include", cache: "no-store", signal: controller.signal });
    expect(init.body).toBeUndefined();
  });

  it.each([
    { id: "approval-a" }, { created_at: "PRIVATE_DATE" }, { status: "PRIVATE_STATUS" },
    { risk: "PRIVATE_RISK" }, { reason_code: "constructor" }, { reviewer: "PRIVATE_REVIEWER" },
    { tool_name: "PRIVATE_TOOL" },
  ])("rejects malformed public fields %j", async (patch) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [{ ...item, ...patch }], next_cursor: null }))));
    await expect(api.modelReviews("run-a")).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates UUID cursors without accepting model record IDs as approval or page cursors", () => {
    const read = vi.fn(); vi.stubGlobal("fetch", read);
    expect(() => api.modelReviews("run-a", { cursor: `model-review:${cursor}` })).toThrow(/UUID/);
    expect(read).not.toHaveBeenCalled();
  });
});
