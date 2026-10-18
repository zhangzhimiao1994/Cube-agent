from __future__ import annotations

import os
import sys
import zipfile
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.files.workspace import ProjectWorkspaceStore

EXCLUDED_DIRECTORIES = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".bundles",
)


def test_each_bundle_has_exclusive_bytes_while_another_download_rebuilds(tmp_path: Path) -> None:
    tenant = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant, 'project', 'session', 'main.js', b'verified source', 'text/plain')
    first = store.create_session_zip(tenant, 'project', 'session')
    verified = first.path.read_bytes()
    store.write_bytes(tenant, 'project', 'session', 'main.js', b'new unverified source', 'text/plain')
    second = store.create_session_zip(tenant, 'project', 'session')
    assert first.path != second.path
    assert first.path.read_bytes() == verified
    with zipfile.ZipFile(second.path) as archive:
        assert archive.read('main.js') == b'new unverified source'


@pytest.mark.parametrize('private', ['credentials', 'credentials.json', 'client.key', 'id_ed25519'])
def test_zip_cannot_include_files_omitted_from_verification_snapshot(tmp_path: Path, private: str) -> None:
    tenant = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant, 'project', 'session', 'src/main.py', b'source', 'text/plain')
    store.write_bytes(tenant, 'project', 'session', 'src/' + private, b'private', 'text/plain')
    bundle = store.create_session_zip(tenant, 'project', 'session')
    with zipfile.ZipFile(bundle.path) as archive:
        assert archive.namelist() == ['src/main.py']


