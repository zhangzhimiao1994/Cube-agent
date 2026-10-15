"""Transaction boundary and filesystem regressions; no live PostgreSQL is exercised."""

import hashlib
import io
import json
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request
from starlette.types import Message

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from tests.unit.plugins.test_handover_admin_writes import (
    AdminContext,
    AuditFailure,
    Payload,
    RowKey,
    SQLBoundary,
    plugin_payload,
)
from tests.unit.plugins.test_handover_admin_writes import context as context  # noqa: PLC0414
from tests.unit.plugins.test_handover_stage_journal import private_directory

ACTIONS = (
    "approve",
    "reject",
    "start",
    "enable",
    "reload",
    "stop",
    "disable",
    "preflight_failed",
    "failed_start",
    "upsert",
    "delete",
    "uninstall",
)


def filesystem_path(path: Path) -> Path:
    # Inspect the real Windows files even when a directory move crosses MAX_PATH.
    if sys.platform == "win32":
        absolute = str(path.absolute())
        if absolute.startswith("\\\\?\\"):
            return Path(absolute)
        if absolute.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + absolute[2:])
        return Path("\\\\?\\" + absolute)
    return path


def filesystem_glob(root: Path, pattern: str) -> list[Path]:
    access_root = filesystem_path(root)
    return [root / path.relative_to(access_root) for path in access_root.glob(pattern)]


