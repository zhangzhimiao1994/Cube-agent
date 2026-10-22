import { describe, expect, it } from "vitest";
import { resolvePermissionSelection } from "./permissionSelection";

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
