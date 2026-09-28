from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.gateway import ModelGateway as ConfiguredModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelMessage, ModelResponse, ToolCall
from agent_hub.runtime.contracts import Artifact, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime


def _context(*, token_budget: int, project_scale: str = "small") -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISPATCH,
        request="Build the requested project.",
        routing_decision={"project_scale": project_scale},
        timeout_seconds=60,
        token_budget=token_budget,
    )


class _CapacityConfigurationStub:
    def validate_configuration(self, deployments: object) -> None:
        del deployments


def _configured_gateway() -> ConfiguredModelGateway:
    registry = ModelRegistry(
        (
            Deployment(
                id="primary",
                logical_model="general",
                context_window_tokens=131_072,
            ),
            Deployment(
                id="fallback",
                logical_model="backup",
                context_window_tokens=32_768,
            ),
        )
    )
    return ConfiguredModelGateway(
        registry,
        _CapacityConfigurationStub(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        fallbacks={"general": "backup"},
    )


def test_crew_content_limits_grow_with_token_budget_and_project_scale() -> None:
    small = adapter._crew_content_limits(
        _context(token_budget=16_384, project_scale="small"),
        max_output_tokens=8_192,
        source_count=4,
    )
    large = adapter._crew_content_limits(
        _context(token_budget=131_072, project_scale="large"),
        max_output_tokens=24_576,
        source_count=4,
    )
    ultra = adapter._crew_content_limits(
        _context(token_budget=1_000_000, project_scale="ultra"),
        max_output_tokens=1_000_000,
        source_count=4,
    )

    assert small.prompt_bytes < large.prompt_bytes <= ultra.prompt_bytes
    assert small.output_bytes < large.output_bytes <= ultra.output_bytes
    assert small.source_artifact_text_bytes < large.source_artifact_text_bytes
    assert small.final_source_artifact_text_bytes < large.final_source_artifact_text_bytes
    assert small.interaction_message_limit < large.interaction_message_limit
    assert ultra.prompt_bytes == adapter._ABSOLUTE_PROMPT_BYTES
    assert ultra.output_bytes == adapter._ABSOLUTE_OUTPUT_BYTES


def test_history_and_final_synthesis_use_dynamic_per_source_budget() -> None:
    context = _context(token_budget=131_072, project_scale="large")
    limits = adapter._crew_content_limits(
        context,
        max_output_tokens=24_576,
        source_count=2,
    )
    marker = "EARLY_DECISION_MUST_SURVIVE"
    text = marker + "x" * 100_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": text},
    )

    history = adapter._artifact_prompt_payload(
        artifact,
        max_text_bytes=limits.source_artifact_text_bytes,
    )
    final = adapter._artifact_final_synthesis_payload(
        artifact,
        max_text_bytes=limits.final_source_artifact_text_bytes,
    )

    assert marker in str(history["content"])
    assert marker in str(final["content"])
    assert len(str(history["content"]).encode("utf-8")) > 8_192
    assert len(str(final["content"]).encode("utf-8")) > 2_048


def test_interaction_compaction_watermark_scales_with_prompt_budget() -> None:
    messages = tuple(
        ModelMessage(role="user", content=f"message-{index}-" + "x" * 2_000)
        for index in range(80)
    )
    small_limits = adapter._crew_content_limits(
        _context(token_budget=16_384), max_output_tokens=8_192, source_count=1
    )
    large_limits = adapter._crew_content_limits(
        _context(token_budget=131_072, project_scale="large"),
        max_output_tokens=24_576,
        source_count=1,
    )

    small = CrewDispatchRuntime._compact_interaction_messages(
        messages,
        max_prompt_bytes=small_limits.interaction_prompt_bytes,
        message_limit=small_limits.interaction_message_limit,
    )
    large = CrewDispatchRuntime._compact_interaction_messages(
        messages,
        max_prompt_bytes=large_limits.interaction_prompt_bytes,
        message_limit=large_limits.interaction_message_limit,
    )

    assert len(small) < len(large) <= len(messages)
    assert small[0] == messages[0]
    assert large[0] == messages[0]
    assert any("EARLIER_INTERACTION_WINDOW_COMPRESSED" in str(item.content) for item in small)


def test_dynamic_limits_remain_below_absolute_json_safety_fuse() -> None:
    limits = adapter._crew_content_limits(
        _context(token_budget=10_000_000, project_scale="ultra"),
        max_output_tokens=1_000_000,
        source_count=1,
    )
    assert limits.prompt_bytes == adapter._ABSOLUTE_PROMPT_BYTES
    assert limits.output_bytes == adapter._ABSOLUTE_OUTPUT_BYTES
    assert len(json.dumps({"text": "x" * limits.output_bytes}).encode()) > limits.output_bytes


def test_crew_content_limits_honor_selected_deployment_context_window() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISPATCH,
        request="Build the requested project.",
        routing_decision={
            "project_scale": "ultra",
            "main_agent_context_window_tokens": 32_768,
            "context_window_tokens": 131_072,
        },
        timeout_seconds=60,
        token_budget=1_000_000,
    )

    limits = adapter._crew_content_limits(
        context,
        max_output_tokens=8_192,
        source_count=1,
    )

    assert limits.prompt_bytes <= (32_768 - 8_192) * 4
    messages = tuple(
        ModelMessage(role="user", content=f"message-{index}-" + "x" * 8_000)
        for index in range(80)
    )
    compacted = CrewDispatchRuntime._compact_interaction_messages(
        messages,
        max_prompt_bytes=limits.interaction_prompt_bytes,
        message_limit=limits.interaction_message_limit,
    )
    assert len(compacted) < len(messages)
    assert sum(
        len(json.dumps(message.content, ensure_ascii=False).encode("utf-8"))
        for message in compacted
    ) <= limits.interaction_prompt_bytes


def test_runtime_content_limits_use_gateway_deployments_without_routing_hint() -> None:
    context = _context(token_budget=1_000_000, project_scale="ultra")
    runtime = CrewDispatchRuntime(
        _configured_gateway(),
        object(),  # type: ignore[arg-type]
    )

    limits = runtime._content_limits(
        context,
        logical_model="general",
        max_output_tokens=8_192,
        source_count=1,
    )

    assert limits.prompt_bytes <= (32_768 - 8_192) * 4


def test_large_project_zip_model_evidence_keeps_only_auditable_summary() -> None:
    files = {
        f"src/file-{index}.txt": f"FILE-{index}-" + "x" * 149_000
        for index in range(60)
    }
    arguments = {"title": "large-project", "files": files}
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert 2_000_000 < len(canonical) < 10_000_000
    completion = GatewayCompletion(
        response=ModelResponse(
            text=None,
            tool_calls=(
                ToolCall(
                    id="call_zip",
                    name="project.generate_zip",
                    arguments=arguments,
                ),
            ),
        ),
        deployment_id="deployment",
        logical_model="general",
        provider_id="provider",
        provider_model="provider/model",
    )

    artifact = CrewDispatchRuntime._model_artifact(
        completion,
        "builder",
        (),
        max_output_bytes=65_536,
    )

    payload = artifact.to_payload()
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 2_000_000
    recorded = artifact.content["tool_calls"][0]
    assert recorded["arguments"] == {
        "audit": {
            "arguments_sha256": hashlib.sha256(canonical).hexdigest(),
            "encoded_bytes": len(canonical),
            "file_count": len(files),
            "omitted": True,
        }
    }
