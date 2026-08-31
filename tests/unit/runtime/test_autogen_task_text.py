from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext


def test_discussion_task_text_serializes_nested_artifact_content() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": "plan", "metadata": {"stage": "dispatch"}},
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Discuss the plan.",
        artifacts=(artifact,),
    )

    task_text = AutoGenDiscussionRuntime._task_text(context)

    assert "Validated artifacts" in task_text
    assert '"metadata": {"stage": "dispatch"}' in task_text


def test_discussion_task_text_truncates_large_artifact_text_for_capacity_estimation() -> None:
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
        mode=TaskMode.DISCUSS,
        request="Discuss the plan.",
        artifacts=(artifact,),
    )

    task_text = AutoGenDiscussionRuntime._task_text(context)

    assert "[truncated:" in task_text
    assert len(task_text.encode("utf-8")) < len(original_text.encode("utf-8"))
    assert artifact.content["text"] == original_text


def test_discussion_task_text_includes_bounded_hermes_memory_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "hermes": {
            "injected_memories": (
                {
                    "summary": "讨论前先引用上一轮已确认结论。",
                    "memory_type": "conversation",
                    "target": "discussion",
                    "reason": "命中讨论记忆",
                },
            )
        }
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="继续讨论方案",
        artifacts=(),
        routing_decision=routing_decision,
    )

    task_text = AutoGenDiscussionRuntime._task_text(context)

    assert "HERMES_MEMORY_CONTEXT" in task_text
    assert "讨论前先引用上一轮已确认结论" in task_text
    assert "Current user instructions override them" in task_text
