import { describe, expect, it } from "vitest";
import { approvalReviewPresentation, resolvePermissionSelection } from "./permissionSelection";

describe("permission submission contract", () => {
  it("submits reviewer choice only after the server advertises support", () => {
    expect(resolvePermissionSelection("auto_review", "workspace_write", ["default", "auto_review", "read_only"])).toEqual({
      permission_mode: "auto_review", sandbox_profile: "workspace_write",
      requested_permissions: ["workspace.read", "workspace.write", "command.run"],
    });
    expect(() => resolvePermissionSelection("auto_review", "workspace_write", [])).toThrow(/服务器/);
  });
  it("never adds write or command authority to read-only", () => {
    expect(resolvePermissionSelection("read_only", "workspace_write", ["read_only"])).toEqual({
      permission_mode: "read_only", sandbox_profile: "read_only", requested_permissions: ["workspace.read"],
    });
    expect(() => resolvePermissionSelection("read_only", "read_only", [])).toThrow(/服务器/);
  });
  it("retains legacy API fields for a default selection without claiming reviewer support", () => {
    expect(resolvePermissionSelection("default", "restricted", [])).toEqual({
      sandbox_profile: "restricted", requested_permissions: ["workspace.read", "command.run"],
    });
  });
  it("does not interpret no workspace authorization as full machine access", () => {
    expect(resolvePermissionSelection("default", "none", ["default"])).toEqual({
      permission_mode: "default", sandbox_profile: "none", requested_permissions: [],
    });
    expect(() => resolvePermissionSelection("full_access", "none", ["full_access"])).toThrow(/完整访问/);
  });
});

it("describes only server-confirmed model availability, including changes", () => {
  expect(approvalReviewPresentation(undefined).summary).toBe("规则审查");
  const config = { engine: "rules_then_model" as const };
  expect(approvalReviewPresentation({ approval_review: config }).summary).toBe("模型未就绪");
  expect(approvalReviewPresentation({ approval_review: config, approval_review_readiness: { status: "ready", reason_code: "ready" } }).summary).toBe("模型审查");
  expect(approvalReviewPresentation({ approval_review: config, approval_review_readiness: { status: "not_ready", reason_code: "runtime_not_ready" } }).description).toContain("服务尚未");
  expect(approvalReviewPresentation({ approval_review: { engine: "rules" }, approval_review_readiness: { status: "ready", reason_code: "ready" } }).summary).toBe("规则审查");
});

it.each([
  ["model_alias_missing", "模型目录"], ["metadata_missing", "模型信息"], ["pricing_missing", "计价"],
  ["capability_missing", "审查能力"], ["provider_contract_missing", "提供方"],
  ["current_mode_fence_unverified", "权限边界"], ["transport_unsupported", "调用方式"],
  ["unknown_future_reason", "尚未确认"],
])("explains server reason %s without internal codes", (reason_code, text) => {
  const result = approvalReviewPresentation({ approval_review: { engine: "rules_then_model" },
    approval_review_readiness: { status: "not_ready", reason_code } });
  expect(result.description).toContain(text);
  expect(result.description).not.toContain(reason_code);
});

it("describes the server's bounded ready scope without promising model review for every action", () => {
  const result = approvalReviewPresentation({ approval_review: { engine: "rules_then_model" },
    approval_review_readiness: { status: "ready", reason_code: "ready", supported_scope: ["fresh_original_run", "workspace.write_text"] } });
  expect(result.description).toContain("新建原始运行");
  expect(result.description).toContain("文本写入");
});

it.each(["constructor", "__proto__", "toString"])("uses the safe fallback for prototype reason %s", (reason_code) => {
  const result = approvalReviewPresentation({ approval_review: { engine: "rules_then_model" },
    approval_review_readiness: { status: "not_ready", reason_code } });
  expect(result.status).toContain("服务器尚未确认模型审查可用，请检查服务配置。");
  expect(result.description).not.toMatch(/native code|object Object|function/);
});
