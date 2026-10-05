from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest

from agent_hub.previews import (
    PreviewCapacityExceeded,
    PreviewLaunch,
    PreviewManager,
    PreviewTokenRejected,
)
from agent_hub.previews.cleanup import (
    BrokerCleanupObservation,
    PreviewIdentityV1,
    PreviewOwnerScope,
)
from agent_hub.previews.dynamic_runtime import (
    DynamicPreviewCleanupError,
    DynamicPreviewResponse,
    DynamicPreviewUnavailable,
)
from tests.unit.previews.test_cleanup import broker_observation, identity

TENANT = UUID("10000000-0000-0000-0000-000000000001")


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...], bytes]] = []
        self.fail_close = False
        self.closed = False
        self.identity: PreviewIdentityV1 = identity("dynamic")
        self.response = DynamicPreviewResponse(201, (("content-type", "application/json"),), b"{}")

    def request(
        self, method: str, target: str, headers: tuple[tuple[str, str], ...], body: bytes
    ) -> DynamicPreviewResponse:
        self.calls.append((method, target, headers, body))
        return self.response

    def close(self) -> BrokerCleanupObservation:
        if self.fail_close:
            raise DynamicPreviewCleanupError("not stopped")
        self.closed = True
        return broker_observation(self.identity)


class FakeBackend:
    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        self.sources: list[Path] = []
        self.lifetimes: list[int] = []
        self.fail = False

    def start(self, source_root: Path, preview_id: str, lifetime_seconds: int, *, scope: PreviewOwnerScope) -> FakeRuntime:
        assert preview_id
        self.sources.append(source_root)
        self.lifetimes.append(lifetime_seconds)
        if self.fail:
            raise DynamicPreviewUnavailable("broker unavailable")
        self.runtime.identity = PreviewIdentityV1.create(preview_id, scope, kind="dynamic", digest="b" * 64, handle="a" * 32)
        return self.runtime


