"""Safe gateway-call scope evidence, never a model completion."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import cast
from uuid import UUID, uuid4

from agent_hub.models.failure_receipt import (
    GatewayFailureReceipt,
    get_gateway_failure_receipt,
)
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayScopeDiagnostic,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _gateway_scope_observer,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import Artifact, EventKind, GatewayProvenance, JsonValue, RunEvent

_SCOPE_KEYS = frozenset({
    "schema_version", "source", "run_id", "tenant_id", "requested_logical_model",
    "history_complete", "call_count", "calls",
    "scope_id", "part_index", "part_count", "call_offset",
})
_RECEIVED_KEYS = frozenset({
    "schema_version", "source", "call_id", "requested_logical_model", "allow_fallback",
    "attempted_logical_models", "provenance",
})


def _canonical_uuid(value: object) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _same_model_history(value: object, model: str) -> bool:
    return isinstance(value, list) and bool(value) and all(item == model for item in value)


def _validate_scope_part(
    value: object, run_id: str,
) -> tuple[dict[str, object], GatewayProvenance]:
    if type(value) is not dict or set(value) != _SCOPE_KEYS:
        raise ValueError
    content = cast(dict[str, object], value)
    model = content["requested_logical_model"]
    if (
        type(content["schema_version"]) is not int or content["schema_version"] != 1
        or content["source"] != "direct_runtime" or not _canonical_uuid(run_id)
        or content["run_id"] != run_id or not _canonical_uuid(content["tenant_id"])
        or not isinstance(model, str)
        or content["history_complete"] is not True
        or type(content["call_count"]) is not int or content["call_count"] < 1
        or not isinstance(content["calls"], list)
        or not content["calls"] or content["call_count"] < len(content["calls"])
        or not _canonical_uuid(content["scope_id"])
        or type(content["part_index"]) is not int or type(content["part_count"]) is not int
        or not 1 <= content["part_index"] <= content["part_count"] <= content["call_count"]
        or type(content["call_offset"]) is not int or content["call_offset"] < 0
        or content["call_offset"] + len(content["calls"]) > content["call_count"]
    ):
        raise ValueError
    seen: set[str] = set()
    final_provenance: GatewayProvenance | None = None
    for call in content["calls"]:
        if not isinstance(call, dict) or set(call) != {"outcome", "receipt"}:
            raise ValueError
        receipt = call["receipt"]
        if call["outcome"] == "failed":
            receipt = GatewayFailureReceipt.from_payload(receipt).to_payload()
            if receipt["history_complete"] is not True:
                raise ValueError
            attempts = receipt["attempts"]
            if not isinstance(attempts, list) or not attempts:
                raise ValueError
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    raise TypeError
                provenance = GatewayProvenance.from_payload(attempt["provenance"])
                if provenance.logical_model != model:
                    raise ValueError
                final_provenance = provenance
        elif call["outcome"] == "received":
            if (
                not isinstance(receipt, dict) or set(receipt) != _RECEIVED_KEYS
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["source"] != "model_gateway"
                or type(receipt["allow_fallback"]) is not bool
            ):
                raise ValueError
            final_provenance = GatewayProvenance.from_payload(receipt["provenance"])
            if final_provenance.logical_model != model:
                raise ValueError
        else:
            raise ValueError
        if (
            receipt["requested_logical_model"] != model
            or not _same_model_history(receipt["attempted_logical_models"], model)
            or not _canonical_uuid(receipt["call_id"])
            or receipt["call_id"] in seen
        ):
            raise ValueError
        seen.add(cast(str, receipt["call_id"]))
    if final_provenance is None:
        raise ValueError
    return content, final_provenance


def _validate_scope_content(artifact: Artifact, run_id: str) -> dict[str, object]:
    if (
        artifact.type != "model_attempt" or artifact.producer != "main_agent"
        or artifact.source_ids or artifact.provenance is None
    ):
        raise ValueError
    content, final_provenance = _validate_scope_part(artifact.to_payload()["content"], run_id)
    if final_provenance != artifact.provenance:
        raise ValueError
    return content


def validate_model_scope_artifact(artifact: Artifact, run_id: str) -> dict[str, object]:
    try:
        return _validate_scope_content(artifact, run_id)
    except Exception:  # noqa: BLE001 - never expose untrusted model evidence.
        raise ValueError("model attempt scope evidence is invalid or incomplete") from None


def validate_model_scope_parts(parts: Sequence[Mapping[str, object]]) -> None:
    """Close JSON scope parts; callers must independently verify envelopes and event links."""
    try:
        if type(parts) not in (list, tuple):
            raise ValueError
        groups: dict[str, list[dict[str, object]]] = {}
        for part in parts:
            if type(part) is not dict or type(part.get("run_id")) is not str:
                raise ValueError
            content, _ = _validate_scope_part(part, cast(str, part["run_id"]))
            groups.setdefault(cast(str, content["scope_id"]), []).append(content)
        call_ids: set[str] = set()
        for group in groups.values():
            group.sort(key=lambda part: cast(int, part["part_index"]))
            first = group[0]
            if len(group) != first["part_count"]:
                raise ValueError
            offset = 0
            for index, content in enumerate(group, 1):
                if (
                    content["part_index"] != index or content["call_offset"] != offset
                    or any(content[key] != first[key] for key in (
                        "run_id", "tenant_id", "requested_logical_model", "call_count", "part_count",
                    ))
                ):
                    raise ValueError
                calls = cast(list[Mapping[str, object]], content["calls"])
                for call in calls:
                    receipt = cast(Mapping[str, object], call["receipt"])
                    call_id = cast(str, receipt["call_id"])
                    if call_id in call_ids:
                        raise ValueError
                    call_ids.add(call_id)
                offset += len(calls)
            if offset != first["call_count"]:
                raise ValueError
    except BaseException:  # noqa: BLE001 - validation must not echo hostile evidence.
        raise ValueError("model attempt scope parts are invalid or incomplete") from None


class ModelScopeTracker:
    def __init__(self, *, run_id: UUID, tenant_id: UUID, logical_model: str) -> None:
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._logical_model = logical_model
        self._scope_id = uuid4()
        self._calls: list[dict[str, JsonValue] | None] = []
        self._count = 0
        self._bytes = 0
        self._incomplete = False
        self._recorded_count = 0
        self._observer: Callable[[GatewayScopeDiagnostic], None] | None = None
        self._first_incomplete: tuple[
            int | None, ScopeIncompletePhase, ScopeIncompleteReason, GatewayScopeDiagnostic | None,
        ] | None = None

    @property
    def call_count(self) -> int:
        return self._count

    @property
    def incomplete(self) -> bool:
        return self._incomplete or any(call is None for call in self._calls)

    def _mark_incomplete(
        self, index: int | None, phase: ScopeIncompletePhase, reason: ScopeIncompleteReason,
        diagnostic: GatewayScopeDiagnostic | None = None,
    ) -> None:
        if self._first_incomplete is None:
            # A previous unrecorded call cannot be hidden by a later identified failure.
            for pending, call in enumerate(self._calls):
                if index is not None and pending >= index:
                    break
                if call is None:
                    index, phase, reason, diagnostic = (
                        pending, ScopeIncompletePhase.SCOPE_TRACKER,
                        ScopeIncompleteReason.UNRECORDED_CALL, None,
                    )
                    break
            self._first_incomplete = (index, phase, reason, diagnostic)
        self._incomplete = True

    @property
    def diagnostic_payload(self) -> dict[str, JsonValue]:
        if not self.incomplete:
            return {}
        if self._first_incomplete is None:
            self._mark_incomplete(None, ScopeIncompletePhase.SCOPE_TRACKER,
                                  ScopeIncompleteReason.UNRECORDED_CALL)
        assert self._first_incomplete is not None
        index, phase, reason, diagnostic = self._first_incomplete
        payload: dict[str, JsonValue] = {
            "first_incomplete_phase": phase.value, "first_incomplete_reason": reason.value,
            "recorded_call_count": self._recorded_count,
        }
        if index is not None:
            payload["first_incomplete_call"] = index + 1
        if diagnostic is not None:
            payload["transport_entered_count"] = diagnostic.transport_entered_count
            payload["failure_attempt_count"] = diagnostic.failure_attempt_count
        return payload

    def begin(self) -> int:
        self._count += 1
        if self._incomplete:
            return -1
        self._calls.append(None)
        index = len(self._calls) - 1

        def observe(diagnostic: GatewayScopeDiagnostic) -> None:
            self._mark_incomplete(index, diagnostic.phase, diagnostic.reason, diagnostic)

        self._observer = observe
        _gateway_scope_observer.set(observe)
        return index

    def _finish_observation(self) -> None:
        if _gateway_scope_observer.get() is self._observer:
            _gateway_scope_observer.set(None)
        self._observer = None

    def _record(self, index: int, call: dict[str, JsonValue]) -> None:
        if index < 0 or self._incomplete:
            return
        try:
            size = len(json.dumps(call, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if index >= len(self._calls) or self._calls[index] is not None:
                raise ValueError
        except Exception:  # noqa: BLE001 - recording failure cannot replace model execution.
            self._mark_incomplete(index, ScopeIncompletePhase.SCOPE_TRACKER,
                                  ScopeIncompleteReason.EVIDENCE_INVALID)
            return
        # This bounds telemetry memory, not model/tool execution or project size.
        if self._bytes + size > 2_000_000:
            self._mark_incomplete(index, ScopeIncompletePhase.SCOPE_TRACKER,
                                  ScopeIncompleteReason.EVIDENCE_LIMIT)
            self._calls.clear()
            return
        self._bytes += size
        self._calls[index] = call
        self._recorded_count += 1

    def received(self, index: int, request: ModelRequest, completion: GatewayCompletion) -> None:
        self._finish_observation()
        if self._incomplete:
            return
        diagnostic = get_gateway_scope_diagnostic(completion)
        if diagnostic is not None:
            self._mark_incomplete(index, diagnostic.phase, diagnostic.reason, diagnostic)
            return
        try:
            provenance = GatewayProvenance(
                logical_model=completion.logical_model, deployment_id=completion.deployment_id,
                provider_id=completion.provider_id, provider_model=completion.provider_model,
            )
            self._record(index, {
                "outcome": "received", "receipt": {
                    "schema_version": 1, "source": "model_gateway", "call_id": str(uuid4()),
                    "requested_logical_model": request.logical_model,
                    "allow_fallback": request.allow_fallback,
                    "attempted_logical_models": tuple(completion.attempted_logical_models),
                    "provenance": cast(dict[str, JsonValue], provenance.to_payload()),
                },
            })
        except Exception:  # noqa: BLE001 - metadata recording must not expose supplier data.
            self._mark_incomplete(index, ScopeIncompletePhase.SCOPE_TRACKER,
                                  ScopeIncompleteReason.EVIDENCE_INVALID)

    def failed(self, index: int, error: Exception) -> None:
        self._finish_observation()
        diagnostic = get_gateway_scope_diagnostic(error)
        if diagnostic is not None:
            self._mark_incomplete(index, diagnostic.phase, diagnostic.reason, diagnostic)
            return
        receipt = get_gateway_failure_receipt(error)
        if receipt is not None:
            if not receipt.history_complete:
                self._mark_incomplete(index, ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                      ScopeIncompleteReason.UNKNOWN_FAILURE)
            else:
                self._record(index, {"outcome": "failed", "receipt": receipt.to_payload()})
        else:
            self._mark_incomplete(index, ScopeIncompletePhase.UNKNOWN_ADAPTER,
                                  ScopeIncompleteReason.UNKNOWN_FAILURE)

    def artifacts(self, *, include_received_only: bool = False) -> tuple[Artifact, ...]:
        if not self._calls or self.incomplete:
            return ()
        calls = cast(list[dict[str, JsonValue]], self._calls)
        if (not include_received_only and len(calls) == 1
                and not any(call["outcome"] == "failed" for call in calls)):
            return ()
        try:
            # Let the shared contract determine each part's structural/byte capacity.
            parts: list[list[dict[str, JsonValue]]] = []
            current: list[dict[str, JsonValue]] = []
            for call in calls:
                # Small wire frames avoid repeatedly revalidating a growing whole-run ledger.
                if len(current) == 16:
                    parts.append(current)
                    current = []
                candidate = [*current, call]
                try:
                    self._artifact(candidate, part_index=1, part_count=1, offset=0)
                except (TypeError, ValueError):
                    if not current:
                        raise
                    parts.append(current)
                    current = [call]
                else:
                    current = candidate
            if current:
                parts.append(current)
            artifacts = []
            offset = 0
            for index, part in enumerate(parts, 1):
                artifacts.append(self._artifact(part, part_index=index,
                                                part_count=len(parts), offset=offset))
                offset += len(part)
            return tuple(artifacts)
        except Exception:  # noqa: BLE001 - incomplete evidence must not change execution.
            self._mark_incomplete(None, ScopeIncompletePhase.SCOPE_TRACKER,
                                  ScopeIncompleteReason.EVIDENCE_INVALID)
            return ()

    def _artifact(
        self, calls: list[dict[str, JsonValue]], *, part_index: int, part_count: int, offset: int,
    ) -> Artifact:
        last = cast(Mapping[str, JsonValue], calls[-1]["receipt"])
        if calls[-1]["outcome"] == "failed":
            attempts = cast(list[Mapping[str, JsonValue]], last["attempts"])
            provenance = GatewayProvenance.from_payload(attempts[-1]["provenance"])
        else:
            provenance = GatewayProvenance.from_payload(last["provenance"])
        artifact = Artifact(
            id=uuid4(), type="model_attempt", producer="main_agent", provenance=provenance,
            content={
                "schema_version": 1, "source": "direct_runtime", "run_id": str(self._run_id),
                "tenant_id": str(self._tenant_id), "requested_logical_model": self._logical_model,
                "history_complete": True, "call_count": self._count, "calls": tuple(calls),
                "scope_id": str(self._scope_id), "part_index": part_index,
                "part_count": part_count, "call_offset": offset,
            },
        )
        # Include wire-envelope overhead when choosing a part's capacity.
        Artifact.from_payload(artifact.to_payload())
        RunEvent.from_payload(RunEvent(
            kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=self._run_id,
            actor="main_agent", artifact=artifact, payload={
                "artifact_id": str(artifact.id), "logical_model": self._logical_model,
                "requested_logical_model": self._logical_model,
                "attempted_logical_models": (self._logical_model,),
                "deployment": provenance.deployment_id, "provider": provenance.provider_id,
                "upstream_model": provenance.provider_model,
            },
        ).to_payload())
        return artifact
