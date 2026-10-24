"""Known rejected output may be corrected once, behind the same durable journal."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from autogen_core.models import UserMessage
from autogen_core.tools import ToolSchema

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayNegotiatedRejectedOutput,
    GatewayRejectedOutput,
)
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    RejectedOutputStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, JsonValue, _mutable_json

PRIVATE = "PRIVATE_REJECTED_OUTPUT_MUST_NOT_REACH_CORRECTION_OR_JOURNAL"
TOOLS = {"workspace_list": "workspace.list"}


@dataclass
class Journal:
    run_id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    snapshots: list[list[dict[str, JsonValue]]] = field(default_factory=list)
    artifacts: dict[UUID, Artifact] = field(default_factory=dict)
    refuse_correction_ack: bool = False

    async def publish(self, artifacts: tuple[Artifact, ...],
                      models: tuple[dict[str, JsonValue], ...],
                      tools: tuple[dict[str, JsonValue], ...]) -> None:
        if self.refuse_correction_ack and models[-1].get("correction_for") == 0:
            raise OSError("simulated correction intent commit failure")
        self.snapshots.append(cast(list[dict[str, JsonValue]], _mutable_json(models)))

    async def store(self, artifact: Artifact) -> UUID:
        self.artifacts[artifact.id] = artifact
        return artifact.id

    async def abort(self, write_id: UUID) -> bool:
        self.artifacts.pop(write_id, None)
        return True

    def finalize(self, write_id: UUID) -> None:
        pass

    def durability(self, *, models: tuple[dict[str, JsonValue], ...] = ()) -> _DiscussionDurability:
        return _DiscussionDurability(self.publish, self.store, self.abort, self.finalize,
            run_id=self.run_id, tenant_id=self.tenant_id, model_entries=models,
            artifacts=tuple(self.artifacts.values()), tool_correction_enabled=True)


class Model:
    def __init__(self, journal: Journal, *, kind: str = "invalid_tool", always_reject: bool = False,
                 forbidden: bool = False, tool_names: dict[str, str] | None = None) -> None:
        self.journal, self.kind = journal, kind
        self.always_reject, self.forbidden = always_reject, forbidden
        self.requests: list[ModelRequest] = []
        self.tool_names = TOOLS if tool_names is None else tool_names

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        assert not self.forbidden, "known result or UNKNOWN may not call the model again"
        self.requests.append(request)
        rows = self.journal.snapshots[-1]
        assert rows[-1]["status"] == "running"
        assert rows[-1]["request_hash"] == _DiscussionDurability.request_hash(request, tool_names=self.tool_names)
        if len(self.requests) == 1 or self.always_reject:
            known = self.kind != "missing"
            status: RejectedOutputStatus = (
                "refused" if self.kind == "refusal" else "incomplete" if self.kind == "incomplete" else "completed"
            )
            error = GatewayNegotiatedRejectedOutput if self.kind == "negotiated" else GatewayRejectedOutput
            raise error(evidence=RejectedOutputEvidence(final_text=PRIVATE if status == "completed" else None,
                usage=TokenUsage(7, 3, 10) if known else None, usage_status="known" if known else "missing",
                status=status, reason="refusal" if self.kind == "refusal" else "incomplete"
                if self.kind == "incomplete" else "invalid_tool"), logical_model=request.logical_model,
                deployment_id="shared", provider_id="test", provider_model="test/model", cost_usd=None)
        return GatewayCompletion(response=ModelResponse(text=None, usage=TokenUsage(2, 1, 3),
            tool_calls=(ToolCall(id="corrected_call", name="workspace_list", arguments={}),)),
            logical_model=request.logical_model, deployment_id="shared", provider_id="test",
            provider_model="test/model", cost_usd=Decimal("0.001"))


def request() -> ModelRequest:
    return ModelRequest(logical_model="shared", messages=(ModelMessage(role="user", content="Build the project"),),
        tools=(ToolDefinition(name="workspace_list", description="List the current workspace",
            parameters={"type": "object", "additionalProperties": False, "properties": {}}),),
        required_tool="workspace_list", max_output_tokens=100, timeout_seconds=10,
        required_capabilities=frozenset({ModelCapability.TOOL_CALLING}))


def client(model: Model, journal: _DiscussionDurability, usage: DiscussionUsage, *, enabled: bool = True,
           tokens: int = 100) -> GatewayChatCompletionClient:
    return GatewayChatCompletionClient(model, "shared", usage, durability=journal,
        supports_tool_calls=True, tool_correction_enabled=enabled,
        engineering_budget=lambda: (max(0, tokens - usage.tokens), 5.0))


async def test_known_completed_tool_rejection_corrects_once_after_both_checkpoints_are_acked() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal)
    durable = journal.durability()
    result, _ = await client(model, durable, usage)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 2 and result.response.tool_calls[0].name == "workspace_list"
    original, corrected = model.requests
    assert corrected.required_tool == original.required_tool and corrected.tools == original.tools
    assert corrected.logical_model == original.logical_model and corrected.messages != original.messages
    assert corrected.max_output_tokens == 90 and corrected.timeout_seconds == 5.0
    assert PRIVATE not in str(corrected.messages) and PRIVATE not in str(journal.snapshots)
    assert any(rows[-1]["status"] == "rejected" for rows in journal.snapshots)
    assert journal.snapshots[-1][0]["status"] == "rejected"
    assert journal.snapshots[-1][1]["status"] == "succeeded"
    assert journal.snapshots[-1][1]["correction_for"] == 0
    assert usage.tokens == 13 and usage.cost_usd == Decimal("0.001")
    assert durable.failure_reason is None and not durable._model_lock.locked()


async def test_large_remaining_run_budget_preserves_the_existing_single_request_cap() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal)
    await client(model, journal.durability(), usage, tokens=1000)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 2
    assert model.requests[1].max_output_tokens == model.requests[0].max_output_tokens == 100
    assert usage.tokens == 13


@pytest.mark.parametrize("kind,enabled,tokens", [
    ("invalid_tool", False, 100), ("incomplete", True, 100), ("refusal", True, 100),
    ("missing", True, 100), ("negotiated", True, 100), ("invalid_tool", True, 10),
])
async def test_ineligible_rejection_legacy_or_exhausted_budget_never_calls_again(
    kind: str, enabled: bool, tokens: int,
) -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal, kind=kind)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="model response rejected|negotiation exhausted"):
        await client(model, durable, usage, enabled=enabled, tokens=tokens)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and journal.snapshots[-1][-1]["status"] == "rejected"
    assert usage.tokens == (0 if kind == "missing" else 10)


async def test_second_rejection_stops_without_third_model_or_any_accepted_result() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal, always_reject=True)
    with pytest.raises(RuntimeExecutionError):
        await client(model, journal.durability(), usage)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 2 and not journal.artifacts
    assert [row["status"] for row in journal.snapshots[-1]] == ["rejected", "rejected"]
    assert usage.tokens == 20 and journal.snapshots[-1][1]["correction_for"] == 0


async def test_correction_intent_commit_failure_never_enters_second_provider() -> None:
    journal, usage = Journal(refuse_correction_ack=True), DiscussionUsage()
    model = Model(journal)
    durable = journal.durability()
    with pytest.raises(OSError, match="correction intent"):
        await client(model, durable, usage)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and usage.tokens == 10
    assert journal.snapshots[-1][-1]["status"] == "rejected"
    assert not durable._model_lock.locked()


async def test_saved_accepted_correction_replays_without_provider_or_usage_recount() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal)
    await client(model, journal.durability(), usage)._complete_model_request(request(), TOOLS, None)
    saved = tuple(dict(row) for row in journal.snapshots[-1])
    other = Model(journal, forbidden=True)
    restored = journal.durability(models=saved)
    result, _ = await client(other, restored, usage)._complete_model_request(request(), TOOLS, None)
    assert result.response.tool_calls[0].name == "workspace_list"
    assert not other.requests and usage.tokens == 13 and usage.cost_usd == Decimal("0.001")


async def test_running_correction_is_unknown_and_cannot_be_reissued() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = Model(journal)
    await client(model, journal.durability(), usage)._complete_model_request(request(), TOOLS, None)
    accepted = journal.snapshots[-1]
    intent = next(rows for rows in journal.snapshots if rows[-1].get("correction_for") == 0
                  and rows[-1]["status"] == "running")
    other = Model(journal, forbidden=True)
    restored = journal.durability(models=tuple(dict(row) for row in intent))
    with pytest.raises(RuntimeExecutionError, match="model outcome is uncertain"):
        await client(other, restored, usage)._complete_model_request(request(), TOOLS, None)
    assert not other.requests and journal.snapshots[-1] == accepted and usage.tokens == 13


async def test_public_create_reports_both_attempts_without_recounting_durable_usage_on_replay() -> None:
    journal, usage = Journal(), DiscussionUsage()
    aliases = {"workspace.list": "workspace.list", "workspace_list": "workspace.list"}
    model = Model(journal, tool_names=aliases)
    tools: list[ToolSchema] = [{"name": "workspace.list", "description": "List the current workspace",
              "parameters": {"type": "object", "additionalProperties": False, "properties": {}}}]

    def public_client(provider: Model, durability: _DiscussionDurability) -> GatewayChatCompletionClient:
        return GatewayChatCompletionClient(provider, "shared", usage, durability=durability,
            supports_tool_calls=True, tool_correction_enabled=True,
            engineering_action=lambda force: "workspace.list",
            engineering_budget=lambda: (100 - usage.tokens, 5.0))

    first = public_client(model, journal.durability())
    result = await first.create([UserMessage(content="Build the project", source="user")], tools=tools)
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (9, 4)
    assert first.actual_usage() == first.total_usage() == result.usage
    assert usage.tokens == 13
    other = Model(journal, forbidden=True)
    restored = public_client(other, journal.durability(models=tuple(dict(row) for row in journal.snapshots[-1])))
    replay = await restored.create([UserMessage(content="Build the project", source="user")], tools=tools)
    assert replay.usage == result.usage and restored.total_usage() == replay.usage
    assert usage.tokens == 13 and not other.requests
