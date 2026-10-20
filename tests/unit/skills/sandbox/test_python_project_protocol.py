"""Python requests have their own discriminator and no project-selected host paths."""

import importlib
import importlib.util
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox.project import (
    ContractProjectBrokerRequest,
    ProjectBrokerRequest,
    ProjectInvocation,
)


@pytest.fixture
def protocol() -> Any:
    name = "agent_hub.skills.sandbox.python_project_protocol"
    assert importlib.util.find_spec(name) is not None, "Python broker protocol missing"
    return importlib.import_module(name)


def original() -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract_sha256": "a" * 64,
        "input_source_sha256": "b" * 64,
        "file_count": 6,
    }


def invocation() -> dict[str, object]:
    return ProjectInvocation(
        execution_id="python_original",
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        project_id="billing",
        session_id="one",
        sandbox_profile="workspace_write",
        timeout_seconds=60,
    ).model_dump()


@pytest.mark.parametrize("kind", ["original", "candidate"])
def test_python_execution_cannot_be_decoded_as_node_v2_or_v3(protocol: Any, kind: str) -> None:
    item = protocol.PythonProjectInvocation.model_validate(
        invocation()
        | {
            "original_reference": original(),
            "source_kind": kind,
            "recipe_sha256": "c" * 64,
            "environment_input_sha256": "d" * 64,
            "install_allowed": True,
        },
        strict=True,
    )
    request = protocol.PythonProjectBrokerRequest(invocation=item)
    assert (request.version, request.action) == (4, "run_python_project")
    assert (
        protocol.PythonProjectBrokerRequest.model_validate_json(
            request.model_dump_json(), strict=True
        )
        == request
    )
    for old in (ProjectBrokerRequest, ContractProjectBrokerRequest):
        with pytest.raises(ValidationError):
            old.model_validate(request.model_dump(), strict=True)
    base = protocol.base_python_invocation(item)
    assert type(base) is ProjectInvocation
    assert base.model_dump() == invocation() | {"install_allowed": True}


@pytest.mark.parametrize(
    "key,value",
    [
        ("python", "/usr/bin/python"),
        ("cwd", "/tmp/other"),
        ("wheel_root", "/some/cache"),
        ("environment_path", "/root/env"),
        ("download_url", "https://untrusted.invalid"),
        ("recipe_sha256", "invalid"),
        ("environment_input_sha256", "D" * 64),
    ],
)
def test_untrusted_host_locations_or_unbound_recipe_are_not_protocol_arguments(
    protocol: Any, key: str, value: object
) -> None:
    data = invocation() | {
        "original_reference": original(),
        "source_kind": "candidate",
        "recipe_sha256": "c" * 64,
        "environment_input_sha256": "d" * 64,
        key: value,
    }
    with pytest.raises(ValidationError):
        protocol.PythonProjectInvocation.model_validate(data, strict=True)


def test_probe_cannot_authorize_install_or_receive_candidate_recipe_arguments(
    protocol: Any,
) -> None:
    item = protocol.PythonProjectProbeInvocation.model_validate(
        invocation() | {"original_reference": original()}, strict=True
    )
    request = protocol.PythonProjectProbeRequest(invocation=item)
    assert (request.version, request.action) == (4, "probe_python_project")
    assert (
        protocol.PythonProjectProbeRequest.model_validate_json(
            request.model_dump_json(), strict=True
        )
        == request
    )
    for changes in (
        {"install_allowed": True},
        {"source_kind": "candidate"},
        {"recipe_sha256": "a" * 64},
    ):
        with pytest.raises(ValidationError):
            protocol.PythonProjectProbeInvocation.model_validate(
                item.model_dump() | changes, strict=True
            )


@pytest.mark.parametrize("version", [True, 2, 3, "4", 4.0])
def test_python_request_version_is_not_coerced(protocol: Any, version: object) -> None:
    item = protocol.PythonProjectProbeInvocation.model_validate(
        invocation() | {"original_reference": original()}, strict=True
    )
    with pytest.raises(ValidationError):
        protocol.PythonProjectProbeRequest.model_validate(
            {"version": version, "action": "probe_python_project", "invocation": item}, strict=True
        )


