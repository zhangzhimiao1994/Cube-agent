"""Shared redacted runtime failure diagnostics."""

from __future__ import annotations

import re
from functools import cache
from typing import get_args

from agent_hub.capabilities.project_prerequisites import (
    project_prerequisite_reason_category,
    validated_project_prerequisite_payload,
)
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityRequestTooLarge,
    CapacityUnavailable,
    CapacityWaitTimeout,
    capacity_backend_diagnostic_payload,
)
from agent_hub.models.gateway import (
    GatewayNegotiatedRejectedOutput,
    GatewayRejectedOutput,
    ModelContextWindowExceeded,
    ModelGatewayError,
    get_gateway_scope_diagnostic,
    is_model_negotiation_exhausted,
)
from agent_hub.models.litellm_client import (
    ModelResponseError,
    ModelTransportError,
)
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import RejectedOutputEvidence

MAX_FAILURE_REASON_LENGTH = 240
RuntimeFailureDiagnostic = dict[str, str | int | bool]
SENSITIVE_FAILURE_REASON = re.compile(
    r"(api[_-]?key|authorization|bearer|credential|password|secret|(?:access|refresh|session)[_-]?token|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
_STATUS_CODE = re.compile(r"\(status=(?P<status>[1-5][0-9]{2})\)")
_TRACEBACK = re.compile(r"\btraceback\b", re.IGNORECASE)
_REJECTED_OUTPUT_ERRORS = (GatewayRejectedOutput, GatewayNegotiatedRejectedOutput)
_REJECTED_OUTPUT_BASE = "model gateway failed: model response rejected"
_REJECTED_OUTPUT_FAILURE = re.compile(
    re.escape(_REJECTED_OUTPUT_BASE)
    + r"(?: \(reason=(?P<rejected_output_reason>invalid_json|schema_mismatch|output_limit|"
    r"invalid_output|invalid_tool|incomplete|refusal|usage_missing|usage_invalid|configuration_error); "
    r"status=(?P<rejected_output_status>completed|incomplete|refused|unknown); "
    r"usage_status=(?P<rejected_usage_status>known|missing|invalid)\))?"
)
_CREWAI_STEP_TIMEOUT = re.compile(
    r"^CrewAI step timed out: step=(?P<step>[A-Za-z0-9_.-]{1,128}) actor=(?P<actor>[A-Za-z0-9_.-]{1,128})$"
)
_HYBRID_CHILD_FAILURE = re.compile(
    r"^hybrid (?P<mode>direct|dispatch|discuss) failed: (?P<reason>.+)$",
    re.IGNORECASE,
)
_DELIVERY_REPORT_BASE = "direct final report is invalid"
_DELIVERY_REPORT_FAILURE = re.compile(
    re.escape(_DELIVERY_REPORT_BASE)
    + r"(?: ?\(reason=(?P<reason>completion_missing|tool_calls|empty_text|json_only|binding_invalid)\))?"
)
_PREREQUISITE_FAILURE = re.compile(
    r"(?:hybrid (?P<mode>direct|dispatch|discuss) failed: )?"
    r"project prerequisite blocked: (?P<reason>[a-z0-9_]{1,64})"
)
_VERIFICATION_BASE = "project verification failed"
_VERIFICATION_PREFIX_CHAIN = re.compile(
    r"(?:(?i:hybrid (?:direct|dispatch|discuss) failed: ))*" + re.escape(_VERIFICATION_BASE)
)
_VERIFICATION_CODES = frozenset({
    "tool_unavailable", "capability_denied", "approval_required", "identity_unavailable",
    "permission_mapping_unavailable", "sandbox_denied", "scope_denied", "backend_unavailable",
    "backend_unsupported", "source_changed", "blocked_prerequisites", "malformed_report",
    "outcome_unconfirmed", "tool_failed", "failed", "invalid_response", "truncated_response",
    "not_passed", "timeout", "execution_failed",
})
_VERIFICATION_LITERALS = {code.replace("_", " "): code for code in _VERIFICATION_CODES}
_VERIFICATION_BACKENDS = ((_VERIFICATION_CODES - {
        "tool_failed", "failed", "invalid_response", "truncated_response", "not_passed", "timeout", "execution_failed",
    }) | {
    "python_project_product_unavailable", "python_project_report_invalid",
    "python_project_response_invalid", "python_project_service_unavailable", "python_project_checks_failed",
    "python_project_systemd_linux_unsupported", "python_project_recipe_invalid",
    "python_project_recipe_unsupported", "python_project_lock_invalid",
    "python_project_declaration_limit", "python_project_install_approval_required",
    "generated_python_project_unavailable", "generated_acceptance_scope_changed",
    "generated_acceptance_state_invalid", "generated_acceptance_persistence_unavailable",
    "generated_acceptance_unknown_no_recapture", "generated_acceptance_unavailable_or_changed",
    "generated_acceptance_manifest_required", "generated_acceptance_scaffold_incomplete",
    "generated_acceptance_python_declaration_invalid", "generated_acceptance_node_declaration_invalid",
    "generated_acceptance_test_execution_invalid", "generated_acceptance_test_evidence_missing",
    "input_supply_approval_required",
})
_VERIFICATION_FAILURE = re.compile(re.escape(_VERIFICATION_BASE)
    + r"(?:: (?P<failure>[a-z ]{1,80}))?(?: \(backend=(?P<backend>[a-z0-9_.]{1,160})\))?")
_VERIFICATION_PERMISSION_CODES = frozenset({
    "capability_denied", "approval_required", "identity_unavailable", "permission_mapping_unavailable",
    "sandbox_denied", "scope_denied", "python_project_recipe_approval_required",
    "python_project_install_approval_required", "input_supply_approval_required",
})
GENERIC_MODEL_GATEWAY_FAILURE = "model gateway failed"
LEGACY_GENERIC_FAILURES = frozenset(
    {
        "model gateway failed",
        "discussion_failed",
        "dispatch execution failed",
        "step execution failed",
        "runtime_failed",
    }
)
SAFE_MODEL_GATEWAY_FAILURES = frozenset(
    {
        "model context window exceeded",
        "model capacity unavailable",
        "model transport failed",
        "model outcome recording failed",
        "model capacity release failed",
        "model gateway completed without a response",
        "model response text is empty",
        "model response is empty",
    }
)
EMPTY_MODEL_RESPONSE_ACTION = (
    "模型返回了空内容；先压缩输入和历史上下文，必要时拆分提示、标记模型 fallback "
    "或切换备用模型后重试，重试后仍失败则保留中断前输出并闭环失败原因。"
)
RECOVERY_BLOCKED_FAILURE_REASON = (
    "runtime recovery blocked: non-replayable event after checkpoint"
)
ORCHESTRATION_CHECKPOINT_FAILURE_REASON = (
    "runtime orchestration checkpoint is incompatible"
)


def _rejected_output_failure_reason(error: GatewayRejectedOutput) -> str:
    evidence = error.evidence
    if type(evidence) is not RejectedOutputEvidence:
        return _REJECTED_OUTPUT_BASE
    # Read only finite fields; provider text, provenance and exception args stay private.
    parts = tuple(getattr(evidence, key, None) for key in ("reason", "status", "usage_status"))
    if not all(type(value) is str and len(value) <= 32 for value in parts):
        return _REJECTED_OUTPUT_BASE
    candidate = (f"{_REJECTED_OUTPUT_BASE} (reason={parts[0]}; "
                 f"status={parts[1]}; usage_status={parts[2]})")
    return candidate if _REJECTED_OUTPUT_FAILURE.fullmatch(candidate) else _REJECTED_OUTPUT_BASE


def safe_model_gateway_failure_reason(error: Exception) -> str | None:
    """Return a stable model-gateway diagnostic that is safe to show in the UI."""

    if is_model_negotiation_exhausted(error):
        reason = f"{GENERIC_MODEL_GATEWAY_FAILURE}: model parameter negotiation exhausted"
        status = error.status_code if isinstance(error, ModelTransportError) else None
        return reason if status is None else f"{reason} (status={status})"
    if isinstance(error, GatewayRejectedOutput) and type(error) in _REJECTED_OUTPUT_ERRORS:
        return _rejected_output_failure_reason(error)
    if isinstance(error, ModelTransportError):
        base = (
            "model response failed"
            if isinstance(error, ModelResponseError)
            else "model transport failed"
        )
        if error.status_code is not None:
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {base} (status={error.status_code})"
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {base}"
    if isinstance(error, NoCapableDeployment):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: no capable deployment"
    if isinstance(error, ModelContextWindowExceeded):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model context window exceeded"
    if isinstance(error, CapacityRequestTooLarge):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model request exceeds capacity token limit"
    if isinstance(error, CapacityQueueFull):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity queue is full"
    if isinstance(error, CapacityWaitTimeout):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity queue timeout"
    if isinstance(error, CapacityUnavailable):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity unavailable"
    if isinstance(error, CapacityConfigurationError):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity configuration failed"
    if isinstance(error, CapacityBackendError):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity backend failed"
    if isinstance(error, ModelGatewayError):
        message = normalize_failure_reason(str(error))
        if message == "model credential resolution failed":
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model configuration failed"
        if message in SAFE_MODEL_GATEWAY_FAILURES:
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {message}"
    return None


def _prerequisite_failure_parts(reason: str) -> tuple[str, str, str | None] | None:
    match = _PREREQUISITE_FAILURE.fullmatch(reason)
    if match is None:
        return None
    category = project_prerequisite_reason_category(match["reason"])
    return None if category is None else (match["reason"], category, match["mode"])


def _delivery_report_failure_parts(reason: str) -> tuple[str, str, str | None] | None:
    child = _HYBRID_CHILD_FAILURE.fullmatch(reason)
    candidate = reason if child is None else child["reason"]
    if candidate != _DELIVERY_REPORT_BASE and not candidate.startswith(
        (_DELIVERY_REPORT_BASE + " (", _DELIVERY_REPORT_BASE + "("),
    ):
        return None
    match = _DELIVERY_REPORT_FAILURE.fullmatch(candidate)
    category = "unknown" if match is None or match["reason"] is None else match["reason"]
    # Never project an unrecognized suffix or attached provider text. A legacy
    # aggregate guard cannot establish which branch rejected the old report.
    summary = _DELIVERY_REPORT_BASE
    if category != "unknown":
        summary += f" (reason={category})"
    return category, summary, None if child is None else child["mode"].lower()


@cache
def _verification_backend_literals() -> frozenset[str]:
    # Protocol modules depend on run repositories. Resolve their finite enums
    # only after import initialization; ordinary model diagnostics need none.
    from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureError
    from agent_hub.skills.sandbox.python_input_supply_protocol import SupplyError
    from agent_hub.skills.sandbox.python_metadata_preparation_client import PUBLIC_FAILURE_CODES
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerError

    return (_VERIFICATION_BACKENDS | frozenset(get_args(PythonProjectBrokerError))
        | frozenset(get_args(SupplyError)) | frozenset(get_args(PythonDependencyClosureError.__value__))
        | PUBLIC_FAILURE_CODES)


def _project_verification_parts(reason: str) -> tuple[str, str | None, str, str | None] | None:
    child = _HYBRID_CHILD_FAILURE.fullmatch(reason)
    candidate = reason if child is None else child["reason"]
    match = _VERIFICATION_FAILURE.fullmatch(candidate)
    if match is None:
        return None
    code = "failed" if match["failure"] is None else _VERIFICATION_LITERALS.get(match["failure"])
    backend = match["backend"]
    if code is None or backend is not None and backend not in _verification_backend_literals():
        return None
    summary = _VERIFICATION_BASE
    if code != "failed":
        summary += ": " + code.replace("_", " ")
    if backend is not None:
        summary += " (backend=" + backend + ")"
    return code, backend, summary, None if child is None else child["mode"].lower()


def _project_verification_candidate(reason: str) -> bool:
    # Detect the strict wrapper prefix chain for rejection without a depth
    # budget or recursive classification. Only _parts may accept a diagnostic;
    # nested wrappers remain invalid even around a finite backend literal.
    return _VERIFICATION_PREFIX_CHAIN.match(reason) is not None


def is_public_project_prerequisite_text(value: object) -> bool:
    """Recognize finite diagnostic literals, never free text or secret field names."""
    if type(value) is not str:
        return False
    if project_prerequisite_reason_category(value) is not None:
        return True
    code_prefix = "project.prerequisite."
    if value.startswith(code_prefix):
        return project_prerequisite_reason_category(value[len(code_prefix):]) is not None
    return _prerequisite_failure_parts(value) is not None


def safe_runtime_failure_reason(error: Exception, *, fallback: str = "runtime_failed") -> str:
    gateway_reason = safe_model_gateway_failure_reason(error)
    if gateway_reason is not None:
        return gateway_reason
    prerequisite = validated_project_prerequisite_payload(
        getattr(error, "project_prerequisite_payload", None),
    )
    if prerequisite is not None:
        return "project prerequisite blocked: " + str(prerequisite["reason"])
    reason = normalize_failure_reason(str(error))
    verification = _project_verification_parts(reason)
    if verification is not None:
        _, _, summary, child_mode = verification
        return summary if child_mode is None else f"hybrid {child_mode} failed: {summary}"
    if _project_verification_candidate(reason):
        return fallback
    report = _delivery_report_failure_parts(reason)
    if report is not None:
        _, summary, child_mode = report
        return summary if child_mode is None else f"hybrid {child_mode} failed: {summary}"
    if _prerequisite_failure_parts(reason) is not None:
        # Only finite diagnostic codes may cross Hybrid's string boundary.
        # No arbitrary authorization text or execution authority is preserved.
        return reason
    if not is_safe_failure_reason(reason):
        return fallback
    return reason[:MAX_FAILURE_REASON_LENGTH]


def safe_runtime_failure_diagnostic(
    error: Exception,
    *,
    fallback: str = "runtime_failed",
) -> RuntimeFailureDiagnostic:
    """Return a redacted, structured diagnostic payload for failed runs."""

    reason = safe_runtime_failure_reason(error, fallback=fallback)
    status_code = error.status_code if isinstance(error, ModelTransportError) else None
    diagnostic = runtime_failure_diagnostic_from_reason(reason, status_code=status_code)
    if type(error) is GatewayNegotiatedRejectedOutput:
        # Keep the stop reason and existing event schema; finite facts fit in its summary.
        match = _REJECTED_OUTPUT_FAILURE.fullmatch(_rejected_output_failure_reason(error))
        if match is not None and match["rejected_output_reason"] is not None:
            diagnostic["error_summary"] = (
                reason + match[0][len(_REJECTED_OUTPUT_BASE):]
            )[:MAX_FAILURE_REASON_LENGTH]
    return {**diagnostic, **gateway_scope_failure_payload(error)}


def gateway_scope_failure_payload(
    error: object, *, include_noncapacity: bool = False,
) -> RuntimeFailureDiagnostic:
    """Project issued observations; optional wait diagnostics preserve legacy defaults."""
    scope = get_gateway_scope_diagnostic(error)
    details = capacity_backend_diagnostic_payload(None if scope is None else scope.capacity_backend)
    if scope is None or (details is None and not include_noncapacity):
        return {}
    return {
        'gateway_scope_phase': scope.phase.value, 'gateway_scope_reason': scope.reason.value,
        'gateway_scope_transport_entered_count': scope.transport_entered_count,
        'gateway_scope_failure_attempt_count': scope.failure_attempt_count,
        **{'capacity_' + key: value for key, value in (details or {}).items()},
    }


def runtime_failure_diagnostic_from_reason(
    reason: str | None,
    *,
    status_code: int | None = None,
) -> RuntimeFailureDiagnostic:
    """Classify an already-redacted failure reason into UI/operator hints."""

    normalized = normalize_failure_reason(reason or "") or "runtime_failed"
    verification = _project_verification_parts(normalized)
    if verification is not None:
        code, backend, summary, child_mode = verification
        permission = code in _VERIFICATION_PERMISSION_CODES or backend in _VERIFICATION_PERMISSION_CODES
        category = "permission" if permission else "backend" if backend is not None else (
            "unknown" if code == "failed" else code)
        action = (
            "核查当前身份与项目权限；当前具体动作仍走原 capability 审批路径，"
            "不得全局放宽权限、自动重试或重放旧 FAILED。"
            if permission else
            "检查并对账既有 execution（inspect/reconcile existing execution）及清理、移交和 receipt 状态；"
            "UNKNOWN 或 cleanup uncertain 不能推定成功或失败，不自动重试、修补平台或重跑原 FAILED。"
        )
        diagnostic = _base_diagnostic(summary, error_stage="project_verification", error_category=category,
            error_code="project_verification." + (backend or code), retryable=False, suggested_action=action)
        if child_mode is not None:
            diagnostic["hybrid_child_mode"] = child_mode
        return diagnostic
    if _project_verification_candidate(normalized):
        normalized = "runtime_failed"
    report = _delivery_report_failure_parts(normalized)
    if report is not None:
        category, summary, child_mode = report
        diagnostic = _base_diagnostic(
            summary, error_stage="delivery_report", error_category=category,
            error_code="delivery_report." + category, retryable=False,
            suggested_action=(
                "最终报告未通过交付校验；仅记录有限诊断并保留当前验证证据。"
                "当前没有安全可执行的报告恢复动作，不自动重试、修补平台或恢复旧失败运行。"
            ),
        )
        if child_mode is not None:
            diagnostic["hybrid_child_mode"] = child_mode
        return diagnostic
    prerequisite = _prerequisite_failure_parts(normalized)
    if prerequisite is not None:
        prerequisite_reason, prerequisite_category, child_mode = prerequisite
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="project_prerequisite",
            error_category=prerequisite_category,
            error_code="project.prerequisite." + prerequisite_reason,
            retryable=False,
            suggested_action="项目执行环境尚未就绪；应先修复环境或完成受控安装，再验证原项目，不要盲目修改源码或重放旧运行。",
        )
        if child_mode is not None:
            diagnostic["hybrid_child_mode"] = child_mode
        return diagnostic
    admission = re.fullmatch(
        r'engineering permission gate failed: (denied|gate_unavailable|identity_unavailable|tool_unavailable|invalid_approval_receipt|approval_unavailable|invalid_result|unclassified)',
        normalized,
    )
    if admission is not None:
        return _base_diagnostic(
            normalized,
            error_stage='engineering_admission',
            error_category='permission_gate',
            error_code='runtime.engineering_permission.' + admission[1],
            retryable=False,
            suggested_action='项目尚未开始执行；请核查当前用户的项目权限和持久化审批记录，不要重放旧运行。',
        )
    if normalized.lower().startswith("capability failed:"):
        return _base_diagnostic(
            "capability failed",
            error_stage="capability",
            error_category="execution_failed",
            error_code="capability.execution_failed",
            retryable=False,
            suggested_action="工具执行被拒绝或失败；查看工具失败事件中的字段校验摘要，修正参数后重试。",
        )
    lowered = normalized.lower()
    if lowered in {
        "plugin tool timed out",
        "plugin credential unavailable",
        "plugin backend unavailable",
        "plugin sandbox profile unsupported",
        "plugin result is invalid",
        "plugin output schema is invalid",
    }:
        plugin_diagnostic = _plugin_runtime_diagnostic(
            normalized,
            lowered,
            status_code=status_code,
        )
        if plugin_diagnostic is not None:
            return plugin_diagnostic
    if not is_safe_failure_reason(normalized):
        normalized = "runtime_failed"
    if status_code is None:
        status_code = _status_code_from_reason(normalized)
    lowered = normalized.lower()

    if lowered == "capability transient execution failed":
        return _base_diagnostic(
            normalized,
            error_stage="capability",
            error_category="transient_execution_failed",
            error_code="capability.transient_execution_failed",
            retryable=True,
            suggested_action=(
                "replay-safe 工具执行遇到临时异常；先压缩输入和工具参数，必要时拆分提示、"
                "标记模型 fallback 后重试，重试后仍失败则保留中断前输出并闭环失败原因。"
            ),
            status_code=status_code,
        )
    if lowered == "planned capability is unavailable":
        return _base_diagnostic(
            normalized,
            error_stage="capability",
            error_category="planned_capability_unavailable",
            error_code="capability.planned_unavailable",
            retryable=False,
            suggested_action=(
                "计划中的工具能力在执行前已经不可用；请重新加载插件/MCP 能力清单，"
                "重新规划角色工具边界，或切换到仍可用的能力后再重试。"
            ),
            status_code=status_code,
        )
    if lowered in {"plugin.adapter_unavailable", "plugin adapter unavailable"} or (
        "plugin_package_adapter_unavailable" in lowered
    ):
        return _base_diagnostic(
            normalized,
            error_stage="plugin_runtime",
            error_category="adapter_unavailable",
            error_code="plugin.adapter_unavailable",
            retryable=False,
            suggested_action=(
                "插件能力已注册但插件适配器不可用；请检查插件运行时适配器注册状态，"
                "重新加载插件/MCP 能力清单，或切换到仍可用的等价能力后再重试。"
            ),
            status_code=status_code,
        )
    plugin_diagnostic = _plugin_runtime_diagnostic(normalized, lowered, status_code=status_code)
    if plugin_diagnostic is not None:
        return plugin_diagnostic
    mcp_diagnostic = _mcp_runtime_diagnostic(normalized, lowered, status_code=status_code)
    if mcp_diagnostic is not None:
        return mcp_diagnostic
    if lowered == "capability outcome requires confirmation":
        return _base_diagnostic(
            normalized,
            error_stage="capability",
            error_category="outcome_uncertain",
            error_code="capability.outcome_uncertain",
            retryable=False,
            suggested_action=(
                "工具结果可能已经产生不可确认副作用，系统已停止自动重放；"
                "请查看上一条工具事件并人工确认状态后再继续。"
            ),
            status_code=status_code,
        )
    if normalized == RECOVERY_BLOCKED_FAILURE_REASON:
        return _base_diagnostic(
            normalized,
            error_stage="runtime_recovery",
            error_category="non_replayable_event_after_checkpoint",
            error_code="runtime.recovery_blocked",
            retryable=False,
            suggested_action=(
                "恢复点之后已经存在不可重放事件，系统已阻止自动恢复以避免重复副作用；"
                "请检查最后一个 checkpoint 之后的事件和外部产物状态，再人工决定是否新建任务或补偿。"
            ),
            status_code=status_code,
        )
    if normalized == ORCHESTRATION_CHECKPOINT_FAILURE_REASON:
        return _base_diagnostic(
            normalized,
            error_stage="runtime_recovery",
            error_category="orchestration_checkpoint_incompatible",
            error_code="runtime.orchestration_checkpoint_incompatible",
            retryable=False,
            suggested_action=(
                "角色交接恢复点与当前编排计划不一致，系统已阻止自动恢复以避免跳过或重复执行角色步骤；"
                "请按角色交接恢复链重新规划，或从安全检查点重新运行。"
            ),
            status_code=status_code,
        ) | {"orchestration_recovery_hint": "retry_blocked_contract_chain"}

    hybrid_child = _HYBRID_CHILD_FAILURE.fullmatch(normalized)
    if hybrid_child is not None:
        inner = normalize_failure_reason(hybrid_child.group("reason"))
        inner_diagnostic = runtime_failure_diagnostic_from_reason(
            inner,
            status_code=status_code,
        )
        inner_diagnostic["hybrid_child_mode"] = hybrid_child.group("mode").lower()
        if inner_diagnostic.get("error_code") != "runtime.failed":
            return inner_diagnostic
        return _base_diagnostic(
            normalized,
            error_stage=f"hybrid_{hybrid_child.group('mode').lower()}",
            error_category="child_runtime_failed",
            error_code="runtime.hybrid_child_failed",
            retryable=True,
            suggested_action=(
                "Hybrid 子流程失败；查看该子流程上一条 step/tool 事件，若已有可下载产物先下载核验，"
                "再压缩输入、拆分任务或切换备用模型后重试。"
            ),
        ) | {"hybrid_child_mode": hybrid_child.group("mode").lower()}

    if "model response text is empty" in lowered or "model response is empty" in lowered:
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="model_response",
            error_category="empty_response",
            error_code="model.empty_response",
            retryable=True,
            suggested_action=EMPTY_MODEL_RESPONSE_ACTION,
            status_code=status_code,
        )
        return diagnostic

    diagnostic = _base_diagnostic(
        normalized,
        error_stage="runtime",
        error_category="internal",
        error_code="runtime.failed",
        retryable=False,
        suggested_action="查看运行详情中的上一条失败事件；如果失败重复出现，检查对应模式、工具或模型配置。",
    )

    crew_timeout = _CREWAI_STEP_TIMEOUT.fullmatch(normalized)
    if crew_timeout is not None:
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="crew_step",
            error_category="step_timeout",
            error_code="crew.step_timeout",
            retryable=True,
            suggested_action="CrewAI 子 Agent 步骤执行超时；可压缩该步骤输入、切换更快模型、提高审查/汇总步骤超时或降低并发等待。",
        )
        for field, group in (("step_id", "step"), ("actor", "actor")):
            identifier = crew_timeout.group(group)
            if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", identifier) is not None:
                diagnostic[field] = identifier
    elif "model gateway failed" in lowered:
        diagnostic = _model_gateway_diagnostic(normalized, status_code=status_code)
    elif normalized == "runtime_not_configured":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_configuration",
            error_category="missing_runtime",
            error_code="runtime.not_configured",
            retryable=False,
            suggested_action="当前运行模式没有可用运行时；请检查服务启动参数和模式注册配置后重试。",
        )
    elif normalized == "dispatch accounting exhausted":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_accounting",
            error_category="accounting_guardrail",
            error_code="runtime.dispatch_accounting_exhausted",
            retryable=False,
            suggested_action="派单运行的账本或预算记录已耗尽，系统已停止交付；请检查任务预算、模型用量记录和产物提交记录。",
        )
    elif normalized == "dispatch budget exhausted":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_accounting",
            error_category="budget_exhausted",
            error_code="runtime.dispatch_budget_exhausted",
            retryable=False,
            suggested_action="派单运行已达到任务或步骤预算上限；请拆分任务、提高交互预算或降低并发/输出规模后重试。",
        )
    elif normalized == "dispatch usage unaccounted":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_accounting",
            error_category="unaccounted_usage",
            error_code="runtime.dispatch_usage_unaccounted",
            retryable=False,
            suggested_action="派单运行的模型用量或成本记录缺失；请检查模型网关用量回传和产物入账链路后重试。",
        )
    elif normalized == "dispatch accounting audit overflow":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_accounting",
            error_category="audit_overflow",
            error_code="runtime.dispatch_accounting_audit_overflow",
            retryable=False,
            suggested_action="派单账本审计值超出安全上限；请检查模型用量/成本是否异常放大，并拆分任务后重试。",
        )
    elif normalized == "artifact rollback failed":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="artifact_storage",
            error_category="rollback_failed",
            error_code="artifact.rollback_failed",
            retryable=False,
            suggested_action="产物写入回滚失败；请检查文件存储/对象存储状态，确认残留产物后再重试。",
        )
    elif normalized == "model response incomplete":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="model_response",
            error_category="incomplete_response",
            error_code="model.incomplete_response",
            retryable=True,
            suggested_action=(
                "模型响应未完成；已确认的用量仍会入账。仅在预算允许且没有工具副作用时，"
                "使用原角色、模型和契约进行有界的紧凑重生成；不会执行残缺内容或重发结果未知的请求。"
            ),
        )
        diagnostic["compression_trigger"] = "incomplete_response"
    elif normalized == "structured output invalid":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="model_response",
            error_category="structured_output_invalid",
            error_code="model.structured_output_invalid",
            retryable=True,
            suggested_action=(
                "模型返回内容不符合结构化输出契约；请压缩上下文，强调只返回 JSON，"
                "必要时切换支持结构化输出的备用模型或重规划被阻塞的角色交接链后重试。"
            ),
        )
    elif normalized == "dispatch execution failed":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="dispatch_runtime",
            error_category="child_runtime_failed",
            error_code="runtime.dispatch_failed",
            retryable=True,
            suggested_action=(
                "派单执行失败；优先查看最近的 step/tool 失败诊断和已保存产物，"
                "再压缩输入、拆分任务或切换备用模型后重试。"
            ),
        )
    elif normalized == "step execution failed":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="crew_step",
            error_category="step_failed",
            error_code="crew.step_failed",
            retryable=True,
            suggested_action=(
                "子 Agent 步骤失败；优先重试该步骤或降低该步骤负载，"
                "仍失败时保留已有产物并关闭失败原因。"
            ),
        )
    elif normalized == "discussion_failed":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="discussion_runtime",
            error_category="discussion_failed",
            error_code="runtime.discussion_failed",
            retryable=True,
            suggested_action="讨论流程失败；压缩讨论上下文或改为更小的子问题后重试，必要时切换备用模型。",
        )
    elif lowered in {"unaccounted_usage", "model_outcome_uncertain", "tool_outcome_uncertain"}:
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime_accounting",
            error_category="accounting_guardrail",
            error_code=f"runtime.{lowered}",
            retryable=False,
            suggested_action="运行结果账本不完整，系统已阻止继续交付；请检查模型/工具结果记录链路后重试。",
        )
    elif "timeout" in lowered or "timed out" in lowered:
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="runtime",
            error_category="timeout",
            error_code="runtime.timeout",
            retryable=True,
            suggested_action="任务运行超时；可缩小任务范围、降低并发或稍后重试。",
        )
    return diagnostic


