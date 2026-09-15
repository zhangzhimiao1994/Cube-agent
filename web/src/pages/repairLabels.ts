const REPAIR_RECOVERY_STRATEGY_LABELS: Record<string, string> = {
  switch_to_available_model_and_retry: "切换到有容量的同类模型，保留已有产物后重试",
  retry_with_fallback_or_reassign_model: "重试失败模型调用，必要时切换备用模型或改派角色",
  reassign_tool_role_to_capable_model_and_retry: "将工具角色改派给支持所需能力的模型后重试",
  repair_plugin_endpoint_or_adapter_and_retry: "修复插件端点或适配器后只重试受影响的插件调用",
  repair_mcp_server_or_adapter_and_retry: "修复 MCP 服务或适配器后只重试受影响的 MCP 调用",
  manual_review_model_credentials: "人工检查模型凭据、API Base、权限和密钥轮换后继续",
  manual_review_model_quota_or_billing: "人工检查模型额度、账单和组织权限后继续",
  manual_review_model_deployment: "人工检查模型名、部署绑定和供应商端点后继续",
  manual_review_model_request_contract: "人工检查模型请求参数、上下文限制和工具 schema 后继续",
  manual_review_plugin_credentials: "人工检查插件凭据、授权边界和轮换状态后继续",
  manual_review_plugin_arguments: "人工检查插件入参 schema、参数结构和调用计划后继续",
  manual_review_plugin_result_contract: "人工检查插件输出契约、适配器版本和 output schema 后继续",
  manual_review_plugin_sandbox: "人工检查插件隔离策略、运行时支持和 host adapter 配置后继续",
  manual_review_mcp_configuration: "人工检查 MCP server 发现状态、工具 allowlist 和 reload 配置后继续",
  manual_review_recovery_checkpoint: "人工复核恢复检查点后继续",
  manual_review_missing_failure_event: "人工复核缺失失败事件后继续",
  preserve_outputs_and_retry_scope: "保留已有产物并缩小失败阶段后重试",
  retry_failed_step_after_context_compaction: "压缩上下文后只重试失败步骤",
  repair_tool_invocation_after_permission_check: "检查工具权限和参数后执行最小修复重试",
  compact_context_before_next_model_call: "下次模型调用前压缩上下文",
  retry_blocked_contract_chain: "重规划被阻塞的角色交接链路后重试",
  retry_blocked_contract_chain_after_replanning: "重规划被阻塞的角色交接链路后重试",
};

const REPAIR_FAILURE_KIND_LABELS: Record<string, string> = {
  runtime_failure: "运行阶段失败",
  step_failure: "执行步骤失败",
  tool_failure: "工具调用失败",
  model_timeout: "模型调用超时",
  model_rate_limited: "模型限流",
  model_provider_unavailable: "模型服务暂不可用",
  model_provider_transient_failed: "模型服务临时失败",
  model_capability_missing: "模型缺少所需能力",
  model_credential_unavailable: "模型凭据不可用",
  model_quota_or_billing_unavailable: "模型额度或账单不可用",
  model_deployment_unavailable: "模型部署不可用",
  model_request_contract_invalid: "模型请求契约无效",
  plugin_runtime_unavailable: "插件运行时不可用",
  plugin_adapter_unavailable: "插件适配器不可用",
  plugin_credential_unavailable: "插件凭据不可用",
  plugin_arguments_invalid: "插件参数无效",
  plugin_result_contract_invalid: "插件结果契约无效",
  plugin_sandbox_unavailable: "插件沙箱不可用",
  mcp_runtime_unavailable: "MCP 服务不可用",
  mcp_configuration_invalid: "MCP 配置不可用",
  missing_failure_event: "缺少失败事件",
  empty_response: "模型返回空响应",
  nonzero_exit: "命令非零退出",
  capability_failed: "能力调用失败",
  invalid_request: "请求参数无效",
  selector_missing: "选择器缺失",
  network_timeout: "网络超时",
};

const REPAIR_ACTION_LABELS: Record<string, string> = {
  draft_repair_proposal: "生成受控修复提案",
  switch_model: "切换模型后重试",
  switch_to_available_model: "切换到可用模型",
  retry_with_fallback: "启用备用路径后重试",
  retry_terminal: "重试终端步骤",
  rerun_browser_probe: "重跑浏览器探测",
  reschedule_or_reassign_model: "重新调度或改派模型",
  preserve_partial_outputs: "保留已有产物",
  request_approval: "请求人工确认",
  tool_execute: "执行工具调用",
  retry_with_fallback_or_reassign_model: "启用备用模型或改派角色后重试",
  reassign_tool_role_to_capable_model: "改派工具角色给具备能力的模型",
  repair_plugin_endpoint_or_adapter: "修复插件端点或适配器",
  repair_mcp_server_or_adapter: "修复 MCP 服务或适配器",
  preserve_outputs_and_retry_scope: "保留已有产物并缩小重试范围",
  retry_failed_step_after_context_compaction: "压缩上下文后重试失败步骤",
  manual_review: "人工复核后继续",
};

const REPAIR_ERROR_CODE_LABELS: Record<string, string> = {
  "model.empty_response": "模型空响应",
  "runtime.failed": "运行失败",
  "step.failed": "执行步骤失败",
  "tool.failed": "工具执行失败",
  "plugin.adapter_unavailable": "插件适配器不可用",
  "plugin.timeout": "插件执行超时",
  "plugin.credential_unavailable": "插件凭据不可用",
  "plugin.invalid_arguments": "插件参数无效",
  "plugin.invalid_result": "插件结果契约无效",
  "plugin.backend_unavailable": "插件后端不可用",
  "plugin.endpoint_unavailable": "插件端点不可用",
  "plugin.sandbox_unsupported": "插件沙箱不支持",
  "mcp.server_not_discovered": "MCP 服务未发现",
  "mcp.server_timeout": "MCP 服务超时",
  "mcp.server_failed": "MCP 服务失败",
  "mcp.server_unavailable": "MCP 服务不可用",
  temporary_failure: "临时故障",
};

export function repairRecoveryStrategyLabel(strategy: string | undefined | null) {
  if (!strategy) return "";
  return REPAIR_RECOVERY_STRATEGY_LABELS[strategy] ?? strategy;
}

export function repairFailureKindLabel(kind: string | undefined | null) {
  if (!kind) return "";
  return REPAIR_FAILURE_KIND_LABELS[kind] ?? kind;
}

export function repairActionLabel(action: string | undefined | null) {
  if (!action) return "";
  return REPAIR_ACTION_LABELS[action] ?? action;
}

export function repairErrorCodeLabel(code: string | undefined | null) {
  if (!code) return "";
  return REPAIR_ERROR_CODE_LABELS[code] ?? repairFailureKindLabel(code);
}
