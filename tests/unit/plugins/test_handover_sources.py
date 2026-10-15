"""Real file-store tests; repository doubles do not prove route/DB integration."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.domain.runs import RunStatus
from agent_hub.files.generated import ZIP_MIME_TYPE, GeneratedFileStore
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.plugins.handover_sources import (
    HandoverSourceService,
    InvalidSource,
    SourceChanged,
    SourceLimits,
    SourceTooLarge,
)
from agent_hub.plugins.handover_storage import ArtifactSource, AuthorizedSource, WorkspaceSource
from agent_hub.runs.repository import RunNotFound, RunRecord


class SourceRepository:
    """Only the existing RunRepository.get/raw_artifacts interface is doubled."""

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        self.payloads: tuple[dict[str, object], ...] = ()
        self.cancel = False

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        if self.cancel:
            raise asyncio.CancelledError
        if (tenant_id, run_id) != (self.record.tenant_id, self.record.id):
            raise RunNotFound("not found")
        return self.record

    async def raw_artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        await self.get(tenant_id, run_id)
        return self.payloads


def manifest_bytes(**package_overrides: object) -> bytes:
    package: dict[str, object] = {
        "kind": "adapter_package",
        "package_version": "1.0.0",
        "adapter_id": "example",
        "sdk_api_version": "1.0",
        "runtime": "python",
        "entrypoint": "main.py",
        "isolation": "local_process",
        "install_mode": "scan_only",
    }
    package.update(package_overrides)
    return json.dumps(
        {
            "id": "example",
            "name": "Example",
            "version": "1.0.0",
            "enabled": False,
            "package": package,
        }
    ).encode()


CODE = b"raise RuntimeError('generated code must never run')\n"


def archive_bytes(files: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(path, content)
    return buffer.getvalue()


@pytest.fixture
def context(
    tmp_path: Path,
) -> tuple[
    HandoverSourceService,
    SourceRepository,
    ProjectWorkspaceStore,
    AuthenticatedPrincipal,
]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    record = RunRecord(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        request="make plugin",
        mode=None,
        status=RunStatus.COMPLETED,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={
            "project_id": "project-a",
            "workspace_session_id": "session-a",
        },
    )
    repository = SourceRepository(record)
    workspace = ProjectWorkspaceStore(tmp_path / "workspaces")
    workspace.write_bytes(
        principal.tenant_id,
        "project-a",
        "session-a",
        "plugin.json",
        manifest_bytes(),
        "application/json",
    )
    workspace.write_bytes(
        principal.tenant_id,
        "project-a",
        "session-a",
        "main.py",
        CODE,
        "text/x-python",
    )
    service = HandoverSourceService(
        runs=repository,
        workspaces=workspace,
        generated_root=tmp_path / "generated",
    )
    return service, repository, workspace, principal


def workspace_source() -> WorkspaceSource:
    return WorkspaceSource(
        project_id="project-a",
        session_id="session-a",
        paths=("main.py", "plugin.json"),
    )


async def test_workspace_freezes_bytes_and_binds_scope(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, workspace, principal = context
    snapshot = await service.freeze(principal, repository.record.id, workspace_source())
    assert snapshot.authorized.tenant_id == principal.tenant_id
    assert snapshot.authorized.run_id == repository.record.id
    assert snapshot.plugin_id == "example"
    assert snapshot.plugin_version == "1.0.0"
    assert tuple(item.path for item in snapshot.files) == ("main.py", "plugin.json")
    code = snapshot.files[0]
    assert code.content == CODE
    assert code.size_bytes == len(CODE)
    assert code.sha256 == hashlib.sha256(CODE).hexdigest()
    assert snapshot.total_size_bytes == len(CODE) + len(manifest_bytes())
    workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "main.py").write_bytes(
        b"changed",
    )
    assert code.content == CODE
    with pytest.raises(FrozenInstanceError):
        code.content = b"forged"  # type: ignore[misc]


@pytest.mark.parametrize("scope", ["tenant", "run", "actor", "missing-actor", "admin-other"])
async def test_run_scope_and_actor_are_checked(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    scope: str,
) -> None:
    service, repository, _, principal = context
    run_id = repository.record.id
    if scope == "tenant":
        principal = replace(principal, tenant_id=uuid4())
    elif scope == "run":
        run_id = uuid4()
    elif scope == "missing-actor":
        repository.record = replace(repository.record, actor_id=None)
    else:
        principal = replace(
            principal,
            user_id=uuid4(),
            role=(Role.ADMIN if scope == "admin-other" else Role.OPERATOR),
        )
    with pytest.raises(PermissionDenied):
        await service.freeze(principal, run_id, workspace_source())


async def test_viewer_cannot_propose(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    with pytest.raises(PermissionDenied):
        await service.freeze(
            replace(principal, role=Role.VIEWER), repository.record.id, workspace_source()
        )


@pytest.mark.parametrize(
    "project,session",
    [
        ("project-b", "session-a"),
        ("project-a", "session-b"),
        ("PROJECT-A", "session-a"),
        ("project-a", "session_a"),
    ],
)
async def test_workspace_requires_exact_run_association(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    project: str,
    session: str,
) -> None:
    service, repository, _, principal = context
    source = WorkspaceSource(project_id=project, session_id=session, paths=("plugin.json",))
    with pytest.raises((PermissionDenied, InvalidSource)):
        await service.freeze(principal, repository.record.id, source)


async def test_absent_server_workspace_association_denied(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    repository.record = replace(repository.record, routing_decision=None)
    with pytest.raises(PermissionDenied):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_revalidate_rejects_changed_source(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, workspace, principal = context
    snapshot = await service.freeze(principal, repository.record.id, workspace_source())
    digest = snapshot.authorized.source_manifest_sha256
    unchanged = await service.revalidate(
        principal, repository.record.id, workspace_source(), digest
    )
    assert unchanged.authorized.source_manifest_sha256 == digest
    workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "main.py").write_bytes(
        b"changed",
    )
    with pytest.raises(SourceChanged):
        await service.revalidate(principal, repository.record.id, workspace_source(), digest)


async def test_digest_is_bound_to_run_and_actor(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    first = await service.freeze(principal, repository.record.id, workspace_source())
    repository.record = replace(repository.record, id=uuid4())
    second = await service.freeze(principal, repository.record.id, workspace_source())
    assert first.authorized.source_manifest_sha256 != second.authorized.source_manifest_sha256
    principal = replace(principal, user_id=uuid4())
    repository.record = replace(repository.record, actor_id=principal.user_id)
    third = await service.freeze(principal, repository.record.id, workspace_source())
    assert third.authorized.source_manifest_sha256 != second.authorized.source_manifest_sha256


@pytest.mark.parametrize("value", ["mapping", "authorized"])
async def test_caller_cannot_supply_authorized_dto_or_files(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    value: str,
) -> None:
    service, repository, _, principal = context
    forged: object = {"files": {"plugin.json": manifest_bytes(), "main.py": CODE}}
    if value == "authorized":
        forged = AuthorizedSource(
            tenant_id=principal.tenant_id,
            run_id=repository.record.id,
            source=workspace_source(),
            source_manifest_sha256="a" * 64,
        )
    with pytest.raises(InvalidSource):
        await service.freeze(principal, repository.record.id, cast(WorkspaceSource, forged))


@pytest.mark.parametrize(
    "path",
    [
        "../main.py",
        "a/../main.py",
        "/main.py",
        "C:/main.py",
        "a\\main.py",
        "main.py:stream",
        "a//main.py",
        "a/./main.py",
        "NUL.py",
        "main.py.",
        "main.py ",
        "a/%2e%2e/main.py",
        "a/\x7f.py",
    ],
)
async def test_unsafe_paths_rejected_even_on_constructed_dto(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    path: str,
) -> None:
    service, repository, _, principal = context
    source = WorkspaceSource.model_construct(
        project_id="project-a",
        session_id="session-a",
        paths=(path, "plugin.json"),
    )
    with pytest.raises(InvalidSource):
        await service.freeze(principal, repository.record.id, source)


@pytest.mark.parametrize(
    "change",
    [
        "missing-manifest",
        "missing-entrypoint",
        "unsigned-plain",
        "server-state",
        "unsupported-sdk",
        "duplicate-json",
    ],
)
async def test_not_a_plugin_or_server_state_forgery_rejected(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    change: str,
) -> None:
    service, repository, workspace, principal = context
    source = workspace_source()
    manifest = workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "plugin.json")
    if change == "missing-manifest":
        source = source.model_copy(update={"paths": ("main.py",)})
    elif change == "missing-entrypoint":
        manifest.write_bytes(manifest_bytes(entrypoint="missing.py"))
    elif change == "unsigned-plain":
        manifest.write_bytes(b'{"id":"example","name":"Example","version":"1.0.0"}')
    elif change == "server-state":
        manifest.write_bytes(manifest_bytes(approval_state="approved"))
    elif change == "unsupported-sdk":
        manifest.write_bytes(manifest_bytes(sdk_api_version="9.0"))
    else:
        manifest.write_bytes(b'{"id":"wrong","id":"example"}')
    with pytest.raises(InvalidSource):
        await service.freeze(principal, repository.record.id, source)


@pytest.mark.parametrize(
    "limits",
    [
        SourceLimits(max_files=1),
        SourceLimits(max_file_bytes=16),
        SourceLimits(max_total_bytes=len(CODE) + len(manifest_bytes()) - 1),
    ],
)
async def test_limits_apply_to_actual_selected_bytes(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    tmp_path: Path,
    limits: SourceLimits,
) -> None:
    _, repository, workspace, principal = context
    service = HandoverSourceService(
        runs=repository,
        workspaces=workspace,
        generated_root=tmp_path / "generated",
        limits=limits,
    )
    with pytest.raises(SourceTooLarge):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_symlink_inside_workspace_is_rejected(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, workspace, principal = context
    root = workspace.session_root(principal.tenant_id, "project-a", "session-a")
    (root / "real.py").write_bytes(CODE)
    (root / "main.py").unlink()
    try:
        (root / "main.py").symlink_to(root / "real.py")
    except OSError:
        pytest.skip("host does not grant symlink creation")
    with pytest.raises(InvalidSource):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_hardlink_rejected(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, workspace, principal = context
    root = workspace.session_root(principal.tenant_id, "project-a", "session-a")
    os.link(root / "main.py", root / "alias.py")
    with pytest.raises(InvalidSource):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_source_changed_during_read_rejected(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, principal = context
    read = os.read

    def mutate(descriptor: int, count: int) -> bytes:
        data = read(descriptor, count)
        if data == CODE:
            with os.fdopen(os.dup(descriptor), "rb") as opened:
                assert opened.read() == b""
            repository.record = replace(repository.record, actor_id=uuid4())
        return data

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(PermissionDenied):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_cancellation_propagates_and_retry_is_clean(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    repository.cancel = True
    with pytest.raises(asyncio.CancelledError):
        await service.freeze(principal, repository.record.id, workspace_source())
    repository.cancel = False
    snapshot = await service.freeze(principal, repository.record.id, workspace_source())
    assert snapshot.files[0].content == CODE


async def test_published_inline_bundle_is_read_from_repository(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    artifact_id = uuid4()
    repository.payloads = (
        {
            "id": str(artifact_id),
            "content": {
                "workspace_bundle": {
                    "files": {
                        "plugin.json": manifest_bytes().decode(),
                        "main.py": CODE.decode(),
                    }
                }
            },
        },
    )
    snapshot = await service.freeze(
        principal, repository.record.id, ArtifactSource(artifact_id=str(artifact_id))
    )
    assert snapshot.files[0].content == CODE
    assert snapshot.authorized.source.kind == "artifact"


async def test_nested_artifact_id_is_not_publication_authority(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    service, repository, _, principal = context
    nested_id = uuid4()
    repository.payloads = (
        {
            "id": str(uuid4()),
            "content": {
                "metadata": {
                    "artifact_id": str(nested_id),
                },
                "workspace_bundle": {
                    "files": {
                        "plugin.json": manifest_bytes().decode(),
                        "main.py": CODE.decode(),
                    }
                },
            },
        },
    )
    with pytest.raises(PermissionDenied):
        await service.freeze(
            principal, repository.record.id, ArtifactSource(artifact_id=str(nested_id))
        )


async def test_generated_zip_binds_raw_metadata_and_actual_bytes(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    tmp_path: Path,
) -> None:
    service, repository, _, principal = context
    artifact_id = uuid4()
    data = archive_bytes((("plugin.json", manifest_bytes()), ("main.py", CODE)))
    store = GeneratedFileStore(tmp_path / "generated")
    metadata = store.store_bytes(
        principal.tenant_id, repository.record.id, artifact_id, "plugin.zip", ZIP_MIME_TYPE, data
    )
    repository.payloads = (
        {
            "id": str(artifact_id),
            "content": {
                "file": metadata.to_content_file(),
                "metadata": metadata.to_content_file(),
            },
        },
    )
    snapshot = await service.freeze(
        principal, repository.record.id, ArtifactSource(artifact_id=str(artifact_id))
    )
    assert snapshot.files[0].content == CODE
    store.resolve(metadata.storage_key).write_bytes(data + b"changed")
    with pytest.raises(SourceChanged):
        await service.freeze(
            principal, repository.record.id, ArtifactSource(artifact_id=str(artifact_id))
        )


@pytest.mark.parametrize(
    "bad_kind",
    [
        "wrong-run",
        "wrong-tenant",
        "wrong-artifact",
        "wrong-sha",
        "wrong-size",
        "traversal",
        "device",
        "duplicate",
        "symlink",
        "zip-bomb",
    ],
)
async def test_artifact_storage_and_archive_safety(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    tmp_path: Path,
    bad_kind: str,
) -> None:
    service, repository, _, principal = context
    artifact_id = uuid4()
    files: tuple[tuple[str, bytes], ...] = (("plugin.json", manifest_bytes()), ("main.py", CODE))
    if bad_kind == "traversal":
        files += (("../secret.py", b"x"),)
    elif bad_kind == "device":
        files += (("NUL.py", b"x"),)
    elif bad_kind == "duplicate":
        files += (("MAIN.py", b"x"),)
    elif bad_kind == "zip-bomb":
        files += (("large.txt", b"x" * 512_001),)
    data = archive_bytes(files)
    if bad_kind == "symlink":
        buffer = io.BytesIO(data)
        with zipfile.ZipFile(buffer, "a") as archive:
            link = zipfile.ZipInfo("link.py")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b"main.py")
        data = buffer.getvalue()
    store = GeneratedFileStore(tmp_path / "generated")
    metadata = store.store_bytes(
        uuid4() if bad_kind == "wrong-tenant" else principal.tenant_id,
        uuid4() if bad_kind == "wrong-run" else repository.record.id,
        uuid4() if bad_kind == "wrong-artifact" else artifact_id,
        "plugin.zip",
        ZIP_MIME_TYPE,
        data,
    ).to_content_file()
    if bad_kind == "wrong-sha":
        metadata["sha256"] = "f" * 64
    elif bad_kind == "wrong-size":
        metadata["size_bytes"] = len(data) + 1
    repository.payloads = (
        {
            "id": str(artifact_id),
            "content": {
                "file": metadata,
                "metadata": metadata,
            },
        },
    )
    with pytest.raises((InvalidSource, SourceChanged, SourceTooLarge)):
        await service.freeze(
            principal, repository.record.id, ArtifactSource(artifact_id=str(artifact_id))
        )


@pytest.mark.parametrize("bad_kind", ["compression-ratio", "nonfinite-json", "directory-alias"])
async def test_archive_preflight_rejects_ambiguous_or_unbounded_data(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    tmp_path: Path,
    bad_kind: str,
) -> None:
    service, repository, _, principal = context
    manifest = manifest_bytes()
    if bad_kind == "nonfinite-json":
        value = json.loads(manifest)
        value["resource_config"] = {"number": float("nan")}
        manifest = json.dumps(value).encode()
    files: tuple[tuple[str, bytes], ...] = (("plugin.json", manifest), ("main.py", CODE))
    if bad_kind == "compression-ratio":
        files += (("zeros.txt", b"0" * 500_000),)
    elif bad_kind == "directory-alias":
        files += (("MAIN.py/", b""),)
    data = archive_bytes(files)
    artifact_id = uuid4()
    metadata = (
        GeneratedFileStore(tmp_path / "generated")
        .store_bytes(
            principal.tenant_id,
            repository.record.id,
            artifact_id,
            "plugin.zip",
            ZIP_MIME_TYPE,
            data,
        )
        .to_content_file()
    )
    repository.payloads = (
        {
            "id": str(artifact_id),
            "content": {
                "file": metadata,
                "metadata": metadata,
            },
        },
    )
    with pytest.raises(InvalidSource):
        await service.freeze(
            principal, repository.record.id, ArtifactSource(artifact_id=str(artifact_id))
        )


async def test_file_modification_during_read_is_not_a_snapshot(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, workspace, principal = context
    path = workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "main.py")
    read = os.read

    def mutate(descriptor: int, count: int) -> bytes:
        content = read(descriptor, count)
        if content == CODE:
            path.write_bytes(b"#" * len(CODE))
        return content

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(SourceChanged):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_earlier_file_change_with_restored_mtime_is_rejected(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, workspace, principal = context
    path = workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "main.py")
    original = path.stat()
    read = os.read
    changed = False

    def mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        content = read(descriptor, count)
        if content == manifest_bytes() and not changed:
            path.write_bytes(b"#" * len(CODE))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            changed = True
        return content

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(SourceChanged):
        await service.freeze(principal, repository.record.id, workspace_source())


async def test_cancellation_after_read_closes_handles_and_never_returns_snapshot(
    context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, workspace, principal = context
    read = os.read
    descriptors: list[int] = []

    def cancel(descriptor: int, count: int) -> bytes:
        content = read(descriptor, count)
        if content == CODE:
            task = asyncio.current_task()
            assert task is not None
            descriptors.append(descriptor)
            task.cancel()
        return content

    monkeypatch.setattr(os, "read", cancel)
    task = asyncio.create_task(service.freeze(principal, repository.record.id, workspace_source()))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert (
        workspace.resolve_file(
            principal.tenant_id,
            "project-a",
            "session-a",
            "main.py",
        ).read_bytes()
        == CODE
    )


@pytest.fixture(autouse=True)
def forbid_execution_and_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.api.routers.admin import PersistentAdminResourceService
    from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
    from agent_hub.capability_installer.service import CapabilityInstallerService
    from agent_hub.plugins import package_builder
    from agent_hub.plugins.runtime import PythonSubprocessPluginPackageRunner, RuntimePluginService
    from agent_hub.skills.scanner import SkillScanner

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("source freezing attempted signing, scanning, installation or execution")

    for name in ("start_plugin", "enable_plugin", "reload_plugin", "upsert_plugin"):
        monkeypatch.setattr(PersistentAdminResourceService, name, forbidden)
    for name in ("build_python", "deploy_cli"):
        monkeypatch.setattr(CapabilityEnvironmentManager, name, forbidden)
    monkeypatch.setattr(CapabilityInstallerService, "install", forbidden)
    monkeypatch.setattr(package_builder, "build_signed_plugin_archive", forbidden)
    monkeypatch.setattr(SkillScanner, "scan", forbidden)
    for name in ("start", "reload", "reload_strict", "preflight_plugin", "invoke"):
        monkeypatch.setattr(RuntimePluginService, name, forbidden)
    for name in ("preflight", "invoke"):
        monkeypatch.setattr(PythonSubprocessPluginPackageRunner, name, forbidden)