def _plugin_runtime_diagnostic(
    reason: str,
    lowered: str,
    *,
    status_code: int | None,
) -> RuntimeFailureDiagnostic | None:
    if lowered == "plugin tool timed out":
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="timeout",
            error_code="plugin.timeout",
            retryable=True,
            suggested_action="插件执行超时；降低该插件输入规模或并发，必要时切换等价能力后重试。",
            status_code=status_code,
        )
    if lowered == "plugin credential unavailable":
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="credential_unavailable",
            error_code="plugin.credential_unavailable",
            retryable=False,
            suggested_action="插件凭证不可用；检查插件 secret 引用、凭证解析配置和租户授权后重试。",
            status_code=status_code,
        )
    if lowered in {"plugin_disabled", "plugin.disabled"} or "plugin_disabled" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="disabled",
            error_code="plugin.disabled",
            retryable=False,
            suggested_action="插件当前被禁用；请启用插件、启动插件运行时并刷新插件/MCP 能力清单后重试。",
            status_code=status_code,
        )
    if lowered.startswith("plugin arguments do not match input schema"):
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="invalid_arguments",
            error_code="plugin.invalid_arguments",
            retryable=False,
            suggested_action="插件入参不符合声明 schema；修正参数结构、类型或必填字段后重试。",
            status_code=status_code,
        )
    if lowered.startswith("plugin result does not match output schema") or lowered in {
        "plugin result is invalid",
        "plugin output schema is invalid",
    }:
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="invalid_result",
            error_code="plugin.invalid_result",
            retryable=False,
            suggested_action="插件返回结果不符合声明契约；检查插件适配器输出、output schema 和版本兼容性。",
            status_code=status_code,
        )
    if lowered == "plugin backend unavailable":
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="backend_unavailable",
            error_code="plugin.backend_unavailable",
            retryable=True,
            suggested_action="插件后端不可用；检查插件服务健康、网络连通性和适配器状态，恢复后可重试。",
            status_code=status_code,
        )
    if lowered == "plugin endpoint unavailable":
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="endpoint_unavailable",
            error_code="plugin.endpoint_unavailable",
            retryable=True,
            suggested_action="插件端点不可用；检查 endpoint_url、域名 allowlist 和插件服务网络状态，恢复后可重试。",
            status_code=status_code,
        )
    if lowered == "plugin sandbox profile unsupported":
        return _base_diagnostic(
            reason,
            error_stage="plugin_runtime",
            error_category="sandbox_unsupported",
            error_code="plugin.sandbox_unsupported",
            retryable=False,
            suggested_action="插件 sandbox 配置不被当前运行时支持；调整插件隔离策略或启用对应适配器后重试。",
            status_code=status_code,
        )
    return None


