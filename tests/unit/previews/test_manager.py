from __future__ import annotations

import http.client
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import pytest

import agent_hub.previews.manager as preview_manager_module
from agent_hub.previews import (
    InvalidPreviewPath,
    PreviewCapacityExceeded,
    PreviewLaunch,
    PreviewManager,
    PreviewNotFound,
    PreviewResponseTooLarge,
    PreviewTokenRejected,
)

TENANT_ID = UUID("10000000-0000-0000-0000-000000000001")


def _session_root(root: Path, project_id: str = "project-a", session_id: str = "session-a") -> Path:
    return root / str(TENANT_ID) / "projects" / project_id / "sessions" / session_id


def _write(root: Path, relative_path: str, content: str | bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _start(manager: PreviewManager, **overrides: object) -> PreviewLaunch:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "conversation_id": "conversation-a",
        "project_id": "project-a",
        "session_id": "session-a",
    }
    values.update(overrides)
    return manager.start(**values)  # type: ignore[arg-type]


def test_start_serves_dist_assets_on_loopback_and_tracks_state(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "dist/index.html", '<script src="/assets/app.js"></script>')
    _write(session_root, "dist/assets/app.js", "window.previewLoaded = true;")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)

        assert launch.state.status == "ready"
        assert launch.state.tenant_id == TENANT_ID
        assert launch.state.conversation_id == "conversation-a"
        assert launch.state.project_id == "project-a"
        assert launch.state.session_id == "session-a"
        assert launch.state.internal_host == "127.0.0.1"
        assert 0 < launch.state.internal_port < 65536
        assert launch.state.token_sha256 != launch.token
        assert len(launch.state.token_sha256) == 64
        assert launch.state.preview_root == session_root / "dist"
        assert manager.current(TENANT_ID, "conversation-a") == launch.state

        connection = http.client.HTTPConnection(
            launch.state.internal_host,
            launch.state.internal_port,
            timeout=2,
        )
        connection.request("GET", "/assets/app.js")
        response = connection.getresponse()

        assert response.status == 200
        assert response.read() == b"window.previewLoaded = true;"
        assert response.getheader("Content-Type") == "text/javascript"
        connection.close()


def test_preview_serves_immutable_snapshot_when_workspace_changes(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "dist/index.html", '<script src="assets/app.js"></script>')
    _write(session_root, "dist/assets/app.js", "window.version = 'original';")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)
        _write(session_root, "dist/assets/app.js", "window.version = 'mutated';")

        response = manager.read(launch.state.preview_id, launch.token, "assets/app.js")

        assert response.body == b"window.version = 'original';"


@pytest.mark.parametrize(
    ("files", "expected_root", "expected_body"),
    [
        ({"build/index.html": "build"}, "build", b"build"),
        ({"preview.html": "preview"}, ".", b"preview"),
        ({"index.html": "index"}, ".", b"index"),
    ],
)
def test_start_uses_safe_preview_fallbacks(
    tmp_path: Path,
    files: dict[str, str],
    expected_root: str,
    expected_body: bytes,
) -> None:
    session_root = _session_root(tmp_path)
    for path, content in files.items():
        _write(session_root, path, content)

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)
        response = manager.read(launch.state.preview_id, launch.token, "")

        assert launch.state.preview_root == (session_root / expected_root).resolve()
        assert response.status_code == 200
        assert response.body == expected_body


def test_start_accepts_explicit_safe_relative_preview_root(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "site/output/index.html", "explicit")
    _write(session_root, "dist/index.html", "automatic")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager, root="site/output")
        response = manager.read(launch.state.preview_id, launch.token, "")

        assert launch.state.preview_root == (session_root / "site/output").resolve()
        assert response.body == b"explicit"


def test_explicit_root_accepts_preview_html_entrypoint(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "public/preview.html", "preview")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager, root="public")

        assert manager.read(launch.state.preview_id, launch.token, "").body == b"preview"


@pytest.mark.parametrize(
    "root",
    ["../dist", "/dist", r"build\output", ".hidden", "site/.hidden", "https://host/dist"],
)
def test_explicit_root_rejects_unsafe_paths(tmp_path: Path, root: str) -> None:
    _write(_session_root(tmp_path), "dist/index.html", "home")

    with PreviewManager(tmp_path) as manager, pytest.raises(InvalidPreviewPath):
        _start(manager, root=root)


