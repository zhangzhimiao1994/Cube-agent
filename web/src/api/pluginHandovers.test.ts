import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./client";

const runId = "11111111-1111-4111-8111-111111111111";
const artifactId = "22222222-2222-4222-8222-222222222222";
const handoverId = `gh-${"a".repeat(64)}`;
const planHash = "b".repeat(64);
const identity = { expected_version: 1, plan_hash: planHash };
const confirmation = { ...identity, key_id: "existing:key.1" };
const createInput = {
  source: { kind: "artifact" as const, artifact_id: artifactId },
  expires_at: "2026-10-14T12:00:00+00:00",
};
const archive = new Blob([new Uint8Array([80, 75, 3, 4])], { type: "application/zip" });
const scanReport = {
  schema_version: 1, report_id: `gs-${"d".repeat(64)}`, report_sha256: "e".repeat(64),
  created_at: "2026-10-15T00:00:00Z", coverage: "complete",
  body: { schema_version: 1, scanner_id: "plugin-python-source-v1", ruleset_id: "plugin-python-signals-v1",
    rules_sha256: "f".repeat(64), parser_id: "cpython-3.12-ast-exec", limits: { max_nodes: 100000 },
    subject: { run_id: runId, handover_id: handoverId, plan_hash: planHash,
      verified_package: { key_id: "existing:key.1", archive_sha256: "c".repeat(64) } },
    coverage: "complete", members: [], findings: [], incomplete_reasons: [],
    limitations: "Static signals only; not proof of malice or absence of vulnerabilities." },
};
const receipt = {
  schema_version: 1,
  handover_id: handoverId,
  run_id: runId,
  creator_id: "33333333-3333-4333-8333-333333333333",
  installer_id: null,
  version: 1,
  status: "awaiting_signature",
  created_at: "2026-10-13T12:00:00Z",
  updated_at: "2026-10-13T12:00:00Z",
  expires_at: createInput.expires_at,
  source: { kind: "artifact", artifact_id: "legacy-artifact" },
  source_manifest_sha256: "c".repeat(64),
  plan_hash: planHash,
  plugin_id: "fixture-plugin",
  plugin_version: "1.0.0",
  permissions: [],
  risk_summary: [],
  signature_payload_sha256: null,
  archive_sha256: null,
  key_id: null,
  receipt_id: null,
  audit_id: null,
  committed_at: null,
  result: null,
  source_grant_id: `sg-${"d".repeat(64)}`,
  source_grant_binding_sha256: "e".repeat(64),
};

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "X-Error-Id": "unit-error" },
  });
}

beforeEach(async () => {
  await api.logout();
  window.sessionStorage.setItem("agent_hub_access_token", "unit-only-bearer");
});

