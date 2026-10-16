"""Read-tool wire contracts must match the actual executor, without query/path coercion."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.capabilities.tools.registry import create_builtin_tool_registry
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall, ToolDefinition
from agent_hub.runtime.contracts import EventKind, JsonValue, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_partial_checkpoint,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    FINAL,
    TOOLS,
    Observations,
    observe,
    write,
)


def validator(schema: Mapping[str, JsonValue]) -> Draft202012Validator:
    return Draft202012Validator(adapter._mutable_json(schema))


@pytest.mark.parametrize("name", ("workspace.read", "workspace_read"))
def test_workspace_read_wire_contract_requires_path_not_query(name: str) -> None:
    definition = adapter._tool_definition("workspace_read", name, {})
    check = validator(definition.parameters)
    assert check.is_valid({"path": "src/fixed.ts"})
    assert not check.is_valid({"query": "Inspect the implementation"})
    assert not check.is_valid({})
    assert not check.is_valid({"path": 123})
    assert not check.is_valid({"path": ""})
    assert not check.is_valid({"path": "src/fixed.ts", "query": "not a search tool"})
    assert "required" in definition.description.lower() and "path" in definition.description.lower()
    assert "conversation context" not in definition.description


def test_registry_runtime_manifest_and_alias_use_identical_contract(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path)
    manifest = gateway.capability_manifest(UUID(int=2))
    items = cast(tuple[Mapping[str, JsonValue], ...], manifest["capabilities"])
    runtime = next(item for item in items if item["id"] == "workspace.read")
    registry = create_builtin_tool_registry().manifests()
    registered = next(item for item in cast(tuple[Mapping[str, JsonValue], ...], registry["capabilities"])
                      if item["id"] == "workspace.read")
    assert "input_schema" in runtime and "input_schema" in registered
    assert runtime["input_schema"] == registered["input_schema"] == adapter._tool_parameters("workspace.read")
    assert runtime["description"] == registered["description"] == adapter._tool_description("workspace.read")
    assert registered["aliases"] == ("workspace_read",)
    assert adapter._mutable_json(adapter._tool_parameters("workspace_read", registered)) == adapter._mutable_json(
        runtime["input_schema"],
    )


@pytest.mark.parametrize("arguments", ({}, {"query": "Find runtime artifacts"}, {"text": "Find context"},
                                      {"path": "src/fixed.ts"}, {"query": None}))
def test_read_context_keeps_query_text_and_path_modes(arguments: dict[str, JsonValue]) -> None:
    schema = adapter._tool_parameters("read_context")
    assert "properties" in schema
    assert validator(schema).is_valid(arguments)
    assert adapter._mutable_json(schema) == adapter._mutable_json(adapter._tool_parameters("read_context", {
        "input_schema": RuntimeCapabilityGateway._builtin_manifest_item(
            RuntimeCapabilityGateway(skill_store_dir=Path("unused")), "read_context",
        )["input_schema"],
    }))
    assert "query" in adapter._tool_description("read_context")


def test_read_context_schema_matches_executor_precedence_and_blank_rejection() -> None:
    check = validator(adapter._tool_parameters("read_context"))
    for arguments in ({"path": "src/fixed.ts", "query": ""}, {"query": "valid", "text": ""},
                      {"query": None, "text": "valid"}):
        assert check.is_valid(arguments)
    for invalid_arguments in ({"query": ""}, {"query": "   "}, {"text": "\t"},
                              {"query": 1}, {"path": None, "query": "valid"}):
        assert not check.is_valid(invalid_arguments)


@pytest.mark.parametrize("kind", ("plugin", "builtin"))
def test_legacy_read_definition_preserves_custom_metadata(kind: str) -> None:
    metadata: Mapping[str, JsonValue] = {
        "kind": kind, "adapter": "runtime_builtin", "description": "Existing custom read contract",
        "input_schema": {"type": "object", "properties": {"custom": {"type": "string"}}},
        "failure_codes": ("workspace.read_denied", "workspace.read_invalid"),
    }
    definitions = adapter._tool_definitions(("workspace.read",), {"workspace.read": metadata}, builtin_contract=False)
    expected = adapter._tool_definition("workspace_read", "workspace.read", metadata)
    assert definitions == (expected,)


def test_legacy_unmarked_explicit_metadata_equal_to_new_defaults_is_not_stripped() -> None:
    metadata: Mapping[str, JsonValue] = {
        "kind": "builtin", "adapter": "runtime_builtin",
        "description": adapter._tool_description("workspace.read"),
        "input_schema": adapter._tool_parameters("workspace.read"),
        "failure_codes": ("workspace.read_denied",),
    }
    assert adapter._tool_definitions(("workspace.read",), {"workspace.read": metadata}, builtin_contract=False) == (
        adapter._tool_definition("workspace_read", "workspace.read", metadata),
    )


def test_new_builtin_metadata_marks_only_new_contract_fields(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path)
    for name in ("workspace.read", "read_context"):
        item = gateway._builtin_manifest_item(name)
        assert item["input_contract_version"] == 1
        assert item["input_contract_added_fields"] == ("input_schema", "description")
        assert adapter._tool_definitions((name,), {name: item}, builtin_contract=False)[0].parameters == {
            "type": "object", "additionalProperties": True,
        }


async def test_actual_query_only_reader_still_rejects_instead_of_guessing_path() -> None:
    gateway = object.__new__(RuntimeCapabilityGateway)
    with pytest.raises(RuntimeCapabilityError, match="workspace reader requires path"):
        await gateway._execute_workspace_read(UUID(int=2), UUID(int=1), {"query": "src/fixed.ts"})


class MissingPathHarness(Observations):
    def __init__(self, backend: str) -> None:
        super().__init__()
        self.backend = backend

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name in {"workspace.read", "workspace_read"} and "path" not in request.arguments:
            self.calls.append(request)
            try:
                await RuntimeCapabilityGateway._execute_workspace_read(
                    object.__new__(RuntimeCapabilityGateway), tenant_id, request.run_id, request.arguments,
                )
            except RuntimeCapabilityError as error:
                if self.backend == "exception":
                    raise
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status="failed", payload={}, failure_reason=str(error))
            raise AssertionError("query must never be converted into a path")
        return await super().invoke(tenant_id, request, user_id=user_id, role=role)


def wrong_read(name: str = "workspace.read", *, with_list: bool = False) -> ModelResponse:
    read = ToolCall(id="wrong-read", name=name, arguments={"query": "private RAM query"})
    calls = (ToolCall(id="list-prefix", name="workspace.list", arguments={}), read) if with_list else (read,)
    return ModelResponse(text=None, tool_calls=calls, usage=TokenUsage(1, 1, 2))


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
async def test_legacy_failed_missing_path_corrects_cached_response_without_replaying_success(backend: str) -> None:
    gateway = WorkspaceDeliverySequenceGateway((wrong_read(with_list=True),))
    harness = MissingPathHarness(backend)
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository

    repository = InMemoryArtifactRepository()
    context = _workspace_delivery_sequence_context()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    stream = runtime.run(context)
    assert isinstance(stream, adapter.CrewRunStream)
    stream._state.builtin_tool_input_contract = False
    # Generate the pre-fix failed boundary with the actual executor rejection.
    original = adapter._is_correctable_tool_argument_failure
    with (patch.object(adapter, "_is_correctable_tool_argument_failure", side_effect=lambda name, reason:
                       False if reason == "workspace reader requires path" else original(name, reason)),
          pytest.raises(adapter.RuntimeExecutionError, match="capability execution failed")):
        async for _event in stream:
            pass
    saved = await runtime.save_checkpoint()
    state = dict(saved.state)
    state.pop("builtin_tool_input_contract", None)
    checkpoint = RuntimeCheckpoint(id=saved.id, runtime_type=saved.runtime_type, runtime_version=saved.runtime_version,
        run_id=saved.run_id, tenant_id=saved.tenant_id, mode=saved.mode, state=state)
    before = checkpoint.to_payload()
    tail_gateway = WorkspaceDeliverySequenceGateway((write(), observe("workspace.read"),
                                                     _workspace_delivery_bundle_response(), FINAL))
    restored = _workspace_delivery_sequence_runtime(tail_gateway, harness, artifact_repository=repository, tools=TOOLS)
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 1 and len(tail_gateway.requests) == 4
    assert sum(call.tool_name == "workspace.list" for call in harness.calls) == 1
    failures = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failures) == 1 and failures[0].payload["status"] == "rejected"
    feedback = "\n".join(str(message.content) for message in tail_gateway.requests[0].messages)
    assert "workspace_read_path_required" in feedback
    assert "CAPABILITY_ARGUMENT_CORRECTION" in feedback
    assert checkpoint.to_payload() == before
    retained = await restored.save_checkpoint()
    assert retained.state["usage"] == {"tokens": 10, "cost_usd": "0"}
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"]).items():
        assert cast(Mapping[str, Mapping[str, JsonValue]], retained.state["models"])[key] == row
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"]).items():
        if row["status"] == "succeeded":
            assert cast(Mapping[str, Mapping[str, JsonValue]], retained.state["tools"])[key] == row


async def test_identical_missing_path_is_bounded_and_not_reexecuted() -> None:
    harness = MissingPathHarness("exception")
    gateway = WorkspaceDeliverySequenceGateway((wrong_read(), wrong_read()))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    with pytest.raises(adapter.RuntimeExecutionError, match="repeated rejected request"):
        async for _event in runtime.run(_workspace_delivery_sequence_context()):
            pass
    assert len(gateway.requests) == 2 and len(harness.calls) == 1


async def test_actual_crew_request_exposes_contract_before_any_workspace_tool() -> None:
    class Gateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            read = next(tool for tool in request.tools if tool.name == "workspace_read")
            assert not validator(read.parameters).is_valid({"query": "Find context"})
            assert validator(read.parameters).is_valid({"path": "src/business.ts"})
            return await super().complete_with_context(request)

    gateway = Gateway((write(), observe("workspace.read"), _workspace_delivery_bundle_response(), FINAL))
    harness = Observations()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=TOOLS)
    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.read", "workspace.bundle",
    ]


async def test_legacy_partial_checkpoint_rebuilds_original_read_request_hashes() -> None:
    original = adapter._tool_definitions

    def legacy_definitions(names: tuple[str, ...], metadata: object = None, **kwargs: object) -> tuple[ToolDefinition, ...]:
        definitions = original(names, cast(Mapping[str, Mapping[str, JsonValue]] | None, metadata))
        return tuple(ToolDefinition(name=item.name, description="Read approved workspace or conversation context.",
                                    parameters={"type": "object", "additionalProperties": True})
                     if item.name == "workspace_read" else item for item in definitions)

    with patch.object(adapter, "_tool_definitions", side_effect=legacy_definitions):
        saved, context, repository, harness, first = await _workspace_delivery_partial_checkpoint(
            responses=(write(), observe("workspace.read")), expected_tools=2, harness=Observations(), tools=TOOLS,
        )
    state = dict(saved.state)
    state.pop("builtin_tool_input_contract", None)
    checkpoint = RuntimeCheckpoint(id=saved.id, runtime_type=saved.runtime_type, runtime_version=saved.runtime_version,
        run_id=saved.run_id, tenant_id=saved.tenant_id, mode=saved.mode, state=state)
    before = checkpoint.to_payload()
    gateway = WorkspaceDeliverySequenceGateway((_workspace_delivery_bundle_response(), FINAL))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first.requests) == len(gateway.requests) == 2
    assert len(harness.calls) == 3
    assert checkpoint.to_payload() == before
    retained = await runtime.save_checkpoint()
    for key, row in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"]).items():
        assert cast(Mapping[str, Mapping[str, JsonValue]], retained.state["models"])[key] == row
