from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from agent_hub.harness.project_scale import ProjectScaleRunPlan, build_project_scale_run_plan

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_QUALITY_KEYS = frozenset(
    {
        "requirements_satisfied",
        "build_passed",
        "tests_passed",
        "interactive_checks_passed",
        "no_placeholders",
        "artifact_integrity",
    }
)
_QUALITY_PAYLOAD_KEYS = (
    "deliverable_quality",
    "project_quality",
    "quality_gate",
    "acceptance",
)
_AGENT_STANDARD_KEYS = frozenset(
    {
        "constraints_read",
        "plan_before_implementation",
        "reproducible_verification",
        "root_cause_repair",
    }
)
_AGENT_STANDARD_PAYLOAD_KEYS = (
    "agent_standard_verification",
    "codex_claude_verification",
    "verification_standard",
)
_DISCUSSION_TRACE_PAYLOAD_KEYS = (
    "discussion_trace",
    "dispatch_discussion_trace",
    "coordination_trace",
)
_PLUGIN_CONTRACT_KEYS = frozenset(
    {
        "manifest_discovered",
        "adapter_contract_checked",
        "policy_boundary_checked",
        "sandbox_profile_checked",
        "failure_recovery_checked",
    }
)
_PLUGIN_CONTRACT_PAYLOAD_KEYS = (
    "plugin_contract",
    "plugin_capability_contract",
    "plugin_validation",
)
_EMBEDDED_WORKSPACE_MAX_FILES = 200
_EMBEDDED_WORKSPACE_MAX_FILE_BYTES = 512_000
_EMBEDDED_WORKSPACE_MAX_TOTAL_BYTES = 2_000_000
_DISCUSSION_TRACE_FLOWS = frozenset(
    {
        "dispatch",
        "hybrid",
        "multi_agent",
        "plugin",
        "model_failure",
        "self_repair",
        "artifact_production",
        "capability_validation",
    }
)
_PLACEHOLDER_MARKERS = (
    "lorem ipsum",
    "placeholder project",
    "coming soon",
    "not implemented",
    "mock only",
    "stub only",
)
_VERIFICATION_REPORT_BASENAMES = frozenset(
    {
        "verification.md",
        "verification_report.md",
        "test_report.md",
        "acceptance_report.md",
        "validation.md",
    }
)
_EXECUTION_PASS_MARKERS = (
    "passed",
    "pass",
    "success",
    "succeeded",
    "ok",
    "0 failed",
)


