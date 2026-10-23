"""Checkpoint publication must fence producers until the receiver acknowledges."""

import asyncio
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.checkpoint_publication import CheckpointPublications
from agent_hub.runtime.contracts import RuntimeCheckpoint


def build(sequence: int) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        id=uuid4(), tenant_id=uuid4(), run_id=uuid4(), mode=TaskMode.DISCUSS,
        runtime_type="autogen_discussion", runtime_version="2",
        state={"next_sequence": sequence},
    )


async def test_publish_waits_for_explicit_acknowledgement_and_build_is_deferred() -> None:
    publications = CheckpointPublications()
    sequences = []

    def deferred(sequence: int) -> RuntimeCheckpoint:
        sequences.append(sequence)
        return build(sequence)

    sender = asyncio.create_task(publications.publish(deferred))
    try:
        pending = await asyncio.wait_for(publications.next(), timeout=1)
        assert not sender.done() and not pending.cancelled and sequences == []
        checkpoint = pending.build(42)
        assert checkpoint.state["next_sequence"] == 42 and sequences == [42]
        assert not sender.done(), "building a checkpoint is not a persistence acknowledgement"
        assert pending.acknowledge() is True
        assert pending.acknowledge() is False
        assert await sender is None
    finally:
        publications.close()
        await asyncio.gather(sender, return_exceptions=True)


async def test_acknowledgement_does_not_release_another_sender() -> None:
    publications = CheckpointPublications()
    first = asyncio.create_task(publications.publish(build))
    second = asyncio.create_task(publications.publish(build))
    try:
        pending_first = await asyncio.wait_for(publications.next(), timeout=1)
        pending_second = await asyncio.wait_for(publications.next(), timeout=1)
        assert pending_first.future is not pending_second.future
        pending_first.acknowledge()
        await first
        assert not second.done() and not pending_second.future.done()
        pending_second.acknowledge()
        await second
    finally:
        publications.close()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_cancelled_sender_cannot_be_acknowledged_or_enter_followup_io() -> None:
    publications = CheckpointPublications()
    io_entered = False

    async def produce() -> None:
        nonlocal io_entered
        await publications.publish(build)
        io_entered = True

    sender = asyncio.create_task(produce())
    try:
        pending = await asyncio.wait_for(publications.next(), timeout=1)
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        assert pending.cancelled and pending.future.cancelled()
        assert pending.acknowledge() is False
        assert io_entered is False
    finally:
        publications.close()
        await asyncio.gather(sender, return_exceptions=True)


async def test_next_skips_cancelled_queued_publication() -> None:
    publications = CheckpointPublications()
    cancelled = asyncio.create_task(publications.publish(build))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    live = asyncio.create_task(publications.publish(build))
    try:
        pending = await asyncio.wait_for(publications.next(), timeout=1)
        assert not pending.cancelled
        pending.acknowledge()
        await live
    finally:
        publications.close()
        await asyncio.gather(live, return_exceptions=True)


async def test_close_cancels_queued_and_inflight_senders_without_acknowledging() -> None:
    publications = CheckpointPublications()
    inflight = asyncio.create_task(publications.publish(build))
    pending = await asyncio.wait_for(publications.next(), timeout=1)
    queued = asyncio.create_task(publications.publish(build))
    await asyncio.sleep(0)
    publications.close()
    publications.close()
    for sender in (inflight, queued):
        with pytest.raises(asyncio.CancelledError):
            await sender
    assert pending.cancelled and pending.acknowledge() is False
    with pytest.raises(asyncio.CancelledError):
        await publications.next()


async def test_close_wakes_all_waiting_receivers() -> None:
    publications = CheckpointPublications()
    receivers = [asyncio.create_task(publications.next()) for _ in range(2)]
    await asyncio.sleep(0)
    publications.close()
    outcomes = await asyncio.wait_for(asyncio.gather(*receivers, return_exceptions=True), timeout=1)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)


async def test_publish_after_close_fails_without_build_or_followup_io() -> None:
    publications = CheckpointPublications()
    publications.close()
    called = False

    def unexpected(sequence: int) -> RuntimeCheckpoint:
        nonlocal called
        called = True
        return build(sequence)

    with pytest.raises(asyncio.CancelledError):
        await publications.publish(unexpected)
    assert not called


async def test_receiver_cancellation_does_not_cancel_live_publication() -> None:
    publications = CheckpointPublications()
    receiver = asyncio.create_task(publications.next())
    await asyncio.sleep(0)
    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver
    sender = asyncio.create_task(publications.publish(build))
    try:
        pending = await asyncio.wait_for(publications.next(), timeout=1)
        assert pending.acknowledge() is True
        await sender
    finally:
        publications.close()
        await asyncio.gather(sender, return_exceptions=True)


async def test_failed_publication_does_not_release_or_cancel_other_sender() -> None:
    publications = CheckpointPublications()
    first = asyncio.create_task(publications.publish(build))
    second = asyncio.create_task(publications.publish(build))
    try:
        failed = await asyncio.wait_for(publications.next(), timeout=1)
        healthy = await asyncio.wait_for(publications.next(), timeout=1)
        failed.future.set_exception(ValueError("checkpoint transaction failed"))
        with pytest.raises(ValueError, match="checkpoint transaction failed"):
            await first
        assert failed.acknowledge() is False
        assert not second.done() and not healthy.cancelled
        healthy.acknowledge()
        await second
    finally:
        publications.close()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_close_does_not_revoke_an_already_successful_acknowledgement() -> None:
    publications = CheckpointPublications()
    sender = asyncio.create_task(publications.publish(build))
    pending = await asyncio.wait_for(publications.next(), timeout=1)
    assert pending.acknowledge() is True
    publications.close()
    assert not pending.cancelled and pending.acknowledge() is False
    assert await sender is None


async def test_no_background_tasks_remain_after_close() -> None:
    before = asyncio.all_tasks()
    publications = CheckpointPublications()
    sender = asyncio.create_task(publications.publish(build))
    await asyncio.wait_for(publications.next(), timeout=1)
    publications.close()
    with pytest.raises(asyncio.CancelledError):
        await sender
    assert asyncio.all_tasks() == before
