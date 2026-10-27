"""Consume only this Hybrid execution's bound native Dispatch delivery outputs."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.crew.delivery_report import final_evidence

POLICY = "engineering_hybrid_report_version"
INPUT = "hybrid_delivery_report_input"


def selected(context: TaskContext) -> bool:
    version = context.routing_decision.get(POLICY)
    return type(version) is int and version == 1


def delivery(context: TaskContext, artifacts: Sequence[Artifact], *,
             output_ids: set[UUID] | None = None) -> tuple[dict[str, JsonValue], tuple[Artifact, ...]]:
    by_id = {str(item.id): item for item in artifacts}
    if len(by_id) != len(artifacts):
        raise ValueError("hybrid final delivery inputs are ambiguous")
    for report in reversed(artifacts):
        metadata = report.content.get("delivery_report_binding")
        if (not isinstance(metadata, Mapping) or metadata.get("runtime") != "crew"
                or output_ids is not None and report.id not in output_ids):
            continue
        current = tuple(item for item in artifacts if output_ids is None or item.id in output_ids)
        observed = final_evidence(report, context, current)
        result, verified = observed["bundle"], observed["verification"]
        if not isinstance(result, Mapping) or not isinstance(verified, Mapping):
            raise TypeError("hybrid final delivery verification is unavailable")
        receipt = verified.get("verification")
        if not isinstance(receipt, Mapping):
            raise TypeError("hybrid final delivery verification is unavailable")
        required_ids = {str(report.id)}
        for name in ("bundle", "verification", "model"):
            reference = metadata.get(name + "_artifact_id")
            if type(reference) is not str or reference not in by_id:
                raise ValueError("hybrid final delivery reference is unavailable")
            required_ids.add(reference)
            if name != "model":
                required_ids.update(by_id[reference].source_ids)
        required = (report, *(item for item in current if item.id != report.id and str(item.id) in required_ids))
        if len(required) != len(required_ids):
            raise ValueError("hybrid final delivery model lineage is unavailable")
        return {**result, "verification": receipt,
                "upstream_report_artifact_id": str(report.id),
                "upstream_report_sha256": report.content_sha256}, required
    raise ValueError("hybrid final delivery report is missing")


def framed(context: TaskContext) -> tuple[dict[str, JsonValue], tuple[Artifact, ...]]:
    frame = context.routing_decision.get(INPUT)
    if not isinstance(frame, Mapping) or not frame:
        raise ValueError("hybrid final delivery frame is missing")
    chosen = tuple(item for item in context.artifacts if str(item.id) in frame)
    if (len(chosen) != len(frame)
            or any(item.content_sha256 != frame[str(item.id)] for item in chosen)):
        raise ValueError("hybrid final delivery frame changed")
    return delivery(context, chosen, output_ids={item.id for item in chosen})