async def perform(
    service: admin.PersistentAdminResourceService,
    action: str,
    tenant_id: UUID,
    actor_id: UUID,
) -> None:
    kwargs = {"tenant_id": tenant_id, "actor_id": actor_id}
    if action in {"approve", "reject"}:
        await getattr(service, action + "_plugin_package")(
            "generated-tool",
            admin.PluginPackageApprovalRequest(reason="reviewed"),
            **kwargs,
        )
        return
    if action == "upsert":
        await service.upsert_plugin(
            admin.PluginResourceRequest(id="generated-tool", name="Edited"),
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return
    if action == "preflight_failed":
        await service.mark_plugin_preflight_failed("generated-tool", **kwargs)
        return
    if action in {"start", "enable", "reload", "failed_start"}:
        preflight = AsyncMock(return_value=None)
        if action == "failed_start":
            preflight.side_effect = PublicAPIError(409, "preflight", "failed")
        await getattr(service, "start_plugin" if action == "failed_start" else action + "_plugin")(
            "generated-tool",
            activation_preflight=preflight,
            **kwargs,
        )
        return
    await getattr(service, action + "_plugin")("generated-tool", **kwargs)


def prepare(
    context: AdminContext, action: str
) -> tuple[
    admin.PersistentAdminResourceService,
    SQLBoundary,
    UUID,
    UUID,
    dict[RowKey, Payload],
    dict[RowKey, UUID],
]:
    service, db, own_tenant = context
    # Keep both the production audit writers and all SQL operations real.
    del service._record_audit
    tenant_id, actor_id = uuid4(), uuid4()
    original = plugin_payload(generated=action != "upsert", enabled=True)
    if action in {"start", "enable", "reload"}:
        original["package_metadata"]["kind"] = "manifest_only"
    if action == "stop":
        original.update(status="running", health="healthy")
    db.put(original, tenant_id=tenant_id)
    db.put(plugin_payload(), tenant_id=own_tenant)
    db.put({"history": "keep"}, kind="generated_plugin_handover", resource_id="receipt")
    return service, db, tenant_id, actor_id, deepcopy(db.rows), deepcopy(db.row_ids)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("failure", ["before", "after", "commit"])
async def test_plugin_mutation_and_audit_roll_back_together(
    context: AdminContext,
    action: str,
    failure: AuditFailure,
) -> None:
    service, db, tenant_id, actor_id, before, before_ids = prepare(context, action)
    db.fail_audit = failure
    with pytest.raises(RuntimeError, match="audit failed|commit failed"):
        await perform(service, action, tenant_id, actor_id)
    assert db.rows == before
    assert db.row_ids == before_ids
    assert any(event[0] == "audit" for event in db.events)


@pytest.mark.parametrize("action", ACTIONS)
async def test_plugin_mutation_commits_one_scoped_audit(context: AdminContext, action: str) -> None:
    service, db, tenant_id, actor_id, before, _ = prepare(context, action)
    if action == "failed_start":
        with pytest.raises(PublicAPIError) as error:
            await perform(service, action, tenant_id, actor_id)
        assert error.value.code == "preflight"
    else:
        await perform(service, action, tenant_id, actor_id)
    audits = [payload for (tenant, kind, _), payload in db.rows.items() if kind == "audit"]
    assert len(audits) == 1
    audit = audits[0]
    expected = {
        "approve": "plugin.package.approved",
        "reject": "plugin.package.rejected",
        "preflight_failed": "plugin.preflight.failed",
        "failed_start": "plugin.preflight.failed",
    }.get(action, "plugin." + action)
    assert audit["action"] == expected
    assert audit["actor"] == str(actor_id)
    assert audit["resource"] == "plugin:generated-tool"
    assert audit["details"]["id"] == "generated-tool"
    assert all(tenant == tenant_id for tenant, kind, _ in db.rows if kind == "audit")
    assert not any(key.startswith("_") for key in audit["details"])
    for key, value in before.items():
        if key != (tenant_id, "plugin", "generated-tool"):
            assert db.rows[key] == value
    if action in {"delete", "uninstall"}:
        assert (tenant_id, "plugin", "generated-tool") not in db.rows
    else:
        assert db.plugin(tenant_id=tenant_id) != before[(tenant_id, "plugin", "generated-tool")]


@pytest.mark.parametrize("changed", ["marker", "row_id", "payload"])
@pytest.mark.parametrize("active_replacement", [False, True])
@pytest.mark.parametrize("generated", [False, True])
async def test_staged_uninstall_rechecks_registration_under_lock(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    changed: str,
    active_replacement: bool,
    generated: bool,
) -> None:
    service, db, tenant_id = context
    original = plugin_payload(generated=generated)
    db.put(original)
    db.put({"history": "keep"}, kind="generated_plugin_handover", resource_id="receipt")
    monkeypatch.setattr(service, "list_plugin_signing_keys", AsyncMock(return_value=()))
    monkeypatch.setattr(
        service,
        "_list_admin_payloads",
        AsyncMock(side_effect=lambda *args, **kwargs: [db.plugin()]),
    )
    # Retain the full pytest parent path, including long full-suite basetemp names.
    store_root = tmp_path / ("long-parent-" + "p" * max(0, 100 - len(str(tmp_path)) - 13))
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(plugin_package_store_dir=store_root),
                )
            ),
        }
    )
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = store_root / str(tenant_id) / "generated-tool"
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    new_file = old_file
    replacement = deepcopy(original)
    row_id = db.row_ids[(tenant_id, "plugin", "generated-tool")]
    if changed == "marker":
        replacement["_generated_handover_id"] = "gh-" + "4" * 64
    elif changed == "row_id":
        row_id = uuid4()
    else:
        replacement["content_sha256"] = "3" * 64
    old_plugin, _ = await service._plugin_for_uninstall("generated-tool", tenant_id=tenant_id)

    def replace_registration() -> None:
        # B owns the registration lock first; A must not stage anything before acquiring it.
        assert filesystem_path(old_file).read_bytes() == b"old P"
        admin._stage_plugin_package_uninstall(request, old_plugin, tenant_id=tenant_id)
        assert not filesystem_path(old_file).exists()
        db.put(replacement, row_id=row_id)
        if active_replacement:
            filesystem_path(new_file.parent).mkdir(parents=True)
            filesystem_path(new_file).write_bytes(b"new Q")

    db.on_lock = replace_registration
    reload_runtime = AsyncMock()
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", reload_runtime)
    pending = store_root / ".uninstalling" / str(tenant_id) / "generated-tool"
    rename = Path.rename
    failed_backup_renames: list[Path] = []

    def fail_backup_rename(path: Path, target: Path) -> Path:
        if path == pending:
            failed_backup_renames.append(path)
            raise OSError("injected conflict backup rename failure")
        return rename(path, target)

    with pytest.raises(PublicAPIError) as error:
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert error.value.status_code == 409
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == row_id
    assert db.rows[(tenant_id, "generated_plugin_handover", "receipt")] == {"history": "keep"}
    assert not any(event[0] in {"delete", "audit"} for event in db.events)
    reload_runtime.assert_not_awaited()
    assert (
        filesystem_path(pending / "artifact" / ("1" * 64) / "adapter.py").read_bytes() == b"old P"
    )
    staged = admin._pending_plugin_package_uninstall_state(
        request, tenant_id=tenant_id, plugin_id="generated-tool"
    )
    assert staged is not None
    with pytest.raises(ValueError, match="identity mismatch"):
        admin._restore_conflicted_plugin_package_uninstall(
            request, staged, tenant_id=uuid4(), plugin_id="generated-tool"
        )
    db.on_lock = None
    # A failed migration must be retryable through the normal API, not a private helper.
    with monkeypatch.context() as retry_fault:
        retry_fault.setattr(Path, "rename", fail_backup_rename)
        with pytest.raises(PublicAPIError) as retry_error:
            await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert retry_error.value.status_code == 503
    assert failed_backup_renames == [pending]
    assert db.plugin() == replacement
    if active_replacement:
        assert filesystem_path(new_file).read_bytes() == b"new Q"
    else:
        assert not filesystem_path(package_root).exists()
    assert (
        filesystem_path(pending / "artifact" / ("1" * 64) / "adapter.py").read_bytes() == b"old P"
    )
    assert (
        f"conflict backup pending tenant_id={tenant_id} plugin_id=generated-tool "
        f"operation_id={staged.operation_id} error_type=OSError"
    ) in caplog.text
    assert not filesystem_glob(store_root / ".uninstalling", "c-*")
    del service._record_audit
    response = await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert response.status == "uninstalled"
    assert not filesystem_path(new_file).exists()
    backups = filesystem_glob(store_root / ".uninstalling", "c-*/p")
    assert len(backups) == 1
    saved_p = backups[0] / "artifact" / ("1" * 64) / "adapter.py"
    assert len(str(saved_p)) <= len(str(pending / "artifact" / ("1" * 64) / "adapter.py"))
    assert len(str(backups[0].relative_to(store_root / ".uninstalling"))) <= 14
    assert filesystem_path(saved_p).read_bytes() == b"old P"
    state_path = backups[0] / "state.json"
    assert admin._read_plugin_uninstall_state(
        state_path, tenant_id=tenant_id, plugin_id="generated-tool"
    ) == (staged.operation_id, staged.plugin_snapshot_sha256, staged.created_at)
    with pytest.raises(ValueError, match="identity mismatch"):
        admin._read_plugin_uninstall_state(
            state_path, tenant_id=uuid4(), plugin_id="generated-tool"
        )
    assert not filesystem_path(pending).exists()
    assert db.rows[(tenant_id, "generated_plugin_handover", "receipt")] == {"history": "keep"}