def project(root: Path) -> Path:
    session = root / str(TENANT) / "projects/project-a/sessions/session-a"
    (session / "dist").mkdir(parents=True)
    (session / "dist/index.html").write_text("<head></head><script>fetch('/tasks')</script>")
    (session / "server.js").write_text("throw Error('never run on host');")
    (session / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
    return session


def start(manager: PreviewManager, conversation: str = "conversation-a") -> PreviewLaunch:
    return manager.start(
        tenant_id=TENANT,
        conversation_id=conversation,
        project_id="project-a",
        session_id="session-a",
        root="dist",
    )


def test_legacy_backend_missing_metadata_keeps_launch_request_and_cleanup(tmp_path: Path) -> None:
    from uuid import uuid4

    from agent_hub.previews.provenance import PreviewProvenanceUnavailable
    project(tmp_path)
    backend = FakeBackend()
    user = uuid4()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = manager.start(tenant_id=TENANT, user_id=user, conversation_id="conversation-a",
            project_id="project-a", session_id="session-a", root="dist")
        with pytest.raises(PreviewProvenanceUnavailable):
            manager.provenance(launch.state.preview_id, TENANT, user)
        assert manager.app_request(launch.state.preview_id, launch.token, "GET", "/tasks", (), b"").status_code == 201
        manager.stop(launch.state.preview_id)
        assert backend.runtime.closed


@pytest.mark.parametrize("failure", ["foreign", "malformed", "error", "stopped"])
def test_metadata_errors_and_revocation_cannot_substitute_preview_or_lose_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from uuid import uuid4

    from agent_hub.previews import PreviewNotFound
    from agent_hub.previews.provenance import (
        PreviewProvenanceUnavailable,
        PreviewProvenanceV1,
        SnapshotManifestV1,
    )
    project(tmp_path)
    user = uuid4()
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = manager.start(tenant_id=TENANT, user_id=user, conversation_id="conversation-a",
            project_id="project-a", session_id="session-a", root="dist")

        def metadata() -> object:
            if failure == "error":
                raise OSError("metadata unavailable")
            if failure == "malformed":
                return {"snapshot_manifest": {"manifest_sha256": "a" * 64}}
            if failure == "stopped":
                manager.stop(launch.state.preview_id)
            bound = backend.runtime.identity
            if failure == "foreign":
                bound = replace(bound, workspace_session_id="other-session")
            return PreviewProvenanceV1(1, bound, SnapshotManifestV1.from_manifest(
                {"package.json": (0, "a" * 64)}, selection_policy="dynamic-staged-session-v1"),
                datetime.now(UTC))

        monkeypatch.setattr(backend.runtime, "source_provenance", metadata, raising=False)
        expected = PreviewNotFound if failure == "stopped" else PreviewProvenanceUnavailable
        with pytest.raises(expected):
            manager.provenance(launch.state.preview_id, TENANT, user)
        manager.stop(launch.state.preview_id)
        assert backend.runtime.closed




def test_dynamic_snapshot_includes_backend_separately_from_display_root(tmp_path: Path) -> None:
    session = project(tmp_path)
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        source = backend.sources[0]
        assert source.is_relative_to(tmp_path / ".preview-staging")
        assert source != session
        assert (source / "server.js").read_text() == "throw Error('never run on host');"
        (session / "server.js").write_text("changed")
        assert (source / "server.js").read_text() != "changed"
        assert launch.state.application_transport is True
        assert backend.lifetimes == [7200]
        response = manager.app_request(
            launch.state.preview_id,
            launch.token,
            "POST",
            "/tasks?label=a%2Fb",
            (("Content-Type", "application/json"),),
            b"{}",
        )
        assert response.status_code == 201
        assert backend.runtime.calls == [
            ("POST", "/tasks?label=a%2Fb", (("content-type", "application/json"),), b"{}")
        ]
    assert backend.runtime.closed
    assert not source.exists()


def test_missing_dynamic_backend_never_becomes_static_ready(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    backend.fail = True
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        with pytest.raises(DynamicPreviewUnavailable):
            start(manager)
        assert manager.current(TENANT, "conversation-a") is None
        assert not backend.sources[0].exists()


@pytest.mark.parametrize("returned_runtime", [False, True])
def test_failed_start_retains_unpublished_cleanup_and_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned_runtime: bool,
) -> None:
    project(tmp_path)

    class WrongIdentityBackend(FakeBackend):
        def start(self, source_root: Path, preview_id: str, lifetime_seconds: int,
                  *, scope: PreviewOwnerScope) -> FakeRuntime:
            return super().start(source_root, preview_id, lifetime_seconds,
                                 scope=replace(scope, conversation_id="wrong-conversation"))

    backend = WrongIdentityBackend() if returned_runtime else FakeBackend()
    backend.fail = not returned_runtime
    backend.runtime.fail_close = returned_runtime
    manager = PreviewManager(tmp_path, dynamic_backend=backend, max_active_global=1,
                             reaper_interval=timedelta(hours=1))
    original_cleanup = TemporaryDirectory.cleanup

    def fail_snapshot_cleanup(snapshot: TemporaryDirectory[str]) -> None:
        if Path(snapshot.name).parent == tmp_path / ".preview-staging":
            raise PermissionError("startup snapshot cleanup fixture")
        original_cleanup(snapshot)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(TemporaryDirectory, "cleanup", fail_snapshot_cleanup)
            with pytest.raises((DynamicPreviewCleanupError, PermissionError)):
                start(manager)
            assert len(manager._runtimes) == 1
            retained = next(iter(manager._runtimes.values()))
            assert retained.state.identity is None
            assert retained.state.status == "stopped"
            assert manager.current(TENANT, "conversation-a") is None
            assert manager.cleanup_record(retained.state.preview_id) is None
            assert not manager._receipt_store._records
            assert backend.sources[0].exists()
            with pytest.raises(PreviewCapacityExceeded):
                start(manager, "conversation-b")
            with pytest.raises(DynamicPreviewCleanupError):
                start(manager)
            assert len(backend.sources) == 1
            with pytest.raises(DynamicPreviewCleanupError):
                manager.reap_expired()
            backend.runtime.fail_close = False
            with pytest.raises(DynamicPreviewCleanupError):
                manager.reap_expired()
            assert len(manager._runtimes) == 1
            assert backend.sources[0].exists()
        if returned_runtime:
            rejected_identity = backend.runtime.identity
            backend.runtime.identity = replace(rejected_identity, runtime_handle="c" * 32)
            with pytest.raises(DynamicPreviewCleanupError):
                manager.reap_expired()
            assert backend.sources[0].exists()
            assert len(manager._runtimes) == 1
            backend.runtime.identity = rejected_identity
        assert manager.reap_expired() == (retained.state.preview_id,)
        assert not manager._runtimes
        assert not backend.sources[0].exists()
        assert not manager._receipt_store._records
        assert manager.cleanup_record(retained.state.preview_id) is None
        backend.fail = False
        backend.runtime.fail_close = False
        if returned_runtime:
            manager._dynamic_backend = FakeBackend()
        assert start(manager, "conversation-b").state.status == "ready"
    finally:
        backend.runtime.fail_close = False
        manager.close()


def test_stop_failure_revokes_retains_stage_and_quota_then_reaper_retries(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    manager = PreviewManager(tmp_path, dynamic_backend=backend, max_active_global=1)
    try:
        launch = start(manager)
        backend.runtime.fail_close = True
        with pytest.raises(DynamicPreviewCleanupError):
            manager.stop(launch.state.preview_id)
        assert backend.sources[0].exists()
        with pytest.raises(PreviewTokenRejected):
            manager.app_request(launch.state.preview_id, launch.token, "POST", "/tasks", (), b"x")
        with pytest.raises(PreviewCapacityExceeded):
            start(manager, "conversation-b")
        with pytest.raises(DynamicPreviewCleanupError):
            start(manager)
        backend.runtime.fail_close = False
        manager.reap_expired()
        assert not backend.sources[0].exists()
        assert backend.runtime.closed
    finally:
        backend.runtime.fail_close = False
        manager.close()


def test_broker_clean_snapshot_failure_keeps_identity_slot_and_stopped_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path)
    backend = FakeBackend()
    now = [datetime.now(UTC)]
    with PreviewManager(tmp_path, dynamic_backend=backend, max_active_global=1, clock=lambda: now[0]) as manager:
        launch = start(manager)
        owned = manager._runtimes[launch.state.preview_id]
        original = owned.snapshot.cleanup
        monkeypatch.setattr(owned.snapshot, "cleanup", lambda: (_ for _ in ()).throw(OSError("snapshot fixture")))
        with pytest.raises(DynamicPreviewCleanupError):
            manager.stop(launch.state.preview_id)
        assert backend.runtime.closed and backend.sources[0].exists()
        stopped = owned.state.stopped_at
        record = manager.cleanup_record(launch.state.preview_id)
        assert record is not None and record.cleanup_receipt is not None
        facts = {f.resource: f.result for f in record.cleanup_receipt.observations}
        assert facts["owned_directory"] == "absent" and facts["snapshot"] == "unknown"
        with pytest.raises(PreviewCapacityExceeded):
            start(manager, "other")
        now[0] += timedelta(seconds=2)
        monkeypatch.setattr(owned.snapshot, "cleanup", original)
        manager.stop(launch.state.preview_id)
        assert owned.state.stopped_at == stopped
        final = manager.cleanup_record(launch.state.preview_id)
        assert final is not None and final.cleanup_receipt is not None
        assert final.identity == record.identity and final.cleanup_receipt.status == "confirmed"
        assert final.cleanup_receipt.requested_at > record.cleanup_receipt.requested_at


def test_persistence_failure_does_not_defer_cleanup_or_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path)
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        with monkeypatch.context() as patch:
            patch.setattr(manager._receipt_store, "put", lambda r: (_ for _ in ()).throw(OSError("disk fixture")))
            with pytest.raises(DynamicPreviewCleanupError):
                manager.stop(launch.state.preview_id)
            assert backend.runtime.closed and not backend.sources[0].exists()
            record = manager.cleanup_record(launch.state.preview_id)
            assert record is not None and record.cleanup_receipt is not None
            assert record.cleanup_receipt.status == "unknown"
            with pytest.raises(PreviewTokenRejected):
                manager.app_request(launch.state.preview_id, launch.token, "POST", "/tasks", (), b"x")
        manager.stop(launch.state.preview_id)


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/tasks",
        "//example.com/a",
        "/../x",
        "/%2e%2e/x",
        "/%252fsecret",
        "/x%00",
        "/x#fragment",
    ],
)
def test_dynamic_target_is_virtual_and_strict(tmp_path: Path, target: str) -> None:
    project(tmp_path)
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        with pytest.raises(ValueError):
            manager.app_request(launch.state.preview_id, launch.token, "GET", target, (), b"")
        assert not backend.runtime.calls