def scope() -> dict[str, object]:
    return {
        "tenant_id": UUID(int=1),
        "run_id": UUID(int=2),
        "project_id": "billing",
        "session_id": "one",
        "original_reference": original(),
    }


def environment() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scope": scope(),
        "recipe_sha256": "c" * 64,
        "environment_input_sha256": "d" * 64,
        "environment_sha256": "e" * 64,
        "security_manifest_sha256": "f" * 64,
        "origin_execution_id": "python_original",
    }


def test_environment_reference_is_a_scoped_root_receipt_not_a_cache_path(protocol: Any) -> None:
    kind = getattr(protocol, "PythonEnvironmentReference", None)
    assert kind is not None, "Python root environment reference missing"
    item = kind.model_validate(environment(), strict=True)
    assert kind.model_validate_json(item.model_dump_json(), strict=True) == item
    for changes in (
        {"cache_path": "/var/lib/another-tenant"},
        {"schema_version": True},
        {"environment_sha256": "invalid"},
        {"origin_execution_id": "../other"},
    ):
        with pytest.raises(ValidationError):
            kind.model_validate(environment() | changes, strict=True)


def test_run_response_carries_independent_root_environment_reference(protocol: Any) -> None:
    kind = getattr(protocol, "PythonProjectBrokerResponse", None)
    assert kind is not None, "Python root response missing"
    result = {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False}
    response = kind.model_validate(
        {
            "version": 4,
            "action": "run_python_project",
            "execution_id": "python_original",
            "ok": True,
            "result": result,
            "environment_reference": environment(),
        },
        strict=True,
    )
    assert response.result.stdout == "{}"
    assert response.environment_reference.environment_sha256 == "e" * 64
    assert kind.model_validate_json(response.model_dump_json(), strict=True) == response
    with pytest.raises(ValidationError):
        kind.model_validate(
            response.model_dump() | {"error": "python_project_execution_unavailable"}, strict=True
        )


def test_probe_or_error_cannot_smuggle_a_passed_execution_result(protocol: Any) -> None:
    kind = getattr(protocol, "PythonProjectBrokerResponse", None)
    assert kind is not None, "Python root response missing"
    base = {
        "version": 4,
        "action": "probe_python_project",
        "execution_id": "python_probe",
        "ok": False,
        "error": "python_project_wheel_missing",
    }
    assert kind.model_validate(base, strict=True).result is None
    for changes in (
        {"result": {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False}},
        {"environment_reference": environment()},
        {"ok": True},
        {"error": "/private/host/path"},
    ):
        with pytest.raises(ValidationError):
            kind.model_validate(base | changes, strict=True)


@pytest.mark.parametrize("change", ["execution", "action", "scope", "recipe", "environment_inputs"])
def test_root_response_must_match_the_actual_request_not_its_own_body(
    protocol: Any, change: str
) -> None:
    request = protocol.PythonProjectBrokerRequest(
        invocation=protocol.PythonProjectInvocation.model_validate(
            invocation()
            | {
                "original_reference": original(),
                "source_kind": "candidate",
                "recipe_sha256": "c" * 64,
                "environment_input_sha256": "d" * 64,
            },
            strict=True,
        )
    )
    payload: dict[str, Any] = {
        "version": 4,
        "action": "run_python_project",
        "execution_id": "python_original",
        "ok": True,
        "result": {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False},
        "environment_reference": environment(),
    }
    kind = protocol.PythonProjectBrokerResponse
    good = kind.model_validate(payload, strict=True)
    assert getattr(good, "require_for", None) is not None, "root response context check missing"
    good.require_for(request)
    if change == "execution":
        payload["execution_id"] = "another_execution"
    elif change == "action":
        payload = {
            "version": 4,
            "action": "probe_python_project",
            "execution_id": "python_original",
            "ok": False,
            "error": "python_project_runtime_missing",
        }
    elif change == "scope":
        payload["environment_reference"]["scope"]["run_id"] = UUID(int=99)
    elif change == "recipe":
        payload["environment_reference"]["recipe_sha256"] = "a" * 64
    else:
        payload["environment_reference"]["environment_input_sha256"] = "a" * 64
    bad = kind.model_validate(payload, strict=True)
    with pytest.raises(ValueError):
        bad.require_for(request)