def _mcp_runtime_diagnostic(
    reason: str,
    lowered: str,
    *,
    status_code: int | None,
) -> RuntimeFailureDiagnostic | None:
    if "mcp_server_not_discovered" in lowered or "mcp.server_not_discovered" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="server_not_discovered",
            error_code="mcp.server_not_discovered",
            retryable=False,
            suggested_action="MCP server 已配置但尚未完成工具发现；检查 server 启动、发现协议和 allowlist 配置后 reload。",
            status_code=status_code,
        )
    if "mcp_server_timeout" in lowered or "mcp.server_timeout" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="server_timeout",
            error_code="mcp.server_timeout",
            retryable=True,
            suggested_action="MCP server 健康检查或工具发现超时；检查连接、降低负载或恢复服务后重试。",
            status_code=status_code,
        )
    if "mcp_server_unavailable" in lowered or "mcp.server_unavailable" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="server_unavailable",
            error_code="mcp.server_unavailable",
            retryable=True,
            suggested_action="MCP server 不可用；检查 server 进程、传输配置和运行时 reload 状态，恢复后可重试。",
            status_code=status_code,
        )
    if "mcp_server_failed" in lowered or "mcp.server_failed" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="server_failed",
            error_code="mcp.server_failed",
            retryable=True,
            suggested_action="MCP server 不健康或不可用；检查 server 进程、传输配置和运行时 reload 状态，恢复后可重试。",
            status_code=status_code,
        )
    if "mcp tool unavailable" in lowered or "mcp.tool_unavailable" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="tool_unavailable",
            error_code="mcp.tool_unavailable",
            retryable=False,
            suggested_action="MCP 工具当前不可用；检查 MCP server 发现状态、工具 allowlist 和运行时 reload 后重试。",
            status_code=status_code,
        )
    if lowered in {"mcp tool timed out", "mcp.timeout"}:
        return _base_diagnostic(
            reason,
            error_stage="mcp_runtime",
            error_category="timeout",
            error_code="mcp.timeout",
            retryable=True,
            suggested_action="MCP 工具调用超时；检查 MCP server 健康、网络/stdio 连接和请求规模，恢复后可重试。",
            status_code=status_code,
        )
    return None


