export type SandboxProfile = "none" | "read_only" | "restricted" | "workspace_write";
export type SupportedPermissionMode = "default" | "auto_review" | "read_only";

export function sandboxPermissions(profile: SandboxProfile): string[] {
  if (profile === "none") return [];
  if (profile === "read_only") return ["workspace.read"];
  if (profile === "restricted") return ["workspace.read", "command.run"];
  return ["workspace.read", "workspace.write", "command.run"];
}

export function resolvePermissionSelection(mode: string, profile: SandboxProfile, supportedModes: readonly string[]): {
  permission_mode?: SupportedPermissionMode;
  sandbox_profile: SandboxProfile;
  requested_permissions: string[];
} {
  if (mode === "full_access") throw new Error("当前执行环境不支持完整访问权限。");
  if (mode !== "default" && mode !== "auto_review" && mode !== "read_only") throw new Error("未知权限模式。");
  const supported = supportedModes.includes(mode);
  if (!supported && mode !== "default") throw new Error("服务器尚不支持本次运行权限模式，请先更新服务。");
  const effectiveProfile = mode === "read_only" ? "read_only" : profile;
  return {
    ...(supported ? { permission_mode: mode } : {}),
    sandbox_profile: effectiveProfile,
    requested_permissions: sandboxPermissions(effectiveProfile),
  };
}