class AcceptanceClient(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]: ...

    def request_bytes(self, method: str, path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProjectScaleCaseResult:
    case_id: str
    run_id: str | None
    status: str | None
    evidence: dict[str, bool]
    validation_focus: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def required_evidence(self) -> tuple[str, ...]:
        required: tuple[str, ...] = (
            "run_details",
            "run_events",
            "terminal_status",
            "final_artifacts",
            "deliverable_quality",
            "agent_standard_verification",
            "project_preflight_approval",
            "workspace_bundle",
            "cleanup_cancel",
        )
        if not _case_requires_project_preflight(self.case_id):
            required = tuple(key for key in required if key != "project_preflight_approval")
        if _case_requires_discussion_trace(self.case_id):
            required = (*required, "discussion_trace")
        if _case_requires_plugin_contract(self.case_id):
            required = (*required, "plugin_contract")
        if "self_repair" in self.case_id or "model_failure" in self.case_id:
            required = (*required, "self_repair_trace")
        return required

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return tuple(key for key in self.required_evidence if self.evidence.get(key) is not True)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing_evidence

    @property
    def repair_attempted(self) -> bool:
        return self.evidence.get("deliverable_repair_trace") is True

    @property
    def repair_outcome(self) -> str:
        if not self.repair_attempted:
            return "not_attempted"
        if not self.missing_evidence and not self.errors:
            return "passed"
        return "failed"

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
            "repair_attempted": self.repair_attempted,
            "repair_outcome": self.repair_outcome,
            "validation_focus": list(self.validation_focus),
            "required_evidence": list(self.required_evidence),
            "missing_evidence": list(self.missing_evidence),
            "evidence": dict(self.evidence),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class ProjectScaleExecutionReport:
    results: tuple[ProjectScaleCaseResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failed_results(self) -> tuple[ProjectScaleCaseResult, ...]:
        return tuple(result for result in self.results if not result.ok)

    def to_payload(self) -> dict[str, object]:
        failed_results = self.failed_results
        return {
            "execute": True,
            "dry_run": False,
            "ok": self.ok,
            "case_count": self.case_count,
            "failed_case_count": len(failed_results),
            "failed_cases": [result.case_id for result in failed_results],
            "missing_evidence_summary": _summarize_missing_evidence(failed_results),
            "failed_validation_focus": _summarize_validation_focus(failed_results),
            "results": [result.to_payload() for result in self.results],
        }


class UrllibAcceptanceClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str = "",
        timeout: float = 20.0,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._bearer_token = bearer_token
        self._timeout = timeout
        self._username = username
        self._password = password
        self._tenant_id = tenant_id

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        raw = self._request(method, path, headers=headers, data=data)
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict | list):
            raise TypeError(f"{method} {path} returned non-object JSON")
        return parsed

    def request_bytes(self, method: str, path: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        return self._request(method, path, headers=headers, data=None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        data: bytes | None,
        retry_auth: bool = True,
        include_bearer: bool = True,
    ) -> bytes:
        if retry_auth and include_bearer and not self._bearer_token and self._can_login():
            self._refresh_bearer_token()
        if include_bearer and self._bearer_token:
            headers = dict(headers)
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        url = urljoin(self._base_url, path.lstrip("/"))
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return cast(bytes, response.read())
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if retry_auth and error.code == 401 and self._can_login() and _is_invalid_token_body(body):
                self._refresh_bearer_token()
                return self._request(
                    method,
                    path,
                    headers=headers,
                    data=data,
                    retry_auth=False,
                    include_bearer=include_bearer,
                )
            raise RuntimeError(f"{method} {path} failed status={error.code} body={body[:240]}") from error
        except URLError as error:
            raise RuntimeError(f"{method} {path} failed: {error.reason}") from error

    def _can_login(self) -> bool:
        return bool(self._username and self._password)

    def _refresh_bearer_token(self) -> None:
        if not self._can_login():
            raise RuntimeError("acceptance login credentials are unavailable")
        body: dict[str, object] = {
            "username": self._username,
            "password": self._password,
        }
        if self._tenant_id:
            body["tenant_id"] = self._tenant_id
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        raw = self._request(
            "POST",
            "/api/v1/auth/login",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            data=data,
            retry_auth=False,
            include_bearer=False,
        )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("access_token"), str):
            raise TypeError("acceptance login returned invalid token response")
        self._bearer_token = cast(str, parsed["access_token"])


@dataclass(frozen=True, slots=True)
class _RunObservation:
    status: str | None
    details: dict[str, object] | None
    events: list[object] | None
    workspace_bundle: bytes | None


@dataclass(frozen=True, slots=True)
class _EvidenceCheck:
    passed: bool
    reasons: tuple[str, ...]


def execute_project_scale_plan(
    plan: ProjectScaleRunPlan,
    client: AcceptanceClient,
    *,
    wait_seconds: float = 0,
    poll_interval_seconds: float = 2,
    execution_id: str | None = None,
) -> ProjectScaleExecutionReport:
    results: list[ProjectScaleCaseResult] = []
    for index, run_request in enumerate(plan.requests):
        evidence = {
            "run_details": False,
            "run_events": False,
            "terminal_status": False,
            "final_artifacts": False,
            "deliverable_quality": False,
            "agent_standard_verification": False,
            "discussion_trace": False,
            "plugin_contract": False,
            "deliverable_repair_trace": False,
            "self_repair_trace": False,
            "project_preflight_approval": False,
            "workspace_bundle": False,
            "cleanup_cancel": False,
        }
        errors: list[str] = []
        run_id: str | None = None
        status: str | None = None
        try:
            response = client.request_json(
                "POST",
                "/api/v1/runs",
                body=run_request.body,
                idempotency_key=_idempotency_key(
                    run_request.case_id,
                    index,
                    execution_id=execution_id,
                ),
            )
            if not isinstance(response, dict):
                raise TypeError("run create returned non-object JSON")
            raw_run_id = response.get("id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                raise RuntimeError("run create response missing id")
            run_id = raw_run_id
            status = _string_value(response.get("status"))
            _validate_run_submission_scope(response, run_request.body)
            _extend_unique(
                errors,
                _validate_mode_control(
                    response,
                    requested_body=run_request.body,
                    validation_focus=run_request.validation_focus,
                ),
            )
            if _case_requires_project_preflight(run_request.case_id):
                approval_body = _project_preflight_approval_body(response)
                approval = client.request_json(
                    "POST",
                    f"/api/v1/runs/{quote(run_id)}/approve-project-preflight",
                    body=approval_body,
                )
                if not isinstance(approval, dict):
                    raise TypeError("project preflight approval returned non-object JSON")
                evidence["project_preflight_approval"] = True
                status = _string_value(approval.get("status")) or status

            observation = _collect_run_observation(
                client,
                run_id=run_id,
                body=run_request.body,
                wait_seconds=wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                current_status=status,
                evidence=evidence,
                errors=errors,
            )
            status = observation.status
            _extend_unique(
                errors,
                _validate_mode_control(
                    observation.details,
                    requested_body=run_request.body,
                    validation_focus=run_request.validation_focus,
                ),
            )
            if evidence["terminal_status"] and status != "completed":
                errors.append(f"terminal_status: {status or 'unknown'}")

            evidence["self_repair_trace"] = _has_self_repair_trace(observation.events)
            deliverable_quality = _evaluate_deliverable_quality(
                observation.details,
                observation.events,
                observation.workspace_bundle,
            )
            evidence["deliverable_quality"] = deliverable_quality.passed
            agent_standard_verification = _evaluate_agent_standard_verification(
                observation.details,
                observation.events,
                observation.workspace_bundle,
            )
            evidence["agent_standard_verification"] = agent_standard_verification.passed
            discussion_trace = _evaluate_discussion_trace(
                observation.details,
                observation.events,
                case_id=run_request.case_id,
            )
            evidence["discussion_trace"] = discussion_trace.passed
            plugin_contract = _evaluate_plugin_contract(
                observation.details,
                observation.events,
                case_id=run_request.case_id,
            )
            evidence["plugin_contract"] = plugin_contract.passed
            if _should_attempt_deliverable_repair(
                status=status,
                evidence=evidence,
                case_id=run_request.case_id,
            ):
                repair_response = client.request_json(
                    "POST",
                    "/api/v1/runs",
                    body=_deliverable_repair_body(
                        run_request.body,
                        run_request.case_id,
                        failed_reasons=(
                            *deliverable_quality.reasons,
                            *agent_standard_verification.reasons,
                            *discussion_trace.reasons,
                            *plugin_contract.reasons,
                        ),
                    ),
                    idempotency_key=_deliverable_repair_idempotency_key(
                        run_request.case_id,
                        index,
                        execution_id=execution_id,
                    ),
                )
                if not isinstance(repair_response, dict):
                    raise TypeError("deliverable repair returned non-object JSON")
                _validate_run_submission_scope(repair_response, run_request.body)
                _extend_unique(
                    errors,
                    _validate_mode_control(
                        repair_response,
                        requested_body=run_request.body,
                        validation_focus=run_request.validation_focus,
                    ),
                )
                repair_run_id = repair_response.get("id")
                if not isinstance(repair_run_id, str) or not repair_run_id:
                    raise RuntimeError("deliverable repair response missing id")
                evidence["deliverable_repair_trace"] = True
                run_id = repair_run_id
                status = _string_value(repair_response.get("status")) or status
                repair_observation = _collect_run_observation(
                    client,
                    run_id=run_id,
                    body=run_request.body,
                    wait_seconds=wait_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    current_status=status,
                    evidence=evidence,
                    errors=errors,
                )
                status = repair_observation.status
                _extend_unique(
                    errors,
                    _validate_mode_control(
                        repair_observation.details,
                        requested_body=run_request.body,
                        validation_focus=run_request.validation_focus,
                    ),
                )
                if evidence["terminal_status"] and status != "completed":
                    errors.append(f"terminal_status: {status or 'unknown'}")
                evidence["self_repair_trace"] = (
                    evidence["self_repair_trace"]
                    or _has_self_repair_trace(repair_observation.events)
                    or _has_deliverable_repair_trace(repair_observation.events)
                )
                deliverable_quality = _evaluate_deliverable_quality(
                    repair_observation.details,
                    repair_observation.events,
                    repair_observation.workspace_bundle,
                )
                evidence["deliverable_quality"] = deliverable_quality.passed
                agent_standard_verification = _evaluate_agent_standard_verification(
                    repair_observation.details,
                    repair_observation.events,
                    repair_observation.workspace_bundle,
                )
                evidence["agent_standard_verification"] = agent_standard_verification.passed
                discussion_trace = _evaluate_discussion_trace(
                    repair_observation.details,
                    repair_observation.events,
                    case_id=run_request.case_id,
                )
                evidence["discussion_trace"] = discussion_trace.passed
                plugin_contract = _evaluate_plugin_contract(
                    repair_observation.details,
                    repair_observation.events,
                    case_id=run_request.case_id,
                )
                evidence["plugin_contract"] = plugin_contract.passed
            if (
                evidence["workspace_bundle"]
                and evidence["final_artifacts"]
                and (
                    not evidence["deliverable_quality"]
                    or not evidence["agent_standard_verification"]
                    or (
                        _case_requires_discussion_trace(run_request.case_id)
                        and not evidence["discussion_trace"]
                    )
                    or (
                        _case_requires_plugin_contract(run_request.case_id)
                        and not evidence["plugin_contract"]
                    )
                )
            ):
                if not evidence["deliverable_quality"]:
                    errors.extend(deliverable_quality.reasons)
                if not evidence["agent_standard_verification"]:
                    errors.extend(agent_standard_verification.reasons)
                if (
                    _case_requires_discussion_trace(run_request.case_id)
                    and not evidence["discussion_trace"]
                ):
                    errors.extend(discussion_trace.reasons)
                if (
                    _case_requires_plugin_contract(run_request.case_id)
                    and not evidence["plugin_contract"]
                ):
                    errors.extend(plugin_contract.reasons)
        except Exception as error:  # noqa: BLE001 - collect per-case failures and continue.
            errors.append(str(error))
        finally:
            if run_id is not None:
                if _is_terminal_status(status):
                    evidence["cleanup_cancel"] = True
                else:
                    try:
                        cleanup = client.request_json("POST", f"/api/v1/runs/{quote(run_id)}/cancel")
                        evidence["cleanup_cancel"] = isinstance(cleanup, dict)
                    except Exception as error:  # noqa: BLE001 - cleanup failure is evidence.
                        errors.append(f"cleanup_cancel: {error}")
        results.append(
            ProjectScaleCaseResult(
                case_id=run_request.case_id,
                run_id=run_id,
                status=status,
                evidence=evidence,
                validation_focus=run_request.validation_focus,
                errors=tuple(errors),
            )
        )
    return ProjectScaleExecutionReport(results=tuple(results))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_hub.harness.project_scale_runner",
        description="Build a safe project-scale acceptance fixture run plan.",
    )
    parser.add_argument("--scale", action="append", dest="scales", default=None)
    parser.add_argument("--flow", action="append", dest="flows", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS", "20")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_WAIT_SECONDS", "0")),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_POLL_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_REPORT_PATH"),
        help="Write the JSON plan or execution report to this path.",
    )
    parser.add_argument(
        "--execution-id",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID"),
        help="Scope execution idempotency keys; generated automatically for --execute.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    bearer_token = os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "")
    username = os.environ.get("AGENT_HUB_ACCEPTANCE_USERNAME")
    password = os.environ.get("AGENT_HUB_ACCEPTANCE_PASSWORD")
    tenant_id = os.environ.get("AGENT_HUB_ACCEPTANCE_TENANT_ID")
    if args.execute and not bearer_token and not (username and password):
        parser.error(
            "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
            "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required for --execute"
        )

    try:
        plan = build_project_scale_run_plan(
            scales=tuple(args.scales) if args.scales is not None else None,
            flows=tuple(args.flows) if args.flows is not None else None,
            execute=args.execute,
        )
    except ValueError as error:
        parser.error(str(error))

    if args.execute:
        report = execute_project_scale_plan(
            plan,
            UrllibAcceptanceClient(
                base_url=args.base_url,
                bearer_token=bearer_token,
                timeout=args.timeout,
                username=username,
                password=password,
                tenant_id=tenant_id,
            ),
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
            execution_id=args.execution_id or _default_execution_id(),
        )
        payload = report.to_payload()
    else:
        payload = plan.to_payload()
    if args.output:
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        if args.execute:
            print(f"project-scale execution cases={report.case_count} ok={str(report.ok).lower()}")
            for result in report.results:
                print(format_project_scale_result_line(result))
        else:
            print(
                f"project-scale plan cases={plan.case_count} "
                f"dry_run={str(plan.dry_run).lower()} execute={str(plan.execute).lower()}"
            )
            for request in plan.requests:
                print(f"{request.case_id} focus={','.join(request.validation_focus)}")
    return 0 if (not args.execute or report.ok) else 1


def format_project_scale_result_line(result: ProjectScaleCaseResult) -> str:
    parts = [
        result.case_id,
        f"run_id={result.run_id or '-'}",
        f"ok={str(result.ok).lower()}",
    ]
    if result.validation_focus:
        parts.append(f"focus={','.join(result.validation_focus)}")
    if result.missing_evidence:
        parts.append(f"missing={','.join(result.missing_evidence)}")
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    if result.repair_attempted:
        parts.append(f"repair={result.repair_outcome}")
    return " ".join(parts)


def _summarize_missing_evidence(results: Sequence[ProjectScaleCaseResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for result in results:
        for evidence in result.missing_evidence:
            summary[evidence] = summary.get(evidence, 0) + 1
    return summary


def _summarize_validation_focus(results: Sequence[ProjectScaleCaseResult]) -> list[str]:
    focus: dict[str, None] = {}
    for result in results:
        for item in result.validation_focus:
            focus.setdefault(item, None)
    return list(focus)


def _is_invalid_token_body(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return False
    return error.get("code") == "invalid_token"


def _extend_unique(target: list[str], items: Sequence[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _workspace_bundle_path(body: dict[str, object]) -> str:
    project_id = body.get("project_id")
    session_id = body.get("workspace_session_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("run request missing project_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("run request missing workspace_session_id")
    return (
        f"/api/v1/workspaces/projects/{quote(project_id, safe='')}/"
        f"sessions/{quote(session_id, safe='')}/bundle/download"
    )


def _collect_run_observation(
    client: AcceptanceClient,
    *,
    run_id: str,
    body: dict[str, object],
    wait_seconds: float,
    poll_interval_seconds: float,
    current_status: str | None,
    evidence: dict[str, bool],
    errors: list[str],
) -> _RunObservation:
    status = current_status
    details: dict[str, object] | None = None
    events: list[object] | None = None
    workspace_bundle: bytes | None = None
    approved_capabilities: set[str] = set()

    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        details_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
        evidence["run_details"] = isinstance(details_response, dict)
        if isinstance(details_response, dict):
            details = details_response
            _validate_run_details_scope(details, run_id)
            status = _string_value(details.get("status")) or status
            evidence["final_artifacts"] = _has_final_artifacts(details)
            approved_status = _approve_pending_capability(
                client,
                run_id=run_id,
                details=details,
                approved_capabilities=approved_capabilities,
                errors=errors,
            )
            if approved_status is not None:
                status = approved_status
        if _is_terminal_status(status):
            evidence["terminal_status"] = True
            break
        if wait_seconds <= 0 or time.monotonic() >= deadline:
            break
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)

    events_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/events")
    normalized_events = _run_events_items(events_response)
    if isinstance(normalized_events, list) and normalized_events:
        events = normalized_events
        evidence["run_events"] = True
        errors.extend(_validate_run_events_scope(normalized_events, run_id))
    elif isinstance(normalized_events, list):
        events = normalized_events
        errors.append("run_events: empty event stream")
    else:
        errors.append("run_events: returned non-list JSON")

    try:
        workspace_bundle = client.request_bytes("GET", _workspace_bundle_path(body))
        evidence["workspace_bundle"] = True
    except Exception as error:  # noqa: BLE001 - acceptance reports must continue cleanup.
        workspace_bundle = _embedded_workspace_bundle_from_observation(details, events)
        if workspace_bundle is not None:
            evidence["workspace_bundle"] = True
        else:
            errors.append(f"workspace_bundle: {error}")

    return _RunObservation(
        status=status,
        details=details,
        events=events,
        workspace_bundle=workspace_bundle,
    )


def _validate_run_submission_scope(response: dict[str, object], body: dict[str, object]) -> None:
    for field in ("project_id", "workspace_session_id"):
        expected = body.get(field)
        actual = response.get(field)
        if not isinstance(expected, str) or not expected:
            raise RuntimeError(f"run request missing {field}")
        if actual != expected:
            got = actual if isinstance(actual, str) and actual else "missing"
            raise RuntimeError(f"run scope mismatch: {field} expected {expected} got {got}")


def _run_events_items(response: dict[str, object] | list[object]) -> list[object] | None:
    if isinstance(response, list):
        return response
    items = response.get("items") if isinstance(response, dict) else None
    return items if isinstance(items, list) else None


def _embedded_workspace_bundle_from_observation(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bytes | None:
    candidates: list[Mapping[str, object]] = []
    if details is not None:
        candidates.append(details)
    if events is not None:
        candidates.extend(event for event in events if isinstance(event, Mapping))
    for candidate in candidates:
        bundle = _embedded_workspace_bundle_from_mapping(candidate)
        if bundle is not None:
            return bundle
    return None


def _embedded_workspace_bundle_from_mapping(mapping: Mapping[str, object]) -> bytes | None:
    direct = _embedded_workspace_bundle_from_payload(mapping)
    if direct is not None:
        return direct
    artifact = mapping.get("artifact")
    if isinstance(artifact, Mapping):
        artifact_bundle = _embedded_workspace_bundle_from_payload(artifact)
        if artifact_bundle is not None:
            return artifact_bundle
    payload = mapping.get("payload")
    if isinstance(payload, Mapping):
        payload_bundle = _embedded_workspace_bundle_from_payload(payload)
        if payload_bundle is not None:
            return payload_bundle
    artifacts = mapping.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, str | bytes):
        for artifact_item in artifacts:
            if not isinstance(artifact_item, Mapping):
                continue
            artifact_bundle = _embedded_workspace_bundle_from_payload(artifact_item)
            if artifact_bundle is not None:
                return artifact_bundle
    return None


def _embedded_workspace_bundle_from_payload(payload: Mapping[str, object]) -> bytes | None:
    workspace_bundle = payload.get("workspace_bundle")
    if isinstance(workspace_bundle, Mapping):
        bundle = _workspace_bundle_mapping_to_zip(workspace_bundle)
        if bundle is not None:
            return bundle
    content = payload.get("content")
    if isinstance(content, Mapping):
        text = content.get("text")
        bundle = _embedded_workspace_bundle_from_text(text)
        if bundle is not None:
            return bundle
    for key in ("text", "output", "result", "summary"):
        bundle = _embedded_workspace_bundle_from_text(payload.get(key))
        if bundle is not None:
            return bundle
    return None


def _embedded_workspace_bundle_from_text(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]
    parsed = _json_mapping_from_text(text)
    if isinstance(parsed, Mapping):
        bundle = _embedded_workspace_bundle_from_payload(parsed)
        if bundle is not None:
            return bundle
    return _markdown_file_blocks_to_zip(text)


def _json_mapping_from_text(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _workspace_bundle_mapping_to_zip(workspace_bundle: Mapping[str, object]) -> bytes | None:
    files = workspace_bundle.get("files")
    if not isinstance(files, Mapping) or not files:
        return None
    if len(files) > _EMBEDDED_WORKSPACE_MAX_FILES:
        return None
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_content in files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            return None
        path = _safe_embedded_workspace_path(raw_path)
        if path is None:
            return None
        content_bytes = raw_content.encode("utf-8")
        if len(content_bytes) > _EMBEDDED_WORKSPACE_MAX_FILE_BYTES:
            return None
        total_bytes += len(content_bytes)
        if total_bytes > _EMBEDDED_WORKSPACE_MAX_TOTAL_BYTES:
            return None
        normalized[path] = raw_content
    if not normalized:
        return None
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in sorted(normalized.items()):
            archive.writestr(path, content)
    return buffer.getvalue()


def _markdown_file_blocks_to_zip(text: str) -> bytes | None:
    lines = text.splitlines()
    files: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not (line.startswith("### `") and line.endswith("`")):
            index += 1
            continue
        path = _safe_embedded_workspace_path(line[5:-1])
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if path is None or index >= len(lines) or not lines[index].lstrip().startswith("```"):
            continue
        index += 1
        content_lines: list[str] = []
        while index < len(lines) and not lines[index].lstrip().startswith("```"):
            content_lines.append(lines[index])
            index += 1
        if index < len(lines):
            index += 1
        files[path] = "\n".join(content_lines).rstrip() + "\n"
    if not files:
        return None
    return _workspace_bundle_mapping_to_zip({"files": files})


def _safe_embedded_workspace_path(value: str) -> str | None:
    candidate = value.replace("\\", "/")
    if candidate.startswith("/") or "\x00" in candidate:
        return None
    path = candidate.strip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    normalized = "/".join(parts)
    if len(normalized) > 512:
        return None
    return normalized


def _validate_mode_control(
    response: dict[str, object] | None,
    *,
    requested_body: dict[str, object],
    validation_focus: Sequence[str],
) -> list[str]:
    if (
        "mode_control" not in validation_focus
        and "no_silent_downgrade" not in validation_focus
    ):
        return []
    requested = requested_body.get("mode")
    if not isinstance(requested, str) or not requested:
        return ["mode_control: request missing mode"]
    if response is None:
        return ["mode_control: response missing mode"]
    actual = response.get("mode")
    if actual != requested:
        got = actual if isinstance(actual, str) and actual else "missing"
        return [f"mode_control: requested {requested} got {got}"]
    return []


def _validate_run_details_scope(details: dict[str, object], run_id: str) -> None:
    actual = details.get("id")
    if actual != run_id:
        got = actual if isinstance(actual, str) and actual else "missing"
        raise RuntimeError(f"run details scope mismatch: id expected {run_id} got {got}")


def _approve_pending_capability(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: dict[str, object],
    approved_capabilities: set[str],
    errors: list[str],
) -> str | None:
    if details.get("status") != "waiting_approval":
        return None
    approval = _capability_approval_request(client, run_id=run_id, details=details)
    if approval is None:
        return None
    approval_id, version = approval
    if approval_id in approved_capabilities:
        return None
    approved_capabilities.add(approval_id)
    try:
        response = client.request_json(
            "POST",
            f"/api/v1/runs/{quote(run_id)}/approve-capability",
            body={"approval_id": approval_id, "version": version},
        )
    except RuntimeError as error:
        errors.append(f"capability_approval: {error}")
        return None
    if not isinstance(response, dict):
        errors.append("capability_approval: approve-capability returned non-object JSON")
        return None
    return _string_value(response.get("status"))


def _capability_approval_request(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: dict[str, object],
) -> tuple[str, int] | None:
    approval = _capability_approval_from_mapping(details)
    if approval is not None:
        return approval
    try:
        admin_response = client.request_json("GET", f"/api/v1/admin/runs/{quote(run_id)}")
    except RuntimeError:
        return None
    if not isinstance(admin_response, dict):
        return None
    return _capability_approval_from_mapping(admin_response)


def _capability_approval_from_mapping(payload: Mapping[str, object]) -> tuple[str, int] | None:
    version = payload.get("version")
    approval_id = payload.get("approval_id")
    explicit_details = payload.get("explicit_details")
    if isinstance(explicit_details, Mapping):
        approval_id = approval_id or explicit_details.get("approval_id")
        raw_version = explicit_details.get("version")
        if isinstance(raw_version, str) and raw_version.isdigit():
            version = int(raw_version)
    if not isinstance(approval_id, str) or not approval_id:
        return None
    if not isinstance(version, int) or version <= 0:
        return None
    return approval_id, version


def _validate_run_events_scope(events: list[object], run_id: str) -> list[str]:
    for event in events:
        if not isinstance(event, dict):
            continue
        field, actual = _event_run_reference(event)
        if actual is None:
            continue
        if actual != run_id:
            return [f"run events scope mismatch: {field} expected {run_id} got {actual}"]
    return []


def _event_run_reference(event: dict[object, object]) -> tuple[str, str | None]:
    for field in ("run_id", "runId"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return field, value
    run = event.get("run")
    if isinstance(run, dict):
        value = run.get("id")
        if isinstance(value, str) and value:
            return "run.id", value
    return "run_id", None


def _idempotency_key(case_id: str, index: int, *, execution_id: str | None = None) -> str:
    safe_case = case_id.replace(":", "-").replace("_", "-")
    key = f"project-scale-{safe_case}-{index}"
    if execution_id is not None:
        key = f"{key}-{_safe_idempotency_token(execution_id)}"
    return key[:90]


def _deliverable_repair_idempotency_key(
    case_id: str,
    index: int,
    *,
    execution_id: str | None = None,
) -> str:
    return f"{_idempotency_key(case_id, index, execution_id=execution_id)}-deliverable-repair"[:90]


def _deliverable_repair_body(
    body: dict[str, object],
    case_id: str,
    *,
    failed_reasons: Sequence[str] = (),
) -> dict[str, object]:
    repair_body = dict(body)
    original_message = body.get("message")
    reason_text = _format_failed_reasons(failed_reasons)
    direct_guidance = (
        " This is a direct run: do not call tools, do not emit DSML/tool-call syntax, and "
        "do not describe commands as if they were executed. Return a machine-verifiable "
        "deliverable inline as strict JSON with workspace_bundle.files mapping safe relative "
        "paths to complete file contents, or as Markdown file blocks headed exactly like "
        "### `path/to/file` followed by a fenced code block. Include README or requirements, "
        "source files, tests or build scripts, implementation plan, and verification report "
        "with reproducible build, test, and interaction evidence. Avoid credential-like terms "
        "and avoid package, file, variable, or fixture names that contain the sk- prefix so "
        "public evidence stays visible."
        if body.get("mode") == "direct" or case_id.endswith(":direct")
        else ""
    )
    repair_body["message"] = (
        f"Project-scale deliverable repair for {case_id}: the previous generated project "
        "failed acceptance quality. Diagnose the mismatches against the original request, "
        "repair the implementation in the same workspace, rerun build/test/interaction checks, "
        "remove placeholders or stub-only output, and record deliverable_quality with "
        "requirements_satisfied, build_passed, tests_passed, interactive_checks_passed, "
        "no_placeholders, and artifact_integrity all true. Also record "
        "agent_standard_verification with constraints_read, plan_before_implementation, "
        "reproducible_verification, and root_cause_repair all true, and keep implementation "
        "plan plus verification notes in the workspace. When the run uses dispatch, hybrid, "
        "multi-agent, discussion, or repair coordination, record discussion_trace with "
        "participants, member statements, disagreements, verification steps, and final decision "
        "so the workbench can show the scheduling debate. For plugin flows, also record "
        "plugin_contract with manifest discovery, adapter contracts, sandbox and policy "
        f"boundaries, and failure recovery behavior.{direct_guidance}{reason_text} "
        "Original request:\n"
        f"{original_message if isinstance(original_message, str) else ''}"
    )
    repair_body["skip_evolution_proposal"] = True
    return repair_body


def _format_failed_reasons(reasons: Sequence[str]) -> str:
    unique: dict[str, None] = {}
    for reason in reasons:
        if reason:
            unique.setdefault(reason, None)
    if not unique:
        return ""
    return "\nPrevious failed evidence:\n" + "\n".join(f"- {reason}" for reason in unique) + "\n"


def _default_execution_id() -> str:
    return _safe_idempotency_token(f"{int(time.time())}-{os.getpid()}")


def _safe_idempotency_token(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "._:-" else "-"
        for character in value.strip()
    ).strip("-")
    return (safe or "run")[:48]


def _case_requires_project_preflight(case_id: str) -> bool:
    scale, _, _flow = case_id.partition(":")
    return scale in {"large", "ultra"}


def _case_requires_discussion_trace(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow in _DISCUSSION_TRACE_FLOWS


def _case_requires_plugin_contract(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow == "plugin"


def _project_preflight_approval_body(response: dict[str, object]) -> dict[str, object]:
    if response.get("status") != "waiting_approval":
        raise RuntimeError("project preflight run did not wait for approval")
    token = response.get("decision_token")
    version = response.get("version")
    if not isinstance(token, str) or not token:
        raise RuntimeError("project preflight run missing decision_token")
    if not isinstance(version, int) or version <= 0:
        raise RuntimeError("project preflight run missing version")
    return {"decision_token": token, "version": version}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_terminal_status(status: str | None) -> bool:
    return status in _TERMINAL_STATUSES


def _has_final_artifacts(details: dict[str, object]) -> bool:
    artifacts = details.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        return True
    final_artifacts = details.get("final_artifacts")
    if isinstance(final_artifacts, list) and final_artifacts:
        return True
    artifact_ids = details.get("artifact_ids")
    if isinstance(artifact_ids, list) and artifact_ids:
        return True
    artifact_count = details.get("artifact_count")
    return isinstance(artifact_count, int) and artifact_count > 0


def _has_deliverable_quality(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> bool:
    return _evaluate_deliverable_quality(details, events, workspace_bundle).passed


def _has_agent_standard_verification(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> bool:
    return _evaluate_agent_standard_verification(details, events, workspace_bundle).passed


def _evaluate_deliverable_quality(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> _EvidenceCheck:
    reasons: list[str] = []
    if not (
        _has_quality_payload(details, events)
        or _workspace_bundle_has_quality_payload(workspace_bundle)
    ):
        reasons.append("deliverable_quality: missing or incomplete structured quality flags")
    reasons.extend(_workspace_bundle_project_quality_reasons(workspace_bundle))
    return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))


def _evaluate_agent_standard_verification(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> _EvidenceCheck:
    reasons: list[str] = []
    if not (
        _has_agent_standard_payload(details, events)
        or _workspace_bundle_has_agent_standard_payload(workspace_bundle)
    ):
        reasons.append(
            "agent_standard_verification: missing or incomplete Codex/Claude standard flags"
        )
    reasons.extend(_workspace_bundle_agent_standard_reasons(workspace_bundle))
    return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))


def _evaluate_discussion_trace(
    details: dict[str, object] | None,
    events: list[object] | None,
    *,
    case_id: str,
) -> _EvidenceCheck:
    if not _case_requires_discussion_trace(case_id):
        return _EvidenceCheck(passed=False, reasons=())
    if _has_discussion_trace_payload(details, events):
        return _EvidenceCheck(passed=True, reasons=())
    return _EvidenceCheck(
        passed=False,
        reasons=("discussion_trace: missing hybrid/discussion process evidence",),
    )


def _evaluate_plugin_contract(
    details: dict[str, object] | None,
    events: list[object] | None,
    *,
    case_id: str,
) -> _EvidenceCheck:
    if not _case_requires_plugin_contract(case_id):
        return _EvidenceCheck(passed=False, reasons=())
    if _has_plugin_contract_payload(details, events):
        return _EvidenceCheck(passed=True, reasons=())
    return _EvidenceCheck(
        passed=False,
        reasons=("plugin_contract: missing or incomplete plugin capability contract evidence",),
    )


def _has_quality_payload(details: dict[str, object] | None, events: list[object] | None) -> bool:
    if details is not None and _mapping_has_quality_payload(details):
        return True
    if events is None:
        return False
    return any(isinstance(event, dict) and _mapping_has_quality_payload(event) for event in events)


def _mapping_has_quality_payload(mapping: Mapping[str, object]) -> bool:
    for key in _QUALITY_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _quality_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _quality_payload_passes(payload.get(key)):
            return True
    for embedded in _embedded_structured_mappings(mapping):
        if _mapping_has_quality_payload(embedded):
            return True
    return False


def _has_agent_standard_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_agent_standard_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_agent_standard_payload(event) for event in events
    )


def _mapping_has_agent_standard_payload(mapping: Mapping[str, object]) -> bool:
    for key in _AGENT_STANDARD_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _agent_standard_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _agent_standard_payload_passes(payload.get(key)):
            return True
    for embedded in _embedded_structured_mappings(mapping):
        if _mapping_has_agent_standard_payload(embedded):
            return True
    return False


def _embedded_structured_mappings(mapping: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    values: list[object] = []
    content = mapping.get("content")
    if isinstance(content, Mapping):
        values.append(content.get("text"))
    artifact = mapping.get("artifact")
    if isinstance(artifact, Mapping):
        artifact_content = artifact.get("content")
        if isinstance(artifact_content, Mapping):
            values.append(artifact_content.get("text"))
    payload = mapping.get("payload")
    if isinstance(payload, Mapping):
        values.extend(payload.get(key) for key in ("text", "output", "result", "summary"))
    values.extend(mapping.get(key) for key in ("text", "output", "result", "summary"))
    parsed: list[Mapping[str, object]] = []
    for value in values:
        item = _json_mapping_from_text(value)
        if item is not None:
            parsed.append(item)
    return tuple(parsed)


def _has_discussion_trace_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_discussion_trace_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_discussion_trace_payload(event)
        for event in events
    )


def _mapping_has_discussion_trace_payload(mapping: Mapping[str, object]) -> bool:
    for key in _DISCUSSION_TRACE_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _discussion_trace_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _discussion_trace_payload_passes(payload.get(key)):
            return True
    return False


def _has_plugin_contract_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_plugin_contract_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_plugin_contract_payload(event)
        for event in events
    )


def _mapping_has_plugin_contract_payload(mapping: Mapping[str, object]) -> bool:
    for key in _PLUGIN_CONTRACT_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _plugin_contract_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _plugin_contract_payload_passes(payload.get(key)):
            return True
    return False


def _agent_standard_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _AGENT_STANDARD_KEYS)


def _quality_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _QUALITY_KEYS)


def _discussion_trace_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_sequence(value.get("participants"))
        and _non_empty_sequence(value.get("member_statements"))
        and _has_present_field(value, ("disagreements", "disagreement_summary"))
        and _non_empty_sequence(value.get("verification_steps"))
        and _non_empty_text(value.get("final_decision"))
    )


def _plugin_contract_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _PLUGIN_CONTRACT_KEYS)


def _non_empty_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes) and bool(value)


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_present_field(mapping: Mapping[str, object], keys: Sequence[str]) -> bool:
    for key in keys:
        if key not in mapping:
            continue
        value = mapping[key]
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            return True
        if _non_empty_text(value):
            return True
    return False


def _workspace_bundle_has_project_quality(workspace_bundle: bytes | None) -> bool:
    return not _workspace_bundle_project_quality_reasons(workspace_bundle)


def _workspace_bundle_project_quality_reasons(workspace_bundle: bytes | None) -> tuple[str, ...]:
    if not workspace_bundle:
        return ("workspace_bundle: missing project bundle",)
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            lowered = tuple(name.lower() for name in names)
            if not lowered:
                return ("workspace_bundle: empty project bundle",)
            text = _workspace_bundle_text(archive, names)
            verification_text = _workspace_bundle_verification_text(archive, names)
            package_json = _read_bundle_file(archive, names, "package.json")
    except (OSError, zipfile.BadZipFile):
        return ("workspace_bundle: invalid or unreadable zip bundle",)

    reasons: list[str] = []
    if not _bundle_has_requirement_document(lowered):
        reasons.append("workspace_bundle: missing requirements or README artifact")
    if not _bundle_has_source_files(lowered):
        reasons.append("workspace_bundle: missing source files")
    if not _bundle_has_verification_path_or_script(lowered, package_json):
        reasons.append("workspace_bundle: missing test path or build/test script")
    if not _bundle_has_build_test_execution_evidence(verification_text):
        reasons.append("workspace_bundle: missing build/test execution evidence")
    if any(marker in text.lower() for marker in _PLACEHOLDER_MARKERS):
        reasons.append("workspace_bundle: contains placeholder or stub markers")
    return tuple(reasons)


def _workspace_bundle_has_agent_standard_evidence(workspace_bundle: bytes | None) -> bool:
    return not _workspace_bundle_agent_standard_reasons(workspace_bundle)


def _workspace_bundle_agent_standard_reasons(workspace_bundle: bytes | None) -> tuple[str, ...]:
    if not workspace_bundle:
        return ("workspace_bundle: missing project bundle",)
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            lowered = tuple(name.lower() for name in names)
    except (OSError, zipfile.BadZipFile):
        return ("workspace_bundle: invalid or unreadable zip bundle",)
    basenames = {name.rsplit("/", 1)[-1] for name in lowered}
    reasons: list[str] = []
    if not (
        basenames
        & {
            "implementation_plan.md",
            "project_plan.md",
            "plan.md",
            "architecture_plan.md",
        }
    ):
        reasons.append("workspace_bundle: missing implementation plan artifact")
    if not (basenames & _VERIFICATION_REPORT_BASENAMES):
        reasons.append("workspace_bundle: missing verification report artifact")
    return tuple(reasons)


def _workspace_bundle_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        if not _is_text_candidate(name):
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_verification_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        if name.lower().rsplit("/", 1)[-1] not in _VERIFICATION_REPORT_BASENAMES:
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_json_mappings(workspace_bundle: bytes | None) -> tuple[Mapping[str, object], ...]:
    if not workspace_bundle:
        return ()
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            mappings: list[Mapping[str, object]] = []
            for name in names:
                if not name.lower().endswith(".json"):
                    continue
                try:
                    raw = archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000]
                except (KeyError, RuntimeError, OSError):
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, Mapping):
                    mappings.append(parsed)
    except (OSError, zipfile.BadZipFile):
        return ()
    return tuple(mappings)


def _workspace_bundle_has_quality_payload(workspace_bundle: bytes | None) -> bool:
    return any(
        _mapping_has_quality_payload(mapping)
        for mapping in _workspace_bundle_json_mappings(workspace_bundle)
    )


def _workspace_bundle_has_agent_standard_payload(workspace_bundle: bytes | None) -> bool:
    return any(
        _mapping_has_agent_standard_payload(mapping)
        for mapping in _workspace_bundle_json_mappings(workspace_bundle)
    )


def _read_bundle_file(archive: zipfile.ZipFile, names: Sequence[str], filename: str) -> str:
    for name in names:
        if name.lower().rsplit("/", 1)[-1] != filename:
            continue
        try:
            return archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000]
        except (KeyError, RuntimeError, OSError):
            return ""
    return ""