def test_proxy_strips_credentials_and_connection_nominated_headers(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        manager.app_request(
            launch.state.preview_id,
            launch.token,
            "GET",
            "/api/v1/runs",
            (
                ("Authorization", "Bearer secret"),
                ("Cookie", "secret=value"),
                ("Host", "console"),
                ("Origin", "console"),
                ("Connection", "accept"),
                ("Accept", "secret"),
                ("Content-Type", "application/json"),
            ),
            b"",
        )
        assert backend.runtime.calls[0][2] == (("content-type", "application/json"),)


def test_failed_writes_are_not_retried_and_response_is_bounded(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    backend.runtime.response = DynamicPreviewResponse(500, (), b"application error")
    with PreviewManager(tmp_path, dynamic_backend=backend, max_response_bytes=32) as manager:
        launch = start(manager)
        response = manager.app_request(
            launch.state.preview_id, launch.token, "PATCH", "/tasks", (), b"x"
        )
        assert response.status_code == 500
        assert response.body == b"application error"
        assert len(backend.runtime.calls) == 1
        backend.runtime.response = DynamicPreviewResponse(200, (), b"x" * 33)
        from agent_hub.previews import PreviewResponseTooLarge

        with pytest.raises(PreviewResponseTooLarge):
            manager.app_request(launch.state.preview_id, launch.token, "GET", "/tasks", (), b"")


def test_expiry_stops_dynamic_runtime(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    with PreviewManager(
        tmp_path, dynamic_backend=backend, clock=lambda: now[0], default_lease=timedelta(seconds=1)
    ) as manager:
        launch = start(manager)
        now[0] += timedelta(seconds=1)
        assert manager.reap_expired() == (launch.state.preview_id,)
        assert backend.runtime.closed


def test_unsafe_source_links_are_rejected_before_backend_start(tmp_path: Path) -> None:
    session = project(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("secret")
    try:
        (session / "alias").symlink_to(secret)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    backend = FakeBackend()
    from agent_hub.previews import InvalidPreviewPath

    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        with pytest.raises(InvalidPreviewPath):
            start(manager)
        assert not backend.sources


def test_source_hardlinks_rejected_and_credentials_not_staged(tmp_path: Path) -> None:
    session = project(tmp_path)
    backend = FakeBackend()
    (session / ".env").write_text("SECRET=value")
    (session / ".npmrc").write_text("secret=value")
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        assert not (backend.sources[0] / ".env").exists()
        assert not (backend.sources[0] / ".npmrc").exists()
        manager.stop(launch.state.preview_id)
        os.link(session / "server.js", session / "linked.js")
        from agent_hub.previews import InvalidPreviewPath

        with pytest.raises(InvalidPreviewPath):
            start(manager)


def test_dynamic_application_without_static_entrypoint(tmp_path: Path) -> None:
    session = project(tmp_path)
    (session / "dist/index.html").unlink()
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = manager.start(
            tenant_id=TENANT,
            conversation_id="conversation-a",
            project_id="project-a",
            session_id="session-a",
        )
        assert launch.state.application_transport
        from agent_hub.previews import PreviewNotFound
        with pytest.raises(PreviewNotFound):
            manager.read(launch.state.preview_id, launch.token)
        assert not backend.runtime.calls


def test_close_failure_reaper_continues_and_owned_close_can_retry(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    manager = PreviewManager(tmp_path, dynamic_backend=backend)
    start(manager)
    backend.runtime.fail_close = True
    with pytest.raises(DynamicPreviewCleanupError):
        manager.close()
    assert not manager._reaper_stop.is_set()
    backend.runtime.fail_close = False
    manager.close()
    assert not backend.sources[0].exists()


def test_stop_revokes_before_inflight_request_finishes(tmp_path: Path) -> None:
    project(tmp_path)
    backend = FakeBackend()
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    class BlockingRuntime(FakeRuntime):
        def request(
            self, method: str, target: str, headers: tuple[tuple[str, str], ...], body: bytes
        ) -> DynamicPreviewResponse:
            entered.set()
            assert release.wait(2)
            return self.response

    backend.runtime = BlockingRuntime()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)

        def request() -> None:
            try:
                manager.app_request(
                    launch.state.preview_id, launch.token, "POST", "/tasks", (), b""
                )
            except (PreviewTokenRejected, AssertionError) as error:
                errors.append(error)

        worker = threading.Thread(target=request)
        worker.start()
        try:
            assert entered.wait(1)
            manager.stop(launch.state.preview_id)
            release.set()
            worker.join(3)
            assert len(errors) == 1 and isinstance(errors[0], PreviewTokenRejected)
        finally:
            release.set()
            worker.join(3)


def test_prepared_application_snapshot_files_are_readonly(tmp_path: Path) -> None:
    import stat

    project(tmp_path)
    backend = FakeBackend()
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        start(manager)
        assert not (backend.sources[0] / "server.js").stat().st_mode & stat.S_IWUSR


def test_static_application_proxy_is_unavailable(tmp_path: Path) -> None:
    session = project(tmp_path)
    (session / "package.json").write_text('{"scripts":{"build":"vite build"}}')
    backend = FakeBackend()
    from agent_hub.previews import PreviewNotFound

    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = start(manager)
        assert not launch.state.application_transport
        with pytest.raises(PreviewNotFound):
            manager.app_request(launch.state.preview_id, launch.token, "POST", "/tasks", (), b"")
        assert not backend.sources


def test_api_only_backend_displays_original_immutable_preview_html(tmp_path: Path) -> None:
    session = project(tmp_path)
    (session / "dist/index.html").unlink()
    (session / "preview.html").write_text(
        '<script src="assets/ui.js"></script><main>actual generated UI</main>'
    )
    (session / "assets").mkdir()
    (session / "assets/ui.js").write_text("fetch('/tasks')")
    backend = FakeBackend()
    backend.runtime.response = DynamicPreviewResponse(
        404, (("content-type", "application/json"),), b'{"error":"not found"}'
    )
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:
        launch = manager.start(
            tenant_id=TENANT,
            conversation_id="conversation-a",
            project_id="project-a",
            session_id="session-a",
        )
        (session / "preview.html").write_text("changed workspace")
        (session / "assets/ui.js").unlink()
        response = manager.read(launch.state.preview_id, launch.token)
        assert response.status_code == 200
        assert b"actual generated UI" in response.body
        assert (
            manager.read(launch.state.preview_id, launch.token, "assets/ui.js").body
            == b"fetch('/tasks')"
        )
        assert manager.read(launch.state.preview_id, launch.token, "tasks").status_code == 404
        assert not backend.runtime.calls
        response = manager.app_request(
            launch.state.preview_id, launch.token, "GET", "/tasks", (), b""
        )
        assert response.status_code == 404 and response.body == b'{"error":"not found"}'
        assert len(backend.runtime.calls) == 1


def test_stop_conversation_during_preparation_cancels_startup(tmp_path: Path) -> None:
    project(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(FakeBackend):
        def start(self, source_root: Path, preview_id: str, lifetime_seconds: int, *, scope: PreviewOwnerScope) -> FakeRuntime:
            entered.set()
            assert release.wait(3)
            return super().start(source_root, preview_id, lifetime_seconds, scope=scope)

    backend = BlockingBackend()
    errors: list[BaseException] = []
    with PreviewManager(tmp_path, dynamic_backend=backend) as manager:

        def launch() -> None:
            try:
                start(manager)
            except PreviewTokenRejected as error:
                errors.append(error)

        worker = threading.Thread(target=launch)
        worker.start()
        try:
            assert entered.wait(2)
            manager.stop_conversation(TENANT, "conversation-a")
        finally:
            release.set()
            worker.join(5)
        assert len(errors) == 1
        assert manager.current(TENANT, "conversation-a") is None
        assert backend.runtime.closed
        assert not backend.sources[0].exists()


def test_reaper_continues_other_tenant_after_cleanup_failure(tmp_path: Path) -> None:
    import shutil
    session = project(tmp_path)
    other_tenant = UUID("20000000-0000-0000-0000-000000000002")
    shutil.copytree(session, tmp_path / str(other_tenant) / "projects/project-a/sessions/session-a")

    class SeparateBackend(FakeBackend):
        def start(self, source_root: Path, preview_id: str, lifetime_seconds: int, *, scope: PreviewOwnerScope) -> FakeRuntime:
            self.runtime = FakeRuntime()
            return super().start(source_root, preview_id, lifetime_seconds, scope=scope)

    backend = SeparateBackend()
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    manager = PreviewManager(tmp_path, dynamic_backend=backend, clock=lambda: now[0],
                             default_lease=timedelta(seconds=1))
    first = start(manager)
    failed_runtime = backend.runtime
    second = manager.start(tenant_id=other_tenant, conversation_id="conversation-b",
                           project_id="project-a", session_id="session-a")
    other_runtime = backend.runtime
    failed_runtime.fail_close = True
    now[0] += timedelta(seconds=2)
    try:
        with pytest.raises(DynamicPreviewCleanupError):
            manager.reap_expired()
        assert other_runtime.closed
        assert not backend.sources[1].exists()
        assert first.state.preview_id in manager._runtimes
        assert second.state.preview_id not in manager._runtimes
        assert backend.sources[0].exists()
    finally:
        failed_runtime.fail_close = False
        manager.close()
