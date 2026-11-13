"""Finite model-selection diagnostics never derive authority from framework text."""
from collections.abc import Mapping
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import ModelCapability
from agent_hub.runtime import failure_reason
from agent_hub.runtime.contracts import EventKind, RunEvent


def test_direct_failure_keeps_typed_code_without_fabricating_request_context() -> None:
    diagnostic = failure_reason.safe_runtime_failure_diagnostic(
        NoCapableDeployment("Authorization: PRIVATE_PROVIDER_SECRET")
    )
    assert diagnostic["error_code"] == "model.no_capable_deployment"
    assert diagnostic["retryable"] is False
    assert diagnostic["model_preflight_error_type"] == "NoCapableDeployment"
    assert "model_preflight_logical_model" not in diagnostic
    assert "model_preflight_required_capabilities" not in diagnostic
    assert "PRIVATE_PROVIDER_SECRET" not in str(diagnostic)


def test_candidate_metadata_reads_real_request_enums_not_exception_args() -> None:
    class Unprintable(NoCapableDeployment):
        def __str__(self) -> str:
            raise AssertionError("diagnostic must not read exception text")

    helper = getattr(failure_reason, "model_candidate_failure_payload", None)
    assert callable(helper)
    error = Unprintable("PRIVATE_USER_PROMPT")
    error.__cause__ = ValueError("password=PRIVATE_PROVIDER_SECRET")
    payload = helper(
        error, logical_model="qwen",
        required_capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.TEXT}),
    )
    assert payload == {
        "model_preflight_error_type": "NoCapableDeployment",
        "model_preflight_error_code": "model.no_capable_deployment",
        "model_preflight_phase": "candidate_selection",
        "model_preflight_logical_model": "qwen",
        "model_preflight_required_capabilities": "text, tool_calling",
    }


@pytest.mark.parametrize("logical_model", ["api_key", "qwen password=PRIVATE", "../qwen", "x" * 129])
def test_untrusted_metadata_cannot_mint_model_or_capability_claims(logical_model: str) -> None:
    helper = getattr(failure_reason, "model_candidate_failure_payload", None)
    assert callable(helper)
    payload = helper(
        NoCapableDeployment("private"), logical_model=logical_model,
        required_capabilities=frozenset({"text", "tool_calling"}),
    )
    assert "model_preflight_logical_model" not in payload
    assert "model_preflight_required_capabilities" not in payload
    assert helper(ValueError("no capable deployment for logical model qwen: text, tool_calling")) == {}


def _event(payload: Mapping[str, object]) -> RunEvent:
    return RunEvent.model_validate({
        "kind": EventKind.RUNTIME_FAILED, "sequence": 1, "run_id": uuid4(),
        "reason": "model gateway failed: no capable deployment", "payload": payload,
    })


def _diagnostic() -> dict[str, object]:
    return {
        **failure_reason.runtime_failure_diagnostic_from_reason("model gateway failed: no capable deployment"),
        **failure_reason.model_candidate_failure_payload(
            NoCapableDeployment("private"), logical_model="qwen",
            required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
        ),
    }


@pytest.mark.parametrize("overrides", [
    {"model_preflight_error_type": "ValueError"},
    {"model_preflight_error_code": "model.transport_failed"},
    {"model_preflight_phase": "response"},
    {"model_preflight_logical_model": "../qwen"},
    {"model_preflight_logical_model": "api_key"},
    {"model_preflight_logical_model": True},
    {"model_preflight_required_capabilities": ""},
    {"model_preflight_required_capabilities": "text, unknown"},
    {"model_preflight_required_capabilities": "tool_calling, text"},
    {"model_preflight_required_capabilities": "text, text"},
    {"model_preflight_required_capabilities": ["text"]},
    {"model_preflight_unrecognized": "candidate_selection"},
])
def test_failure_event_rejects_unbounded_or_fabricated_candidate_metadata(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _event({**_diagnostic(), **overrides})


@pytest.mark.parametrize("field", [
    "model_preflight_error_type", "model_preflight_error_code", "model_preflight_phase",
])
def test_failure_event_requires_complete_typed_candidate_identity(field: str) -> None:
    payload = _diagnostic()
    del payload[field]
    with pytest.raises(ValidationError):
        _event(payload)


@pytest.mark.parametrize("capabilities", ["none", ", ".join(sorted(item.value for item in ModelCapability))])
def test_failure_event_accepts_only_canonical_known_capability_sets(capabilities: str) -> None:
    payload = _diagnostic()
    payload["model_preflight_required_capabilities"] = capabilities
    event = _event(payload)
    assert RunEvent.model_validate_json(event.model_dump_json()).payload == event.payload


@pytest.mark.parametrize("alias", ["qwen", "reasoning"])
def test_emitted_model_alias_is_always_accepted_as_a_value_not_a_payload_key(alias: str) -> None:
    payload = {
        **failure_reason.runtime_failure_diagnostic_from_reason("model gateway failed: no capable deployment"),
        **failure_reason.model_candidate_failure_payload(NoCapableDeployment("private"), logical_model=alias),
    }
    assert payload["model_preflight_logical_model"] == alias
    assert _event(payload).payload["model_preflight_logical_model"] == alias