@pytest.mark.parametrize("directory_name", ("NODE_MODULES", ".VENV", ".PyTest_Cache"))
def test_cache_exclusion_is_stable_on_case_insensitive_filesystems(
    tmp_path: Path, directory_name: str,
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    cache = store.session_root(tenant_id, "project", "session") / directory_name
    cache.mkdir()
    (cache / "cached.py").write_bytes(b"cached")
    _exercise(store, tenant_id, "list")


def _create_junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _exercise(store: ProjectWorkspaceStore, tenant_id: UUID, operation: str) -> None:
    if operation == "list":
        assert [item.path for item in store.list_files(tenant_id, "project", "session")] == [
            "src/main.py"
        ]
    elif operation == "zip":
        bundle = store.create_session_zip(tenant_id, "project", "session")
        with zipfile.ZipFile(bundle.path) as archive:
            assert archive.namelist() == ["src/main.py"]
            assert archive.read("src/main.py") == b"source"
    else:
        assert store.prune_files(
            tenant_id, "project", "session", keep_paths=("src/main.py",)
        ) == ()


@pytest.mark.parametrize("directory_name", EXCLUDED_DIRECTORIES)
@pytest.mark.parametrize("prefix", ("", "packages/app"))
def test_list_skips_dependency_and_cache_directories_at_any_depth(
    tmp_path: Path, directory_name: str, prefix: str
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    cache = store.session_root(tenant_id, "project", "session") / prefix / directory_name
    (cache / "nested").mkdir(parents=True)
    (cache / "nested" / "cached.py").write_bytes(b"cached")

    _exercise(store, tenant_id, "list")


@pytest.mark.parametrize("operation", ("list", "zip", "prune"))
@pytest.mark.parametrize("populated", (False, True), ids=("empty", "large"))
def test_workspace_operations_never_scan_or_read_excluded_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, populated: bool
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(
        tmp_path, max_file_bytes=8, max_bundle_files=1, max_bundle_bytes=8
    )
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    root = store.session_root(tenant_id, "project", "session")
    caches = [root / "packages" / name for name in EXCLUDED_DIRECTORIES]
    for cache in caches:
        cache.mkdir(parents=True)
        if populated:
            nested = cache / "nested"
            nested.mkdir()
            for index in range(128):
                (nested / f"cached-{index}.bin").write_bytes(b"x" * 64)

    original_scandir = os.scandir
    original_iterdir = Path.iterdir
    original_read_bytes = Path.read_bytes

    def check_path(path: str | os.PathLike[str]) -> None:
        candidate = Path(path)
        assert not any(candidate.is_relative_to(cache) for cache in caches), (
            f"excluded tree was accessed: {candidate}"
        )

    def scandir(
        path: str | os.PathLike[str],
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        check_path(path)
        return original_scandir(path)

    def iterdir(path: Path) -> Iterator[Path]:
        check_path(path)
        return original_iterdir(path)

    def read_bytes(path: Path) -> bytes:
        check_path(path)
        return original_read_bytes(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    _exercise(store, tenant_id, operation)


def test_zip_keeps_manifests_locks_configuration_sources_and_build_outputs(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    paths = (
        ".gitignore",
        "build/app.js",
        "dist/app.js",
        "node_modules",
        "node_modules.txt",
        "package.json",
        "package/library.py",
        "package-lock.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "src/main.py",
        "tsconfig.json",
        "uv.lock",
        "vite.config.ts",
        "yarn.lock",
    )
    for path in paths:
        store.write_bytes(tenant_id, "project", "session", path, b"delivery", "text/plain")
    root = store.session_root(tenant_id, "project", "session")
    for name in EXCLUDED_DIRECTORIES:
        cache = root / "package" / name
        cache.mkdir(parents=True)
        (cache / "cached.bin").write_bytes(b"cache")

    # Repeated ZIP creation must also skip the previously generated root bundle.
    for _ in range(2):
        bundle = store.create_session_zip(tenant_id, "project", "session")
        assert [item.path for item in bundle.files] == sorted(paths)
        with zipfile.ZipFile(bundle.path) as archive:
            assert archive.namelist() == sorted(paths)
            assert all(archive.read(path) == b"delivery" for path in paths)


@pytest.mark.parametrize("directory_name", EXCLUDED_DIRECTORIES)
@pytest.mark.parametrize("populated", (False, True), ids=("empty", "populated"))
def test_prune_preserves_excluded_directories_and_their_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str, populated: bool
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    root = store.session_root(tenant_id, "project", "session")
    cache = root / "packages" / "app" / directory_name
    cache.mkdir(parents=True)
    if populated:
        (cache / "cached.bin").write_bytes(b"cache")
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    store.write_bytes(tenant_id, "project", "session", "packages/app/old.py", b"old", "text/plain")
    store.write_bytes(tenant_id, "project", "session", "obsolete/old.py", b"old", "text/plain")
    (root / "empty" / "nested").mkdir(parents=True)
    protected = {cache, *cache.parents}
    original_rmdir = Path.rmdir

    def rmdir(path: Path) -> None:
        assert path not in protected, f"attempted to remove a protected directory: {path}"
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", rmdir)

    assert store.prune_files(
        tenant_id, "project", "session", keep_paths=("src/main.py",)
    ) == ("obsolete/old.py", "packages/app/old.py")
    assert cache.is_dir()
    assert cache.parent.is_dir()
    if populated:
        assert (cache / "cached.bin").read_bytes() == b"cache"
    assert not (root / "obsolete").exists()
    assert not (root / "empty").exists()
    assert not (cache.parent / "old.py").exists()


@pytest.mark.parametrize("operation", ("list", "zip", "prune"))
@pytest.mark.parametrize("kind", ("file", "directory", "junction"))
def test_workspace_operations_skip_internal_and_external_links(
    tmp_path: Path, operation: str, kind: str
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path / "workspaces")
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    root = store.session_root(tenant_id, "project", "session")
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.py").write_bytes(b"secret")
    links = []
    for name, target in (("internal", root / "src"), ("external", external)):
        link = root / name
        if kind == "junction":
            _create_junction(link, target)
        else:
            if kind == "file":
                target = target / ("main.py" if name == "internal" else "secret.py")
            try:
                link.symlink_to(target, target_is_directory=kind == "directory")
            except OSError:
                pytest.skip("symlink creation is not available on this platform")
        links.append(link)

    _exercise(store, tenant_id, operation)

    assert all(link.exists() for link in links)
    assert (external / "secret.py").read_bytes() == b"secret"
    assert (root / "src" / "main.py").read_bytes() == b"source"
    if kind == "junction":
        with pytest.raises(ValueError, match="workspace path"):
            store.resolve_file(tenant_id, "project", "session", "external/secret.py")


@pytest.mark.parametrize("component", ("tenant", "projects", "project", "sessions", "session"))
def test_workspace_still_rejects_scope_junctions_to_another_tenant(
    tmp_path: Path, component: str
) -> None:
    tenant_id, other_tenant_id = uuid4(), uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(other_tenant_id, "project", "session", "private.txt", b"private", "text/plain")
    names = [str(tenant_id), "projects", "project", "sessions", "session"]
    other_names = [str(other_tenant_id), "projects", "project", "sessions", "session"]
    index = ("tenant", "projects", "project", "sessions", "session").index(component)
    link = tmp_path.joinpath(*names[:index + 1])
    target = tmp_path.joinpath(*other_names[:index + 1])
    link.parent.mkdir(parents=True, exist_ok=True)
    _create_junction(link, target)

    for operation in ("list", "zip", "prune"):
        with pytest.raises(ValueError, match="workspace path"):
            _exercise(store, tenant_id, operation)
    with pytest.raises(ValueError, match="workspace path"):
        store.resolve_file(tenant_id, "project", "session", "private.txt")
    with pytest.raises(ValueError, match="workspace path"):
        store.write_bytes(tenant_id, "project", "session", "private.txt", b"changed", "text/plain")
    assert store.resolve_file(other_tenant_id, "project", "session", "private.txt").read_bytes() == (
        b"private"
    )


@pytest.mark.parametrize("operation", ("list", "zip", "prune"))
@pytest.mark.parametrize("hidden_path", (".env", ".git/config", "src/.gitignore"))
def test_exclusions_preserve_rejection_of_other_hidden_paths(
    tmp_path: Path, operation: str, hidden_path: str
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant_id, "project", "session", "src/main.py", b"source", "text/plain")
    hidden = store.session_root(tenant_id, "project", "session") / hidden_path
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_bytes(b"private")

    with pytest.raises(ValueError, match="workspace path"):
        _exercise(store, tenant_id, operation)

    assert hidden.read_bytes() == b"private"
