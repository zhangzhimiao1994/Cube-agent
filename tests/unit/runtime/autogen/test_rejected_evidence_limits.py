"""Stopped model receipts retain evidence, never execution capacity."""

from collections.abc import Callable
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.models.gateway import GatewayRejectedOutput
from agent_hub.models.types import ModelMessage, ModelRequest, RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.autogen.engineering_delivery import (
    EngineeringEvidenceLimits,
    engineering_evidence_limits,
)
from agent_hub.runtime.autogen.model_rejection import rejection_record


def snapshot() -> dict[str, object]:
    run_id, tenant_id = uuid4(), uuid4()
    request_hash = "a" * 64
    request = ModelRequest(logical_model="shared", messages=(ModelMessage(role="user", content="build"),))
    rejected = GatewayRejectedOutput(
        evidence=RejectedOutputEvidence(final_text=None, usage=TokenUsage(7, 3, 10),
            usage_status="known", status="incomplete", reason="incomplete"),
        logical_model="shared", deployment_id="shared", provider_id="test",
        provider_model="test/model", cost_usd=Decimal("0.002"),
    )
    receipt = rejection_record(rejected, run_id=run_id, tenant_id=tenant_id, position=0,
                               request=request, request_hash=request_hash)
    return {"id": str(uuid4()), "runtime_type": "autogen", "runtime_version": "2",
            "run_id": str(run_id), "tenant_id": str(tenant_id), "mode": "discuss",
            "state_sha256": "b" * 64, "state": {
                "model_ledger": [{"status": "rejected", "request_hash": request_hash,
                                  "rejection": receipt.to_payload()}],
                "tool_ledger": [], "artifact_registry": {},
            }}


def limits(payload: dict[str, object], *, used: int = 10,
           remaining: float = 60) -> EngineeringEvidenceLimits:
    return engineering_evidence_limits(payload, token_budget=10, used_tokens=used,
        timeout_seconds=60, remaining_seconds=remaining, max_turns=2, tool_fanout=16)


@pytest.mark.parametrize("used,remaining", [(10, 60), (0, 0)], ids=["tokens-exhausted", "wall-expired"])
def test_bound_rejection_is_retained_after_execution_budget_expires(used: int, remaining: float) -> None:
    payload = snapshot()
    before = repr(payload)
    result = limits(payload, used=used, remaining=remaining)
    assert result.soft_limit == 1
    assert result.soft_limit <= result.hard_limit
    assert repr(payload) == before


def model_row(payload: dict[str, object]) -> dict[str, object]:
    state = cast(dict[str, object], payload["state"])
    return cast(list[dict[str, object]], state["model_ledger"])[0]


def receipt(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], model_row(payload)["rejection"])


def usage(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], receipt(payload)["usage"])


@pytest.mark.parametrize("mutate", [
    lambda p: model_row(p).pop("rejection"),
    lambda p: model_row(p).update(status="running"),
    lambda p: model_row(p).update(status="UNKNOWN"),
    lambda p: model_row(p).update(status="succeeded"),
    lambda p: model_row(p).update(request_hash="c" * 64),
    lambda p: model_row(p).update(artifact_id=str(uuid4())),
    lambda p: receipt(p).update(schema_version=True),
    lambda p: receipt(p).update(schema_version=2),
    lambda p: receipt(p).update(run_id=str(uuid4())),
    lambda p: receipt(p).update(tenant_id=str(uuid4())),
    lambda p: receipt(p).update(position=1),
    lambda p: receipt(p).update(position=False),
    lambda p: receipt(p).update(usage_status="invalid"),
    lambda p: usage(p).update(prompt_tokens=True),
    lambda p: usage(p).update(total_tokens=11),
    lambda p: receipt(p).update(cost_usd="NaN"),
    lambda p: receipt(p).update(cost_usd="-0.1"),
    lambda p: cast(dict[str, object], receipt(p)["provenance"]).update(provider_model="other/model"),
    lambda p: receipt(p).update(fallback_used=True),
    lambda p: receipt(p).update(unexpected="extra"),
], ids=["missing", "running", "unknown", "accepted-status", "request", "accepted-artifact",
        "bool-version", "new-version", "run", "tenant", "position", "bool-position",
        "invalid-usage", "bool-usage", "usage-sum", "invalid-cost", "negative-cost",
        "provenance", "fallback", "extra-field"])
def test_malformed_rejection_cannot_claim_retention_capacity(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    payload = snapshot()
    mutate(payload)
    with pytest.raises(ValueError) as failure:
        limits(payload)
    diagnostic = str(failure.value).lower()
    assert "rejection" in diagnostic or "rejectedmodelrecord" in diagnostic


@pytest.mark.parametrize("used,remaining", [(10, 60), (0, 0)], ids=["tokens-exhausted", "wall-expired"])
@pytest.mark.parametrize("pending", ["model", "tool"])
def test_retained_rejection_does_not_admit_another_pending_call(
    used: int, remaining: float, pending: str,
) -> None:
    payload = snapshot()
    state = cast(dict[str, object], payload["state"])
    pending_row: dict[str, object] = {"status": "running", "request_hash": "c" * 64}
    if pending == "model":
        receipt(payload)["position"] = 1
        cast(list[dict[str, object]], state["model_ledger"]).insert(0, pending_row)
    else:
        cast(list[dict[str, object]], state["tool_ledger"]).append(pending_row)
    with pytest.raises(ValueError, match="engineering evidence budget is exhausted"):
        limits(payload, used=used, remaining=remaining)


def test_rejected_tool_row_is_not_a_terminal_model_receipt() -> None:
    payload = snapshot()
    state = cast(dict[str, object], payload["state"])
    state["tool_ledger"], state["model_ledger"] = state["model_ledger"], []
    with pytest.raises(ValueError):
        limits(payload)


@pytest.mark.parametrize("padding", [tuple(range(4096)), ("x" * 500_000,) * 4], ids=["nodes", "bytes"])
def test_rejection_retention_preserves_contract_fuses(padding: object) -> None:
    payload = snapshot()
    cast(dict[str, object], payload["state"])["padding"] = padding
    with pytest.raises(ValueError):
        limits(payload)


def test_rejection_must_be_the_last_model_row() -> None:
    payload = snapshot()
    state = cast(dict[str, object], payload["state"])
    artifact_id = str(UUID(int=1))
    cast(list[dict[str, object]], state["model_ledger"]).append({
        "status": "succeeded", "artifact_id": artifact_id, "artifact_sha256": "d" * 64,
    })
    state["artifact_registry"] = {artifact_id: "d" * 64}
    with pytest.raises(ValueError):
        limits(payload)
