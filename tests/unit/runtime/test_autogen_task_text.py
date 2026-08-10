from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import Artifact, TaskContext


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