def _model_gateway_diagnostic(
    reason: str,
    *,
    status_code: int | None,
) -> RuntimeFailureDiagnostic:
    lowered = reason.lower()
    if re.search(
        r"(?:^|: )model gateway failed: model parameter negotiation exhausted(?: \(status=[1-5][0-9]{2}\))?$",
        lowered,
    ):
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="parameter_negotiation",
            error_code="model.request_negotiation_exhausted",
            retryable=False,
            suggested_action="模型参数兼容协商已用尽；保留真实失败与未知结果，核查协议配置后再创建新运行，不自动重发或切换模型。",
            status_code=status_code,
        )
    rejection = _REJECTED_OUTPUT_FAILURE.fullmatch(reason)
    if rejection is not None:
        diagnostic = _base_diagnostic(
            reason,
            error_stage="model_response",
            error_category="rejected_output",
            error_code="model.response_rejected",
            retryable=False,
            suggested_action="模型输出已被网关拒绝；仅保留已确认的拒绝诊断，核查私有回执和输出契约，不自动重放未知请求。",
        )
        return diagnostic
    if "model response text is empty" in lowered or "model response is empty" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_response",
            error_category="empty_response",
            error_code="model.empty_response",
            retryable=True,
            suggested_action=EMPTY_MODEL_RESPONSE_ACTION,
            status_code=status_code,
        )
    if "no capable deployment" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_routing",
            error_category="no_capable_model",
            error_code="model.no_capable_deployment",
            retryable=False,
            suggested_action="在模型配置中为当前能力补齐可用部署，或调整 Agent/工作流使用的逻辑模型。",
        )
    if "model context window exceeded" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_context",
            error_category="context_window_exceeded",
            error_code="model.context_window_exceeded",
            retryable=False,
            suggested_action="先压缩输入和工具历史，使其符合已配置的上下文窗口，再发起新调用；不原样重复请求或放宽窗口限制。",
        )
    if "model request exceeds capacity token limit" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="request_too_large",
            error_code="model.capacity_request_too_large",
            retryable=False,
            suggested_action="单次请求预留超过有效 token 配额；压缩请求上下文或使用已配置的兼容部署，不重复等待或自动放宽配额。",
        )
    if "capacity queue is full" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="queue_full",
            error_code="model.capacity_queue_full",
            retryable=True,
            suggested_action="模型并发队列已满；稍后重试，或降低并发/切换备用模型。",
        )
    if "capacity queue timeout" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="queue_timeout",
            error_code="model.capacity_timeout",
            retryable=True,
            suggested_action="模型容量等待超时；稍后重试，或增加可用席位/切换备用模型。",
        )
    if "capacity configuration failed" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="configuration",
            error_code="model.capacity_configuration_failed",
            retryable=False,
            suggested_action="检查模型容量配置、配额作用域和并发参数，修复后重新发布配置。",
        )
    if "capacity backend failed" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="backend",
            error_code="model.capacity_backend_failed",
            retryable=True,
            suggested_action="检查容量后端服务状态；恢复后可重试本次任务。",
        )
    if "capacity unavailable" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_capacity",
            error_category="unavailable",
            error_code="model.capacity_unavailable",
            retryable=True,
            suggested_action="当前模型容量不可用；稍后重试，或切换到可用模型。",
        )
    if "configuration failed" in lowered:
        return _base_diagnostic(
            reason,
            error_stage="model_configuration",
            error_category="configuration",
            error_code="model.configuration_failed",
            retryable=False,
            suggested_action="检查模型 API Key、API Base、模型名和供应商权限后重试。",
            status_code=status_code,
        )
    if "response failed" in lowered:
        return _provider_diagnostic(reason, status_code=status_code, response=True)
    if "transport failed" in lowered:
        return _provider_diagnostic(reason, status_code=status_code, response=False)
    return _base_diagnostic(
        reason,
        error_stage="model_gateway",
        error_category="gateway",
        error_code="model.gateway_failed",
        retryable=True,
        suggested_action="模型网关调用失败；查看模型配置与调用错误日志，必要时切换备用模型后重试。",
        status_code=status_code,
    )


