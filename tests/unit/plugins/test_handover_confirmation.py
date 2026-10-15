"""Real Ed25519/filesystem, SQL doubles only: no live PostgreSQL proof."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import TextClause
from starlette.applications import Starlette
from starlette.requests import Request

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, UserRow
from agent_hub.plugins.handover_confirmation import _PinnedDirectory
from agent_hub.plugins.handover_grants import SourceGrant
from agent_hub.plugins.handover_storage import ArtifactSource, Conflict, StorageUnavailable
from agent_hub.plugins.handover_verification import PackageVerificationError
from agent_hub.runtime.contracts import Artifact
from tests.unit.plugins import test_handover_plans as plan_tests
from tests.unit.plugins import test_handover_sources as source_tests
from tests.unit.plugins import test_handover_verification as verification_tests

source_context = plan_tests.source_context
plan_context = plan_tests.context
forbid_execution = plan_tests.forbid_execution
POSTGRESQL_DIALECT = cast(type[Dialect], postgresql.dialect)


def test_confirmation_service_contract_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_confirmation") is not None


@pytest.fixture
async def context(plan_context: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    assert importlib.util.find_spec("agent_hub.plugins.handover_confirmation") is not None
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    c = plan_context
    now = [plan_tests.NOW]
    for service in (c["service"], c["storage"], c["grants"]):
        service._clock = lambda: now[0]
    receipt = await plan_tests.create(c)
    creator = c["principal"]
    installer = AuthenticatedPrincipal(uuid4(), creator.tenant_id, Role.ADMIN)
    installer_row = UserRow(
        id=installer.user_id,
        tenant_id=installer.tenant_id,
        username="installer",
        role="admin",
        disabled=False,
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    key = AdminResourceRow(
        id=uuid4(),
        tenant_id=creator.tenant_id,
        kind="plugin_signing_key",
        resource_id="tenant-key",
        payload={
            "key_id": "tenant-key",
            "algorithm": "ed25519",
            "public_key": base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
            "trusted": True,
        },
    )
    c["db"].rows.extend([installer_row, key])
    app = Starlette()
    app.state.settings = type("Settings", (), {"plugin_package_store_dir": tmp_path / "packages"})()
    request = Request({"type": "http", "app": app})
    data = verification_tests.signed(
        private,
        json.loads(source_tests.manifest_bytes()),
        members={"main.py": source_tests.CODE},
        key_id="tenant-key",
    )
    c.update(
        confirmation=module.GeneratedPluginConfirmationService(
            storage=c["storage"],
            snapshots=c["store"],
            grants=c["grants"],
            clock=lambda: now[0],
        ),
        receipt=receipt,
        installer=installer,
        installer_row=installer_row,
        key=key,
        private=private,
        public=public,
        archive=data,
        clock=now,
        request=request,
        package_root=tmp_path / "packages",
    )
    return c


async def confirm(c: dict[str, Any], **changes: Any) -> Any:
    r = c["receipt"]
    args = {
        "principal": c["installer"],
        "run_id": r.run_id,
        "handover_id": r.handover_id,
        "expected_version": 1,
        "plan_hash": r.plan_hash,
        "confirm": True,
        "idempotency_key": "confirm-source",
        "archive_bytes": c["archive"],
        "key_id": "tenant-key",
        "request": c["request"],
    }
    args.update(changes)
    return await c["confirmation"].confirm(**args)


def resources(c: dict[str, Any]) -> list[AdminResourceRow]:
    return [r for r in c["db"].rows if isinstance(r, AdminResourceRow)]


def security(c: dict[str, Any]) -> list[tuple[str, str, Any]]:
    return deepcopy([(r.kind, r.resource_id, r.payload) for r in resources(c)])


def artifact_root(c: dict[str, Any]) -> Path:
    return (
        cast(Path, c["package_root"])
        / str(c["installer"].tenant_id)
        / str(c["receipt"].plugin_id)
        / hashlib.sha256(c["archive"]).hexdigest()
    )


async def test_actual_signed_source_atomic_disabled_registration(
    context: dict[str, Any],
    forbid_execution: None,
) -> None:
    c = context
    c["db"].statements.clear()
    result = await confirm(c)
    plugin = next(r for r in resources(c) if r.kind == "plugin")
    grant = SourceGrant.model_validate_json(
        json.dumps(next(r for r in resources(c) if r.resource_id.startswith("sg-")).payload)
    )
    assert grant.state == "consumed" and grant.receipt_id == result.receipt_id
    assert result.status == "pending" and result.installer_id == c["installer"].user_id
    assert plugin.payload["_generated_handover_id"] == result.handover_id
    assert plugin.payload["enabled"] is False
    assert plugin.payload["status"] == plugin.payload["health"] == "disabled"
    package = plugin.payload["package_metadata"]
    assert isinstance(package, dict)
    assert package["signature_verification"] == "verified"
    assert package["approval_state"] == "pending"
    assert package["activation_state"] == "blocked_pending_approval"
    assert package["approved_by"] is package["approved_at"] is None
    assert package["artifact"]["stored_at"] == c["clock"][0].isoformat().replace("+00:00", "Z")
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE
    assert len(c["sessions"]) == 2
    statements = c["db"].statements
    plan_at = next(
        i
        for i, s in enumerate(statements)
        if isinstance(s, Select)
        and str(c["db"].params(s).get("resource_id_1", "")).startswith("gh-")
    )
    reg_at = next(i for i, s in enumerate(statements) if isinstance(s, TextClause))
    plugin_at = next(
        i
        for i, s in enumerate(statements)
        if isinstance(s, Select) and c["db"].params(s).get("kind_1") == "plugin"
    )
    key_at = next(
        i
        for i, s in enumerate(statements)
        if isinstance(s, Select) and c["db"].params(s).get("kind_1") == "plugin_signing_key"
    )
    assert plan_at < reg_at < plugin_at < key_at
    for s in (statements[plan_at], statements[plugin_at], statements[key_at]):
        assert "FOR " in str(s.compile(dialect=POSTGRESQL_DIALECT()))
        assert s.get_execution_options()["populate_existing"] is True


async def test_pending_retry_checks_live_plugin_without_reinstall(context: dict[str, Any]) -> None:
    c = context
    first = await confirm(c)
    before = security(c)
    c["db"].statements.clear()
    assert await confirm(c) == first
    assert security(c) == before
    assert not any(isinstance(s, Insert) for s in c["db"].statements)


async def test_pending_retry_with_advanced_clock_is_same_receipt(context: dict[str, Any]) -> None:
    c = context
    first = await confirm(c)
    before = security(c)
    c["clock"][0] += timedelta(minutes=10)
    assert await confirm(c) == first
    assert security(c) == before


@pytest.mark.parametrize("expiry", ["plan", "key"])
async def test_expiry_after_storage_return_rolls_back_confirm(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    expiry: str,
) -> None:
    c = context
    until = c["receipt"].expires_at if expiry == "plan" else c["clock"][0] + timedelta(minutes=10)
    if expiry == "key":
        c["key"].payload = {**c["key"].payload, "not_after": until.isoformat()}
    before = security(c)
    original = c["storage"]._commit_pending_in_session
    reached = [False]

    async def late(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        reached[0] = True
        c["clock"][0] = until
        return result

    monkeypatch.setattr(c["storage"], "_commit_pending_in_session", late)
    with pytest.raises((Conflict, PackageVerificationError)):
        await confirm(c)
    assert reached[0]
    assert security(c) == before
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_key_expiry_during_stage_lock_wait_prevents_publication(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original = module._shared_fs_lock
    until = c["clock"][0] + timedelta(minutes=10)
    c["key"].payload = {**c["key"].payload, "not_after": until.isoformat()}
    before = security(c)

    @contextmanager
    def delayed(*args: Any, **kwargs: Any) -> Any:
        with original(*args, **kwargs):
            c["clock"][0] = until
            yield

    monkeypatch.setattr(module, "_shared_fs_lock", delayed)
    with pytest.raises(PackageVerificationError):
        await confirm(c)
    assert security(c) == before
    assert not artifact_root(c).exists()


def test_windows_path_and_descriptor_ctime_need_not_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    target = tmp_path / "member.bin"
    target.write_bytes(b"safe")
    original = os.fstat

    def windows_fstat(fd: int) -> os.stat_result:
        info = original(fd)
        data = {
            name: getattr(info, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_nlink",
            )
        }
        data["st_ctime_ns"] -= 100
        return cast(os.stat_result, SimpleNamespace(**data))

    monkeypatch.setattr(module.os, "fstat", windows_fstat)
    module._read_regular(target, b"safe")


@pytest.mark.parametrize("mutation", ["deleted", "marker", "hash", "version"])
async def test_pending_retry_rejects_deleted_or_rebound_plugin(
    context: dict[str, Any],
    mutation: str,
) -> None:
    c = context
    original = await confirm(c)
    plugin = next(r for r in resources(c) if r.kind == "plugin")
    if mutation == "deleted":
        c["db"].rows.remove(plugin)
    else:
        field = {
            "marker": "_generated_handover_id",
            "hash": "content_sha256",
            "version": "version",
        }[mutation]
        plugin.payload = {**plugin.payload, field: "different"}
    before = security(c)
    with pytest.raises(Conflict):
        await confirm(c)
    assert security(c) == before
    assert (
        await c["storage"].get_receipt(c["installer"], original.run_id, original.handover_id)
        == original
    )


@pytest.mark.parametrize("mutation", ["extra", "changed", "empty_dir", "hardlink"])
async def test_existing_unknown_artifact_is_never_overwritten(
    context: dict[str, Any],
    mutation: str,
) -> None:
    c = context
    root = artifact_root(c)
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(source_tests.CODE)
    if mutation == "extra":
        (root / "unknown.bin").write_bytes(b"preserve")
    elif mutation == "changed":
        (root / "main.py").write_bytes(b"preserve")
    elif mutation == "empty_dir":
        (root / "unknown").mkdir()
    else:
        shared = root.parent / "shared.bin"
        shared.write_bytes(source_tests.CODE)
        (root / "main.py").unlink()
        os.link(shared, root / "main.py")
    before = security(c)
    inode = root.stat().st_ino
    content = (root / "main.py").read_bytes()
    with pytest.raises(Conflict):
        await confirm(c)
    assert security(c) == before and root.stat().st_ino == inode
    assert (root / "main.py").read_bytes() == content


async def test_exact_shared_artifact_is_preserved(context: dict[str, Any]) -> None:
    c = context
    root = artifact_root(c)
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(source_tests.CODE)
    inode = root.stat().st_ino
    assert (await confirm(c)).status == "pending"
    assert root.stat().st_ino == inode


def publication_events(c: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = cast(Path, c["package_root"]) / ".stage-ownership"
    return [json.loads(path.read_bytes()) for path in sorted(ledger.rglob("*.json"))]


async def test_publication_records_durable_rename_fence_before_publish(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original = module._rename_noreplace
    observed: list[list[str]] = []

    def publish(parent: Any, stage: str, digest: str) -> None:
        events = publication_events(c)
        assert events, "publication fence must exist before rename"
        observed.append([event["state"] for event in events])
        assert observed[-1][-1] == "rename_attempted"
        assert events[-1]["details"]["stage"] == stage
        assert events[-1]["details"]["target"] == digest
        original(parent, stage, digest)

    monkeypatch.setattr(module, "_rename_noreplace", publish)
    assert (await confirm(c)).status == "pending"
    assert len(observed) == 1
    events = publication_events(c)
    assert events[0]["state"] == "creating"
    assert events[-1]["state"] == "published"
    assert "ready" in observed[0]
    assert events[-1]["details"]["target"] == artifact_root(c).name
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_journal_creating_intent_precedes_stage_mkdir(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original = module._PinnedDirectory.mkdir
    observed: list[str] = []

    def mkdir(directory: Any, name: str) -> None:
        if name.startswith("tmp-gh-"):
            events = publication_events(c)
            assert events and events[0]["state"] == "creating"
            observed.append(name)
        original(directory, name)

    monkeypatch.setattr(module._PinnedDirectory, "mkdir", mkdir)
    await confirm(c)
    assert len(observed) == 1


async def test_exact_shared_artifact_is_not_claimed_by_journal(context: dict[str, Any]) -> None:
    c = context
    root = artifact_root(c)
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(source_tests.CODE)
    await confirm(c)
    assert not publication_events(c)


async def test_publication_journal_contains_no_source_bytes(context: dict[str, Any]) -> None:
    c = context
    await confirm(c)
    events = publication_events(c)
    assert events, "actual generated publisher must write ownership metadata"
    for path in (cast(Path, c["package_root"]) / ".stage-ownership").rglob("*.json"):
        assert source_tests.CODE not in path.read_bytes()


@pytest.mark.parametrize("phase", ["ready", "rename_attempted"])
async def test_failed_publication_journal_fence_never_attempts_rename(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from agent_hub.plugins.handover_stage_journal import StagePublicationJournal

    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original_record = StagePublicationJournal.record
    attempts: list[str] = []
    failure = OSError("injected journal persistence failure")

    def record(journal: Any, state: str, details: dict[str, object]) -> None:
        if state == phase:
            raise failure
        original_record(journal, state, details)

    def rename(parent: Any, stage: str, digest: str) -> None:
        attempts.append(stage)

    monkeypatch.setattr(StagePublicationJournal, "record", record)
    monkeypatch.setattr(module, "_rename_noreplace", rename)
    before = security(c)
    with pytest.raises((OSError, Conflict)):
        await confirm(c)
    assert not attempts and security(c) == before
    assert not artifact_root(c).exists()
    assert any(artifact_root(c).parent.glob("tmp-gh-*"))
    assert all(event["state"] != "published" for event in publication_events(c))


async def test_sql_rollback_does_not_reclassify_published_journal(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    failure = RuntimeError("injected SQL consumption failure")
    monkeypatch.setattr(c["grants"], "consume", AsyncMock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        await confirm(c)
    assert caught.value is failure
    events = publication_events(c)
    assert events[-1]["state"] == "published"
    assert not any(event["state"] == "confirmed_aborted" for event in events)
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_rename_failure_preserves_fence_and_stage(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")

    def fail(parent: Any, stage: str, digest: str) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(module, "_rename_noreplace", fail)
    with pytest.raises((OSError, Conflict)):
        await confirm(c)
    assert publication_events(c)[-1]["state"] == "rename_attempted"
    assert not artifact_root(c).exists()
    assert any(
        path.read_bytes() == source_tests.CODE
        for path in artifact_root(c).parent.glob("tmp-gh-*/main.py")
    )


async def test_journal_wait_cannot_write_rebound_member(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.plugins.handover_stage_journal import StagePublicationJournal

    c = context
    original = StagePublicationJournal.record
    victims: list[Path] = []

    def record(journal: Any, state: str, details: dict[str, object]) -> None:
        original(journal, state, details)
        if details.get("kind") == "file_created" and details.get("path") == "main.py":
            member = next(artifact_root(c).parent.glob("tmp-gh-*/main.py"))
            victim = artifact_root(c).parent / "must-stay-empty.bin"
            os.link(member, victim)
            victims.append(victim)

    monkeypatch.setattr(StagePublicationJournal, "record", record)
    with pytest.raises(Conflict):
        await confirm(c)
    assert len(victims) == 1
    assert victims[0].read_bytes() == b""
    assert not artifact_root(c).exists()


@pytest.mark.parametrize("mutation", ["members", "permission", "signature"])
async def test_signed_archive_must_match_saved_source_before_publication(
    context: dict[str, Any],
    mutation: str,
) -> None:
    c = context
    raw = json.loads(source_tests.manifest_bytes())
    members = {"main.py": source_tests.CODE}
    if mutation == "members":
        members["main.py"] = b"changed"
    elif mutation == "permission":
        raw["capabilities"] = [{"id": "danger", "permission_class": "file:write"}]
    private = Ed25519PrivateKey.generate() if mutation == "signature" else c["private"]
    data = verification_tests.signed(private, raw, members=members, key_id="tenant-key")
    before = security(c)
    with pytest.raises(PackageVerificationError):
        await confirm(c, archive_bytes=data)
    assert security(c) == before
    assert not (c["package_root"] / str(c["installer"].tenant_id)).exists()


@pytest.mark.parametrize(
    "mutation", ["creator", "installer", "grant", "key", "run", "expiry", "key_expiry"]
)
async def test_current_authority_and_expiry_after_last_key_wait(
    context: dict[str, Any],
    mutation: str,
) -> None:
    c = context
    before = security(c)
    fired = [False]

    def after(s: Any) -> None:
        if (
            fired[0]
            or not isinstance(s, Select)
            or c["db"].params(s).get("kind_1") != "plugin_signing_key"
        ):
            return
        fired[0] = True
        if mutation in {"creator", "installer"}:
            c["user" if mutation == "creator" else "installer_row"].disabled = True
        elif mutation == "grant":
            row = next(r for r in resources(c) if r.resource_id.startswith("sg-"))
            row.payload = {
                **row.payload,
                "state": "revoked",
                "revoked_by": str(c["principal"].user_id),
                "revoked_at": plan_tests.NOW.isoformat(),
            }
        elif mutation == "key":
            c["key"].payload = {**c["key"].payload, "trusted": False}
        elif mutation == "run":
            next(r for r in c["db"].rows if type(r).__name__ == "RunRow").actor_id = uuid4()
        elif mutation == "expiry":
            c["clock"][0] = c["receipt"].expires_at
        else:
            c["key"].payload = {**c["key"].payload, "not_after": c["clock"][0].isoformat()}

    c["db"].after = after
    with pytest.raises((Conflict, PermissionDenied, StorageUnavailable, PackageVerificationError)):
        await confirm(c)
    assert fired[0] and security(c) == before


@pytest.mark.parametrize("phase", ["audit", "consume"])
async def test_sql_failure_preserves_published_bytes_and_rolls_back(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    c = context
    before = security(c)
    failure = RuntimeError("injected caller failure")
    if phase == "audit":
        original = c["db"].execute

        async def execute(statement: Any, parameters: Any = None) -> Any:
            if isinstance(statement, Insert) and c["db"].params(statement).get("kind") == "audit":
                raise failure
            return await original(statement, parameters)

        monkeypatch.setattr(c["db"], "execute", execute)
    else:
        monkeypatch.setattr(c["grants"], "consume", AsyncMock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        await confirm(c)
    assert caught.value is failure and security(c) == before
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_cross_tenant_and_dto_archive_are_rejected(context: dict[str, Any]) -> None:
    c = context
    stranger = AuthenticatedPrincipal(c["installer"].user_id, uuid4(), Role.ADMIN)
    with pytest.raises((Conflict, PermissionDenied, RuntimeError)):
        await confirm(c, principal=stranger)
    with pytest.raises(ValueError):
        await confirm(c, archive_bytes=c["receipt"])
    assert not any(r.kind in {"plugin", "audit"} for r in resources(c))


async def test_lock_open_inode_rebind_is_rejected_before_use(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    leaf = c["package_root"] / ".uninstall-locks" / str(c["installer"].tenant_id) / "example.lock"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(b"\0")
    reached = [False]

    def replace_leaf() -> None:
        reached[0] = True
        leaf.rename(leaf.with_suffix(".original"))
        leaf.write_bytes(b"\0")

    if hasattr(module, "_PinnedDirectory"):
        original = module._PinnedDirectory._native_open

        def native_open(directory: Any, name: str, flags: int) -> int:
            if name == "example.lock" and not reached[0]:
                replace_leaf()
            return cast(int, original(directory, name, flags))

        monkeypatch.setattr(module._PinnedDirectory, "_native_open", native_open)
    else:
        original_open = Path.open

        def path_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if path == leaf and mode == "a+b":
                replace_leaf()
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", path_open)
    entered = [False]
    with (
        pytest.raises(Conflict),
        module._shared_fs_lock(c["request"], c["installer"].tenant_id, "example"),
    ):
        entered[0] = True
    assert reached[0] and not entered[0]


async def test_publisher_leaf_link_race_never_writes_outside(
    context: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    victim = tmp_path / "victim.py"
    victim.write_bytes(b"do-not-touch")
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(victim)
    except OSError:
        pytest.skip("real symlinks unavailable to this Windows test account")
    probe.unlink()
    reached = [False]
    if hasattr(module, "_PinnedDirectory"):
        original = module._PinnedDirectory._native_open

        def native_open(directory: Any, name: str, flags: int) -> int:
            if name == "main.py" and flags & os.O_CREAT and not reached[0]:
                reached[0] = True
                (directory.path / name).symlink_to(victim)
            return cast(int, original(directory, name, flags))

        monkeypatch.setattr(module._PinnedDirectory, "_native_open", native_open)
    else:
        original_write = Path.write_bytes

        def write_bytes(path: Path, data: Any) -> int:
            if path.name == "main.py" and "tmp-" in str(path) and not reached[0]:
                reached[0] = True
                path.symlink_to(victim)
            return original_write(path, data)

        monkeypatch.setattr(Path, "write_bytes", write_bytes)
    before = security(c)
    with pytest.raises(Conflict):
        await confirm(c)
    assert reached[0] and victim.read_bytes() == b"do-not-touch"
    assert security(c) == before


async def test_lock_inode_rebind_after_open_is_rejected_before_use(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    leaf = c["package_root"] / ".uninstall-locks" / str(c["installer"].tenant_id) / "example.lock"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(b"\0")
    original = module._PinnedDirectory._native_open
    reached, entered = [False], [False]

    def native_open(directory: Any, name: str, flags: int) -> int:
        fd = cast(int, original(directory, name, flags))
        if name == leaf.name:
            reached[0] = True
            try:
                leaf.rename(leaf.with_suffix(".old"))
                leaf.write_bytes(b"\0")
            except BaseException:
                os.close(fd)
                raise
        return fd

    monkeypatch.setattr(module._PinnedDirectory, "_native_open", native_open)
    with (
        pytest.raises(Conflict),
        module._shared_fs_lock(c["request"], c["installer"].tenant_id, "example"),
    ):
        entered[0] = True
    assert reached[0] and not entered[0]


async def test_publisher_parent_rebind_cannot_overwrite_unknown_bytes(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original = module._PinnedDirectory._native_open
    reached, victims = [False], []

    def native_open(directory: Any, name: str, flags: int) -> int:
        if name == "main.py" and flags & os.O_CREAT and not reached[0]:
            reached[0] = True
            directory.path.rename(directory.path.with_name(directory.path.name + "-original"))
            directory.path.mkdir()
            victim = directory.path / name
            victim.write_bytes(b"do-not-touch")
            victims.append(victim)
        return cast(int, original(directory, name, flags))

    monkeypatch.setattr(module._PinnedDirectory, "_native_open", native_open)
    before = security(c)
    with pytest.raises(Conflict):
        await confirm(c)
    assert reached[0] and security(c) == before
    assert all(p.read_bytes() == b"do-not-touch" for p in victims)
    if not victims:
        # Windows non-delete-sharing handles reject the parent rename itself.
        assert any(c["package_root"].rglob("tmp-gh-*"))


async def test_atomic_publication_does_not_replace_racing_empty_canonical(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original = module._rename_noreplace
    identities: list[int] = []

    def race(parent: Any, stage: str, digest: str) -> None:
        root = parent.path / digest
        root.mkdir()
        identities.append(root.stat().st_ino)
        original(parent, stage, digest)

    monkeypatch.setattr(module, "_rename_noreplace", race)
    before = security(c)
    with pytest.raises(Conflict):
        await confirm(c)
    assert security(c) == before and artifact_root(c).stat().st_ino == identities[0]
    assert list(artifact_root(c).iterdir()) == []
    assert any(
        p.read_bytes() == source_tests.CODE
        for p in artifact_root(c).parent.glob("tmp-gh-*/main.py")
    )


async def test_artifact_source_sql_provenance_is_verified_after_key_wait(
    context: dict[str, Any],
) -> None:
    c = context
    item = Artifact(
        id=uuid4(),
        type="workspace_bundle",
        producer="unit",
        content={
            "workspace_bundle": {
                "files": {
                    "plugin.json": source_tests.manifest_bytes().decode(),
                    "main.py": source_tests.CODE.decode(),
                }
            }
        },
    )
    payload = item.to_payload()
    c["repository"].payloads = (payload,)
    row = RunArtifactRow(
        id=item.id,
        tenant_id=c["principal"].tenant_id,
        run_id=c["receipt"].run_id,
        type=item.type,
        producer=item.producer,
        content_sha256=item.content_sha256,
        payload=payload,
    )
    c["db"].rows.append(row)
    c["receipt"] = await c["service"].create_plan(
        c["principal"],
        c["receipt"].run_id,
        ArtifactSource(artifact_id=str(item.id)),
        idempotency_key="artifact-plan",
        expires_at=plan_tests.EXPIRY,
    )
    fired = [False]

    def after(statement: Any) -> None:
        if (
            isinstance(statement, Select)
            and c["db"].params(statement).get("kind_1") == "plugin_signing_key"
        ):
            fired[0] = True
            row.payload = {**row.payload, "producer": "rebound"}

    c["db"].after = after
    before = security(c)
    with pytest.raises(PermissionDenied):
        await confirm(c)
    assert fired[0] and security(c) == before
    c["db"].after = None
    assert (await confirm(c)).status == "pending"


async def test_saved_snapshot_is_used_without_fetching_as_creator(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    c["workspace"].write_bytes(
        c["principal"].tenant_id, "project-a", "session-a", "main.py", b"new-source", "text/plain"
    )
    freeze = AsyncMock(side_effect=AssertionError("confirmation must never impersonate creator"))
    monkeypatch.setattr(c["sources"], "freeze", freeze)
    assert (await confirm(c)).status == "pending"
    freeze.assert_not_called()
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_unknown_commit_result_retains_bytes_and_can_retry_receipt(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    original = c["storage"]._transaction

    @asynccontextmanager
    async def unknown() -> Any:
        async with original() as session:
            yield session
        raise RuntimeError("lost commit acknowledgement")

    monkeypatch.setattr(c["storage"], "_transaction", unknown)
    with pytest.raises(RuntimeError, match="lost commit acknowledgement"):
        await confirm(c)
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE
    before = security(c)
    monkeypatch.setattr(c["storage"], "_transaction", original)
    assert (await confirm(c)).status == "pending"
    assert security(c) == before


def test_directory_close_attempts_all_handles_after_error(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    directory = module._PinnedDirectory.__new__(module._PinnedDirectory)
    directory.handles, directory.fd = [101, 102], 103
    calls: list[int] = []
    failure = OSError("first close failed")

    def close(fd: int) -> None:
        calls.append(fd)
        if fd == 101:
            raise failure

    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(OSError) as caught:
        directory.close()
    assert caught.value is failure
    assert calls == [101, 102, 103]
    assert directory.handles == [] and directory.fd == -1
    directory.close()
    assert calls == [101, 102, 103]


def test_linux_directory_constructor_owns_new_fd_before_old_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    opens: list[int] = []
    closes: list[int] = []
    failure = OSError("old directory close failed")

    def open_fd(*args: Any, **kwargs: Any) -> int:
        fd = 100 + len(opens)
        opens.append(fd)
        return fd

    def close_fd(fd: int) -> None:
        closes.append(fd)
        if fd == 100:
            raise failure

    fake_os = SimpleNamespace(**{key: getattr(os, key) for key in dir(os)})
    fake_os.open, fake_os.close = open_fd, close_fd
    fake_os.O_DIRECTORY, fake_os.O_NOFOLLOW = 0x10000, 0x20000
    monkeypatch.setattr(module, "os", fake_os)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(OSError) as caught:
        module._PinnedDirectory(tmp_path)
    assert caught.value is failure
    assert opens == [100, 101] and closes == [100, 101]


def test_publication_syncs_member_directories_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    original, rename = module._PinnedDirectory, module._rename_noreplace
    events: list[tuple[str, Path]] = []

    class RecordingDirectory(_PinnedDirectory):
        def sync(self) -> None:
            events.append(("sync", self.path))
            original.sync(self)

    def publish(parent: Any, stage: str, digest: str) -> None:
        events.append(("rename", parent.path / stage))
        rename(parent, stage, digest)

    monkeypatch.setattr(module, "_PinnedDirectory", RecordingDirectory)
    monkeypatch.setattr(module, "_rename_noreplace", publish)
    root = tmp_path / "packages" / "digest"
    module._publish_artifact(root, (("nested/deep/member.bin", b"saved"),))
    at = next(i for i, event in enumerate(events) if event[0] == "rename")
    stage = events[at][1]
    assert events[at - 3 : at] == [
        ("sync", stage / "nested" / "deep"),
        ("sync", stage / "nested"),
        ("sync", stage),
    ]
    assert events[-1] == ("sync", root.parent)


async def test_unlock_exception_closes_descriptor_and_real_opener_relocks(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    module = importlib.import_module("agent_hub.plugins.handover_confirmation")
    target: ModuleType
    hook: Callable[..., None]
    if sys.platform == "win32":
        import msvcrt

        original = msvcrt.locking

        def locking(fd: int, mode: int, size: int) -> None:
            if mode == msvcrt.LK_UNLCK:
                raise OSError("unlock failed")
            original(fd, mode, size)

        target, name, hook = msvcrt, "locking", locking
    else:
        import fcntl

        flock = fcntl.flock

        def unlock(fd: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                raise OSError("unlock failed")
            flock(fd, operation)

        target, name, hook = fcntl, "flock", unlock
    with monkeypatch.context() as patch:
        patch.setattr(target, name, hook)
        with (
            pytest.raises(Conflict),
            module._shared_fs_lock(c["request"], c["installer"].tenant_id, "example"),
        ):
            pass
    with module._shared_fs_lock(c["request"], c["installer"].tenant_id, "example"):
        pass


async def test_consume_checks_actual_pending_grant_pair_before_frozen_helper(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    storage = importlib.import_module("agent_hub.plugins.handover_storage")
    consumed = AsyncMock(side_effect=AssertionError("mismatched pair reached frozen consume"))
    monkeypatch.setattr(c["grants"], "consume", consumed)
    reached = [False]
    before = security(c)

    def after(statement: Any) -> None:
        if not isinstance(statement, Select):
            return
        if c["db"].params(statement).get("resource_id_1") != c["receipt"].handover_id:
            return
        row = next(r for r in resources(c) if r.resource_id == c["receipt"].handover_id)
        if row.payload["status"] != "pending":
            return
        reached[0] = True
        record = storage._HandoverRecord.model_validate_json(json.dumps(row.payload)).model_copy(
            update={"source_grant_binding_sha256": "f" * 64}
        )
        fingerprint = storage._plan_fingerprint(
            record.run_id,
            record.source,
            record.source_manifest_sha256,
            record.plan_data,
            source_grant_id=record.source_grant_id,
            source_grant_binding_sha256=record.source_grant_binding_sha256,
        )
        row.payload = record.model_copy(
            update={
                "request_fingerprint": fingerprint,
                "plan_hash": storage._digest(
                    "plan",
                    {
                        "handover_id": record.handover_id,
                        "request_fingerprint": fingerprint,
                    },
                ),
            }
        ).model_dump(mode="json")

    c["db"].after = after
    with pytest.raises(Conflict, match="persisted source grant"):
        await confirm(c)
    assert reached[0] and security(c) == before
    consumed.assert_not_called()
    assert (artifact_root(c) / "main.py").read_bytes() == source_tests.CODE


async def test_directory_sync_failure_preserves_stage_without_sql_commit(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    original = _PinnedDirectory.sync
    reached = [False]

    def fail_stage(directory: _PinnedDirectory) -> None:
        if directory.path.name.startswith("tmp-gh-"):
            reached[0] = True
            raise OSError("directory sync failed")
        original(directory)

    monkeypatch.setattr(_PinnedDirectory, "sync", fail_stage)
    before = security(c)
    with pytest.raises(Conflict):
        await confirm(c)
    assert reached[0] and security(c) == before
    assert not artifact_root(c).exists()
    stages = list(artifact_root(c).parent.glob("tmp-gh-*/main.py"))
    assert len(stages) == 1 and stages[0].read_bytes() == source_tests.CODE
