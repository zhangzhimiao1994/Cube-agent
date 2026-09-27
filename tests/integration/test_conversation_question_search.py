from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import ConversationRow, RunRow, TenantRow
from agent_hub.runs.repository import RunRepository


async def _add_conversation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation_id: str,
    project_id: str,
    archived: bool = False,
) -> None:
    now = datetime.now(UTC)
    session.add(
        ConversationRow(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            title=f"Title for {conversation_id}",
            project_id=project_id,
            workspace_path=f"workspace-{conversation_id}",
            archived_at=now if archived else None,
            created_at=now,
            updated_at=now,
        )
    )


def _run(
    *,
    run_id: UUID,
    tenant_id: UUID,
    conversation_id: str,
    request: str,
    created_at: datetime,
) -> RunRow:
    return RunRow(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request=request,
        mode="direct",
        status="completed",
        routing_decision={
            "conversation_id": conversation_id,
            "internal_payload": "must never reach search responses",
        },
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.integration
async def test_search_questions_scans_beyond_recent_100_and_matches_chinese_middle_and_end(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    now = datetime.now(UTC)
    old_run_id = UUID("00000000-0000-4000-8000-000000000001")
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"search-{tenant_id}", name="Search tenant"))
        await session.flush()
        await _add_conversation(
            session,
            tenant_id=tenant_id,
            conversation_id="conv-history",
            project_id="project-history",
        )
        session.add(
            _run(
                run_id=old_run_id,
                tenant_id=tenant_id,
                conversation_id="conv-history",
                request="开头内容，想查找跨会话中文问题尾部",
                created_at=now - timedelta(days=2),
            )
        )
        session.add_all(
            [
                _run(
                    run_id=uuid4(),
                    tenant_id=tenant_id,
                    conversation_id="conv-history",
                    request=f"new unrelated question {index}",
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(101)
            ]
        )

    repository = RunRepository(auth_session_factory)
    middle = await repository.search_conversation_questions(
        tenant_id, q="跨会话中文", archived=False, limit=20
    )
    ending = await repository.search_conversation_questions(
        tenant_id, q="问题尾部", archived=False, limit=20
    )

    assert [item.run_id for item in middle.items] == [old_run_id]
    assert [item.run_id for item in ending.items] == [old_run_id]
    assert middle.items[0].question == "开头内容，想查找跨会话中文问题尾部"
    assert middle.items[0].conversation_title == "Title for conv-history"


@pytest.mark.integration
async def test_search_questions_enforces_tenant_project_and_real_archived_filters(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    now = datetime.now(UTC)
    active_id = UUID("10000000-0000-4000-8000-000000000001")
    archived_id = UUID("20000000-0000-4000-8000-000000000001")
    other_tenant_run_id = UUID("30000000-0000-4000-8000-000000000001")
    async with auth_session_factory() as session, session.begin():
        session.add_all(
            [
                TenantRow(id=tenant_id, slug=f"search-{tenant_id}", name="Search tenant"),
                TenantRow(
                    id=other_tenant_id,
                    slug=f"search-{other_tenant_id}",
                    name="Other tenant",
                ),
            ]
        )
        await session.flush()
        await _add_conversation(
            session,
            tenant_id=tenant_id,
            conversation_id="conv-active",
            project_id="project-a",
        )
        await _add_conversation(
            session,
            tenant_id=tenant_id,
            conversation_id="conv-archived",
            project_id="project-b",
            archived=True,
        )
        await _add_conversation(
            session,
            tenant_id=other_tenant_id,
            conversation_id="conv-active",
            project_id="project-a",
        )
        session.add_all(
            [
                _run(
                    run_id=active_id,
                    tenant_id=tenant_id,
                    conversation_id="conv-active",
                    request="shared needle active",
                    created_at=now,
                ),
                _run(
                    run_id=archived_id,
                    tenant_id=tenant_id,
                    conversation_id="conv-archived",
                    request="shared needle archived",
                    created_at=now - timedelta(seconds=1),
                ),
                _run(
                    run_id=other_tenant_run_id,
                    tenant_id=other_tenant_id,
                    conversation_id="conv-active",
                    request="shared needle other tenant",
                    created_at=now + timedelta(seconds=1),
                ),
            ]
        )

    repository = RunRepository(auth_session_factory)
    active = await repository.search_conversation_questions(
        tenant_id,
        q="shared needle",
        project_id="project-a",
        archived=False,
        limit=20,
    )
    archived = await repository.search_conversation_questions(
        tenant_id,
        q="shared needle",
        project_id="project-b",
        archived=True,
        limit=20,
    )

    assert [item.run_id for item in active.items] == [active_id]
    assert [item.run_id for item in archived.items] == [archived_id]
    assert other_tenant_run_id not in {item.run_id for item in active.items + archived.items}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("literal", "matching_question", "wildcard_decoy"),
    [
        ("%", "find literal % percent", "find literal x percent"),
        ("_", "find literal _ underscore", "find literal x underscore"),
        ("\\", r"find literal \ slash", "find literal x slash"),
    ],
)
async def test_search_questions_treats_like_metacharacters_as_literals(
    auth_session_factory: async_sessionmaker[AsyncSession],
    literal: str,
    matching_question: str,
    wildcard_decoy: str,
) -> None:
    tenant_id = uuid4()
    matching_id = uuid4()
    now = datetime.now(UTC)
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"search-{tenant_id}", name="Search tenant"))
        await session.flush()
        await _add_conversation(
            session,
            tenant_id=tenant_id,
            conversation_id="conv-literals",
            project_id="project-literals",
        )
        session.add_all(
            [
                _run(
                    run_id=matching_id,
                    tenant_id=tenant_id,
                    conversation_id="conv-literals",
                    request=matching_question,
                    created_at=now,
                ),
                _run(
                    run_id=uuid4(),
                    tenant_id=tenant_id,
                    conversation_id="conv-literals",
                    request=wildcard_decoy,
                    created_at=now - timedelta(seconds=1),
                ),
            ]
        )

    page = await RunRepository(auth_session_factory).search_conversation_questions(
        tenant_id, q=literal, archived=False, limit=20
    )

    assert [item.run_id for item in page.items] == [matching_id]


@pytest.mark.integration
async def test_search_questions_uses_stable_created_at_and_id_cursor_pagination(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    created_at = datetime.now(UTC)
    ordered_ids = [
        UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    ]
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"search-{tenant_id}", name="Search tenant"))
        await session.flush()
        await _add_conversation(
            session,
            tenant_id=tenant_id,
            conversation_id="conv-page",
            project_id="project-page",
        )
        session.add_all(
            [
                _run(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    conversation_id="conv-page",
                    request=f"page needle {index}",
                    created_at=created_at,
                )
                for index, run_id in enumerate(ordered_ids)
            ]
        )

    repository = RunRepository(auth_session_factory)
    first = await repository.search_conversation_questions(
        tenant_id, q="page needle", archived=False, limit=2
    )
    second = await repository.search_conversation_questions(
        tenant_id, q="page needle", archived=False, limit=2, cursor=first.next_cursor
    )
    third = await repository.search_conversation_questions(
        tenant_id, q="page needle", archived=False, limit=2, cursor=second.next_cursor
    )

    assert [item.run_id for item in first.items] == ordered_ids[:2]
    assert [item.run_id for item in second.items] == ordered_ids[2:4]
    assert [item.run_id for item in third.items] == ordered_ids[4:]
    assert first.next_cursor is not None
    assert second.next_cursor is not None
    assert third.next_cursor is None
    assert len({item.run_id for page in (first, second, third) for item in page.items}) == 5
