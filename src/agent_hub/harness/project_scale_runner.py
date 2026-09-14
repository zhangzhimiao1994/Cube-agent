from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from agent_hub.harness.project_scale import ProjectScaleRunPlan, build_project_scale_run_plan


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
    def ok(self) -> bool:
        return not self.errors and all(self.evidence.values())

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
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
) -> ProjectScaleExecutionReport:
    results: list[ProjectScaleCaseResult] = []
    for index, run_request in enumerate(plan.requests):
        evidence = {
            "run_details": False,
            "run_events": False,
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
                idempotency_key=_idempotency_key(run_request.case_id, index),
            )
            if not isinstance(response, dict):
                raise TypeError("run create returned non-object JSON")
            raw_run_id = response.get("id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                raise RuntimeError("run create response missing id")
            run_id = raw_run_id
            status = _string_value(response.get("status"))

            details = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
            evidence["run_details"] = isinstance(details, dict)
            if isinstance(details, dict):
                status = _string_value(details.get("status")) or status

            events = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/events")
            evidence["run_events"] = isinstance(events, list)

            try:
                client.request_bytes("GET", _workspace_bundle_path(run_request.body))
                evidence["workspace_bundle"] = True
            except Exception as error:  # noqa: BLE001 - acceptance reports must continue cleanup.
                errors.append(f"workspace_bundle: {error}")
        except Exception as error:  # noqa: BLE001 - collect per-case failures and continue.
            errors.append(str(error))
        finally:
            if run_id is not None:
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
        )
        payload = report.to_payload()
    else:
        payload = plan.to_payload()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        if args.execute:
            print(f"project-scale execution cases={report.case_count} ok={str(report.ok).lower()}")
            for result in report.results:
                print(f"{result.case_id} run_id={result.run_id or '-'} ok={str(result.ok).lower()}")
        else:
            print(
                f"project-scale plan cases={plan.case_count} "
                f"dry_run={str(plan.dry_run).lower()} execute={str(plan.execute).lower()}"
            )
            for request in plan.requests:
                print(request.case_id)
    return 0 if (not args.execute or report.ok) else 1


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


def _idempotency_key(case_id: str, index: int) -> str:
    safe_case = case_id.replace(":", "-").replace("_", "-")
    return f"project-scale-{safe_case}-{index}"


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