def test_uninstall_finishes_real_staged_files_beyond_windows_max_path(tmp_path: Path) -> None:
    store_root = tmp_path / ("long-parent-" + "p" * max(0, 128 - len(str(tmp_path)) - 13))
    request = package_request(store_root)
    tenant_id = uuid4()
    plugin = admin._plugin_response_from_payload(plugin_payload())
    package_root = store_root / str(tenant_id) / plugin.id
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    state = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    assert state.staged_artifact_root is not None
    staged_file = state.staged_artifact_root / ("1" * 64) / "adapter.py"
    assert len(str(staged_file)) > 260
    assert filesystem_path(staged_file).read_bytes() == b"old P"
    assert not filesystem_path(old_file).exists()
    admin._finish_plugin_package_uninstall(request, state)
    assert not filesystem_path(state.pending_root).exists()
    assert not filesystem_path(package_root).exists()


async def test_delete_empty_snapshot_cannot_remove_new_registration(
    context: AdminContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, _ = context
    read_payload = service._get_admin_payload

    async def read_then_register(
        kind: str,
        resource_id: str,
        *,
        tenant_id: UUID | None = None,
    ) -> dict[str, object] | None:
        observed = await read_payload(kind, resource_id, tenant_id=tenant_id)
        db.put(plugin_payload())
        return observed

    monkeypatch.setattr(service, "_get_admin_payload", read_then_register)
    with pytest.raises(KeyError):
        await service.delete_plugin("generated-tool")
    assert db.plugin()["_generated_handover_id"] == "gh-" + "2" * 64
    assert not any(event[0] in {"lock", "delete", "audit"} for event in db.events)


@pytest.mark.parametrize("failure", [None, "before", "after", "commit"])
async def test_uninstall_route_audit_failure_restores_package(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: AuditFailure | None,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    db.put(plugin_payload())
    db.put({"history": "keep"}, kind="generated_plugin_handover", resource_id="receipt")
    before = deepcopy(db.rows)
    db.fail_audit = failure

    async def list_payloads(kind: str, *, tenant_id: UUID | None = None) -> list[dict[str, object]]:
        return [
            value
            for (tenant, row_kind, _), value in db.rows.items()
            if tenant == (db.tenant_id if tenant_id is None else tenant_id) and row_kind == kind
        ]

    monkeypatch.setattr(service, "_list_admin_payloads", list_payloads)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(plugin_package_store_dir=tmp_path),
                )
            ),
        }
    )
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    old_file = tmp_path / str(tenant_id) / "generated-tool" / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    reload_runtime = AsyncMock()
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", reload_runtime)
    if failure:
        with pytest.raises(RuntimeError, match="audit failed|commit failed"):
            await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert db.rows == before
        assert filesystem_path(old_file).read_bytes() == b"old P"
        reload_runtime.assert_not_awaited()
    else:
        response = await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert response.status == "uninstalled"
        assert response.cleanup.package_artifact_removed is True
        assert (tenant_id, "plugin", "generated-tool") not in db.rows
        assert not filesystem_path(old_file).exists()
        audits = [payload for (_, kind, _), payload in db.rows.items() if kind == "audit"]
        assert len(audits) == 1
        assert audits[0]["action"] == "plugin.uninstall"
        assert audits[0]["actor"] == str(principal.user_id)
        reload_runtime.assert_awaited_once()
    assert db.rows[(tenant_id, "generated_plugin_handover", "receipt")] == {"history": "keep"}
    assert not filesystem_path(
        tmp_path / ".uninstalling" / str(tenant_id) / "generated-tool"
    ).exists()