def _is_text_candidate(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(
        (
            ".css",
            ".html",
            ".js",
            ".json",
            ".jsx",
            ".md",
            ".py",
            ".ts",
            ".tsx",
            ".txt",
            ".yaml",
            ".yml",
        )
    )


def _bundle_has_requirement_document(lowered_names: Sequence[str]) -> bool:
    basenames = {name.rsplit("/", 1)[-1] for name in lowered_names}
    return bool(
        basenames
        & {
            "readme.md",
            "project_requirements.md",
            "requirements.md",
            "spec.md",
            "acceptance.md",
        }
    )


def _bundle_has_source_files(lowered_names: Sequence[str]) -> bool:
    return any(
        name.startswith(("src/", "app/", "pages/"))
        or name.endswith(("/main.py", "/main.ts", "/main.tsx", "/index.html"))
        or _is_project_source_file(name)
        or name in {"main.py", "index.html", "package.json"}
        for name in lowered_names
    )


def _is_project_source_file(name: str) -> bool:
    if "/" not in name:
        return False
    if name.startswith(("tests/", "test/", "scripts/", "docs/", "quality/")):
        return False
    if "/tests/" in name or "/test/" in name:
        return False
    return name.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))


def _bundle_has_verification_path_or_script(
    lowered_names: Sequence[str],
    package_json: str,
) -> bool:
    has_test_path = any(
        name.startswith(("tests/", "test/")) or "/tests/" in name or name.endswith(".test.ts")
        for name in lowered_names
    )
    if has_test_path:
        return True
    try:
        package = json.loads(package_json)
    except (json.JSONDecodeError, TypeError):
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else None
    return isinstance(scripts, dict) and bool({"build", "test"} <= set(scripts))


