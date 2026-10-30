export type SandboxProfile = "none" | "read_only" | "restricted" | "workspace_write";
export type SupportedPermissionMode = "default" | "auto_review" | "read_only";

/** Never infer model readiness from a draft, alias, budget or tool_approval_mode. */
export function approvalReviewPresentation(settings?: {
  approval_review?: { engine: string };
  approval_review_readiness?: { status: string; reason_code: string; supported_scope?: readonly string[] };
}) {
  if (settings?.approval_review?.engine !== "rules_then_model") return {
    summary: "规则审查", status: "仅规则审查，未启用独立模型。",
    description: "仅规则审查，非独立模型；其余动作仍需人工确认。",
  };
  if (settings.approval_review_readiness?.status === "ready") {
    const scope = settings.approval_review_readiness.supported_scope ?? [];
    const scopeText = scope.includes("fresh_original_run") && scope.includes("workspace.write_text")
      ? "当前仅支持新建原始运行的工作区文本写入。" : "仅在服务器支持的范围内生效。";
    return {
      summary: "模型审查", status: `独立模型审查已就绪（服务器确认）。${scopeText}`,
      description: `规则检查后由独立模型审查；${scopeText}其余动作仍可能需人工确认。`,
    };
  }
  const reasons: Record<string, string> = {
    rules_only: "服务器当前仅启用规则审查。",
    runtime_not_ready: "服务尚未完成模型审查运行支持，请联系管理员完成配置。",
    model_alias_missing: "模型目录中未找到审查模型，请重新选择已有模型。",
    metadata_missing: "审查模型信息尚不完整，请联系管理员补全模型目录。",
    pricing_missing: "审查模型缺少计价信息，请联系管理员配置价格。",
    capability_missing: "所选模型尚未声明所需审查能力，请检查模型配置。",
    provider_contract_missing: "模型提供方的审查支持尚未验证，请联系管理员。",
    current_mode_fence_unverified: "当前权限边界尚未验证，请联系管理员完成服务配置。",
    transport_unsupported: "服务尚不支持该模型的调用方式，请选择受支持的模型。",
  };
  const code = settings.approval_review_readiness?.reason_code ?? "";
  const reason = Object.hasOwn(reasons, code) ? reasons[code] : "服务器尚未确认模型审查可用，请检查服务配置。";
  return { summary: "模型未就绪", status: `独立模型审查未就绪：${reason}`,
    description: `模型未就绪：${reason}规则审查仍可选择，需审批的动作由人工确认。` };
}

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
