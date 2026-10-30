"""Pure review codecs: validation, digest binding and private snapshot isolation."""

import builtins
import hashlib
import importlib
import json
import socket
from dataclasses import FrozenInstanceError
from types import MappingProxyType, ModuleType
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.types import CapabilityRequest


def contracts() -> ModuleType:
    # Delayed import makes the initial missing implementation a test failure.
    return importlib.import_module("agent_hub.capabilities.review_contracts")


DEFAULTS = {
    "schema_version": 1,
    "engine": "rules",
    "reviewer_logical_model": None,
    "require_distinct_model": False,
    "timeout_seconds": 15,
    "max_input_tokens": 4096,
    "max_output_tokens": 512,
    "max_reviews_per_run": 8,
    "max_transport_attempts_per_review": 1,
    "max_cost_usd_per_run": "0.00",
    "denial_breaker_consecutive": 3,
    "denial_breaker_denials": 10,
    "denial_breaker_window": 50,
}
TURN = UUID("00000000-0000-0000-0000-000000000003")
DIGEST = "a" * 64


def request(**changes: object) -> CapabilityRequest:
    payload: dict[str, object] = {
        "tenant_id": UUID("00000000-0000-0000-0000-000000000001"),
        "user_id": UUID("00000000-0000-0000-0000-000000000002"),
        "agent_id": "worker-1",
        "capability": "workspace",
        "operation": "write",
        "resource": "private-path",
        "arguments": {"nested": [{"text": "private-content"}]},
        "idempotency_key": "key-1",
        "run_id": UUID("00000000-0000-0000-0000-000000000004"),
        "tool_origin": "plugin",
    }
    payload.update(changes)
    return CapabilityRequest.model_validate(payload)


def freeze(req: CapabilityRequest | None = None, **changes: object):
    bindings = {
        "role": Role.OPERATOR,
        "authorization_turn_id": TURN,
        "authorization_sha256": "b" * 64,
        "policy_sha256": "c" * 64,
        "config_sha256": "d" * 64,
        "source_binding": {"version": 1, "evidence": [{"hash": "private-source"}]},
        "normalized_effect": {"operation": "write", "target": "private-target"},
    }
    bindings.update(changes)
    return contracts().freeze_review_action(req if req is not None else request(), **bindings)


def verdict_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "decision": "allow",
        "risk": "low",
        "reason_code": "authorized_action",
        "rationale": "Within the requested scope.",
        "action_digest": DIGEST,
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize("payload", [None, {}, MappingProxyType({})])
def test_legacy_configs_default_to_rules_without_io(payload, monkeypatch):
    module = contracts()

    def forbidden(*args, **kwargs):
        raise AssertionError("codec attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        config = module.ApprovalReviewConfig.from_payload(payload)
        assert config.to_payload() == DEFAULTS
        assert config.engine == "rules"
        assert config.reviewer_logical_model is None
        assert config.max_cost_usd_per_run == "0.00"
        assert module.ApprovalReviewConfig.from_payload(config.to_payload()) == config
        config.to_payload()["engine"] = "rules_then_model"
        assert config.engine == "rules"
    with pytest.raises(FrozenInstanceError):
        config.engine = "rules_then_model"


@pytest.mark.parametrize("payload", [[], "rules", 0, False, {1: "private-value"}])
def test_config_requires_object(payload):
    with pytest.raises(ValueError):
        contracts().ApprovalReviewConfig.from_payload(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"engine": "model"},
        {"engine": []},
        {"require_distinct_model": 1},
        {"timeout_seconds": True},
        {"timeout_seconds": 0.9},
        {"timeout_seconds": 61},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": "15"},
        {"max_input_tokens": True},
        {"max_input_tokens": 0},
        {"max_input_tokens": 1_000_001},
        {"max_output_tokens": 1.0},
        {"max_output_tokens": -1},
        {"max_output_tokens": 1_000_001},
        {"max_reviews_per_run": True},
        {"max_reviews_per_run": 0},
        {"max_reviews_per_run": 2**63},
        {"max_reviews_per_run": 1.5},
        {"max_transport_attempts_per_review": True},
        {"max_transport_attempts_per_review": 0},
        {"max_transport_attempts_per_review": 2},
        {"max_cost_usd_per_run": 0},
        {"max_cost_usd_per_run": "NaN"},
        {"max_cost_usd_per_run": "Infinity"},
        {"max_cost_usd_per_run": "-0.01"},
        {"max_cost_usd_per_run": "1000000.000001"},
        {"max_cost_usd_per_run": "0.0000001"},
        {"denial_breaker_consecutive": 0},
        {"denial_breaker_consecutive": True},
        {"denial_breaker_consecutive": 11},
        {"denial_breaker_denials": 51},
        {"denial_breaker_denials": 2},
        {"denial_breaker_window": 2**63},
        {"denial_breaker_window": 50.0},
        {"reviewer_logical_model": "Upper"},
        {"reviewer_logical_model": "a/b"},
        {"reviewer_logical_model": "x" * 129},
        {"reviewer_logical_model": "x\n"},
        {"reviewer_logical_model": ""},
        {"reviewer_logical_model": 1},
        {"engine": "rules_then_model"},
        {"engine": "rules_then_model", "reviewer_logical_model": "reviewer"},
        {"engine": "rules_then_model", "max_cost_usd_per_run": "1.00"},
        {"api_base": "private-value"},
        {"api_key": "private-value"},
        {"prompt": "private-value"},
        {"fallback": "private-value"},
    ],
)
def test_reject_invalid_config_without_echoing_input(changes):
    with pytest.raises(ValueError) as error:
        contracts().ApprovalReviewConfig.from_payload(changes)
    assert "private-value" not in str(error.value)