def test_explicit_root_rejects_alias_and_requires_entrypoint(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "dist/index.html", "home")
    _write(session_root, "empty/asset.js", "asset")
    alias_candidate = session_root / "alias"
    alias: Path | None = alias_candidate
    try:
        alias_candidate.symlink_to(session_root / "dist", target_is_directory=True)
    except OSError:
        alias = None

    with PreviewManager(tmp_path) as manager:
        with pytest.raises(PreviewNotFound, match="preview entrypoint"):
            _start(manager, root="empty")
        if alias is not None:
            with pytest.raises(InvalidPreviewPath, match="alias"):
                _start(manager, root="alias")


def test_start_rejects_missing_preview_and_unsafe_workspace_aliases(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    session_root.mkdir(parents=True)

    with PreviewManager(tmp_path) as manager:
        with pytest.raises(PreviewNotFound, match="preview entrypoint"):
            _start(manager)

        with pytest.raises(InvalidPreviewPath, match="single safe path segment"):
            _start(manager, project_id="../other")


def test_server_and_proxy_reject_traversal_and_directory_listing(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "dist/index.html", "home")
    _write(session_root, "dist/assets/app.js", "asset")
    _write(session_root, "secret.txt", "secret")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)

        for path in ("../secret.txt", "%2e%2e/secret.txt", "assets", "assets/"):
            with pytest.raises(InvalidPreviewPath):
                manager.read(launch.state.preview_id, launch.token, path)

        connection = http.client.HTTPConnection("127.0.0.1", launch.state.internal_port, timeout=2)
        connection.request("GET", "/%2e%2e/secret.txt")
        assert connection.getresponse().status == 404
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", launch.state.internal_port, timeout=2)
        connection.request("GET", "/assets/")
        assert connection.getresponse().status == 404
        connection.close()


def test_start_rejects_nested_file_symlink(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "dist/index.html", "home")
    _write(session_root, "secret.txt", "secret")
    symlink = session_root / "dist" / "assets" / "secret-link.txt"
    symlink.parent.mkdir(parents=True)
    try:
        symlink.symlink_to(session_root / "secret.txt")
    except OSError:
        pytest.skip("file symlinks are unavailable on this platform")

    with PreviewManager(tmp_path) as manager, pytest.raises(InvalidPreviewPath, match="alias"):
        _start(manager)


@pytest.mark.parametrize("path", ["//other-host/asset.js", "https://other-host/asset.js"])
def test_proxy_rejects_absolute_or_network_paths(tmp_path: Path, path: str) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)

        with pytest.raises(InvalidPreviewPath):
            manager.read(launch.state.preview_id, launch.token, path)


def test_token_is_required_and_invalidated_after_idempotent_stop(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)

        with pytest.raises(PreviewTokenRejected):
            manager.read(launch.state.preview_id, "wrong-token", "")

        stopped = manager.stop(launch.state.preview_id)
        stopped_again = manager.stop(launch.state.preview_id)

        assert stopped.status == "stopped"
        assert stopped_again == stopped
        assert manager.current(TENANT_ID, "conversation-a") is None
        with pytest.raises(PreviewTokenRejected):
            manager.read(launch.state.preview_id, launch.token, "")


def test_response_size_is_bounded_in_server_and_proxy(tmp_path: Path) -> None:
    session_root = _session_root(tmp_path)
    _write(session_root, "index.html", "home")
    _write(session_root, "large.bin", b"x" * 9)

    with PreviewManager(tmp_path, max_response_bytes=8) as manager:
        launch = _start(manager)

        with pytest.raises(PreviewResponseTooLarge):
            manager.read(launch.state.preview_id, launch.token, "large.bin")

        connection = http.client.HTTPConnection("127.0.0.1", launch.state.internal_port, timeout=2)
        connection.request("GET", "/large.bin")
        response = connection.getresponse()
        assert response.status == 413
        assert len(response.read()) <= 8
        connection.close()


