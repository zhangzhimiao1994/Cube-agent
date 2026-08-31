from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from typing import cast

from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime


class UnusedGateway:
    pass


def test_direct_prompt_truncates_large_artifact_text_for_capacity_estimation() -> None:
    original_text = "长文本" * 1_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": original_text},
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Synthesize the artifacts.",
        artifacts=(artifact,),
        token_budget=1_000_000,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    request = runtime._build_request(context).request

    assert request is not None
    user_content = request.messages[-1].content
    assert isinstance(user_content, str)
    assert "[truncated:" in user_content
    assert request.max_output_tokens <= 8192
    assert len(user_content.encode("utf-8")) < len(original_text.encode("utf-8"))
    assert artifact.content["text"] == original_text


def test_direct_prompt_includes_bounded_hermes_memory_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "hermes": {
            "injected_memories": (
                {
                    "summary": "reviewer 超时时先压缩上下文再分块审查。",
                    "memory_type": "error_handling",
                    "target": "reviewer",
                    "reason": "命中 reviewer 超时经验",
                },
            )
        }
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "HERMES_MEMORY_CONTEXT" in serialized
    assert "reviewer 超时时先压缩上下文再分块审查" in serialized
    assert "Current user instructions override them" in serialized
