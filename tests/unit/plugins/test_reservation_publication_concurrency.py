"""Cooperative publication locking with real filesystem/process boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from agent_hub.plugins import handover_stage_journal as journals
from agent_hub.plugins.handover_confirmation import _shared_fs_lock_at_root
from agent_hub.plugins.handover_stage_reservations import (
    ReservationPolicy,
    ReservationToken,
    StagePublicationContext,
    StageReservationStore,
)
from tests.unit.plugins.test_handover_stage_journal import private_directory

TENANT = UUID("11111111-1111-4111-8111-111111111111")
PLUGIN = "example.plugin"


def make_store(tmp_path: Path, *, watermark: int = 0) -> StageReservationStore:
    root = tmp_path / "store"
    private_directory(root)
    private_directory(root / str(TENANT))
    private_directory(root / str(TENANT) / PLUGIN)
    return StageReservationStore(root, policy(watermark))


def policy(watermark: int = 0) -> ReservationPolicy:
    return ReservationPolicy(
        max_bytes=200_000_000, max_members=50_000, max_operations=4,
        min_available_bytes=watermark,
    )


def reserve(store: StageReservationStore | StagePublicationContext) -> ReservationToken:
    operation = uuid4()
    return store.reserve(
        operation_id=operation, tenant=TENANT, plugin_id=PLUGIN,
        stage_name="tmp-gh-" + operation.hex, target_digest="b" * 64,
        source_bytes=5, source_members=2,
        deadline=datetime.now(UTC) + timedelta(seconds=30), revalidate=lambda: True,
    )


def journal(store: StageReservationStore, token: ReservationToken) -> journals.StagePublicationJournal:
    return journals.StagePublicationJournal(
        store.store_root, TENANT, PLUGIN, token.stage_name, token.target_digest,
        token.source_bytes, token.source_members, reservation=token,
    )


READER = """
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from agent_hub.plugins.handover_stage_journal import StagePublicationJournal
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
root = Path(sys.argv[1])
p = ReservationPolicy(max_bytes=200000000, max_members=50000, max_operations=4,
                      min_available_bytes=0)
store = StageReservationStore(root, p)
tenant = UUID('11111111-1111-4111-8111-111111111111')
print('ready', flush=True)
sys.stdin.readline()
if len(sys.argv) > 2 and sys.argv[2] == 'usage':
    print(store.usage().operations, flush=True)
    sys.exit(0)
operation = uuid4()
token = store.reserve(operation_id=operation, tenant=tenant, plugin_id='example.plugin',
    stage_name='tmp-gh-'+operation.hex, target_digest='c'*64, source_bytes=5, source_members=2,
    deadline=datetime.now(UTC)+timedelta(seconds=30), revalidate=lambda: True)
with StagePublicationJournal(root, tenant, 'example.plugin', token.stage_name,
        token.target_digest, 5, 2, reservation=token):
    pass