async def test_old_uninstall_does_not_restore_a_new_operations_staged_package(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, tenant_id = context
    db.put(plugin_payload())
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(plugin_package_store_dir=tmp_path),
                )
            ),
        }
    )
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    staged: list[admin._PluginPackageUninstallState] = []
    stage = admin._stage_plugin_package_uninstall

    def capture_stage(
        request: Request,
        plugin: admin.PluginResourceResponse,
        *,
        tenant_id: UUID,
    ) -> admin._PluginPackageUninstallState:
        state = stage(request, plugin, tenant_id=tenant_id)
        staged.append(state)
        return state

    monkeypatch.setattr(admin, "_stage_plugin_package_uninstall", capture_stage)
    replacement = plugin_payload()
    replacement["_generated_handover_id"] = "gh-" + "4" * 64

    def delete_p_and_stage_q() -> None:
        staged.append(
            stage(request, admin._plugin_response_from_payload(db.plugin()), tenant_id=tenant_id)
        )
        admin._finish_plugin_package_uninstall(request, staged[0])
        db.put(replacement, row_id=uuid4())
        new_file = package_root / ("1" * 64) / "adapter.py"
        filesystem_path(new_file.parent).mkdir(parents=True)
        filesystem_path(new_file).write_bytes(b"new Q")
        staged.append(
            stage(
                request,
                admin._plugin_response_from_payload(replacement),
                tenant_id=tenant_id,
            )
        )

    db.on_lock = delete_p_and_stage_q
    with pytest.raises(PublicAPIError) as error:
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert error.value.status_code == 409
    assert db.plugin() == replacement
    assert staged[0].operation_id != staged[1].operation_id
    q_state = staged[1]
    assert (
        filesystem_path(q_state.pending_root / "artifact" / ("1" * 64) / "adapter.py").read_bytes()
        == b"new Q"
    )
    assert json.loads(filesystem_path(q_state.pending_root / "state.json").read_text("utf-8"))[
        "operation_id"
    ] == str(
        q_state.operation_id,
    )
    assert not filesystem_path(package_root).exists()
    assert not any(event[0] in {"delete", "audit"} for event in db.events)


def package_request(store_root: Path) -> Request:
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(plugin_package_store_dir=store_root))
            ),
        }
    )


@pytest.mark.parametrize("changed", ["operation_id", "plugin_snapshot_sha256", "created_at"])
def test_old_finish_cannot_delete_new_pending_identity(tmp_path: Path, changed: str) -> None:
    tenant_id = uuid4()
    request = package_request(tmp_path)
    plugin = admin._plugin_response_from_payload(plugin_payload())
    package_root = tmp_path / str(tenant_id) / plugin.id
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"old P")
    old = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    admin._finish_plugin_package_uninstall(request, old)
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"new Q")
    new = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    identity = replace(
        new,
        operation_id=old.operation_id,
        plugin_snapshot_sha256=old.plugin_snapshot_sha256,
        created_at=old.created_at,
    )
    if changed == "operation_id":
        identity = replace(identity, operation_id=uuid4())
    elif changed == "plugin_snapshot_sha256":
        identity = replace(identity, plugin_snapshot_sha256="a" * 64)
    else:
        identity = replace(identity, created_at=old.created_at + timedelta(seconds=1))
    state_path = new.pending_root / "state.json"
    filesystem_path(state_path).write_text(
        json.dumps(
            admin._plugin_uninstall_state_payload(
                tenant_id=tenant_id,
                plugin_id=plugin.id,
                operation_id=identity.operation_id,
                plugin_snapshot_sha256=identity.plugin_snapshot_sha256,
                created_at=identity.created_at,
            )
        ),
        encoding="utf-8",
    )
    before = filesystem_path(state_path).read_bytes()
    admin._finish_plugin_package_uninstall(request, old)
    assert filesystem_path(state_path).read_bytes() == before
    assert filesystem_path(new.pending_root / "artifact" / "adapter.py").read_bytes() == b"new Q"


async def test_delayed_route_finish_cannot_consume_q_staged_by_next_operation(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    db.put(plugin_payload())
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"old P")
    staged_q: list[admin._PluginPackageUninstallState] = []
    replacement = plugin_payload()
    q_id = uuid4()
    reloads = 0

    async def reload_and_replace(_request: Request, _tenant: UUID) -> None:
        nonlocal reloads
        reloads += 1
        if reloads != 1:
            return
        retry = await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert retry.status == "uninstalled"
        db.put(replacement, row_id=q_id)
        filesystem_path(package_root).mkdir(parents=True)
        filesystem_path(package_root / "adapter.py").write_bytes(b"new Q")
        plugin, _ = await service._plugin_for_uninstall("generated-tool", tenant_id=tenant_id)
        staged_q.append(admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id))

    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", reload_and_replace)
    response = await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert response.status == "uninstalled"
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    assert (
        filesystem_path(staged_q[0].pending_root / "artifact" / "adapter.py").read_bytes()
        == b"new Q"
    )
    assert json.loads(filesystem_path(staged_q[0].pending_root / "state.json").read_text("utf-8"))[
        "operation_id"
    ] == str(staged_q[0].operation_id)


