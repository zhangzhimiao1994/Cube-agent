"""Shared redacted runtime failure diagnostics."""

from __future__ import annotations

import re

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import ModelGatewayError
from agent_hub.models.litellm_client import ModelResponseError, ModelTransportError
from agent_hub.models.registry import NoCapableDeployment

MAX_FAILURE_REASON_LENGTH = 240
RuntimeFailureDiagnostic = dict[str, str | int | bool]
SENSITIVE_FAILURE_REASON = re.compile(
    r"(api[_-]?key|authorization|bearer|credential|password|secret|(?:access|refresh|session)[_-]?token|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
_STATUS_CODE = re.compile(r"\(status=(?P<status>[1-5][0-9]{2})\)")
_CREWAI_STEP_TIMEOUT = re.compile(
    r"^CrewAI step timed out: step=(?P<step>[A-Za-z0-9_.-]{1,128}) actor=(?P<actor>[A-Za-z0-9_.-]{1,128})$"
)
_HYBRID_CHILD_FAILURE = re.compile(
    r"^hybrid (?P<mode>direct|dispatch|discuss) failed: (?P<reason>.+)$",
    re.IGNORECASE,
)
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


def safe_model_gateway_failure_reason(error: Exception) -> str | None:
    """Return a stable model-gateway diagnostic that is safe to show in the UI."""

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


def safe_runtime_failure_reason(error: Exception, *, fallback: str = "runtime_failed") -> str:
    gateway_reason = safe_model_gateway_failure_reason(error)
    if gateway_reason is not None:
        return gateway_reason
    reason = normalize_failure_reason(str(error))
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
    return runtime_failure_diagnostic_from_reason(reason, status_code=status_code)


def runtime_failure_diagnostic_from_reason(
    reason: str | None,
    *,
    status_code: int | None = None,
) -> RuntimeFailureDiagnostic:
    """Classify an already-redacted failure reason into UI/operator hints."""

    normalized = normalize_failure_reason(reason or "") or "runtime_failed"
    if normalized.lower().startswith("capability failed:"):
        return _base_diagnostic(
            "capability failed",
            error_stage="capability",
            error_category="execution_failed",
            error_code="capability.execution_failed",
            retryable=False,
            suggested_action="工具执行被拒绝或失败；查看工具失败事件中的字段校验摘要，修正参数后重试。",
        )
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
        diagnostic["step_id"] = crew_timeout.group("step")
        diagnostic["actor"] = crew_timeout.group("actor")
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
    elif normalized == "artifact rollback failed":
        diagnostic = _base_diagnostic(
            normalized,
            error_stage="artifact_storage",
            error_category="rollback_failed",
            error_code="artifact.rollback_failed",
            retryable=False,
            suggested_action="产物写入回滚失败；请检查文件存储/对象存储状态，确认残留产物后再重试。",
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


def _model_gateway_diagnostic(
    reason: str,
    *,
    status_code: int | None,
) -> RuntimeFailureDiagnostic:
    lowered = reason.lower()
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
        retryable=response is False,
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
    return bool(reason) and SENSITIVE_FAILURE_REASON.search(reason) is None


def is_legacy_generic_failure_reason(reason: str | None) -> bool:
    if reason is None:
        return False
    return normalize_failure_reason(reason) in LEGACY_GENERIC_FAILURES


__all__ = [
    "GENERIC_MODEL_GATEWAY_FAILURE",
    "MAX_FAILURE_REASON_LENGTH",
    "SENSITIVE_FAILURE_REASON",
    "is_legacy_generic_failure_reason",
    "is_safe_failure_reason",
    "normalize_failure_reason",
    "runtime_failure_diagnostic_from_reason",
    "safe_model_gateway_failure_reason",
    "safe_runtime_failure_diagnostic",
    "safe_runtime_failure_reason",
]
