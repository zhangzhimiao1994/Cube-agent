"""Reject altered public Crew report carriers against unchanged native evidence."""
from __future__ import annotations

import pytest

from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent
from tests.unit.runs.reviewed_report_revision_typed_fixture import object_map
from tests.unit.runs.test_crew_report_publication_red_oct29 import (
    NativeCase,
    native_case,  # noqa: F401
    stream,
    terminal_index,
)
from tests.unit.runtime.crew.test_framework_parser_boundary import (
    installed_parser,  # noqa: F401
    offline_only,  # noqa: F401
)


@pytest.mark.parametrize("carrier", ["artifact", "review_inputs", "step_inputs"])
@pytest.mark.parametrize("field", ["text", "producer"])
async def test_stale_hash_report_carrier_is_rejected_before_publication(
    native_case: NativeCase, carrier: str, field: str,  # noqa: F811
) -> None:
    case = native_case
    original = case.candidate
    altered = original.model_copy(update=(
        {"content": {**original.content, "text": "UNREVIEWED_ALTERED_REPORT"}}
        if field == "text" else {"producer": "foreign_reporter"}
    ))
    assert altered.content_sha256 == original.content_sha256
    assert altered.recompute_content_sha256() != original.content_sha256
    events = list(case.events[:terminal_index(case) + 1])
    for index, event in enumerate(events):
        if carrier == "artifact" and event.artifact == original:
            events[index] = event.model_copy(update={"artifact": altered})
            break
        if (carrier == "review_inputs" and event.kind is EventKind.REVIEW_COMPLETED
                or carrier == "step_inputs" and event.kind is EventKind.STEP_COMPLETED
                and event.step_id == "report") and original in event.inputs:
            events[index] = event.model_copy(update={
                "inputs": tuple(altered if item == original else item for item in event.inputs),
            })
            break
    else:
        pytest.fail("native fixture is missing the selected report carrier")

    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match="crew report publication .*invalid"):
        async for group in _delivery_publication_batches(
            stream(tuple(events)), case.context, runtime=case.runtime,
        ):
            published.extend(group)
    assert not any(event.artifact == altered or altered in event.inputs for event in published)


@pytest.mark.parametrize("defect", ["run", "tenant", "actor", "plan"])
async def test_rehashed_report_input_cannot_replace_current_native_report(
    native_case: NativeCase, defect: str,  # noqa: F811
) -> None:
    case = native_case
    original = case.candidate
    payload = original.to_payload()
    binding = object_map(object_map(payload["content"])["delivery_report_binding"])
    if defect in {"run", "tenant"}:
        binding[defect + "_id"] = "00000000-0000-4000-8000-000000000099"
    elif defect == "actor":
        binding["actor"] = payload["producer"] = "foreign_reporter"
    else:
        binding["plan_digest"] = "f" * 64
    payload["content_sha256"] = ""
    altered = Artifact.from_payload(payload)
    assert altered.recompute_content_sha256() == altered.content_sha256
    assert altered.content_sha256 != original.content_sha256
    events = list(case.events[:terminal_index(case) + 1])
    index = next(i for i, event in enumerate(events)
                 if event.kind is EventKind.REVIEW_COMPLETED and original in event.inputs)
    events[index] = events[index].model_copy(update={
        "inputs": tuple(altered if item == original else item for item in events[index].inputs),
    })

    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match="crew report publication .*invalid"):
        async for group in _delivery_publication_batches(
            stream(tuple(events)), case.context, runtime=case.runtime,
        ):
            published.extend(group)
    assert not any(altered in event.inputs for event in published)