@pytest.mark.parametrize("generated", [False, True])
async def test_archive_cas_conflict_preserves_artifact_reused_by_q(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generated: bool,
) -> None:
    service, db, tenant_id = context
    db.put(plugin_payload(generated=False))
    store_root = tmp_path / "packages"
    private_directory(store_root)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.json", json.dumps({"id": "generated-tool", "name": "Installed"}))
        package.writestr("adapter.py", b"shared artifact")
    body = archive.getvalue()
    sha = hashlib.sha256(body).hexdigest()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-agent-hub-plugin-filename", b"plugin.zip")],
            "app": package_request(store_root).app,
        },
        receive,
    )
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    # Only cryptographic verification is supplied here; extraction and CAS use production code.
    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    monkeypatch.setattr(admin, "_verified_plugin_package_metadata", lambda *args: verified)
    monkeypatch.setattr(service, "list_plugin_signing_keys", AsyncMock(return_value=()))
    artifact = store_root / str(tenant_id) / "generated-tool" / sha / "adapter.py"
    replacement = plugin_payload(generated=generated)
    replacement["content_sha256"] = sha
    replacement["package_metadata"]["artifact"] = admin.PluginPackageArtifactMetadata(
        storage_key=f"{tenant_id}/generated-tool/{sha}",
        content_sha256=sha,
        file_count=2,
        total_size_bytes=len(body),
        stored_at=datetime.now(UTC),
        quarantine_state="stored",
    ).model_dump(mode="json")
    q_id = uuid4()

    def register_q() -> None:
        assert filesystem_path(artifact).read_bytes() == b"shared artifact"
        db.put(replacement, row_id=q_id)

    db.on_lock = register_q
    with pytest.raises(PublicAPIError) as error:
        await admin.install_plugin_archive(request, principal, service)
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    assert filesystem_path(artifact).read_bytes() == b"shared artifact"
    assert not any(event[0] in {"write", "audit"} for event in db.events)


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("changed", ["row_id", "marker"])
async def test_api_retry_preserves_old_pending_even_when_q_has_no_active_files(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generated: bool,
    changed: str,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    original = plugin_payload(generated=generated)
    db.put(original)
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    plugin, _ = await service._plugin_for_uninstall("generated-tool", tenant_id=tenant_id)
    assert plugin.model_dump(mode="json") == admin._plugin_response_from_payload(
        original
    ).model_dump(mode="json")
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"old P")
    old = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    replacement = deepcopy(original)
    q_id = db.row_ids[(tenant_id, "plugin", "generated-tool")]
    if changed == "row_id":
        q_id = uuid4()
    else:
        replacement["_generated_handover_id"] = "gh-" + "4" * 64
    db.put(replacement, row_id=q_id)
    assert not filesystem_path(package_root).exists()
    rename = Path.rename

    def fail_backup(path: Path, target: Path) -> Path:
        if path == old.pending_root:
            raise OSError("temporary rename failure")
        return rename(path, target)

    with monkeypatch.context() as failure:
        failure.setattr(Path, "rename", fail_backup)
        with pytest.raises(PublicAPIError) as error:
            await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert error.value.status_code == 503
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    assert filesystem_path(old.pending_root / "artifact" / "adapter.py").read_bytes() == b"old P"
    assert not filesystem_path(package_root).exists()
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", AsyncMock())
    response = await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert response.status == "uninstalled"
    assert (tenant_id, "plugin", "generated-tool") not in db.rows
    backups = filesystem_glob(tmp_path / ".uninstalling", "c-*/p/state.json")
    assert len(backups) == 1
    assert filesystem_path(backups[0].parent / "artifact" / "adapter.py").read_bytes() == b"old P"
    identity = admin._read_plugin_uninstall_state(
        backups[0], tenant_id=tenant_id, plugin_id="generated-tool"
    )
    assert identity == (old.operation_id, old.plugin_snapshot_sha256, old.created_at)