def inspect_request(protocol: Any, **changes: Any) -> Any:
    item = protocol.PythonProjectInvocation.model_validate(
        invocation()
        | {
            "original_reference": original(),
            "source_kind": "candidate",
            "recipe_sha256": "c" * 64,
            "environment_input_sha256": "d" * 64,
        },
        strict=True,
    )
    return protocol.PythonProjectInspectEnvironmentRequest.model_validate(
        {
            "invocation": item,
            "expected_environment": environment(),
            **changes,
        },
        strict=True,
    )


def test_inspect_uses_explicit_python_v4_and_only_existing_invocation_fields(protocol: Any) -> None:
    kind = getattr(protocol, "PythonProjectInspectEnvironmentRequest", None)
    assert kind is not None, "explicit Python cache inspection request missing"
    request = inspect_request(protocol)
    assert (request.version, request.action) == (4, "inspect_python_project_environment")
    assert set(request.model_dump()) == {"version", "action", "invocation", "expected_environment"}
    assert kind.model_validate_json(request.model_dump_json(), strict=True) == request
    for old in (ProjectBrokerRequest, ContractProjectBrokerRequest):
        with pytest.raises(ValidationError):
            old.model_validate_json(request.model_dump_json(), strict=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", UUID(int=3)),
        ("run_id", UUID(int=3)),
        ("project_id", "other"),
        ("session_id", "other"),
        ("original_reference", original() | {"contract_sha256": "b" * 64}),
        ("recipe_sha256", "b" * 64),
        ("environment_input_sha256", "b" * 64),
    ],
)
def test_inspect_expected_environment_must_match_invocation(
    protocol: Any,
    field: str,
    value: object,
) -> None:
    expected: Any = environment()
    if field in {"recipe_sha256", "environment_input_sha256"}:
        expected[field] = value
    else:
        expected["scope"][field] = value
    with pytest.raises(ValidationError):
        inspect_request(protocol, expected_environment=expected)


@pytest.mark.parametrize("extra", ["cache_path", "model_path", "install_authority", "lease"])
def test_inspect_accepts_no_new_host_locations_or_authority_fields(
    protocol: Any, extra: str
) -> None:
    with pytest.raises(ValidationError):
        inspect_request(protocol, **{extra: "/private/host"})


def test_inspect_success_contains_only_the_exact_expected_reference(protocol: Any) -> None:
    request = inspect_request(protocol)
    response = protocol.PythonProjectBrokerResponse.model_validate(
        {
            "version": 4,
            "action": request.action,
            "execution_id": request.invocation.execution_id,
            "ok": True,
            "environment_reference": environment(),
        },
        strict=True,
    )
    response.require_for(request)
    assert response.result is None and response.observation is None
    assert response.environment_reference == request.expected_environment


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_sha256", "a" * 64),
        ("security_manifest_sha256", "a" * 64),
        ("origin_execution_id", "other_execution"),
    ],
)
def test_inspect_reply_cannot_replace_an_expected_cache_receipt(
    protocol: Any,
    field: str,
    value: object,
) -> None:
    request = inspect_request(protocol)
    changed = environment() | {field: value}
    response = protocol.PythonProjectBrokerResponse.model_validate(
        {
            "version": 4,
            "action": request.action,
            "execution_id": request.invocation.execution_id,
            "ok": True,
            "environment_reference": changed,
        },
        strict=True,
    )
    with pytest.raises(ValueError):
        response.require_for(request)


@pytest.mark.parametrize(
    "change", ["missing", "result", "observation", "error", "failed-with-reference"]
)
def test_inspect_cannot_smuggle_execution_or_probe_results(protocol: Any, change: str) -> None:
    request = inspect_request(protocol)
    payload: dict[str, Any] = {
        "version": 4,
        "action": request.action,
        "execution_id": request.invocation.execution_id,
        "ok": True,
        "environment_reference": environment(),
    }
    if change == "missing":
        payload.pop("environment_reference")
    elif change == "result":
        payload["result"] = {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False}
    elif change == "observation":
        payload["observation"] = {}
    elif change == "failed-with-reference":
        payload.update(ok=False, error="python_project_environment_unavailable")
    else:
        payload["error"] = "python_project_environment_unavailable"
    with pytest.raises(ValidationError):
        protocol.PythonProjectBrokerResponse.model_validate(payload, strict=True)
