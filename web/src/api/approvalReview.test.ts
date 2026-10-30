import { afterEach, describe, expect, it, vi } from "vitest";
import { api, defaultApprovalReview, validateApprovalReview } from "./client";
const legacy = { default_mode: "auto", default_workflow_id: null, default_agent_ids: [], log_level: "warning", hermes_enabled: true, safe_tools_enabled: true, require_approval_for_tools: true, channel_entry: "web", attachment_retention_days: 7, attachment_max_mb: 25, supported_permission_modes: ["default", "auto_review", "read_only"] };
const expectedReview = {
  schema_version: 1, engine: "rules", reviewer_logical_model: null, require_distinct_model: false,
  timeout_seconds: 15, max_input_tokens: 4096, max_output_tokens: 512, max_reviews_per_run: 8,
  max_transport_attempts_per_review: 1, max_cost_usd_per_run: "0.00",
  denial_breaker_consecutive: 3, denial_breaker_denials: 10, denial_breaker_window: 50,
};
function serve(extra: Record<string, unknown> = {}) {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ ...legacy, ...extra }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());
describe("approval review settings transport", () => {
  it.each([
    ["max_reviews_per_run", "9007199254740992"],
    ["denial_breaker_window", "9007199254740992"],
    ["max_reviews_per_run", "9007199254740993"],
    ["denial_breaker_consecutive", "9007199254740993"],
    ["denial_breaker_denials", "9007199254740993"],
    ["denial_breaker_window", "9223372036854775807"],
  ])("preserves raw int64 %s=%s while changing other settings", async (field, rawInteger) => {
    const fields = field === "denial_breaker_consecutive"
      ? [field, "denial_breaker_denials", "denial_breaker_window"]
      : field === "denial_breaker_denials" ? [field, "denial_breaker_window"] : [field];
    let raw = JSON.stringify({ ...legacy, approval_review: expectedReview });
    for (const key of fields) raw = raw.replace(new RegExp(`"${key}":\\d+`), `"${key}":${rawInteger}`);
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(raw, { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetcher);
    const settings = await api.settings();
    expect(settings.approval_review?.[field as "max_reviews_per_run"]).toBe(BigInt(rawInteger));
    await api.updateSettings({ ...settings, log_level: "error" });
    const body = String(fetcher.mock.calls.at(-1)?.[1]?.body);
    for (const key of fields) expect(body).toMatch(new RegExp(`"${key}":${rawInteger}(?=[,}])`));
    expect(body).toContain('"log_level":"error"');
    expect(body).not.toContain('"approval_review_readiness"');
  });
  it("still rejects unsafe new numeric input and out-of-range raw int64", async () => {
    const review = { ...defaultApprovalReview(), max_reviews_per_run: 9007199254740992 };
    expect(() => validateApprovalReview(review)).toThrow();
    const raw = JSON.stringify({ ...legacy, approval_review: expectedReview }).replace('"max_reviews_per_run":8', '"max_reviews_per_run":9223372036854775808');
    vi.stubGlobal("fetch", vi.fn(async () => new Response(raw)));
    await expect(api.settings()).rejects.toThrow();
  });
  it("does not pretend a rounded number is exact when raw source context is unavailable", async () => {
    const raw = JSON.stringify({ ...legacy, approval_review: expectedReview }).replace('"max_reviews_per_run":8', '"max_reviews_per_run":9007199254740993');
    vi.stubGlobal("fetch", vi.fn(async () => new Response(raw)));
    const parse = JSON.parse;
    const spy = vi.spyOn(JSON, "parse").mockImplementation((text, reviver) => parse(text, reviver
      ? function (key, value) { return reviver.call(this, key, value); } : undefined));
    try { await expect(api.settings()).rejects.toThrow(/无法无损读取审查计数/); }
    finally { spy.mockRestore(); }
  });
  it("parses legacy missing fields into rules without inventing ready", async () => {
    serve(); const settings = await api.settings();
    expect(settings.approval_review).toEqual(expectedReview);
    expect(settings.approval_review_readiness?.status).not.toBe("ready");
  });
  it.each(["ready", "not_ready"])("reads trusted %s and never PUTs response-only fields", async (status) => {
    const review = { ...defaultApprovalReview(), engine: "rules_then_model" as const, reviewer_logical_model: "reviewer", max_cost_usd_per_run: "0.010000" };
    const supported_scope = status === "ready" ? ["fresh_original_run", "workspace.write_text"] : [];
    const fetcher = serve({ approval_review: review, approval_review_readiness: { status, reason_code: "runtime_not_ready", supported_scope }, plugin_package_subprocess_registration_status: "ready" });
    const settings = await api.settings(); expect(settings.approval_review_readiness?.status).toBe(status);
    expect(settings.approval_review_readiness?.supported_scope).toEqual(supported_scope);
    await api.updateSettings(settings);
    const body = JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body));
    expect(body.approval_review).toEqual(review);
    for (const key of ["approval_review_readiness", "supported_permission_modes", "plugin_package_subprocess_registration_status"]) expect(body).not.toHaveProperty(key);
  });
  it.each(["0", "0.0000000", "NaN", "Infinity", "-1", "1000000.000001", " 0.1 "])("rejects invalid model budget %s", (cost) => {
    const review = { ...defaultApprovalReview(), engine: "rules_then_model" as const, reviewer_logical_model: "reviewer", max_cost_usd_per_run: cost };
    expect(() => validateApprovalReview(review)).toThrow(/预算/);
  });
  it.each([0, 1.5, 1000001, NaN])("rejects invalid token limit %s", (max_input_tokens) => {
    const review = { ...defaultApprovalReview(), max_input_tokens };
    expect(() => validateApprovalReview(review)).toThrow(/输入/);
  });
  it.each([{ timeout_seconds: 0 }, { timeout_seconds: 61 }, { max_reviews_per_run: 1.5 }, { require_distinct_model: "true" }, { max_output_tokens: 0 }, { reviewer_logical_model: "https://example.invalid" }, { max_cost_usd_per_run: 1 }, { prompt: "injected" }])("rejects invalid review configuration %j", (patch) => {
    const review = { ...defaultApprovalReview(), ...patch };
    expect(() => validateApprovalReview(review)).toThrow();
  });
  it.each([null, {}])("normalizes legacy review %j and unknown readiness fails closed", async (review) => {
    serve({ approval_review: review, approval_review_readiness: { status: "unexpected", reason_code: "future" } });
    const settings = await api.settings();
    expect(settings.approval_review).toEqual(expectedReview);
    expect(settings.approval_review_readiness).toBeUndefined();
  });
  it("rejects invalid writes before fetch and strips unknown GET-only properties", async () => {
    const fetcher = serve(); const settings = await api.settings(); fetcher.mockClear();
    expect(() => api.updateSettings({ ...settings, approval_review: { ...defaultApprovalReview(), max_input_tokens: 0 } })).toThrow();
    expect(fetcher).not.toHaveBeenCalled();
    await api.updateSettings({ ...settings, extra_read_only: "ignored" } as typeof settings);
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).not.toHaveProperty("extra_read_only");
  });
  it("requires alias and preserves exact decimal strings including tiny positive budgets", () => {
    const review = { ...defaultApprovalReview(), engine: "rules_then_model" as const, reviewer_logical_model: "reviewer", max_cost_usd_per_run: "0.000001" };
    expect(validateApprovalReview(review).max_cost_usd_per_run).toBe("0.000001");
    expect(validateApprovalReview({ ...review, max_cost_usd_per_run: "1e-6" }).max_cost_usd_per_run).toBe("1e-6");
    expect(() => validateApprovalReview({ ...review, reviewer_logical_model: null })).toThrow(/模型/);
    expect(() => validateApprovalReview({ ...review, max_transport_attempts_per_review: 2 })).toThrow();
    expect(() => validateApprovalReview({ ...review, denial_breaker_consecutive: 11 })).toThrow();
  });
});