def test_model_config_accepts_storage_bounds_without_enabling_provider():
    payload = {
        "engine": "rules_then_model",
        "reviewer_logical_model": "0" + "a" * 127,
        "require_distinct_model": True,
        "timeout_seconds": 1.5,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 1,
        "max_reviews_per_run": 2**63 - 1,
        "max_cost_usd_per_run": "1000000.000000",
        "denial_breaker_consecutive": 1,
        "denial_breaker_denials": 1,
        "denial_breaker_window": 1,
    }
    config = contracts().ApprovalReviewConfig.from_payload(payload)
    assert config.to_payload() == {**DEFAULTS, **payload}


def test_snapshot_isolates_all_nested_inputs_and_returns_fresh_copies():
    req = request()
    source = {"entry": [{"text": "original"}]}
    effect = {"target": ["original"]}
    action = freeze(req, source_binding=source, normalized_effect=effect)
    before = action.to_payload()
    digest = action.action_digest
    req.arguments["nested"][0]["text"] = "mutated"
    source["entry"][0]["text"] = "mutated"
    effect["target"].append("mutated")
    reconstructed = action.request()
    assert reconstructed == request()
    assert reconstructed is not req
    reconstructed.arguments["nested"].append("mutated")
    payload = action.to_payload()
    payload["request"]["arguments"]["nested"].append("mutated")
    payload["source_binding"]["entry"].clear()
    assert action.to_payload() == before
    assert action.action_digest == digest
    assert action.request() == request()
    assert "private-content" not in repr(action)
    assert "private-source" not in repr(action)
    assert "private-target" not in repr(action)
    with pytest.raises(FrozenInstanceError):
        action.action_digest = "e" * 64


def test_immutable_mapping_and_tuple_inputs_are_json_snapshotted():
    req = request()
    req.arguments["immutable"] = MappingProxyType({"tuple": (1, True, None, "中文")})
    action = freeze(
        req,
        source_binding=MappingProxyType({"evidence": (1, {"b": 2})}),
        normalized_effect=MappingProxyType({"target": ("中文",)}),
    )
    assert action.request().arguments["immutable"] == {"tuple": [1, True, None, "中文"]}
    assert action.to_payload()["normalized_effect"] == {"target": ["中文"]}


def test_canonical_digest_is_bound_to_utf8_compact_sorted_payload():
    action = freeze(normalized_effect={"z": "中文", "a": [1, False]})
    payload = action.to_payload()
    assert payload["role"] == "operator"
    assert payload["authorization_turn_id"] == str(TURN)
    assert payload["request"]["tool_origin"] == "plugin"
    unsigned = {k: v for k, v in payload.items() if k not in {"action_digest", "effect_digest"}}
    encoded = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    assert action.action_digest == hashlib.sha256(encoded).hexdigest()
    reordered = freeze(normalized_effect={"a": [1, False], "z": "中文"})
    assert reordered.action_digest == action.action_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"role": Role.ADMIN},
        {"authorization_turn_id": UUID(int=99)},
        {"authorization_sha256": "e" * 64},
        {"policy_sha256": "e" * 64},
        {"config_sha256": "e" * 64},
        {"source_binding": {"version": 1, "new_field": {"private": "bound"}}},
        {"normalized_effect": {"operation": "delete", "target": "private-target"}},
    ],
)
def test_action_digest_binds_every_review_context(changes):
    assert freeze(**changes).action_digest != freeze().action_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"agent_id": "worker-2"},
        {"idempotency_key": "key-2"},
        {"run_id": UUID(int=99)},
        {"resource": "another-path"},
        {"arguments": {"changed": True}},
        {"tool_origin": "mcp"},
        {"capability": "another"},
        {"operation": "read"},
    ],
)
def test_action_binds_every_request_field_but_effect_uses_server_normalization(changes):
    original = freeze()
    changed = freeze(request(**changes))
    assert changed.action_digest != original.action_digest
    assert changed.effect_digest == original.effect_digest