def _bundle_has_build_test_execution_evidence(verification_text: str) -> bool:
    lowered = verification_text.lower()
    return (
        _line_has_execution_pass(lowered, ("build", "npm run build", "pnpm build", "yarn build"))
        and _line_has_execution_pass(lowered, ("test", "npm test", "npm run test", "pytest"))
    )


def _line_has_execution_pass(text: str, command_markers: Sequence[str]) -> bool:
    for line in text.splitlines():
        if not any(marker in line for marker in command_markers):
            continue
        if any(marker in line for marker in _EXECUTION_PASS_MARKERS):
            return True
    return False


def _should_attempt_deliverable_repair(
    *,
    status: str | None,
    evidence: dict[str, bool],
    case_id: str,
) -> bool:
    return (
        status == "completed"
        and evidence.get("final_artifacts") is True
        and (
            evidence.get("workspace_bundle") is not True
            or evidence.get("deliverable_quality") is not True
            or evidence.get("agent_standard_verification") is not True
            or (
                _case_requires_discussion_trace(case_id)
                and evidence.get("discussion_trace") is not True
            )
            or (
                _case_requires_plugin_contract(case_id)
                and evidence.get("plugin_contract") is not True
            )
        )
    )


def _has_deliverable_repair_trace(events: list[object] | None) -> bool:
    if not isinstance(events, list):
        return False
    return any("deliverable.repair" in json.dumps(event, ensure_ascii=False).lower() for event in events)


def _has_self_repair_trace(events: object) -> bool:
    if not isinstance(events, list):
        return False
    return any("repair" in json.dumps(event, ensure_ascii=False).lower() for event in events)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