async def test_audit_rollback_probe_window_cannot_restore_next_operations_q(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    original = plugin_payload()
    db.put(original)
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"old P")
    db.fail_audit = "after"
    replacement = plugin_payload()
    q_id = uuid4()
    staged_q: list[admin._PluginPackageUninstallState] = []
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", AsyncMock())

    async def retry_p_then_stage_q(
        _service: admin.AdminResourceService,
        _plugin_id: str,
        *,
        tenant_id: UUID,
    ) -> admin.PluginResourceResponse:
        assert db.plugin() == original
        assert not any(kind == "audit" for _, kind, _ in db.rows)
        db.fail_audit = None
        retried = await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert retried.status == "uninstalled"
        db.put(replacement, row_id=q_id)
        filesystem_path(package_root).mkdir(parents=True)
        filesystem_path(package_root / "adapter.py").write_bytes(b"new Q")
        plugin, _ = await service._plugin_for_uninstall("generated-tool", tenant_id=tenant_id)
        staged_q.append(admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id))
        return plugin

    monkeypatch.setattr(admin, "_current_plugin_for_activation_check", retry_p_then_stage_q)
    with pytest.raises(RuntimeError, match="audit failed"):
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    state = staged_q[0]
    assert filesystem_path(state.pending_root / "artifact" / "adapter.py").read_bytes() == b"new Q"
    assert admin._read_plugin_uninstall_state(
        state.pending_root / "state.json", tenant_id=tenant_id, plugin_id="generated-tool"
    ) == (state.operation_id, state.plugin_snapshot_sha256, state.created_at)
    assert not filesystem_path(package_root).exists()


def archive_install_request(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Request, AuthenticatedPrincipal, Path]:
    service, _, tenant_id = context
    store_root = tmp_path / "packages"
    private_directory(store_root)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.json", json.dumps({"id": "generated-tool", "name": "Installed"}))
        package.writestr("adapter.py", b"new Q")
    body = archive.getvalue()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-agent-hub-plugin-filename", b"plugin.zip")],
            "app": package_request(store_root).app,
        },
        receive,
    )
    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    monkeypatch.setattr(admin, "_verified_plugin_package_metadata", lambda *args: verified)
    monkeypatch.setattr(service, "list_plugin_signing_keys", AsyncMock(return_value=()))
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    artifact = (
        store_root
        / str(tenant_id)
        / "generated-tool"
        / hashlib.sha256(body).hexdigest()
        / "adapter.py"
    )
    return request, principal, artifact


