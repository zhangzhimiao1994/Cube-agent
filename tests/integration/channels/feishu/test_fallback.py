from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from agent_hub.channels.feishu.cards import FeishuCard, FeishuCardBuilder, InMemoryFeishuActionStore
from agent_hub.channels.feishu.delivery import (
    FeishuClarification,
    FeishuDelivery,
    FeishuDeliveryError,
)
from agent_hub.domain.runs import TaskMode

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


@dataclass(slots=True)
class FakeFeishuApi:
    fail_cards: bool = False
    cards: list[tuple[str, FeishuCard]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)

    async def send_card(self, conversation_id: str, card: FeishuCard) -> None:
        if self.fail_cards:
            raise FeishuDeliveryError("card rejected")
        self.cards.append((conversation_id, card))

    async def send_text(self, conversation_id: str, text: str) -> None:
        self.texts.append((conversation_id, text))


def sample_choice() -> FeishuClarification:
    return FeishuClarification(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        conversation_id="oc_1",
        options=(
            TaskMode.DIRECT,
            TaskMode.DISPATCH,
            TaskMode.DISCUSS,
            TaskMode.HYBRID,
        ),
        reason="low_confidence",
    )


async def test_clarification_card_failure_sends_text() -> None:
    api = FakeFeishuApi(fail_cards=True)
    delivery = FeishuDelivery(
        api=api,
        card_builder=FeishuCardBuilder(InMemoryFeishuActionStore()),
    )

    await delivery.send_clarification(sample_choice())

    assert api.cards == []
    assert api.texts[-1][1].startswith("请选择执行模式")
    assert "2. dispatch" in api.texts[-1][1]


async def test_text_choice_binds_to_pending_conversation_and_user() -> None:
    api = FakeFeishuApi(fail_cards=True)
    delivery = FeishuDelivery(
        api=api,
        card_builder=FeishuCardBuilder(InMemoryFeishuActionStore()),
    )
    await delivery.send_clarification(sample_choice())

    assert (
        delivery.consume_text_choice(
            conversation_id="oc_1",
            sender_user_id=OTHER_USER_ID,
            text="2",
        )
        is None
    )
    assert (
        delivery.consume_text_choice(
            conversation_id="other_conversation",
            sender_user_id=USER_ID,
            text="2",
        )
        is None
    )
    assert (
        delivery.consume_text_choice(
            conversation_id="oc_1",
            sender_user_id=USER_ID,
            text="2",
        )
        is TaskMode.DISPATCH
    )
    assert (
        delivery.consume_text_choice(
            conversation_id="oc_1",
            sender_user_id=USER_ID,
            text="2",
        )
        is None
    )


async def test_card_success_still_registers_text_choice() -> None:
    api = FakeFeishuApi()
    delivery = FeishuDelivery(
        api=api,
        card_builder=FeishuCardBuilder(InMemoryFeishuActionStore()),
    )

    await delivery.send_clarification(sample_choice())

    assert len(api.cards) == 1
    assert api.texts == []
    assert (
        delivery.consume_text_choice(
            conversation_id="oc_1",
            sender_user_id=USER_ID,
            text="1",
        )
        is TaskMode.DIRECT
    )


async def test_text_choice_expires() -> None:
    now = 10.0
    api = FakeFeishuApi(fail_cards=True)
    delivery = FeishuDelivery(
        api=api,
        card_builder=FeishuCardBuilder(InMemoryFeishuActionStore()),
        monotonic=lambda: now,
        text_choice_ttl_seconds=1,
    )
    await delivery.send_clarification(sample_choice())
    now = 12.0

    assert (
        delivery.consume_text_choice(
            conversation_id="oc_1",
            sender_user_id=USER_ID,
            text="1",
        )
        is None
    )


async def test_approval_card_failure_sends_text_fallback() -> None:
    api = FakeFeishuApi(fail_cards=True)
    delivery = FeishuDelivery(
        api=api,
        card_builder=FeishuCardBuilder(InMemoryFeishuActionStore()),
    )

    await delivery.send_approval(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        conversation_id="oc_1",
        approval_id="approval_1",
        summary="HTTP read",
    )

    assert api.texts[-1][1].startswith("需要审批")
    assert "approval_1" in api.texts[-1][1]
