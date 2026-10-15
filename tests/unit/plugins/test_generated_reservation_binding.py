"""Generated confirmation wiring with real source/crypto/filesystem, SQL doubles only."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.plugins import handover_stage_journal as journal
from agent_hub.plugins import handover_stage_reservations as reservations
from agent_hub.plugins.handover_storage import Conflict
from tests.unit.plugins.test_handover_confirmation import (
    confirm,
    plan_context,  # noqa: F401 - pytest fixture dependency
    source_context,  # noqa: F401 - pytest fixture dependency
)
from tests.unit.plugins.test_handover_confirmation import (
    context as confirmation_context,
)
from tests.unit.plugins.test_handover_stage_journal import private_directory

context = confirmation_context


async def test_generated_confirmation_charges_publication_but_not_idempotent_reuse(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    # An explicitly separated domain; the default mixed snapshot store is not covered.
    root = c["package_root"].parent / "isolated-artifacts"
    private_directory(root)
    c["request"].app.state.settings.plugin_package_store_dir = root
    store = reservations.StageReservationStore(
        root,
        reservations.ReservationPolicy(
            max_bytes=100_000_000,
            max_members=20_000,
            max_operations=1,
            min_available_bytes=0,
        ),
    )
    c["request"].app.state.plugin_publication_reservations = store
    monkeypatch.setattr(reservations, "_clock_time", lambda: c["clock"][0])
    monkeypatch.setattr(journal, "_clock_time", lambda: c["clock"][0])
    first = await confirm(c)
    assert first.status == "pending"
    usage = store.usage()
    assert usage.operations == 1
    second = await confirm(c)
    assert second == first
    assert store.usage() == usage
    assert len(list((root / ".stage-ownership").rglob("000001.json"))) == 1
    assert not list(root.rglob("tmp-gh-*"))


async def test_mixed_snapshot_store_refuses_admission_without_deleting_source(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    root = c["package_root"]
    snapshot_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    store = reservations.StageReservationStore(
        root,
        reservations.ReservationPolicy(
            max_bytes=100_000_000,
            max_members=20_000,
            max_operations=1,
            min_available_bytes=0,
        ),
    )
    c["request"].app.state.plugin_publication_reservations = store
    monkeypatch.setattr(reservations, "_clock_time", lambda: c["clock"][0])
    with pytest.raises(Conflict, match="scope admission unavailable") as refused:
        await confirm(c)
    assert isinstance(refused.value.__cause__, RuntimeError)
    assert "unaccounted" in str(refused.value.__cause__)
    assert {path: path.read_bytes() for path in snapshot_files} == snapshot_files
    assert not (root / ".stage-ownership").exists()
    assert not list(root.rglob("tmp-gh-*"))