def test_missing_asset_is_proxied_as_not_found(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(tmp_path, max_response_bytes=8) as manager:
        launch = _start(manager)

        response = manager.read(launch.state.preview_id, launch.token, "missing.js")

        assert response.status_code == 404
        assert response.body == b""


def test_spa_route_falls_back_to_entrypoint_without_masking_missing_assets(
    tmp_path: Path,
) -> None:
    _write(_session_root(tmp_path), "dist/index.html", "<!doctype html><main>SPA</main>")
    _write(_session_root(tmp_path), "dist/assets/app.js", "window.ready = true;")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)

        route = manager.read(launch.state.preview_id, launch.token, "dashboard/settings")
        missing_asset = manager.read(
            launch.state.preview_id,
            launch.token,
            "assets/missing.js",
        )

        assert route.status_code == 200
        assert route.body == b"<!doctype html><main>SPA</main>"
        assert route.header("Content-Type") == "text/html"
        assert missing_asset.status_code == 404
        assert missing_asset.body == b""


@pytest.mark.parametrize("path", ["", "missing.js"])
def test_every_response_has_untrusted_content_security_headers(
    tmp_path: Path,
    path: str,
) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(tmp_path) as manager:
        launch = _start(manager)
        response = manager.read(launch.state.preview_id, launch.token, path)

        assert response.header("Content-Security-Policy") == (
            "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'none'"
        )
        assert response.header("Referrer-Policy") == "no-referrer"
        assert response.header("X-Content-Type-Options") == "nosniff"
        assert response.header("Cache-Control") == "no-store"


def test_renew_never_exceeds_absolute_maximum_and_reap_expires_preview(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")
    now = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)

    with PreviewManager(
        tmp_path,
        default_lease=timedelta(minutes=5),
        max_lifetime=timedelta(minutes=12),
        clock=lambda: now,
    ) as manager:
        launch = _start(manager)
        renewed = manager.renew(
            launch.state.preview_id,
            launch.token,
            extension=timedelta(hours=1),
            now=now + timedelta(minutes=4),
        )

        assert renewed.lease_expires_at == now + timedelta(minutes=12)
        assert renewed.max_expires_at == now + timedelta(minutes=12)
        assert manager.reap_expired(now=now + timedelta(minutes=11)) == ()
        assert manager.reap_expired(now=now + timedelta(minutes=12)) == (
            launch.state.preview_id,
        )
        assert manager.current(TENANT_ID, "conversation-a") is None
        with pytest.raises(PreviewTokenRejected):
            manager.renew(launch.state.preview_id, launch.token)


def test_renew_does_not_shorten_an_existing_lease(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")
    now = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)

    with PreviewManager(
        tmp_path,
        default_lease=timedelta(minutes=30),
        max_lifetime=timedelta(hours=2),
        clock=lambda: now,
    ) as manager:
        launch = _start(manager)

        renewed = manager.renew(
            launch.state.preview_id,
            launch.token,
            extension=timedelta(minutes=5),
            now=now + timedelta(minutes=1),
        )

        assert renewed.lease_expires_at == now + timedelta(minutes=30)