def _provider_diagnostic(
    reason: str,
    *,
    status_code: int | None,
    response: bool,
) -> RuntimeFailureDiagnostic:
    if status_code == 400:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="bad_request",
            error_code="model.provider_bad_request",
            retryable=False,
            suggested_action="模型请求参数被供应商拒绝；检查模型名、上下文长度、工具参数和供应商兼容性。",
            status_code=status_code,
        )
    if status_code in {401, 403}:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="authentication",
            error_code="model.provider_auth_failed",
            retryable=False,
            suggested_action="模型供应商拒绝请求；检查 API Key、Base URL、模型权限和账号额度后重试。",
            status_code=status_code,
        )
    if status_code == 402:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="quota_or_billing",
            error_code="model.provider_quota_or_billing_failed",
            retryable=False,
            suggested_action="模型账号额度或计费状态不可用；检查供应商余额、额度、账单或组织权限后重试。",
            status_code=status_code,
        )
    if status_code == 404:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="model_not_found",
            error_code="model.provider_model_not_found",
            retryable=False,
            suggested_action="供应商找不到该模型或端点；检查模型名、API Base 路径和部署配置。",
            status_code=status_code,
        )
    if status_code == 413:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="payload_too_large",
            error_code="model.provider_payload_too_large",
            retryable=False,
            suggested_action="模型请求体或上下文过大；压缩上下文、减少附件/历史消息或降低输出长度后重试。",
            status_code=status_code,
        )
    if status_code == 429:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="rate_limited",
            error_code="model.provider_rate_limited",
            retryable=True,
            suggested_action="模型供应商限流；稍后重试，或降低并发/切换备用模型。",
            status_code=status_code,
        )
    if status_code is not None and status_code >= 500:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="upstream_unavailable",
            error_code="model.provider_unavailable",
            retryable=True,
            suggested_action="上游模型服务异常；稍后重试，或临时切换备用模型。",
            status_code=status_code,
        )
    if status_code in {408, 409, 425}:
        return _base_diagnostic(
            reason,
            error_stage="model_provider",
            error_category="transient",
            error_code="model.provider_transient_failed",
            retryable=True,
            suggested_action="上游模型请求临时失败；可直接重试或切换备用模型。",
            status_code=status_code,
        )
    category = "invalid_response" if response else "transport"
    code = "model.provider_response_failed" if response else "model.provider_transport_failed"
    action = (
        "模型返回结构不符合系统契约；检查模型适配器、返回格式和供应商兼容性。"
        if response
        else "模型网络或上游连接失败；检查 API Base、网络连通性和供应商状态。"
    )
    return _base_diagnostic(
        reason,
        error_stage="model_provider",
        error_category=category,
        error_code=code,
        retryable=response is False and status_code is None,
        suggested_action=action,
        status_code=status_code,
    )