afterEach(async () => {
  await api.logout();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("public plugin handover transport", () => {
  it("scans original ZIP bytes with the stored key and a unique idempotency key, then GETs the report", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => response(scanReport));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api.scanPluginHandover(runId, handoverId, "existing:key.1", archive, "scan:1")).toEqual(scanReport);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe(`/api/v1/runs/${runId}/plugin-handovers/${handoverId}/scan-reports?key_id=existing%3Akey.1`);
    expect(init.body).toBe(archive);
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/zip");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("scan:1");
    expect(await api.pluginHandoverScanReport(runId, handoverId, scanReport.report_id)).toEqual(scanReport);
    expect(fetchMock.mock.calls[1][1].method).toBe("GET");
  });

  it.each([{ body: "<script>bad()</script>" }, { coverage: "safe" }, { report_sha256: "bad" },
    { body: { ...scanReport.body, coverage: "incomplete" } },
    { body: { ...scanReport.body, incomplete_reasons: ["parse_error"] } }])("rejects malformed scan reports %#", async (patch) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...scanReport, ...patch })));
    await expect(api.scanPluginHandover(runId, handoverId, "existing:key.1", archive, "scan:1"))
      .rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects invalid scan identities and ZIP before fetch", async () => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    await expect(api.scanPluginHandover(runId, handoverId, "key\n", archive, "scan:1")).rejects.toBeInstanceOf(ApiError);
    await expect(api.scanPluginHandover(runId, handoverId, "key", new Blob(), "scan:1")).rejects.toBeInstanceOf(ApiError);
    await expect(api.pluginHandoverScanReport(runId, handoverId, "../report")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([401, 403, 404, 409, 413, 422, 503])("does not retry or approve a failed scan (HTTP %s)", async (status) => {
    const fetchMock = vi.fn().mockResolvedValue(response({ error: { code: "scan_rejected", message: "rejected" } }, status));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.scanPluginHandover(runId, handoverId, "existing:key.1", archive, "scan:1"))
      .rejects.toMatchObject({ status });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends exact report approval fields and preserves reason-only legacy requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetchMock);
    const payload = { reason: "reviewed", scan_report_id: scanReport.report_id, scan_report_sha256: scanReport.report_sha256 };
    await api.approvePluginPackage("sample.plugin", payload).catch(() => undefined);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
    await api.approvePluginPackage("legacy", { reason: "reviewed" }).catch(() => undefined);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ reason: "reviewed" });
  });

  it.each([{ scan_report_id: scanReport.report_id }, { scan_report_sha256: scanReport.report_sha256 },
    { scan_report_id: "bad", scan_report_sha256: scanReport.report_sha256 }])("rejects incomplete report approval identity before fetch %#", async (payload) => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    await expect(api.approvePluginPackage("sample.plugin", payload)).rejects.toMatchObject({ code: "request_validation" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates only a plan with the exact source, expiry and idempotency header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(receipt));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api.createPluginHandover(runId, createInput, "plan:1")).toEqual(receipt);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe(`/api/v1/runs/${encodeURIComponent(runId)}/plugin-handovers`);
    expect(init).toMatchObject({ method: "POST", cache: "no-store", credentials: "include" });
    expect(JSON.parse(init.body)).toEqual(createInput);
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("plan:1");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer unit-only-bearer");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends checked workspace paths as a JSON array", async () => {
    const source = { kind: "workspace" as const, project_id: "project:1", session_id: "session.1",
      paths: ["src/plugin.py", "manifest.json"] };
    const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, source }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await api.createPluginHandover(runId, { ...createInput, source }, "plan-2")).source)
      .toEqual(source);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).source).toEqual(source);
  });

  it("reads a historical receipt without cache or a mutation request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, status: "cancelled" }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.pluginHandover(runId, handoverId);
    expect(result.status).toBe("cancelled");
    const [path, init] = fetchMock.mock.calls[0];
    const parsed = new URL(path, "http://unit.local");
    expect(parsed.pathname).toBe(`/api/v1/runs/${runId}/plugin-handovers/${handoverId}`);
    expect([...parsed.searchParams.keys()]).toEqual(["_"]);
    expect(init).toMatchObject({ method: "GET", cache: "no-store", credentials: "include" });
    expect(init.body).toBeUndefined();
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer unit-only-bearer");
  });

  it("cancels with only strict version and plan hash", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, status: "cancelled", version: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await api.cancelPluginHandover(runId, handoverId, identity)).version).toBe(2);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe(`/api/v1/runs/${runId}/plugin-handovers/${handoverId}/cancel`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual(identity);
    expect(new Headers(init.headers).has("Idempotency-Key")).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("confirms once using the original Blob and exactly four query fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, status: "pending",
      result: "blocked_pending_approval", installer_id: artifactId, version: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await api.confirmPluginHandover(runId, handoverId, confirmation, archive, "confirm:1"))
      .result).toBe("blocked_pending_approval");
    const [path, init] = fetchMock.mock.calls[0];
    const parsed = new URL(path, "http://unit.local");
    expect(parsed.pathname).toBe(`/api/v1/runs/${runId}/plugin-handovers/${handoverId}/confirm`);
    expect([...parsed.searchParams.entries()]).toEqual([
      ["expected_version", "1"], ["plan_hash", planHash], ["confirm", "true"], ["key_id", "existing:key.1"],
    ]);
    expect(path).toContain("key_id=existing%3Akey.1");
    expect(init).toMatchObject({ method: "POST", cache: "no-store", credentials: "include" });
    expect(init.body).toBe(archive);
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/zip");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("confirm:1");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer unit-only-bearer");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("permits an exact 2,000,000 byte archive without rewriting its bytes", async () => {
    const blob = new Blob([new Uint8Array(2_000_000)]);
    const fetchMock = vi.fn().mockResolvedValue(response(receipt));
    vi.stubGlobal("fetch", fetchMock);
    await api.confirmPluginHandover(runId, handoverId, confirmation, blob, "confirm-2");
    expect(fetchMock.mock.calls[0][1].body).toBe(blob);
  });

  it("rejects 2,000,001 archive bytes with 413 before fetch", async () => {
    const blob = new Blob([new Uint8Array(2_000_001)]);
    const fetchMock = vi.fn().mockResolvedValue(response(receipt));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.confirmPluginHandover(runId, handoverId, confirmation, blob, "confirm-2"))
      .rejects.toMatchObject({ status: 413, code: "request_too_large" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("workspace path code point boundary", () => {
  const unicodePath = Array.from({ length: 4 }, () => "\u{1F600}".repeat(32)).join("/");
  const exactPath = Array.from({ length: 8 }, () => "\u{1F600}".repeat(31)).join("/");
  const sourceFor = (path: string) => ({ kind: "workspace" as const,
    project_id: "project", session_id: "session", paths: [path] });

  it.each([unicodePath, exactPath].map((path) => ({ path, points: [...path].length })))(
    "creates a workspace plan with $points Unicode code points without rewriting paths", async ({ path, points }) => {
      expect(path.length).toBeGreaterThan(255);
      expect(points).toBeLessThanOrEqual(255);
      const source = sourceFor(path);
      const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, source }));
      vi.stubGlobal("fetch", fetchMock);
      expect((await api.createPluginHandover(runId, { ...createInput, source }, "unicode-plan")).source)
        .toEqual(source);
      expect(JSON.parse(fetchMock.mock.calls[0][1].body).source).toEqual(source);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it.each([unicodePath, exactPath].map((path) => ({ path, points: [...path].length })))(
    "reads a historical workspace receipt with $points Unicode code points", async ({ path }) => {
      const source = sourceFor(path);
      const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, source }));
      vi.stubGlobal("fetch", fetchMock);
      expect((await api.pluginHandover(runId, handoverId)).source).toEqual(source);
      expect(fetchMock.mock.calls[0][1].method).toBe("GET");
      expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["a".repeat(256), exactPath + "a"].map((path, index) => ({ path, index })))(
    "rejects 256-code-point workspace paths in create and GET receipt ($index)", async ({ path }) => {
      expect([...path]).toHaveLength(256);
      const source = sourceFor(path);
      const fetchMock = vi.fn().mockResolvedValue(response({ ...receipt, source }));
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.createPluginHandover(runId, { ...createInput, source }, "unicode-plan"))
        .rejects.toMatchObject({ status: 422, code: "request_validation" });
      expect(fetchMock).not.toHaveBeenCalled();
      await expect(api.pluginHandover(runId, handoverId))
        .rejects.toMatchObject({ status: 200, code: "invalid_response" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );
});

describe("local handover input boundary", () => {
  it.each(["", "../run", `${runId}?extra=true`, `${runId}/other`, true, null])(
    "rejects an invalid run ID before fetch (%s)", async (value) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.createPluginHandover(value as string, createInput, "plan-1"))
        .rejects.toMatchObject({ status: 422, code: "request_validation" });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", "../handover", `${handoverId}?confirm=true`, true, null, undefined])(
    "rejects an invalid handover ID before fetch (%s)", async (value) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.pluginHandover(runId, value as string)).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", " key", "key\n", "key\r\nX-Injected: true", "a".repeat(129), true])(
    "rejects invalid idempotency keys for create and confirm (%s)", async (value) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.createPluginHandover(runId, createInput, value as string))
        .rejects.toBeInstanceOf(ApiError);
      await expect(api.confirmPluginHandover(runId, handoverId, confirmation, archive, value as string))
        .rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each([
    {},
    { ...createInput, authority: "admin" },
    { ...createInput, expires_at: true },
    { ...createInput, expires_at: "2026-10-14T12:00:00" },
    { ...createInput, source: { kind: "artifact", artifact_id: "ordinary-string" } },
    { ...createInput, source: { kind: "artifact", artifact_id: true } },
    { ...createInput, source: { ...createInput.source, tenant_id: runId } },
  ])("rejects invalid or authority-injected create bodies %#", async (value) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.createPluginHandover(runId, value as typeof createInput, "plan-1"))
      .rejects.toMatchObject({ status: 422, code: "request_validation" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([[], [true], [""], ["/absolute"], ["../outside"], ["src/../outside"],
    ["src\\file"], ["src/%2e%2e"], ["src//file"], ["file\u0000"], ["file\n"],
    ["a".repeat(256)], ["file", "file"], Array.from({ length: 201 }, (_, i) => `file-${i}`)]
    .map((paths) => ({ paths })))(
    "rejects unsafe or oversized workspace path sets %#", async ({ paths }) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const input = { ...createInput, source: { kind: "workspace" as const,
        project_id: "project", session_id: "session", paths: paths as string[] } };
      await expect(api.createPluginHandover(runId, input, "plan-1")).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each([0, -1, 1.5, "1", true, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    "rejects non-strict versions for cancel and confirm (%s)", async (expected_version) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const value = { ...identity, expected_version: expected_version as number };
      await expect(api.cancelPluginHandover(runId, handoverId, value)).rejects.toBeInstanceOf(ApiError);
      await expect(api.confirmPluginHandover(runId, handoverId,
        { ...value, key_id: "existing" }, archive, "confirm-1")).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", "A".repeat(64), "a".repeat(63), `${planHash}\n`, true])(
    "rejects invalid plan hashes for cancel and confirm (%s)", async (plan_hash) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const value = { ...identity, plan_hash: plan_hash as string };
      await expect(api.cancelPluginHandover(runId, handoverId, value)).rejects.toBeInstanceOf(ApiError);
      await expect(api.confirmPluginHandover(runId, handoverId,
        { ...value, key_id: "existing" }, archive, "confirm-1")).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", "unknown?trust=true", "key\n", "a".repeat(129), true])(
    "rejects invalid key identifiers without attempting to trust them (%s)", async (key_id) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.confirmPluginHandover(runId, handoverId,
        { ...identity, key_id: key_id as string }, archive, "confirm-1")).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("rejects extra confirmation and cancellation authority fields", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.cancelPluginHandover(runId, handoverId, { ...identity, approved: true } as typeof identity))
      .rejects.toBeInstanceOf(ApiError);
    await expect(api.confirmPluginHandover(runId, handoverId,
      { ...confirmation, confirm: false } as typeof confirmation, archive, "confirm-1"))
      .rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([new Blob(), new Blob([new Uint8Array(2 * 1024 * 1024 + 1)]), "zip-json", null])(
    "rejects empty, oversized or non-Blob archives locally %#", async (blob) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.confirmPluginHandover(runId, handoverId, confirmation, blob as Blob, "confirm-1"))
        .rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});

describe("handover receipt and error boundary", () => {
  it("accepts the legacy nullable grant pair and string artifact ID", async () => {
    const legacy = { ...receipt, source_grant_id: null, source_grant_binding_sha256: null };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(legacy)));
    expect(await api.pluginHandover(runId, handoverId)).toEqual(legacy);
  });

  it("accepts real record string and array upper bounds", async () => {
    const bounded = { ...receipt,
      permissions: Array.from({ length: 256 }, (_, i) => `permission-${i}`),
      risk_summary: Array.from({ length: 64 }, (_, i) => `risk-${i}`),
      plugin_id: "p".repeat(128), plugin_version: "v".repeat(128),
      key_id: "k".repeat(128), receipt_id: "r".repeat(128), audit_id: "a".repeat(128),
    };
    bounded.permissions[0] = "p".repeat(128);
    bounded.risk_summary[0] = "r".repeat(128);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(bounded)));
    expect(await api.pluginHandover(runId, handoverId)).toEqual(bounded);
  });

  it.each([
    { source_grant_id: null }, { source_grant_binding_sha256: null },
    { source_grant_id: "unbound" }, { source_grant_binding_sha256: "invalid" },
    { status: "enabled" }, { version: "1" }, { plan_hash: "bad" },
    { result: "executed" }, { schema_version: 2 }, { run_id: true },
    { source: { kind: "artifact", artifact_id: true } },
    { source: { kind: "workspace", project_id: "p", session_id: "s", paths: ["../outside"] } },
    { permissions: Array.from({ length: 257 }, (_, i) => `permission-${i}`) },
    { risk_summary: Array.from({ length: 65 }, (_, i) => `risk-${i}`) },
    { permissions: ["p".repeat(129)] }, { risk_summary: ["r".repeat(129)] },
    { permissions: [""] }, { risk_summary: ["unsafe/risk"] },
    { plugin_id: "p".repeat(129) }, { plugin_id: "" },
    { plugin_version: "v".repeat(129) }, { plugin_version: "" },
    { key_id: "k".repeat(129) }, { key_id: "" },
    { receipt_id: "r".repeat(129) }, { receipt_id: "" },
    { audit_id: "a".repeat(129) }, { audit_id: "" },
  ])("rejects malformed receipt facts for JSON and binary requests %#", async (patch) => {
    const fetchMock = vi.fn().mockImplementation(async () => response({ ...receipt, ...patch }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.pluginHandover(runId, handoverId))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
    await expect(api.confirmPluginHandover(runId, handoverId, confirmation, archive, "confirm-1"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([403, 404, 409, 413, 422, 500, 503])(
    "propagates server authority errors as ApiError without retries (HTTP %s)", async (status) => {
      const fetchMock = vi.fn().mockResolvedValue(response({ error: {
        code: "handover_conflict", message: "handover request rejected",
      } }, status));
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.confirmPluginHandover(runId, handoverId, confirmation, archive, "confirm-1"))
        .rejects.toMatchObject({ status, code: "handover_conflict", errorId: "unit-error" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("does not retry a network failure or trigger any other operation", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.createPluginHandover(runId, createInput, "plan-1"))
      .rejects.toMatchObject({ status: 0, code: "network_error" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([new TypeError("private body transport detail"),
    new DOMException("private cancelled body detail", "AbortError")])(
    "maps interrupted binary response bodies to safe ApiError without retry %#", async (failure) => {
      const reply = response(receipt);
      vi.spyOn(reply, "json").mockRejectedValue(failure);
      const fetchMock = vi.fn().mockResolvedValue(reply);
      vi.stubGlobal("fetch", fetchMock);
      await expect(api.confirmPluginHandover(runId, handoverId, confirmation, archive, "confirm-1"))
        .rejects.toMatchObject({ message: "network response interrupted", status: 0,
          code: "network_error", errorId: null, details: null });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][1].body).toBe(archive);
    },
  );

  it("leaves binary SyntaxError classification unchanged without retry", async () => {
    const failure = new SyntaxError("invalid JSON fixture");
    const reply = response(receipt);
    vi.spyOn(reply, "json").mockRejectedValue(failure);
    const fetchMock = vi.fn().mockResolvedValue(reply);
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.confirmPluginHandover(runId, handoverId, confirmation, archive, "confirm-1"))
      .rejects.toBe(failure);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
