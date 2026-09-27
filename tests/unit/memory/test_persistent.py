from __future__ import annotations

from uuid import UUID

from agent_hub.memory.persistent import select_runtime_memories

ACTOR = UUID("11111111-1111-4111-8111-111111111111")
OTHER_ACTOR = UUID("22222222-2222-4222-8222-222222222222")


def test_runtime_memory_selection_is_actor_and_scope_isolated() -> None:
    selected = select_runtime_memories(
        (
            (
                "mine",
                {
                    "scope": f"user:{ACTOR}",
                    "owner_actor_id": str(ACTOR),
                    "value": "Use pytest for backend verification.",
                    "layer": "core",
                    "category": "preference",
                },
            ),
            (
                "other-user",
                {
                    "scope": f"user:{OTHER_ACTOR}",
                    "owner_actor_id": str(OTHER_ACTOR),
                    "value": "Use an unsafe private instruction.",
                    "layer": "core",
                },
            ),
            (
                "malformed-tenant-owned",
                {
                    "scope": "tenant",
                    "owner_actor_id": str(ACTOR),
                    "value": "Malformed tenant memory must not activate.",
                    "layer": "core",
                },
            ),
            (
                "legacy-user",
                {"scope": "user", "value": "Legacy private text must not activate."},
            ),
            (
                "legacy-unknown-scope",
                {"scope": "old-project-name", "value": "Legacy text must not activate."},
            ),
        ),
        actor_id=ACTOR,
        query="verify the backend with pytest",
        project_id=None,
        conversation_id=None,
    )

    assert [item.id for item in selected] == ["mine"]


def test_runtime_memory_selection_can_reach_past_two_hundred_unrelated_rows() -> None:
    unrelated = tuple(
        (
            f"other-{index:03d}",
            {
                "scope": f"user:{OTHER_ACTOR}",
                "owner_actor_id": str(OTHER_ACTOR),
                "value": "pytest backend verification",
                "layer": "core",
            },
        )
        for index in range(250)
    )

    selected = select_runtime_memories(
        unrelated
        + (
            (
                "mine-old",
                {
                    "scope": f"user:{ACTOR}",
                    "owner_actor_id": str(ACTOR),
                    "value": "pytest backend verification",
                    "layer": "core",
                },
            ),
        ),
        actor_id=ACTOR,
        query="pytest backend verification",
        project_id=None,
        conversation_id=None,
    )

    assert [item.id for item in selected] == ["mine-old"]


def test_runtime_memory_selection_prioritizes_exact_conversation_and_project() -> None:
    selected = select_runtime_memories(
        (
            (
                "tenant-core",
                {
                    "scope": "tenant",
                    "value": "Always preserve verification evidence.",
                    "layer": "core",
                    "category": "decision",
                    "locked": True,
                },
            ),
            (
                "project-fact",
                {
                    "scope": f"user:{ACTOR}",
                    "value": "The project backend verification uses pytest.",
                    "layer": "episodic",
                    "category": "fact",
                    "project_id": "cube-agent",
                    "confidence": 0.9,
                },
            ),
            (
                "conversation-note",
                {
                    "scope": f"user:{ACTOR}",
                    "value": "For this conversation verify the backend with pytest first.",
                    "layer": "working",
                    "category": "task",
                    "project_id": "cube-agent",
                    "conversation_id": "conv-1",
                },
            ),
            (
                "wrong-conversation",
                {
                    "scope": f"user:{ACTOR}",
                    "value": "Verify the backend with pytest in another conversation.",
                    "layer": "working",
                    "conversation_id": "conv-2",
                },
            ),
        ),
        actor_id=ACTOR,
        query="verify the backend with pytest",
        project_id="cube-agent",
        conversation_id="conv-1",
    )

    assert [item.id for item in selected] == [
        "conversation-note",
        "project-fact",
        "tenant-core",
    ]


def test_runtime_memory_selection_is_bounded_and_rejects_sensitive_rows() -> None:
    rows = [
        (
            f"memory-{index}",
            {
                "scope": f"user:{ACTOR}",
                "value": f"pytest backend verification rule {index}",
                "layer": "episodic",
                "confidence": 0.9,
            },
        )
        for index in range(5)
    ]
    rows.append(
        (
            "secret",
            {
                "scope": f"user:{ACTOR}",
                "value": "API key sk-secret-value",
                "layer": "core",
            },
        )
    )

    selected = select_runtime_memories(
        tuple(rows),
        actor_id=ACTOR,
        query="pytest backend verification",
        project_id=None,
        conversation_id=None,
        limit=3,
    )

    assert len(selected) == 3
    assert all(item.id != "secret" for item in selected)