def _base_diagnostic(
    reason: str,
    *,
    error_stage: str,
    error_category: str,
    error_code: str,
    retryable: bool,
    suggested_action: str,
    status_code: int | None = None,
) -> RuntimeFailureDiagnostic:
    diagnostic: RuntimeFailureDiagnostic = {
        "error_summary": reason[:MAX_FAILURE_REASON_LENGTH],
        "error_stage": error_stage,
        "error_category": error_category,
        "error_code": error_code,
        "retryable": retryable,
        "suggested_action": suggested_action,
    }
    if status_code is not None:
        diagnostic["status_code"] = status_code
    return diagnostic


def _status_code_from_reason(reason: str) -> int | None:
    match = _STATUS_CODE.search(reason)
    if match is None:
        return None
    return int(match.group("status"))


def normalize_failure_reason(reason: str) -> str:
    return " ".join(reason.strip().split())


def is_safe_failure_reason(reason: str) -> bool:
    return (bool(reason) and SENSITIVE_FAILURE_REASON.search(reason) is None
            and _TRACEBACK.search(reason) is None)


def is_legacy_generic_failure_reason(reason: str | None) -> bool:
    if reason is None:
        return False
    return normalize_failure_reason(reason) in LEGACY_GENERIC_FAILURES


__all__ = [
    "GENERIC_MODEL_GATEWAY_FAILURE",
    "MAX_FAILURE_REASON_LENGTH",
    "ORCHESTRATION_CHECKPOINT_FAILURE_REASON",
    "RECOVERY_BLOCKED_FAILURE_REASON",
    "SENSITIVE_FAILURE_REASON",
    "gateway_scope_failure_payload",
    "is_legacy_generic_failure_reason",
    "is_public_project_prerequisite_text",
    "is_safe_failure_reason",
    "normalize_failure_reason",
    "runtime_failure_diagnostic_from_reason",
    "safe_model_gateway_failure_reason",
    "safe_runtime_failure_diagnostic",
    "safe_runtime_failure_reason",
]
