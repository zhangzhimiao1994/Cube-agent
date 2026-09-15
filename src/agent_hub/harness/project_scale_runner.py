from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from agent_hub.harness.project_scale import ProjectScaleRunPlan, build_project_scale_run_plan

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


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
    errors: tuple[str, ...] = ()

    @property
    def required_evidence(self) -> tuple[str, ...]:
        required: tuple[str, ...] = (
            "run_details",
            "run_events",
            "terminal_status",
            "final_artifacts",
            "project_preflight_approval",
            "workspace_bundle",
            "cleanup_cancel",
        )
        if not _case_requires_project_preflight(self.case_id):
            required = tuple(key for key in required if key != "project_preflight_approval")
        if "self_repair" in self.case_id or "model_failure" in self.case_id:
            required = (*required, "self_repair_trace")
        return required

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return tuple(key for key in self.required_evidence if self.evidence.get(key) is not True)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing_evidence

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
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

    def to_payload(self) -> dict[str, object]:
        return {
            "execute": True,
            "dry_run": False,
            "ok": self.ok,
            "case_count": self.case_count,
            "results": [result.to_payload() for result in self.results],
        }


class UrllibAcceptanceClient:
    def __init__(self, *, base_url: str, bearer_token: str, timeout: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._bearer_token = bearer_token
        self._timeout = timeout

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
    ) -> bytes:
        url = urljoin(self._base_url, path.lstrip("/"))
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return cast(bytes, response.read())
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed status={error.code} body={body[:240]}") from error
        except URLError as error:
            raise RuntimeError(f"{method} {path} failed: {error.reason}") from error


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

            deadline = time.monotonic() + max(wait_seconds, 0)
            while True:
                details = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
                evidence["run_details"] = isinstance(details, dict)
                if isinstance(details, dict):
                    _validate_run_details_scope(details, run_id)
                    status = _string_value(details.get("status")) or status
                    evidence["final_artifacts"] = _has_final_artifacts(details)
                if _is_terminal_status(status):
                    evidence["terminal_status"] = True
                    break
                if wait_seconds <= 0 or time.monotonic() >= deadline:
                    break
                if poll_interval_seconds > 0:
                    time.sleep(poll_interval_seconds)
            if evidence["terminal_status"] and status != "completed":
                errors.append(f"terminal_status: {status or 'unknown'}")

            events = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/events")
            if isinstance(events, list) and events:
                evidence["run_events"] = True
                errors.extend(_validate_run_events_scope(events, run_id))
            elif isinstance(events, list):
                errors.append("run_events: empty event stream")
            else:
                errors.append("run_events: returned non-list JSON")
            evidence["self_repair_trace"] = _has_self_repair_trace(events)

            try:
                client.request_bytes("GET", _workspace_bundle_path(run_request.body))
                evidence["workspace_bundle"] = True
            except Exception as error:  # noqa: BLE001 - acceptance reports must continue cleanup.
                errors.append(f"workspace_bundle: {error}")
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

    if args.execute and not os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN"):
        parser.error("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN is required for --execute")

    try:
        plan = build_project_scale_run_plan(
            scales=tuple(args.scales) if args.scales is not None else None,
            flows=tuple(args.flows) if args.flows is not None else None,
            execute=args.execute,
        )
    except ValueError as error:
        parser.error(str(error))

    if args.execute:
        token = os.environ["AGENT_HUB_ACCEPTANCE_BEARER_TOKEN"]
        report = execute_project_scale_plan(
            plan,
            UrllibAcceptanceClient(base_url=args.base_url, bearer_token=token, timeout=args.timeout),
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
                print(request.case_id)
    return 0 if (not args.execute or report.ok) else 1


def format_project_scale_result_line(result: ProjectScaleCaseResult) -> str:
    parts = [
        result.case_id,
        f"run_id={result.run_id or '-'}",
        f"ok={str(result.ok).lower()}",
    ]
    if result.missing_evidence:
        parts.append(f"missing={','.join(result.missing_evidence)}")
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    return " ".join(parts)


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


def _validate_run_submission_scope(response: dict[str, object], body: dict[str, object]) -> None:
    for field in ("project_id", "workspace_session_id"):
        expected = body.get(field)
        actual = response.get(field)
        if not isinstance(expected, str) or not expected:
            raise RuntimeError(f"run request missing {field}")
        if actual != expected:
            got = actual if isinstance(actual, str) and actual else "missing"
            raise RuntimeError(f"run scope mismatch: {field} expected {expected} got {got}")


def _validate_run_details_scope(details: dict[str, object], run_id: str) -> None:
    actual = details.get("id")
    if actual != run_id:
        got = actual if isinstance(actual, str) and actual else "missing"
        raise RuntimeError(f"run details scope mismatch: id expected {run_id} got {got}")


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
    artifact_count = details.get("artifact_count")
    return isinstance(artifact_count, int) and artifact_count > 0


def _has_self_repair_trace(events: dict[str, object] | list[object]) -> bool:
    if not isinstance(events, list):
        return False
    return any("repair" in json.dumps(event, ensure_ascii=False).lower() for event in events)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
