"""Real cooperative reservation/publication binding, never release or GC authority."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.plugins.handover_confirmation import _publish_artifact
from agent_hub.plugins.handover_stage_journal import StagePublicationJournal
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
from tests.unit.plugins.test_handover_stage_journal import TENANT, private_directory

PLUGIN = "example.plugin"
DIGEST = "b" * 64
FILES = (("file.txt", b"saved"),)


@pytest.fixture
def store(tmp_path: Path) -> StageReservationStore:
    root = tmp_path / "store"
    private_directory(root)
    return StageReservationStore(
        root,
        ReservationPolicy(
            max_bytes=200_000_000, max_members=50_000, max_operations=4, min_available_bytes=0
        ),
    )


def test_publication_binds_issued_reservation_and_reuse_is_uncharged(
    store: StageReservationStore,
) -> None:
    root = store._root
    artifact = root / str(TENANT) / PLUGIN / DIGEST
    assert (
        _publish_artifact(
            artifact,
            FILES,
            store_root=root,
            tenant=TENANT,
            plugin_id=PLUGIN,
            reservation_store=store,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            revalidate=lambda: True,
        )
        is True
    )
    usage = store.usage()
    assert usage.operations == 1
    ledger = root / ".stage-ownership" / str(TENANT) / PLUGIN
    operations = list(ledger.iterdir())
    assert len(operations) == 1
    event = json.loads((operations[0] / "000001.json").read_bytes())
    assert event["reservation"]["operation_id"] == operations[0].name
    assert event["operation_id"] == operations[0].name
    assert event["state"] == "creating"
    assert (
        _publish_artifact(
            artifact,
            FILES,
            store_root=root,
            tenant=TENANT,
            plugin_id=PLUGIN,
            reservation_store=store,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
            revalidate=lambda: False,
        )
        is False
    )
    assert store.usage() == usage


def test_corrupt_reserved_record_cannot_create_publication_journal(
    store: StageReservationStore,
) -> None:
    root = store._root
    operation = uuid4()
    token = store.reserve(
        operation_id=operation,
        tenant=TENANT,
        plugin_id=PLUGIN,
        stage_name="tmp-gh-" + operation.hex,
        target_digest=DIGEST,
        source_bytes=5,
        source_members=2,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        revalidate=lambda: True,
    )
    private_directory(root / str(TENANT))
    private_directory(root / str(TENANT) / PLUGIN)
    (root / ".stage-reservations" / (str(operation) + ".json")).write_text("{}", encoding="ascii")
    with pytest.raises((ValueError, RuntimeError)):
        StagePublicationJournal(
            root,
            TENANT,
            PLUGIN,
            token.stage_name,
            DIGEST,
            5,
            2,
            reservation=token,
        )
    assert not (root / ".stage-ownership").exists()


def test_second_publication_validates_prior_charged_journal(store: StageReservationStore) -> None:
    root = store._root
    for digest in (DIGEST, "c" * 64):
        artifact = root / str(TENANT) / PLUGIN / digest
        assert (
            _publish_artifact(
                artifact,
                FILES,
                store_root=root,
                tenant=TENANT,
                plugin_id=PLUGIN,
                reservation_store=store,
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                revalidate=lambda: True,
            )
            is True
        )
    assert store.usage().operations == 2


def test_wrong_scope_token_is_refused_before_journal_creation(store: StageReservationStore) -> None:
    root = store._root
    operation = uuid4()
    token = store.reserve(
        operation_id=operation,
        tenant=TENANT,
        plugin_id=PLUGIN,
        stage_name="tmp-gh-" + operation.hex,
        target_digest=DIGEST,
        source_bytes=5,
        source_members=2,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        revalidate=lambda: True,
    )
    with pytest.raises((ValueError, RuntimeError)):
        StagePublicationJournal(
            root,
            TENANT,
            PLUGIN,
            token.stage_name,
            DIGEST,
            5,
            2,
            reservation=replace(token, plugin_id="other"),
        )
    assert not (root / ".stage-ownership").exists()


@pytest.mark.parametrize("expired", [False, True])
def test_refused_publication_does_not_create_unaccounted_payload_parents(
    store: StageReservationStore,
    expired: bool,
) -> None:
    root = store.store_root
    artifact = root / str(TENANT) / PLUGIN / DIGEST
    deadline = datetime.now(UTC) + timedelta(seconds=-1 if expired else 30)
    with pytest.raises(RuntimeError):
        _publish_artifact(
            artifact,
            FILES,
            store_root=root,
            tenant=TENANT,
            plugin_id=PLUGIN,
            reservation_store=store,
            deadline=deadline,
            revalidate=lambda: expired,
        )
    assert not artifact.parent.exists()
    assert not (root / str(TENANT)).exists()
    other = root / str(TENANT) / "other.plugin" / DIGEST
    assert (
        _publish_artifact(
            other,
            FILES,
            store_root=root,
            tenant=TENANT,
            plugin_id="other.plugin",
            reservation_store=store,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            revalidate=lambda: True,
        )
        is True
    )


def test_failed_creating_retains_charge_without_source_stage(
    store: StageReservationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = store._root
    original = StagePublicationJournal._append

    def fail(journal: StagePublicationJournal, state: str, details: dict[str, object]) -> None:
        if state == "creating":
            raise OSError("fixture-only creating failure")
        original(journal, state, details)

    monkeypatch.setattr(StagePublicationJournal, "_append", fail)
    artifact = root / str(TENANT) / PLUGIN / DIGEST
    with pytest.raises(OSError):
        _publish_artifact(
            artifact,
            FILES,
            store_root=root,
            tenant=TENANT,
            plugin_id=PLUGIN,
            reservation_store=store,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            revalidate=lambda: True,
        )
    assert len(list((root / ".stage-reservations").glob("*.json"))) == 2
    assert not artifact.exists()
    assert not list(artifact.parent.glob("tmp-gh-*"))
    # Uncertain/empty publication metadata must block admission, never charge zero.
    with pytest.raises(RuntimeError):
        store.usage()