async def test_install_cannot_rebaseline_after_uninstall_consumes_extracted_files(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    db.put(plugin_payload(generated=False))
    request, principal, artifact = archive_install_request(context, tmp_path, monkeypatch)
    old_file = artifact.parent.parent / "old.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    write = service.upsert_plugin
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", AsyncMock())

    async def uninstall_between_extract_and_write(
        plugin: admin.PluginResourceRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        source_filename: str | None = None,
        content_sha256: str | None = None,
        package_metadata: admin.PluginPackageMetadata | None = None,
    ) -> admin.PluginResourceResponse:
        assert filesystem_path(artifact).read_bytes() == b"new Q"
        removed = await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert removed.status == "uninstalled"
        assert (principal.tenant_id, "plugin", "generated-tool") not in db.rows
        assert not filesystem_path(artifact).exists()
        return await write(
            plugin,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
        )

    monkeypatch.setattr(service, "upsert_plugin", uninstall_between_extract_and_write)
    with pytest.raises(PublicAPIError) as error:
        await admin.install_plugin_archive(request, principal, service)
    assert error.value.code == "plugin_registration_conflict"
    assert (tenant_id, "plugin", "generated-tool") not in db.rows
    assert not filesystem_path(artifact).exists()


@pytest.mark.parametrize("baseline", ["absent", "absent_none", "row_id", "payload"])
async def test_install_binds_pre_extract_absence_uuid_and_payload(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    baseline: str,
) -> None:
    service, db, tenant_id = context
    original = plugin_payload(generated=False)
    if baseline in {"row_id", "payload"}:
        db.put(original)
    read = service._get_admin_payload
    if baseline == "absent_none":

        async def absent_read(
            kind: str, resource_id: str, *, tenant_id: UUID | None = None
        ) -> dict[str, object] | None:
            if not db.rows:
                return None
            return await read(kind, resource_id, tenant_id=tenant_id)

        monkeypatch.setattr(service, "_get_admin_payload", absent_read)
    request, principal, artifact = archive_install_request(context, tmp_path, monkeypatch)
    stored = admin._stored_plugin_package_metadata
    replacement = deepcopy(original)
    q_id = db.row_ids.get((tenant_id, "plugin", "generated-tool"), uuid4())
    if baseline == "row_id":
        q_id = uuid4()
    elif baseline == "payload":
        replacement["name"] = "Changed in the same row"

    def store_then_register(
        request: Request,
        *,
        tenant_id: UUID,
        plugin_id: str,
        package: admin.PluginPackageMetadata | None,
        archive_bytes: bytes,
        content_sha256: str,
    ) -> admin._StoredPluginPackageMetadata:
        result = stored(
            request,
            tenant_id=tenant_id,
            plugin_id=plugin_id,
            package=package,
            archive_bytes=archive_bytes,
            content_sha256=content_sha256,
        )
        assert filesystem_path(artifact).read_bytes() == b"new Q"
        db.put(replacement, row_id=q_id)
        return result

    monkeypatch.setattr(admin, "_stored_plugin_package_metadata", store_then_register)
    with pytest.raises(PublicAPIError) as error:
        await admin.install_plugin_archive(request, principal, service)
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    assert filesystem_path(artifact).read_bytes() == b"new Q"
    assert not any(event[0] in {"write", "audit"} for event in db.events)


async def test_absent_registration_aba_never_commits_staged_archive(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    request, principal, artifact = archive_install_request(context, tmp_path, monkeypatch)
    write = service.upsert_plugin
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config_strict", AsyncMock())

    async def create_and_remove_between_publish_and_cas(
        plugin: admin.PluginResourceRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        source_filename: str | None = None,
        content_sha256: str | None = None,
        package_metadata: admin.PluginPackageMetadata | None = None,
    ) -> admin.PluginResourceResponse:
        assert filesystem_path(artifact).read_bytes() == b"new Q"
        await write(
            admin.PluginResourceRequest(id="generated-tool", name="Intervening P"),
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        removed = await admin.uninstall_plugin("generated-tool", request, principal, service)
        assert removed.status == "uninstalled"
        assert not filesystem_path(artifact).exists()
        return await write(
            plugin,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
        )

    monkeypatch.setattr(service, "upsert_plugin", create_and_remove_between_publish_and_cas)
    with pytest.raises(PublicAPIError) as error:
        await admin.install_plugin_archive(request, principal, service)
    assert error.value.code == "plugin_registration_conflict"
    assert (tenant_id, "plugin", "generated-tool") not in db.rows
    assert not filesystem_path(artifact).exists()
    assert not any(
        payload.get("action") == "plugin.install"
        for (_, kind, _), payload in db.rows.items()
        if kind == "audit"
    )


@pytest.mark.parametrize(
    "changed", ["operation_id", "plugin_snapshot_sha256", "created_at", "absent"]
)
def test_delayed_audit_marker_never_marks_next_operation(tmp_path: Path, changed: str) -> None:
    tenant_id = uuid4()
    request = package_request(tmp_path)
    plugin = admin._plugin_response_from_payload(plugin_payload())
    package_root = tmp_path / str(tenant_id) / plugin.id
    filesystem_path(package_root).mkdir(parents=True)
    filesystem_path(package_root / "adapter.py").write_bytes(b"old P")
    old = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    admin._finish_plugin_package_uninstall(request, old)
    if changed != "absent":
        filesystem_path(package_root).mkdir(parents=True)
        filesystem_path(package_root / "adapter.py").write_bytes(b"new Q")
        new = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
        identity = replace(
            new,
            operation_id=old.operation_id,
            plugin_snapshot_sha256=old.plugin_snapshot_sha256,
            created_at=old.created_at,
        )
        if changed == "operation_id":
            identity = replace(identity, operation_id=uuid4())
        elif changed == "plugin_snapshot_sha256":
            identity = replace(identity, plugin_snapshot_sha256="a" * 64)
        else:
            identity = replace(identity, created_at=old.created_at + timedelta(seconds=1))
        filesystem_path(new.pending_root / "state.json").write_text(
            json.dumps(
                admin._plugin_uninstall_state_payload(
                    tenant_id=tenant_id,
                    plugin_id=plugin.id,
                    operation_id=identity.operation_id,
                    plugin_snapshot_sha256=identity.plugin_snapshot_sha256,
                    created_at=identity.created_at,
                )
            ),
            encoding="utf-8",
        )
    admin._mark_plugin_uninstall_audit_confirmed(old)
    assert not filesystem_path(old.audit_confirmed_path).exists()
    if changed != "absent":
        assert (
            filesystem_path(old.pending_root / "artifact" / "adapter.py").read_bytes() == b"new Q"
        )


@pytest.mark.parametrize("q_files", ["absent", "same_sha", "different_sha"])
async def test_ordinary_rollback_restore_never_publishes_p_over_winning_q(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    q_files: str,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    db.put(plugin_payload())
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"old P")
    db.fail_audit = "after"
    replacement = plugin_payload()
    q_id = uuid4()
    q_sha = "1" * 64 if q_files == "same_sha" else "3" * 64
    q_file = package_root / q_sha / "adapter.py"

    async def replace_in_probe(
        _service: admin.AdminResourceService,
        _id: str,
        *,
        tenant_id: UUID,
    ) -> admin.PluginResourceResponse:
        db.fail_audit = None
        db.put(replacement, row_id=q_id)
        if q_files != "absent":
            filesystem_path(q_file.parent).mkdir(parents=True)
            filesystem_path(q_file).write_bytes(b"new Q")
        return admin._plugin_response_from_payload(replacement)

    monkeypatch.setattr(admin, "_current_plugin_for_activation_check", replace_in_probe)
    with pytest.raises(RuntimeError, match="audit failed"):
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert db.plugin() == replacement
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == q_id
    if q_files == "absent":
        assert not filesystem_path(package_root).exists()
    else:
        assert filesystem_path(q_file).read_bytes() == b"new Q"
    backups = filesystem_glob(tmp_path / ".uninstalling", "c-*/p")
    assert len(backups) == 1
    assert (
        filesystem_path(backups[0] / "artifact" / ("1" * 64) / "adapter.py").read_bytes()
        == b"old P"
    )


@pytest.mark.parametrize("q_sha", ["1" * 64, "3" * 64])
async def test_audit_rollback_restores_registered_p_over_uncommitted_q_files(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    q_sha: str,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    original = plugin_payload()
    db.put(original)
    row_id = db.row_ids[(tenant_id, "plugin", "generated-tool")]
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"registered P")
    db.fail_audit = "after"

    async def publish_uncommitted_q(
        _service: admin.AdminResourceService,
        _id: str,
        *,
        tenant_id: UUID,
    ) -> admin.PluginResourceResponse:
        assert db.plugin() == original
        assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == row_id
        uncommitted = package_root / q_sha / "adapter.py"
        filesystem_path(uncommitted.parent).mkdir(parents=True)
        filesystem_path(uncommitted).write_bytes(b"uncommitted Q")
        return admin._plugin_response_from_payload(original)

    monkeypatch.setattr(admin, "_current_plugin_for_activation_check", publish_uncommitted_q)
    with pytest.raises(RuntimeError, match="audit failed"):
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert db.plugin() == original
    assert db.row_ids[(tenant_id, "plugin", "generated-tool")] == row_id
    assert filesystem_path(old_file).read_bytes() == b"registered P"
    assert not filesystem_glob(tmp_path / ".uninstalling", "*/generated-tool/state.json")
    assert any(
        filesystem_path(path).read_bytes() == b"uncommitted Q"
        for path in filesystem_glob(tmp_path / ".uninstalling", "**/adapter.py")
    )


@pytest.mark.parametrize("failure", ["displace_q", "restore_p"])
async def test_registered_p_restore_rename_failure_preserves_both_and_retries(
    context: AdminContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, db, tenant_id = context
    del service._record_audit
    original = plugin_payload()
    db.put(original)
    request = package_request(tmp_path)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    package_root = tmp_path / str(tenant_id) / "generated-tool"
    old_file = package_root / ("1" * 64) / "adapter.py"
    filesystem_path(old_file.parent).mkdir(parents=True)
    filesystem_path(old_file).write_bytes(b"registered P")
    db.fail_audit = "after"
    original_replace = Path.replace
    armed = True

    async def publish_uncommitted_q(
        _service: admin.AdminResourceService,
        _id: str,
        *,
        tenant_id: UUID,
    ) -> admin.PluginResourceResponse:
        state = admin._pending_plugin_package_uninstall_state(
            request,
            tenant_id=tenant_id,
            plugin_id="generated-tool",
        )
        assert state is not None
        q_file = package_root / ("3" * 64) / "adapter.py"
        filesystem_path(q_file.parent).mkdir(parents=True)
        filesystem_path(q_file).write_bytes(b"uncommitted Q")

        def fail_once(path: Path, target: str | Path) -> Path:
            nonlocal armed
            selected = package_root if failure == "displace_q" else state.staged_artifact_root
            if armed and path == selected:
                armed = False
                raise OSError("injected restore rename failure")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_once)
        return admin._plugin_response_from_payload(original)

    monkeypatch.setattr(admin, "_current_plugin_for_activation_check", publish_uncommitted_q)
    with pytest.raises(PublicAPIError) as raised:
        await admin.uninstall_plugin("generated-tool", request, principal, service)
    assert raised.value.code == "plugin_uninstall_cleanup_pending"
    assert db.plugin() == original
    state = admin._pending_plugin_package_uninstall_state(
        request,
        tenant_id=tenant_id,
        plugin_id="generated-tool",
    )
    assert state is not None and state.staged_artifact_root is not None
    assert (
        filesystem_path(state.staged_artifact_root / ("1" * 64) / "adapter.py").read_bytes()
        == b"registered P"
    )
    assert any(
        filesystem_path(path).read_bytes() == b"uncommitted Q"
        for path in filesystem_glob(tmp_path, "**/adapter.py")
    )
    _, snapshot = await service._plugin_for_uninstall("generated-tool", tenant_id=tenant_id)
    assert snapshot is not None
    await service._restore_plugin_uninstall(
        request,
        state,
        tenant_id=tenant_id,
        plugin_id="generated-tool",
        snapshot=snapshot,
    )
    assert filesystem_path(old_file).read_bytes() == b"registered P"
    assert not filesystem_path(state.pending_root).exists()
    assert any(
        filesystem_path(path).read_bytes() == b"uncommitted Q"
        for path in filesystem_glob(tmp_path / ".uninstalling", "**/adapter.py")
    )
