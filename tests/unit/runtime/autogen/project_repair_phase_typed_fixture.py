"""Checked JSON mutation boundaries and DTOs for project-repair phase tests only."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard
from uuid import UUID, uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayRejectedOutput
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    RejectedOutputEvidence,
    TokenUsage,
    ToolDefinition,
)
from agent_hub.runtime.autogen.model_rejection import rejection_record
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    TaskContext,
    _freeze_object,
    _mutable_json,
)


def _object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mapping(value: object) -> dict[str, object]:
    """Narrow an existing mutable object without copying away mutation identity."""
    assert _object_dict(value)
    return value


def _object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def sequence(value: object) -> list[object]:
    assert _object_list(value)
    return value


def _row_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(_object_dict(row) for row in value)


def rows(value: object) -> list[dict[str, object]]:
    assert _row_list(value)
    return value


def integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def number(value: object) -> int | float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return value


def string(value: object) -> str:
    assert isinstance(value, str)
    return value


def json_map(value: object) -> Mapping[str, JsonValue]:
    return _freeze_object(value, name='project repair phase fixture')


def ledger(value: object) -> tuple[Mapping[str, JsonValue], ...]:
    assert isinstance(value, (tuple, list))
    return tuple(json_map(row) for row in value)


def mutable(value: object) -> dict[str, object]:
    return mapping(_mutable_json(json_map(value)))


def mutable_rows(value: Sequence[Mapping[str, JsonValue]]) -> list[dict[str, object]]:
    return rows(_mutable_json(tuple(value)))


class UnexpectedTaskQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError('repair continuation must use the transactional outbox')


def pair() -> list[dict[str, JsonValue]]:
    """One completed invalid-tool rejection with one bounded accepted successor."""
    request = ModelRequest(logical_model='shared', messages=(), required_tool='workspace_list',
                           tools=(ToolDefinition(name='workspace_list', description='List workspace',
                               parameters={'type': 'object', 'properties': {},
                                           'additionalProperties': False}),),
                           max_output_tokens=100, timeout_seconds=10.0,
                           required_capabilities=frozenset({ModelCapability.TOOL_CALLING}))
    rejected = GatewayRejectedOutput(
        evidence=RejectedOutputEvidence(final_text=None, usage=TokenUsage(7, 3, 10),
            usage_status='known', status='completed', reason='invalid_tool'),
        logical_model='shared', deployment_id='fixture', provider_id='fixture',
        provider_model='fixture/model', cost_usd=None,
    )
    receipt = rejection_record(rejected, run_id=uuid4(), tenant_id=uuid4(),
        position=0, request=request, request_hash='a' * 64)
    return [
        {'status': 'rejected', 'request_hash': 'a' * 64, 'rejection': receipt.to_payload(),
         'required_tool': 'workspace_list', 'max_output_tokens': 100, 'timeout_seconds': 10.0},
        {'status': 'succeeded', 'request_hash': 'b' * 64, 'correction_for': 0,
         'required_tool': 'workspace_list', 'max_output_tokens': 90, 'timeout_seconds': 5.0,
         'artifact_id': str(uuid4()), 'artifact_sha256': 'c' * 64},
    ]


RUN = UUID("00000000-0000-4000-8000-000000000001")
OWNER = "delivery_main_agent"
MODEL = "delivery-model"
TEXT = "## Delivery\nThe archive is attached.\n\nTests passed; browser use was not verified.\n"


@dataclass
class Evidence:
    context: TaskContext
    models: list[dict[str, JsonValue]]
    tools: list[dict[str, JsonValue]]
    artifacts: list[Artifact]

    def metadata(self, *, text: str = TEXT, logical_model: str = MODEL,
                 owner: str = OWNER) -> dict[str, JsonValue]:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_metadata

        return engineering_report_metadata(
            context=self.context, text=text, logical_model=logical_model, owner=owner,
            model_entries=self.models, tool_entries=self.tools, artifacts=self.artifacts,
        )

    def report(self, *, metadata: Mapping[str, JsonValue] | None = None,
               text: str = TEXT, source_ids: tuple[str, ...] | None = None,
               producer: str = OWNER, presentation: str = "final_answer") -> Artifact:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_id

        bound = self.metadata() if metadata is None else metadata
        return Artifact(
            id=engineering_report_id(self.context, bound), type="text", producer=producer,
            content={"text": text, "presentation": presentation,
                     "engineering_report": bound},
            source_ids=(str(self.artifacts[-1].id), str(self.artifacts[-2].id))
            if source_ids is None else source_ids,
        )

    def valid(self, report: Artifact, *, logical_model: str = MODEL) -> bool:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_valid

        return engineering_report_valid(
            report, context=self.context, logical_model=logical_model, model_entries=self.models,
            tool_entries=self.tools, artifacts=self.artifacts,
        )


def fixture() -> Evidence:
    context = TaskContext(
        run_id=RUN, tenant_id=uuid4(), mode=TaskMode.DISCUSS, request="deliver",
        routing_decision={"engineering_final_report_version": 1},
    )
    artifacts: list[Artifact] = []
    tools: list[dict[str, JsonValue]] = []
    for position, name in enumerate(("workspace.read", "workspace.bundle")):
        artifact = Artifact(id=uuid4(), type="tool_result", producer="engineer", content={
            "tool_name": name, "arguments_sha256": "a" * 64,
            "result": {"text": "tool summary", "presentation": "final_attachment"},
        })
        artifacts.append(artifact)
        tools.append({
            "position": position, "actor": "engineer", "name": name,
            "arguments_hash": "a" * 64, "status": "succeeded", "replay_safe": False,
            "idempotency_key": hashlib.sha256(
                f"{RUN}:engineer:{position}:{name}:{'a' * 64}".encode()).hexdigest(),
            "artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256,
        })
    model = Artifact(id=uuid4(), type="model_result", producer="agent_hub", content={
        "text": TEXT, "tool_calls": (), "logical_model": MODEL,
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "deployment_id": "deployment", "provider_id": "provider",
        "provider_model": "provider/model", "cost_usd": "0",
    })
    artifacts.append(model)
    models: list[dict[str, JsonValue]] = [{
        "status": "succeeded", "request_hash": "b" * 64,
        "artifact_id": str(model.id), "artifact_sha256": model.content_sha256,
    }]
    models[-1]["report_binding"] = {
        "version": 1, "run_id": str(RUN), "tenant_id": str(context.tenant_id),
        "owner": OWNER, "requested_logical_model": MODEL,
        "bundle_artifact_id": str(artifacts[-2].id),
        "bundle_artifact_sha256": artifacts[-2].content_sha256,
        "tool_prefix_length": len(tools),
        "tool_prefix_sha256": hashlib.sha256(json.dumps(
            tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest(),
    }
    return Evidence(context, models, tools, artifacts)