def test_current_expires_stale_preview_without_explicit_reaper(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")
    now = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    clock_value = [now]

    with PreviewManager(
        tmp_path,
        default_lease=timedelta(minutes=5),
        clock=lambda: clock_value[0],
    ) as manager:
        launch = _start(manager)
        clock_value[0] = now + timedelta(minutes=5)

        assert manager.current(TENANT_ID, "conversation-a") is None
        assert manager.stop(launch.state.preview_id).status == "expired"


def test_start_replaces_existing_conversation_preview_and_stop_conversation_is_scoped(
    tmp_path: Path,
) -> None:
    _write(_session_root(tmp_path, session_id="session-a"), "index.html", "first")
    _write(_session_root(tmp_path, session_id="session-b"), "index.html", "second")
    other_tenant = uuid4()
    _write(
        tmp_path / str(other_tenant) / "projects/project-a/sessions/session-a",
        "index.html",
        "other",
    )

    with PreviewManager(tmp_path) as manager:
        first = _start(manager)
        second = _start(manager, session_id="session-b")
        other = manager.start(
            tenant_id=other_tenant,
            conversation_id="conversation-a",
            project_id="project-a",
            session_id="session-a",
        )

        assert first.state.preview_id != second.state.preview_id
        assert manager.stop(first.state.preview_id).status == "stopped"
        assert manager.current(TENANT_ID, "conversation-a") == second.state
        assert manager.current(other_tenant, "conversation-a") == other.state

        stopped = manager.stop_conversation(TENANT_ID, "conversation-a")

        assert stopped is not None
        assert stopped.preview_id == second.state.preview_id
        assert stopped.status == "stopped"
        assert manager.current(other_tenant, "conversation-a") == other.state


def test_close_stops_every_server_and_is_idempotent(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "first")
    other_tenant = uuid4()
    _write(
        tmp_path / str(other_tenant) / "projects/project-b/sessions/session-b",
        "index.html",
        "second",
    )
    manager = PreviewManager(tmp_path)
    first = _start(manager)
    second = manager.start(
        tenant_id=other_tenant,
        conversation_id="conversation-b",
        project_id="project-b",
        session_id="session-b",
    )
    ports = (first.state.internal_port, second.state.internal_port)

    manager.close()
    manager.close()

    assert manager.current(TENANT_ID, "conversation-a") is None
    assert manager.current(other_tenant, "conversation-b") is None
    with pytest.raises(PreviewNotFound):
        manager.stop(first.state.preview_id)
    assert not any(
        thread.is_alive() and thread.name.startswith("preview-server-")
        for thread in threading.enumerate()
    )
    for port in ports:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
        with pytest.raises(OSError):
            connection.request("GET", "/")
        connection.close()


def test_manager_limits_active_previews_but_allows_same_conversation_replacement(
    tmp_path: Path,
) -> None:
    _write(_session_root(tmp_path, session_id="session-a"), "index.html", "first")
    _write(_session_root(tmp_path, session_id="session-b"), "index.html", "second")

    with PreviewManager(tmp_path, max_active_global=1, max_active_per_tenant=1) as manager:
        first = _start(manager)
        replacement = _start(manager, session_id="session-b")

        assert manager.current(TENANT_ID, "conversation-a") == replacement.state
        assert manager.stop(first.state.preview_id).status == "stopped"
        with pytest.raises(PreviewCapacityExceeded):
            _start(
                manager,
                conversation_id="conversation-b",
                session_id="session-a",
            )


@pytest.mark.parametrize(
    ("max_active_global", "max_active_per_tenant", "expected_message"),
    [
        (1, 2, "global preview capacity is exhausted"),
        (2, 1, "tenant preview capacity is exhausted"),
    ],
)
def test_capacity_is_rejected_before_snapshot_or_port_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_active_global: int,
    max_active_per_tenant: int,
    expected_message: str,
) -> None:
    _write(_session_root(tmp_path, session_id="session-a"), "index.html", "first")
    _write(_session_root(tmp_path, session_id="session-b"), "index.html", "second")

    with PreviewManager(
        tmp_path,
        max_active_global=max_active_global,
        max_active_per_tenant=max_active_per_tenant,
    ) as manager:
        _start(manager)
        snapshot_calls = 0
        server_calls = 0
        def counting_snapshot(
            preview_root: Path,
            *,
            max_bytes: int,
            max_files: int,
        ) -> object:
            del preview_root, max_bytes, max_files
            nonlocal snapshot_calls
            snapshot_calls += 1
            raise AssertionError("capacity rejection copied a preview snapshot")

        def counting_server(*args: object, **kwargs: object) -> object:
            del args, kwargs
            nonlocal server_calls
            server_calls += 1
            raise AssertionError("capacity rejection allocated a preview port")

        monkeypatch.setattr(preview_manager_module, "_snapshot_preview_root", counting_snapshot)
        monkeypatch.setattr(preview_manager_module, "_LoopbackPreviewServer", counting_server)

        with pytest.raises(PreviewCapacityExceeded, match=expected_message):
            _start(
                manager,
                conversation_id="conversation-b",
                session_id="session-b",
            )

        assert snapshot_calls == 0
        assert server_calls == 0