print(store.usage().operations, flush=True)
"""


@pytest.mark.parametrize("boundary", ["empty_operation", "partial_event"])
def test_process_admission_waits_for_journal_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    store = make_store(tmp_path)
    token = reserve(store)
    paused, resume = Event(), Event()
    original_append, original_write = journals.StagePublicationJournal._append, os.write

    def append(value: journals.StagePublicationJournal, state: str, details: dict[str, object]) -> None:
        if boundary == "empty_operation" and state == "creating":
            paused.set()
            assert resume.wait(10)
        original_append(value, state, details)

    def write(fd: int, data: bytes | bytearray | memoryview) -> int:
        if boundary == "partial_event" and b'"ordinal":2,' in bytes(data):
            count = original_write(fd, data[:1])
            paused.set()
            assert resume.wait(10)
            return count
        return original_write(fd, data)

    monkeypatch.setattr(journals.StagePublicationJournal, "_append", append)
    monkeypatch.setattr(os, "write", write)

    def publish() -> None:
        with journal(store, token) as value:
            value.record("recording", {})

    process = subprocess.Popen(
        [sys.executable, "-c", READER, str(store.store_root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert process.stdout.readline().strip() == b"ready"
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(publish)
            try:
                assert paused.wait(10)
                process.stdin.write(b"go\n")
                process.stdin.flush()
                reader = executor.submit(process.communicate, timeout=15)
                with pytest.raises(TimeoutError):
                    reader.result(timeout=0.3)
            finally:
                resume.set()
            writer.result(timeout=10)
            out, err = reader.result(timeout=15)
            assert process.returncode == 0, err.decode(errors="replace")
            assert out.strip() == b"2"
    finally:
        resume.set()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    assert store.usage().operations == 2


def test_bound_journal_uses_low_policy_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    monkeypatch.setattr(journals, "_available_bytes", lambda directory: 64 * 1024 * 1024)
    token = reserve(store)
    with journal(store, token) as value:
        value.record("recording", {})
    assert store.usage().operations == 1


def test_bound_journal_enforces_high_policy_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path, watermark=200 * 1024 * 1024)
    monkeypatch.setattr(journals, "_available_bytes", lambda directory: 256 * 1024 * 1024)
    token = reserve(store)
    monkeypatch.setattr(journals, "_available_bytes", lambda directory: 180 * 1024 * 1024)
    with pytest.raises(RuntimeError, match="capacity"):
        journal(store, token)
    assert store.usage().operations == 1


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows byte-range lock")
def test_windows_publication_lock_byte_does_not_block_reservation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN):
        token = reserve(store)
        store.validate_token(token)
        with journal(store, token):
            pass
    assert store.usage().operations == 1


def test_explicit_publication_admits_before_payload_parents_and_never_relocks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    private_directory(root)
    store = StageReservationStore(root, policy())
    with store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication:
        token = reserve(publication)
        assert not (root / str(TENANT)).exists()
        private_directory(root / str(TENANT))
        private_directory(root / str(TENANT) / PLUGIN)
        with journals.StagePublicationJournal(
            root, TENANT, PLUGIN, token.stage_name, token.target_digest, 5, 2,
            reservation=token, publication=publication,
        ) as value:
            value.record("ready", {})
            value.record("rename_attempted", {})
            value.record("published", {})
            publication.validate_token(token)
            assert publication.usage().operations == 1
        # Closing a journal must not release the caller's enclosing context.
        publication.validate_token(token)
        for call in (store.usage, lambda: store.validate_token(token), lambda: reserve(store)):
            with pytest.raises(RuntimeError, match="nested domain"):
                call()
        with (
            pytest.raises(RuntimeError, match="nested domain"),
            store.publication(tenant=TENANT, plugin_id=PLUGIN),
        ):
            pass
    assert store.usage().operations == 1
    with pytest.raises(RuntimeError, match="inactive"):
        publication.validate_token(token)


@pytest.mark.parametrize("foreign", ["scope", "policy", "root", "token"])
def test_explicit_publication_preserves_strict_token_binding(
    tmp_path: Path, foreign: str,
) -> None:
    from dataclasses import replace

    store = make_store(tmp_path)
    token = reserve(store)
    selected = store
    tenant, plugin = TENANT, PLUGIN
    if foreign == "scope":
        plugin = "other.plugin"
    elif foreign == "policy":
        selected = StageReservationStore(store.store_root, replace(policy(), max_operations=3))
    elif foreign == "root":
        other = tmp_path / "other"
        private_directory(other)
        selected = StageReservationStore(other, policy())
    else:
        token = replace(token, source_bytes=4)
    with (
        selected.publication(tenant=tenant, plugin_id=plugin) as publication,
        pytest.raises((ValueError, RuntimeError)),
    ):
        journals.StagePublicationJournal(
            store.store_root, TENANT, PLUGIN, token.stage_name, token.target_digest,
            token.source_bytes, token.source_members,
            reservation=token, publication=publication,
        )
    assert not (store.store_root / ".stage-ownership").exists()
    assert store.usage().operations == 1


def test_partial_event_after_writer_failure_remains_blocked(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    token = reserve(store)
    with journal(store, token) as value:
        event = value.root / "000002.json"
        fd = value._directories["operation"].open("000002.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, b"{")
            os.fsync(fd)
        finally:
            os.close(fd)
    for call in (store.usage, lambda: store.validate_token(token), lambda: reserve(store)):
        with pytest.raises(RuntimeError, match="corrupt"):
            call()
    assert event.read_bytes() == b"{"
    assert len(list((store.store_root / ".stage-reservations").glob("*.json"))) == 2


def test_unbound_journal_keeps_128_mib_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    monkeypatch.setattr(journals, "_available_bytes", lambda directory: 64 * 1024 * 1024)
    with pytest.raises(RuntimeError, match="capacity"):
        journals.StagePublicationJournal(
            store.store_root, TENANT, PLUGIN, "tmp-gh-" + uuid4().hex, "b" * 64, 5, 2,
        )
    assert not (store.store_root / ".stage-ownership").exists()


def test_context_cannot_be_used_from_another_thread(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication:
        token = reserve(publication)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(publication.validate_token, token)
            with pytest.raises(RuntimeError, match="another execution"):
                future.result(timeout=2)
        publication.validate_token(token)


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows byte-range lock")
@pytest.mark.parametrize("fault", ["oversize", "hardlink", "rebound"])
def test_windows_locked_file_still_rejects_unsafe_identity_and_size(
    tmp_path: Path, fault: str,
) -> None:
    store = make_store(tmp_path)
    lock = store.store_root / ".uninstall-locks" / str(TENANT) / (PLUGIN + ".lock")
    if fault == "hardlink" and sys.platform == "win32":
        import msvcrt

        private_directory(lock.parent.parent)
        private_directory(lock.parent)
        lock.write_bytes(b"\0")
        alias = lock.with_name("zalias")
        try:
            os.link(lock, alias)
        except PermissionError:
            pytest.skip("Windows host denies creating the hardlink fixture")
        fd = os.open(lock, os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            try:
                with pytest.raises(RuntimeError, match="alias"):
                    reserve(store)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
            alias.unlink()
        return
    with _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN):
        if fault == "oversize":
            fd = os.open(lock, os.O_WRONLY)
            try:
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, b"extra")
                with pytest.raises(RuntimeError, match="unsafe"):
                    reserve(store)
            finally:
                os.ftruncate(fd, 1)
                os.close(fd)
        else:
            # A token pins the historical lock's node, even when replacement has valid ACLs.
            token = reserve(store)
    if fault == "rebound":
        lock.rename(lock.with_name(PLUGIN + ".old"))
        with _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN):
            lock.with_name(PLUGIN + ".old").unlink()
            with pytest.raises(RuntimeError, match="historical publication lock"):
                store.validate_token(token)


def test_failed_constructor_unlocks_before_the_next_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    token = reserve(store)
    monkeypatch.setattr(journals, "_available_bytes", lambda directory: 0)
    with pytest.raises(RuntimeError, match="capacity"):
        journal(store, token)
    monkeypatch.undo()
    assert store.usage().operations == 1
    assert reserve(store).operation_id != token.operation_id


@pytest.mark.parametrize("reader_action", ["reserve", "usage"])
def test_publication_holds_domain_from_reserve_through_journal_close(
    tmp_path: Path, reader_action: str,
) -> None:
    store = make_store(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", READER, str(store.store_root), reader_action],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert process.stdout.readline().strip() == b"ready"
        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication:
                token = reserve(publication)
                process.stdin.write(b"go\n")
                process.stdin.flush()
                reader = executor.submit(process.communicate, timeout=15)
                with pytest.raises(TimeoutError):
                    reader.result(timeout=0.3)
                with journals.StagePublicationJournal(
                    store.store_root, TENANT, PLUGIN, token.stage_name, token.target_digest,
                    5, 2, reservation=token, publication=publication,
                ) as value:
                    value.record("recording", {})
                with pytest.raises(TimeoutError):
                    reader.result(timeout=0.3)
            out, err = reader.result(timeout=15)
            assert process.returncode == 0, err.decode(errors="replace")
            assert out.strip() == (b"2" if reader_action == "reserve" else b"1")
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    assert store.usage().operations == (2 if reader_action == "reserve" else 1)


def test_context_clock_regression_does_not_create_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    with store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication:
        token = reserve(publication)
        monkeypatch.setattr(journals, "_utc_now", lambda: token.created_at - timedelta(seconds=1))
        with pytest.raises(RuntimeError, match="clock precedes"):
            journals.StagePublicationJournal(
                store.store_root, TENANT, PLUGIN, token.stage_name, token.target_digest,
                5, 2, reservation=token, publication=publication,
            )
        publication.validate_token(token)
    assert not (store.store_root / ".stage-ownership").exists()
    assert store.usage().operations == 1


def test_foreign_thread_cannot_close_a_live_journal(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication:
        token = reserve(publication)
        with journals.StagePublicationJournal(
            store.store_root, TENANT, PLUGIN, token.stage_name, token.target_digest,
            5, 2, reservation=token, publication=publication,
        ) as value, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(value.close)
            with pytest.raises(RuntimeError, match="another execution"):
                future.result(timeout=2)
            value.record("recording", {})
    assert store.usage().operations == 1
