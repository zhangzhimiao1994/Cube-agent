import { describe, expect, it } from "vitest";

import { ApiError, formatApiError } from "./client";

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
