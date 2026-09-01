import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, formatApiError } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("formatApiError", () => {
  it("includes safe structured details without dumping unrelated values", () => {
    const error = new ApiError("model transport failed", 503, "model_check_failed", "err-123", {
      logical_model: "main_agent",
      provider: "litellm",
      upstream_model: "qwen-max",
      status_code: "503",
      hint: "Check model config and rate limits.",
      credential_ref: "secret://private-key",
      traceback: "private stack",
    });

    const formatted = formatApiError(error, "模型测试失败");

    expect(formatted).toContain("模型测试失败: model transport failed");
    expect(formatted).toContain("model_check_failed");
    expect(formatted).toContain("HTTP 503");
    expect(formatted).toContain("error err-123");
    expect(formatted).toContain("logical_model=main_agent");
    expect(formatted).toContain("provider=litellm");
    expect(formatted).toContain("upstream_model=qwen-max");
    expect(formatted).toContain("status_code=503");
    expect(formatted).toContain("hint=Check model config and rate limits.");
    expect(formatted).not.toContain("secret://private-key");
    expect(formatted).not.toContain("private stack");
  });
});

describe("api client transport", () => {
  it("disables browser caching for API reads used by live run surfaces", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.runs();

    expect(fetchMock.mock.calls[0]?.[0]).toMatch(/^\/api\/v1\/admin\/runs\?_=/);
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ cache: "no-store" }));
  });
});