@pytest.mark.parametrize("field", ["tenant_id", "user_id"])
def test_effect_is_tenant_and_user_bound(field):
    assert freeze(request(**{field: UUID(int=99)})).effect_digest != freeze().effect_digest


def test_effect_binds_original_authorization_and_normalized_effect():
    original = freeze()
    assert freeze(authorization_turn_id=UUID(int=99)).effect_digest != original.effect_digest
    assert freeze(normalized_effect={"target": "elsewhere"}).effect_digest != original.effect_digest
    assert freeze(role=Role.ADMIN, policy_sha256="e" * 64).effect_digest == original.effect_digest


class PrivateObject:
    def __repr__(self):
        return "private-value-never-echo"


@pytest.mark.parametrize(
    "bad",
    [
        PrivateObject(),
        b"private-value-never-echo",
        float("nan"),
        float("inf"),
        {1: "private-value-never-echo"},
        {"set"},
        "\ud800",
    ],
)
@pytest.mark.parametrize("location", ["arguments", "source_binding", "normalized_effect"])
def test_reject_non_json_without_echoing_private_values(bad, location):
    req = request()
    bindings = {}
    if location == "arguments":
        req.arguments["bad"] = bad  # CapabilityRequest is only shallowly frozen.
    else:
        bindings[location] = {"bad": bad}
    with pytest.raises(ValueError) as error:
        freeze(req, **bindings)
    assert "private-value-never-echo" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"authorization_sha256": "invalid"},
        {"policy_sha256": "a" * 63},
        {"config_sha256": "G" * 64},
        {"role": "operator"},
        {"authorization_turn_id": str(TURN)},
        {"source_binding": []},
        {"normalized_effect": []},
    ],
)
def test_snapshot_requires_typed_context_and_sha256(changes):
    with pytest.raises(ValueError):
        freeze(**changes)


def test_snapshot_rejects_cycles_without_echoing_values():
    cycle = {"secret": "private-value-never-echo"}
    cycle["cycle"] = cycle
    with pytest.raises(ValueError) as error:
        freeze(source_binding=cycle)
    assert "private-value-never-echo" not in str(error.value)


@pytest.mark.parametrize(
    "decision,risk",
    [("allow", "low"), ("allow", "medium"), ("deny", "high"), ("needs_human", "unknown")],
)
def test_verdict_roundtrip_keeps_private_rationale_out_of_repr(decision, risk):
    payload = verdict_payload(decision=decision, risk=risk)
    verdict = contracts().parse_model_review(json.dumps(payload), expected_action_digest=DIGEST)
    assert verdict.to_payload() == payload
    assert verdict.decision == decision
    assert verdict.rationale == payload["rationale"]
    assert payload["rationale"] not in repr(verdict)
    verdict.to_payload()["decision"] = "deny"
    assert verdict.decision == decision
    with pytest.raises(FrozenInstanceError):
        verdict.decision = "deny"