@pytest.mark.parametrize(
    ("max_active_global", "max_active_per_tenant"),
    [(1, 2), (2, 1)],
)
def test_concurrent_start_reserves_capacity_and_releases_it_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_active_global: int,
    max_active_per_tenant: int,
) -> None:
    _write(_session_root(tmp_path, session_id="session-a"), "index.html", "first")
    _write(_session_root(tmp_path, session_id="session-b"), "index.html", "second")
    original_snapshot = preview_manager_module._snapshot_preview_root
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    first_errors: list[OSError] = []

    def failing_snapshot(
        preview_root: Path,
        *,
        max_bytes: int,
        max_files: int,
    ) -> tuple[TemporaryDirectory[str], Path]:
        del preview_root, max_bytes, max_files
        snapshot_entered.set()
        assert release_snapshot.wait(timeout=2)
        raise OSError("snapshot failed")

    monkeypatch.setattr(preview_manager_module, "_snapshot_preview_root", failing_snapshot)

    with PreviewManager(
        tmp_path,
        max_active_global=max_active_global,
        max_active_per_tenant=max_active_per_tenant,
    ) as manager:
        def start_first() -> None:
            try:
                _start(manager)
            except OSError as exc:
                first_errors.append(exc)

        thread = threading.Thread(target=start_first)
        thread.start()
        assert snapshot_entered.wait(timeout=2)
        try:
            with pytest.raises(PreviewCapacityExceeded):
                _start(
                    manager,
                    conversation_id="conversation-b",
                    session_id="session-b",
                )
        finally:
            release_snapshot.set()
            thread.join(timeout=2)

        assert not thread.is_alive()
        assert len(first_errors) == 1
        assert isinstance(first_errors[0], OSError)
        assert manager._start_reservations == set()

        monkeypatch.setattr(
            preview_manager_module,
            "_snapshot_preview_root",
            original_snapshot,
        )
        recovered = _start(
            manager,
            conversation_id="conversation-b",
            session_id="session-b",
        )
        assert recovered.state.status == "ready"
        assert manager._start_reservations == set()


def test_port_allocation_failure_releases_start_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(_session_root(tmp_path), "index.html", "home")
    original_server = preview_manager_module._LoopbackPreviewServer

    def failing_server(*args: object, **kwargs: object) -> object:
        raise OSError("port allocation failed")

    with PreviewManager(tmp_path, max_active_global=1) as manager:
        monkeypatch.setattr(preview_manager_module, "_LoopbackPreviewServer", failing_server)
        with pytest.raises(OSError, match="port allocation failed"):
            _start(manager)
        assert manager._start_reservations == set()

        monkeypatch.setattr(preview_manager_module, "_LoopbackPreviewServer", original_server)
        recovered = _start(manager)
        assert recovered.state.status == "ready"
        assert manager._start_reservations == set()


def test_concurrent_start_for_same_conversation_does_not_duplicate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(_session_root(tmp_path), "index.html", "home")
    original_snapshot = preview_manager_module._snapshot_preview_root
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    snapshot_calls = 0
    first_launches: list[PreviewLaunch] = []

    def blocking_snapshot(
        preview_root: Path,
        *,
        max_bytes: int,
        max_files: int,
    ) -> tuple[TemporaryDirectory[str], Path]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            snapshot_entered.set()
            assert release_snapshot.wait(timeout=2)
        return original_snapshot(
            preview_root,
            max_bytes=max_bytes,
            max_files=max_files,
        )

    monkeypatch.setattr(preview_manager_module, "_snapshot_preview_root", blocking_snapshot)

    with PreviewManager(tmp_path, max_active_global=1, max_active_per_tenant=1) as manager:
        def start_first() -> None:
            first_launches.append(_start(manager))

        thread = threading.Thread(target=start_first)
        thread.start()
        assert snapshot_entered.wait(timeout=2)
        try:
            with pytest.raises(PreviewCapacityExceeded, match="already in progress"):
                _start(manager)
        finally:
            release_snapshot.set()
            thread.join(timeout=2)

        assert not thread.is_alive()
        assert len(first_launches) == 1
        assert snapshot_calls == 1
        assert manager.current(TENANT_ID, "conversation-a") is not None
        assert manager._start_reservations == set()


def test_background_reaper_releases_expired_preview_without_follow_up_request(
    tmp_path: Path,
) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(
        tmp_path,
        default_lease=timedelta(milliseconds=40),
        max_lifetime=timedelta(seconds=1),
        reaper_interval=timedelta(milliseconds=10),
    ) as manager:
        launch = _start(manager)
        deadline = time.monotonic() + 2
        released = False
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                launch.state.internal_port,
                timeout=0.05,
            )
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
            except OSError:
                released = True
                break
            finally:
                connection.close()
            time.sleep(0.02)

        assert released is True


def test_stopped_preview_runtime_records_are_bounded(tmp_path: Path) -> None:
    _write(_session_root(tmp_path), "index.html", "home")

    with PreviewManager(tmp_path) as manager:
        latest_id = ""
        for _ in range(270):
            launch = _start(manager)
            latest_id = launch.state.preview_id
            manager.stop(latest_id)

        assert manager._runtimes == {}
        assert len(manager._finished) <= 256
        assert manager.stop(latest_id).status == "stopped"
