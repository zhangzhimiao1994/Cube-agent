"""Post-tool recovery accepts only causally proven semantic tool reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.models.types import ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, GatewayProvenance, JsonValue
from agent_hub.runtime.crew import adapter as a
from agent_hub.runtime.crew.plan import DispatchStep
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_incremental_read_rejection import offline_only  # noqa: F401
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_post_tool_incomplete_finalization import PostToolGateway
from tests.unit.runtime.crew.test_tool_incomplete_recovery import draft_models, subject, tool_plan


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


@dataclass
class History:
    step: DispatchStep
    models: a._ModelLedger
    tools: a._ToolLedger
    sources: tuple[str, ...]
    chain: list[JsonValue]

    def verify(self, *, closed: bool = False) -> str | None:
        return object.__new__(a.CrewDispatchRuntime)._post_tool_history_sha256(
            self.step, self.step.agent, 0, 2, self.models.states, self.models.artifacts,
            self.tools.states, self.tools.artifacts, self.sources, closed=closed,
        )


def history(*, reuse: bool = True, tool: str = "workspace.read") -> History:
    step = tool_plan().steps[0].model_copy(update={"tools": (tool,)})
    models, tools = a._ModelLedger(), a._ToolLedger()
    evidence: list[str] = []
    chain: list[JsonValue] = []
    provenance = GatewayProvenance(logical_model="selected", deployment_id="synthetic",
        provider_id="openai", provider_model="openai/synthetic")
    for index in range(2):
        arguments = {"path": "synthetic" if reuse else f"synthetic-{index}"}
        artifact = Artifact(id=uuid4(), type="model_response", producer=step.agent,
            source_ids=a._lineage_window_ids(tuple(evidence), anchor_count=0), provenance=provenance,
            content={"text": None, "tool_calls": cast(JsonValue, [{"id": f"call-{index}", "name": tool,
                      "arguments": arguments}]), "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}, "cost_usd": "0"})
        state: dict[str, JsonValue] = {"step_id": step.id, "actor": step.agent, "attempt": 0,
            "purpose": "step", "call_index": index, "status": "succeeded", "artifact_id": str(artifact.id),
            "sha256": artifact.content_sha256}
        key = f"model-{index}"
        models.states[key], models.artifacts[key] = state, artifact
        evidence.append(str(artifact.id))
        chain.append({"key": key, "state": state, "sha256": artifact.content_sha256})
        if index == 1 and reuse:
            evidence.append(str(tools.artifacts["tool-0"].id))
            continue
        result = Artifact(id=uuid4(), type="tool_result", producer=step.agent,
            source_ids=(str(artifact.id),), content={"tool_name": tool, "arguments_sha256": digest(arguments),
                                                  "result": {"text": "synthetic"}})
        tool_state: dict[str, JsonValue] = {"step_id": step.id, "attempt": 0, "round": index,
            "tool_index": 0, "status": "succeeded", "name": tool, "arguments_sha256": digest(arguments),
            "trigger_model_artifact_id": str(artifact.id), "artifact_id": str(result.id), "sha256": result.content_sha256}
        tool_key = f"tool-{index}"
        tools.states[tool_key], tools.artifacts[tool_key] = tool_state, result
        evidence.append(str(result.id))
        chain.append({"key": tool_key, "state": tool_state, "sha256": result.content_sha256})
    return History(step, models, tools, a._lineage_window_ids(tuple(evidence), anchor_count=0), chain)


def test_real_history_accepts_causal_reuse_and_does_not_mutate_ledgers() -> None:
    case = history()
    before = (dict(case.models.states), dict(case.tools.states), case.sources)
    result = case.verify()
    assert result is not None
    assert result == case.verify(closed=True)
    assert (case.models.states, case.tools.states, case.sources) == before


def test_dense_legacy_history_hash_is_byte_identical() -> None:
    case = history(reuse=False)
    assert case.verify() == digest(case.chain)


@pytest.mark.asyncio
async def test_actual_repeated_tool_round_then_incomplete_finalizes_without_replay() -> None:
    gateway = PostToolGateway("tool", "tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = subject(gateway, tool_plan(), repository, harness)
    async with asyncio.timeout(5):
        events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5 and len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    assert len(cast(Mapping[str, JsonValue], checkpoint.state["tools"])) == 1
    assert [(row["call_index"], row["status"]) for row in draft_models(checkpoint)] == [
        (0, "succeeded"), (1, "succeeded"), (2, "rejected"), (3, "succeeded"),
    ]
    frozen = checkpoint.to_payload()
    cached_gateway = PostToolGateway()
    cached_harness = RecordingHarnessToolGateway()
    cached = subject(cached_gateway, tool_plan(), repository, cached_harness)
    await cached.restore_checkpoint(checkpoint)
    await collect(cached, checkpoint=checkpoint)
    assert not cached_gateway.requests and not cached_harness.calls
    assert checkpoint.to_payload() == frozen
    assert (await cached.save_checkpoint()).state["usage"] == checkpoint.state["usage"]


@pytest.mark.parametrize("status", ["running", "uncertain", "rejected", "waiting_approval"])
def test_unsettled_or_non_successful_receipt_cannot_prove_reuse(status: str) -> None:
    case = history()
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "status": status}
    assert case.verify() is None


@pytest.mark.parametrize("field,value", [("step_id", "foreign"), ("attempt", 1), ("round", 1),
                                        ("arguments_sha256", "0" * 64), ("sha256", "0" * 64)])
def test_wrong_receipt_scope_arguments_or_hash_is_rejected(field: str, value: JsonValue) -> None:
    case = history()
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], field: value}
    assert case.verify() is None


def test_duplicate_coordinates_are_not_hidden_by_dictionary_overwrite() -> None:
    case = history(reuse=False)
    case.tools.states["duplicate"] = dict(case.tools.states["tool-0"])
    case.tools.artifacts["duplicate"] = case.tools.artifacts["tool-0"]
    assert case.verify() is None


def test_extra_unconsumed_receipt_is_rejected() -> None:
    case = history()
    case.tools.states["extra"] = {**case.tools.states["tool-0"], "round": 3}
    case.tools.artifacts["extra"] = case.tools.artifacts["tool-0"]
    assert case.verify() is None


@pytest.mark.parametrize("field", ["producer", "source_ids"])
def test_foreign_artifact_or_source_lineage_is_rejected(field: str) -> None:
    case = history()
    old = case.tools.artifacts["tool-0"]
    fields = old.to_payload()
    fields[field] = "foreign" if field == "producer" else [str(uuid4())]
    fields["content_sha256"] = ""
    changed = Artifact.from_payload(fields)
    case.tools.artifacts["tool-0"] = changed
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "sha256": changed.content_sha256}
    assert case.verify() is None


def test_changed_final_sources_cannot_claim_valid_reuse() -> None:
    case = history()
    case.sources = tuple(reversed(case.sources))
    assert case.verify() is None


def test_future_receipt_cannot_fill_an_unproven_earlier_slot() -> None:
    case = history()
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "round": 1}
    assert case.verify() is None


@pytest.mark.parametrize("tool", ["workspace.list", "project.generate_zip"])
def test_non_native_semantic_names_are_not_unconditionally_excluded(tool: str) -> None:
    assert history(tool=tool).verify() is not None


def test_future_matching_receipt_first_cannot_shadow_consumed_past() -> None:
    case = history(reuse=False)
    previous = case.models.artifacts["model-1"]
    fields = previous.to_payload()
    content = cast(dict[str, JsonValue], fields["content"])
    arguments: dict[str, JsonValue] = {"path": "synthetic-0"}
    content["tool_calls"] = cast(JsonValue, [{"id": f"repeat-{i}", "name": "workspace.read", "arguments": arguments} for i in range(2)])
    fields["content_sha256"] = ""
    changed = Artifact.from_payload(fields)
    case.models.artifacts["model-1"] = changed
    case.models.states["model-1"] = {**case.models.states["model-1"], "sha256": changed.content_sha256}
    fields = case.tools.artifacts["tool-1"].to_payload()
    cast(dict[str, JsonValue], fields["content"])["arguments_sha256"] = digest(arguments)
    fields["content_sha256"] = ""
    changed = Artifact.from_payload(fields)
    case.tools.artifacts["tool-1"] = changed
    case.tools.states["tool-1"] = {**case.tools.states["tool-1"], "tool_index": 1,
        "arguments_sha256": digest(arguments), "sha256": changed.content_sha256}
    case.tools.states = {key: case.tools.states[key] for key in ("tool-1", "tool-0")}
    assert case.verify() is not None
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "arguments_sha256": "0" * 64}
    assert case.verify() is None


def test_current_generated_attachment_requires_its_own_slot_not_semantic_gap() -> None:
    case = history(tool="project.generate_zip")
    assert case.verify() is not None
    fields = case.tools.artifacts["tool-0"].to_payload()
    cast(dict[str, JsonValue], fields["content"])["result"] = {"presentation": "final_attachment",
        "artifact_id": "synthetic", "file": {"filename": "synthetic.zip", "mime_type": "application/zip",
                                             "download_url": "/synthetic-download"}}
    fields["content_sha256"] = ""
    changed = Artifact.from_payload(fields)
    case.tools.artifacts["tool-0"] = changed
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "sha256": changed.content_sha256}
    assert case.verify() is None


@pytest.mark.parametrize("name,refresh", [("workspace.read", False), ("workspace.list", False),
                                         ("workspace_read", True), ("workspace.bundle", False)])
def test_shared_graph_resolver_preserves_mutable_and_fresh_bundle_guards(name: str, refresh: bool) -> None:
    case = history(tool=name)
    artifact = case.tools.artifacts["tool-0"]
    call = ToolCall(id="synthetic", name=name, arguments={"path": "synthetic"})
    def resolve(workspace: bool) -> Artifact | None:
        return a._causal_semantic_tool_result(
            case.tools, step_id=case.step.id, call=call, arguments_sha256=digest({"path": "synthetic"}),
            evidence=tuple(case.tools.artifacts.values()), consumed_artifact_ids={str(artifact.id)},
            trigger_model_artifact_id=str(case.models.artifacts["model-1"].id),
            workspace_continuation=workspace, context_refresh=refresh,
        )
    assert resolve(False) is artifact
    assert resolve(True) is None


@pytest.mark.parametrize("index", [-1, 1])
def test_out_of_call_range_slot_is_rejected(index: int) -> None:
    case = history()
    case.tools.states["tool-0"] = {**case.tools.states["tool-0"], "tool_index": index}
    assert case.verify() is None


@pytest.mark.parametrize("field,value", [("actor", "foreign"), ("step_id", "foreign"),
                                        ("attempt", 1), ("purpose", "review"), ("call_index", 3)])
def test_model_prefix_scope_and_order_are_verified_before_reuse(field: str, value: JsonValue) -> None:
    case = history()
    case.models.states["model-0"] = {**case.models.states["model-0"], field: value}
    assert case.verify() is None