@pytest.mark.parametrize(
    "reason",
    [
        "authorized_action",
        "outside_authorized_scope",
        "protected_resource",
        "untrusted_content",
        "unclear_impact",
        "missing_context",
        "policy_requires_human",
    ],
)
def test_verdict_accepts_only_documented_reason_vocabulary(reason):
    verdict = contracts().parse_model_review(
        json.dumps(verdict_payload(decision="deny", reason_code=reason)),
        expected_action_digest=DIGEST,
    )
    assert verdict.reason_code == reason


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"decision": "approve"},
        {"decision": []},
        {"risk": "critical"},
        {"risk": None},
        {"reason_code": "free-text"},
        {"reason_code": []},
        {"extra": "private-value-never-echo"},
        {"action_digest": "b" * 64},
        {"action_digest": DIGEST.upper()},
        {"action_digest": None},
        {"risk": "high"},
        {"risk": "unknown"},
        {"rationale": ""},
        {"rationale": " padded"},
        {"rationale": "padded "},
        {"rationale": "two\nlines"},
        {"rationale": "two\rlines"},
        {"rationale": "tab\ttext"},
        {"rationale": "a\u2028b"},
        {"rationale": "a\u200bb"},
        {"rationale": "x" * 241},
        {"rationale": 7},
        {"rationale": "api_key=private-value-never-echo"},
        {"rationale": "Authorization: Bearer private-value-never-echo"},
        {"rationale": "sk-proj-private-value-never-echo"},
        {"rationale": "password: private-value-never-echo"},
        {"rationale": "token=private-value-never-echo"},
        {"rationale": "client_secret=private-value-never-echo"},
        {"rationale": "github_pat_private-value-never-echo"},
        {"rationale": "ghp_private-value-never-echo"},
        {"rationale": "xoxb-private-value-never-echo"},
        {"rationale": "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.private-value-never-echo"},
        {"rationale": "https://user:private-value-never-echo@example.test"},
    ],
)
def test_verdict_rejects_invalid_and_secret_fields_without_echoing(changes):
    with pytest.raises(ValueError) as error:
        contracts().parse_model_review(
            json.dumps(verdict_payload(**changes)), expected_action_digest=DIGEST
        )
    assert "private-value-never-echo" not in str(error.value)
    assert "private-value-never-echo" not in repr(error.value)


@pytest.mark.parametrize("key", ["api_key", "password", "client_secret"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_verdict_rejects_quoted_credential_keys_without_echoing(key, quote):
    secret = "synthetic-review-secret"
    rationale = "{" + quote + key + quote + ": " + quote + secret + quote + "}"
    with pytest.raises(ValueError) as error:
        contracts().parse_model_review(
            json.dumps(verdict_payload(rationale=rationale)), expected_action_digest=DIGEST
        )
    assert secret not in str(error.value)
    assert secret not in repr(error.value)


@pytest.mark.parametrize(
    "rationale",
    [
        'The "api_key" field is omitted.',
        'The "password" field is outside the requested scope.',
        'The "client_secret" name is documented without a value.',
        "The 'api_key' field is omitted.",
        '{"api_key_present": false}',
        '{"summary": "credential fields omitted"}',
    ],
)
def test_verdict_accepts_credential_field_mentions_without_assignment(rationale):
    verdict = contracts().parse_model_review(
        json.dumps(verdict_payload(rationale=rationale)), expected_action_digest=DIGEST
    )
    assert verdict.rationale == rationale


@pytest.mark.parametrize("field", list(verdict_payload()))
def test_verdict_rejects_missing_and_duplicate_fields(field):
    module = contracts()
    payload = verdict_payload()
    del payload[field]
    with pytest.raises(ValueError):
        module.parse_model_review(json.dumps(payload), expected_action_digest=DIGEST)
    valid = json.dumps(verdict_payload())
    duplicate = (
        valid[:-1] + "," + json.dumps(field) + ":" + json.dumps(verdict_payload()[field]) + "}"
    )
    with pytest.raises(ValueError):
        module.parse_model_review(duplicate, expected_action_digest=DIGEST)


@pytest.mark.parametrize("wrapper", ["```json\n{}\n```", "{} trailing", "{}{}", "[{}]"])
def test_verdict_rejects_non_object_or_wrapped_output(wrapper):
    with pytest.raises(ValueError):
        contracts().parse_model_review(
            wrapper.replace("{}", json.dumps(verdict_payload())), expected_action_digest=DIGEST
        )


def test_verdict_enforces_utf8_byte_limit_and_rationale_character_limit():
    module = contracts()
    valid = json.dumps(verdict_payload(rationale="中" * 240), ensure_ascii=False)
    assert module.parse_model_review(valid, expected_action_digest=DIGEST).rationale == "中" * 240
    exact_limit = valid + " " * (8192 - len(valid.encode("utf-8")))
    module.parse_model_review(exact_limit, expected_action_digest=DIGEST)
    for bad in (exact_limit + " ", valid + "中" * 2600, "\ud800", "NaN"):
        with pytest.raises(ValueError):
            module.parse_model_review(bad, expected_action_digest=DIGEST)
