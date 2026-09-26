from typing import cast

from sqlalchemy import Table, UniqueConstraint

from agent_hub.db.models import ConversationQueueItemRow, RunRow
from agent_hub.domain.runs import ConversationQueueStatus


def test_conversation_queue_statuses_are_stable_wire_values() -> None:
    assert [status.value for status in ConversationQueueStatus] == [
        "queued",
        "redirecting",
        "released",
        "cancelled",
        "running",
        "completed",
        "failed",
    ]


def test_run_row_exposes_a_nullable_blocking_predecessor() -> None:
    column = RunRow.__table__.c.blocked_by_run_id

    assert column.nullable is True
    assert column.index is True
    assert next(iter(column.foreign_keys)).target_fullname == "agent_hub_runs.id"


def test_conversation_queue_has_tenant_idempotency_and_ordering_contracts() -> None:
    table = cast(Table, ConversationQueueItemRow.__table__)
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexes = {
        str(index.name): tuple(column.name for column in index.columns) for index in table.indexes
    }

    assert ("tenant_id", "idempotency_key") in unique_columns
    assert indexes["ix_agent_hub_conversation_queue_order"] == (
        "tenant_id",
        "conversation_id",
        "position",
    )
    assert table.c.version.server_default is not None
    assert table.c.failure_detail.nullable is True
