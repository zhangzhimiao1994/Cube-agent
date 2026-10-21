"""Native mount boundary: Windows structure checks, real Linux bwrap when available."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "src/agent_hub/skills/sandbox/project_contract_mounts.py"
SANDBOX = SOURCE.parents[2] / "harness/project_validation_sandbox.py"


def helper() -> Any:
    assert SOURCE.is_file(), "native contract mount helper is missing"
    spec = importlib.util.spec_from_file_location("native_contract_mounts", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def digest(data: bytes, *, executable: bool = False) -> str:
    return hashlib.sha256((b"x\0" if executable else b"f\0") + data).hexdigest()


def source_hash(root: Path, *, outputs: tuple[str, ...] = ()) -> str:
    members = {
        file.relative_to(root).as_posix(): digest(file.read_bytes(), executable=bool(file.stat().st_mode & 0o111))
        for file in root.rglob("*") if file.is_file()
        and file.relative_to(root).parts[0] not in outputs
    }
    return hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode("ascii")).hexdigest()


@pytest.fixture
def project(tmp_path: Path) -> Iterator[dict[str, Any]]:
    original, work = tmp_path / "original", tmp_path / "work"
    for name, content in {
        "package.json": b'{"scripts":{"test":"node --test tests/original.test.js"}}',
        "tests/original.test.js": b"original assertion; never executed on host\n",
        "tests/fixture.json": b'{"expected":3}', "src/app.js": b"buggy implementation\n",
    }.items():
        file = original / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    shutil.copytree(original, work)
    protected = {name: digest((original / name).read_bytes()) for name in (
        "package.json", "tests/original.test.js", "tests/fixture.json",
    )}
    binding = {"schema_version": 1, "tenant_id": "10000000-0000-4000-8000-000000000001",
               "run_id": "10000000-0000-4000-8000-000000000002", "project_id": "project-a",
               "session_id": "session-a", "input_source_sha256": source_hash(original),
               "editable_paths": ["src"], "protected_files": protected}
    for file in original.rglob("*"):
        file.chmod(0o555 if file.is_dir() else 0o444)
    original.chmod(0o555)
    try:
        yield {"original": original, "work": work, "protected": protected, "binding": binding}
    finally:
        original.chmod(0o700)
        for file in original.rglob("*"):
            file.chmod(0o700 if file.is_dir() else 0o600)


def arguments(project: dict[str, Any], **changes: Any) -> dict[str, Any]:
    binding = {**project["binding"], "protected_files": changes.pop("protected_files", project["protected"])}
    contract_sha = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True).encode("ascii")).hexdigest()
    result = {"contract_payload": {**binding, "protected_sha256": contract_sha},
              "expected_contract_sha256": contract_sha, "original_root": project["original"],
              "work_root": project["work"], "expected_source_sha256": source_hash(project["work"])}
    result.update(changes)
    return result


def test_native_helper_captures_patched_candidate_but_keeps_original_checks(project: dict[str, Any]) -> None:
    module = helper()
    (project["work"] / "src/app.js").write_bytes(b"fixed by trusted workspace API before verification\n")
    view = module.validate_contract_work(**arguments(project))
    assert view.source_sha256 == source_hash(project["work"])
    assert set(view.protected_paths) == set(project["protected"])
    assert set(view.readonly_paths) == {"src/app.js", *project["protected"]}
    assert view.ephemeral_dirs == ()


def test_trusted_source_budgets_are_explicit_not_fixed_global_thresholds(project: dict[str, Any]) -> None:
    module = helper()
    actual = sum(file.stat().st_size for file in project["original"].rglob("*") if file.is_file())
    for options in ({"source_file_limit": 3}, {"source_byte_limit": actual - 1}):
        with pytest.raises(module.ContractMountError):
            module.validate_contract_work(**arguments(project), **options)
    view = module.validate_contract_work(**arguments(project), source_file_limit=4,
        source_byte_limit=actual, source_timeout_seconds=30)
    assert len(view.readonly_paths) == 4


@pytest.mark.parametrize("options", [
    {"source_file_limit": True}, {"source_file_limit": 0}, {"source_byte_limit": 0},
    {"source_byte_limit": True}, {"source_timeout_seconds": float("nan")},
    {"source_timeout_seconds": float("inf")}, {"source_timeout_seconds": 0},
    {"source_timeout_seconds": True}, {"source_timeout_seconds": 10**1000},
])
def test_bad_trusted_source_budgets_fail_closed(project: dict[str, Any], options: dict[str, Any]) -> None:
    module = helper()
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project), **options)


def test_native_helper_and_shared_validator_load_without_application_imports(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def stdlib_only(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name.split(".")[0] in sys.stdlib_module_names, name
        return original_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("isolated_mount_helper", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.name not in sys.modules
    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "__import__", stdlib_only)
        spec.loader.exec_module(module)
        view = module.validate_contract_work(**arguments(project))
    assert view.source_sha256 == source_hash(project["work"])


@pytest.mark.parametrize("name", ["package.json", "tests/original.test.js", "tests/fixture.json"])
def test_modified_original_check_is_rejected_even_with_updated_candidate_hash(
    project: dict[str, Any], name: str,
) -> None:
    module = helper()
    (project["work"] / name).write_bytes(b"weakened check")
    with pytest.raises(module.ContractMountError, match="project_contract_mounts_rejected"):
        module.validate_contract_work(**arguments(project))


def test_unlink_replace_and_missing_check_are_rejected(project: dict[str, Any]) -> None:
    module = helper()
    target = project["work"] / "tests/original.test.js"
    target.unlink()
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project))
    target.write_bytes(b"replacement")
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project))


def test_new_test_cannot_enter_after_candidate_source_was_bound(project: dict[str, Any]) -> None:
    module, inputs = helper(), arguments(project)
    (project["work"] / "tests/injected.test.js").write_bytes(b"new assertion")
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**inputs)


def test_shared_contract_rejects_new_check_even_with_updated_candidate_digest(project: dict[str, Any]) -> None:
    module = helper()
    (project["work"] / "tests/injected.test.js").write_bytes(b"injected check")
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project))


@pytest.mark.parametrize("fault", ["foreign_digest", "extra_authority", "changed_original_app"])
def test_shared_pure_contract_and_original_input_binding_are_mandatory(
    project: dict[str, Any], fault: str,
) -> None:
    module, inputs = helper(), arguments(project)
    if fault == "foreign_digest":
        inputs["expected_contract_sha256"] = "0" * 64
    elif fault == "extra_authority":
        inputs["contract_payload"]["install_allowed"] = True
    else:
        file = project["original"] / "src/app.js"
        file.chmod(0o600)
        file.write_bytes(b"different original app")
        file.chmod(0o444)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**inputs)


def test_empty_directory_injection_does_not_change_hash_but_is_still_rejected(project: dict[str, Any]) -> None:
    module, inputs = helper(), arguments(project)
    (project["work"] / "tests/injected").mkdir()
    assert source_hash(project["work"]) == inputs["expected_source_sha256"]
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**inputs)


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "tests//x", "tests/./x",
    "tests\\x", "tests/x ", "tests/x.", "tests/AUX", "tests/x:stream", "tests/x\n"])
def test_unsafe_protected_paths_never_reach_filesystem(project: dict[str, Any], relative: str) -> None:
    module = helper()
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, protected_files={relative: "a" * 64}))


@pytest.mark.parametrize("change", ["digest", "nonhex", "upper", "empty", "parent", "case_alias"])
def test_protected_manifest_is_not_a_pattern_or_unchecked_string(project: dict[str, Any], change: str) -> None:
    module = helper()
    protected = dict(project["protected"])
    if change == "empty":
        protected = {}
    elif change == "parent":
        protected["tests"] = "a" * 64
    elif change == "case_alias":
        protected["TESTS/original.test.js"] = protected["tests/original.test.js"]
    else:
        protected["package.json"] = ("0" if change == "digest" else "g" if change == "nonhex" else "A") * 64
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, protected_files=protected))


@pytest.mark.parametrize("original", [False, True])
def test_protected_hardlink_alias_is_rejected(project: dict[str, Any], original: bool) -> None:
    module = helper()
    root = project["original"] if original else project["work"]
    root.chmod(0o700)
    try:
        os.link(root / "package.json", root / "alias.json")
    except PermissionError:
        if sys.platform == "win32":
            pytest.skip("Windows filesystem policy denies hardlink creation")
        raise
    root.chmod(0o555 if original else 0o700)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project))


@pytest.mark.parametrize("which", ["original", "work", "ancestor"])
def test_source_or_ancestor_symlink_is_rejected(project: dict[str, Any], tmp_path: Path, which: str) -> None:
    module = helper()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(project["original"] if which == "original" else project["work"], target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable to this Windows user")
    changes = {"original_root": alias} if which == "original" else {"work_root": alias}
    if which == "ancestor":
        changes = {"work_root": alias / "src"}
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, **changes))


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable/immutable modes")
@pytest.mark.parametrize("change", ["work_execute", "original_execute", "original_write", "directory_write"])
def test_protected_mode_and_immutable_snapshot_are_checked(project: dict[str, Any], change: str) -> None:
    module = helper()
    if change == "directory_write":
        project["original"].chmod(0o755)
    else:
        root = project["work"] if change == "work_execute" else project["original"]
        (root / "package.json").chmod(0o600 if change == "original_write" else 0o555)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project))


@pytest.mark.parametrize("name", ["node_modules", "dist", "build"])
def test_owned_empty_outputs_are_explicit_and_source_readonly(project: dict[str, Any], name: str) -> None:
    module = helper()
    (project["work"] / name).mkdir()
    view = module.validate_contract_work(**arguments(project, ephemeral_dirs=(name,), ephemeral_budget_bytes=4096))
    assert view.ephemeral_dirs == (name,)
    assert "src/app.js" in view.readonly_paths


def test_output_descriptors_are_passed_to_bwrap_through_its_own_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = helper()
    view = module.ContractWork("a" * 64, "b" * 64, (), (), ("dist",), 4096, (), ())
    work = SimpleNamespace(directories={"dist": SimpleNamespace(fd=123)}, unchanged=lambda: None)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(
        open=os.open, supports_dir_fd={os.open}, memfd_create=object(), getpid=lambda: 4321,
    ))
    monkeypatch.setattr(module, "_capture", lambda *args, **kwargs: (view, work))
    command = ["/usr/bin/bwrap", "--bind", str(tmp_path), "/workspace", "--", "/usr/bin/true"]
    with module.build_contract_mounts(command, contract_payload={}, expected_contract_sha256="b" * 64,
            original_root=tmp_path, work_root=tmp_path, expected_source_sha256="a" * 64,
            ephemeral_dirs=("dist",), ephemeral_budget_bytes=4096) as plan:
        assert plan.pass_fds == (123,)
        position = plan.argv.index("--bind")
        assert ("--bind", "/proc/self/fd/123", "/workspace/dist") == plan.argv[position:position + 3]
        assert not any("/proc/4321/" in argument for argument in plan.argv)
        launcher = plan.argv[plan.argv.index("--") + 1:]
        assert launcher[:5] == ("/usr/bin/python3", "-I", "-S", "-B", "-c")
        assert launcher[-2:] == ("123", "/usr/bin/true")


def test_project_launcher_closes_inherited_descriptors_before_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    module = helper()
    calls: list[tuple[str, Any]] = []

    def close(fd: int) -> None:
        calls.append(("close", fd))
        if fd == 17:
            raise OSError(9, "already consumed by bwrap")

    fake_os = SimpleNamespace(close=close, execv=lambda *args: calls.append(("exec", args)))
    fake_sys = SimpleNamespace(argv=["-c", "17,18", "/usr/bin/true", "argument"])
    original_import = builtins.__import__

    def imported(name: str, *args: Any, **kwargs: Any) -> Any:
        return {"os": fake_os, "sys": fake_sys}.get(name) or original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", imported)
    exec(compile(module._CLOSE_FDS_AND_EXEC, "<trusted-fd-launcher>", "exec"), {})  # noqa: S102 - trusted literal, mocked execv.
    assert calls == [("close", 17), ("close", 18), ("exec", ("/usr/bin/true", ["/usr/bin/true", "argument"]))]


@pytest.mark.parametrize("name", ["node_modules", "dist", "build"])
def test_outputs_never_hide_original_input_even_if_not_in_protected_map(project: dict[str, Any], name: str) -> None:
    module = helper()
    project["original"].chmod(0o700)
    output = project["original"] / name
    output.mkdir()
    (output / "original.js").write_bytes(b"source")
    (output / "original.js").chmod(0o444)
    output.chmod(0o555)
    project["original"].chmod(0o555)
    (project["work"] / name).mkdir()
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, ephemeral_dirs=(name,), ephemeral_budget_bytes=4096))


@pytest.mark.parametrize("options", [
    {"ephemeral_dirs": ("src",), "ephemeral_budget_bytes": 4096},
    {"ephemeral_dirs": ("tests",), "ephemeral_budget_bytes": 4096},
    {"ephemeral_dirs": ("dist", "dist"), "ephemeral_budget_bytes": 4096},
    {"ephemeral_dirs": ("dist",), "ephemeral_budget_bytes": 0},
    {"ephemeral_dirs": ("dist",), "ephemeral_budget_bytes": True},
])
def test_output_scope_is_fixed_and_requires_native_budget(project: dict[str, Any], options: dict[str, Any]) -> None:
    module = helper()
    (project["work"] / "dist").mkdir()
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, **options))


def test_output_budget_is_checked_before_mount(project: dict[str, Any]) -> None:
    module = helper()
    (project["work"] / "dist").mkdir()
    (project["work"] / "dist/large").write_bytes(b"x" * 100)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, ephemeral_dirs=("dist",), ephemeral_budget_bytes=99))


def generated_command(project: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sandbox = runpy.run_path(str(SANDBOX))
    monkeypatch.setitem(sandbox["generated_command"].__globals__, "sandbox_available", lambda: True)
    return list(sandbox["generated_command"](("node", "--version"), cwd=project["work"], config={}))


def test_structural_command_uses_only_readonly_source_and_named_output_mounts(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = helper()
    (project["work"] / "dist").mkdir()
    view = module.validate_contract_work(**arguments(project, ephemeral_dirs=("dist",), ephemeral_budget_bytes=4096))
    prefix, suffix = module._command_prefix(generated_command(project, monkeypatch), project["work"])
    # Arbitrary descriptors are STRUCTURE ONLY; this does not claim Linux execution.
    mounts = module._mount_arguments(view, tuple(range(10, 14)), {"dist": "/proc/123/fd/99"})
    assert suffix == ("--", "/opt/validator/node/bin/node", "--version") or suffix == (
        "--", "/usr/bin/node", "--version")
    assert str(project["work"]) not in prefix
    assert mounts[:2] == ("--tmpfs", "/workspace")
    assert mounts[-5:] == ("--remount-ro", "/workspace", "--bind", "/proc/123/fd/99", "/workspace/dist")
    assert mounts.count("--bind") == 1
    for name in ("package.json", "tests/original.test.js", "tests/fixture.json", "src/app.js"):
        position = mounts.index("/workspace/" + name)
        assert mounts[position - 2] == "--ro-bind-data"


def test_only_exact_npm_config_null_mask_survives_contract_reassembly(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = helper()
    argv = generated_command(project, monkeypatch)
    position = argv.index('--')
    argv[position:position] = ['--ro-bind', '/dev/null', '/workspace/.npmrc']
    prefix, suffix = module._command_prefix(argv, project['work'])
    assert suffix[0] == '--'
    assert '/workspace/.npmrc' in prefix
    sealed = module._contract_argv(argv, project['work'], ('--tmpfs', '/workspace', '--remount-ro', '/workspace'))
    assert sealed.index('/workspace/.npmrc') > sealed.index('--remount-ro')
    for altered in ('/workspace/package.json', '/workspace/.env'):
        with pytest.raises(module.ContractMountError):
            module._command_prefix([altered if value == '/workspace/.npmrc' else value for value in argv],
                                   project['work'])


@pytest.mark.parametrize("tail", [
    ("--bind", "/host", "/outside"),
    ("--ro-bind", "/dev/null", "/workspace/package.json"),
    ("--tmpfs", "/workspace/tests"),
    ("--ro-bind", "/host", "/"),
    ("--ro-bind", "/host", "/workspace/../workspace/tests"),
    ("--args", "4"),
])
def test_conflicting_or_indirect_mount_options_are_rejected(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tail: tuple[str, ...],
) -> None:
    module = helper()
    argv = generated_command(project, monkeypatch)
    position = argv.index("--")
    argv[position:position] = tail
    with pytest.raises(module.ContractMountError):
        module._command_prefix(argv, project["work"])


def test_late_file_mutation_during_capture_cannot_be_sealed_as_verified(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, inputs = helper(), arguments(project)
    original_read = module.os.read
    changed = False
    app_reads = 0

    def read(fd: int, amount: int) -> bytes:
        nonlocal changed, app_reads
        result: bytes = original_read(fd, amount)
        if result == b"buggy implementation\n":
            app_reads += 1
        if app_reads == 2 and not changed:
            changed = True
            (project["work"] / "package.json").write_bytes(b"late replacement of an already read input")
        return result

    monkeypatch.setattr(module.os, "read", read)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**inputs)
    assert changed


def test_readonly_capture_does_not_follow_later_source_replacement(project: dict[str, Any]) -> None:
    module = helper()
    view = module.validate_contract_work(**arguments(project))
    (project["work"] / "tests/original.test.js").write_bytes(b"late host replacement")
    captured = {item.path: item.data for item in view._files}
    assert captured["tests/original.test.js"] == b"original assertion; never executed on host\n"


def test_runtime_owned_manifest_is_readonly_source_not_a_writable_exception(project: dict[str, Any]) -> None:
    module = helper()
    (project["work"] / "DELIVERY_MANIFEST.json").write_bytes(b"runtime metadata")
    view = module.validate_contract_work(**arguments(project))
    assert "DELIVERY_MANIFEST.json" in view.readonly_paths and not view.ephemeral_dirs


@pytest.mark.parametrize("fault", ["bytes", "files"])
def test_output_budget_is_aggregate_not_per_directory(project: dict[str, Any], fault: str) -> None:
    module = helper()
    for name in ("dist", "build"):
        (project["work"] / name).mkdir()
        (project["work"] / name / "file").write_bytes(b"x" * 10)
    with pytest.raises(module.ContractMountError):
        module.validate_contract_work(**arguments(project, ephemeral_dirs=("dist", "build"),
            ephemeral_budget_bytes=19 if fault == "bytes" else 100, ephemeral_file_limit=3))


@pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux sealing and bwrap")
def test_real_generated_command_mounts_are_readonly_and_source_fds_are_sealed(
    project: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux FD sealing")
    module = helper()
    sandbox = runpy.run_path(str(SANDBOX))
    monkeypatch.setitem(sandbox["generated_command"].__globals__, "sandbox_available", lambda: True)
    argv = sandbox["generated_command"](("node", "--version"), cwd=project["work"], config={})
    with module.build_contract_mounts(argv, **arguments(project)) as plan:
        assert plan.argv[-2:] == tuple(argv[-2:])
        assert "--remount-ro" in plan.argv and "--ro-bind-data" in plan.argv
        assert len(plan.pass_fds) == 4
        import fcntl
        for fd in plan.pass_fds:
            assert fcntl.fcntl(fd, 1034) & 15 == 15
        descriptors = plan.pass_fds
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(sys.platform != "linux", reason="real bwrap unavailable on Windows; not a Linux PASS")
@pytest.mark.parametrize("operation", [
    "open('/workspace/tests/original.test.js','w').write('fake')",
    "os.unlink('/workspace/tests/original.test.js')",
    "os.unlink('/workspace/tests/original.test.js'); open('/workspace/tests/original.test.js','w').write('replacement')",
    "os.mkdir('/workspace/tests/injected')",
    "open('/workspace/package.json','w').write('{}')",
    "open('/workspace/src/app.js','w').write('fix hidden in private copy')",
])
def test_real_bwrap_denies_check_and_source_mutation(project: dict[str, Any], operation: str) -> None:
    run_real_bwrap(project, operation, denied=True)


@pytest.mark.skipif(sys.platform != "linux", reason="real bwrap unavailable on Windows; not a Linux PASS")
def test_real_bwrap_runs_prepatched_source_and_only_writes_owned_output(project: dict[str, Any]) -> None:
    (project["work"] / "src/app.js").write_bytes(b"prepatched app source")
    run_real_bwrap(project, "assert open('/workspace/src/app.js').read() == 'prepatched app source'; "
                   "open('/workspace/dist/result','w').write('built')", denied=False)
    assert (project["work"] / "dist/result").read_text() == "built"


def run_real_bwrap(project: dict[str, Any], operation: str, *, denied: bool) -> None:
    module = helper()
    if not Path("/usr/bin/bwrap").is_file() or not Path("/usr/bin/python3").is_file():
        pytest.skip("bwrap or system Python not installed; no native mount proof")
    sandbox = runpy.run_path(str(SANDBOX))
    sandbox["sandbox_command"].__globals__["sandbox_available"] = lambda: True
    command = sandbox["sandbox_command"](("/usr/bin/python3", "-I", "-c", "pass"),
                                           cwd=project["work"], shared_network=False, config={})
    available = subprocess.run(command, capture_output=True, timeout=10, check=False)
    if available.returncode:
        pytest.skip("bwrap user namespace unavailable; no native mount proof")
    (project["work"] / "dist").mkdir()
    if denied:
        command[-1] = (
            "import errno, os\ntry:\n    " + operation + "\nexcept OSError as error:\n"
            "    assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM, errno.EBUSY, errno.EXDEV)\n"
            "    print('DENIED_READONLY')\nelse:\n    raise SystemExit(42)\n"
        )
    else:
        command[-1] = "import os; " + operation
    with module.build_contract_mounts(command, **arguments(project, ephemeral_dirs=("dist",),
            ephemeral_budget_bytes=4096)) as plan:
        leak_check = (
            "\nfor descriptor in " + repr(plan.pass_fds) + ":\n"
            "    try:\n        os.fstat(descriptor)\n"
            "    except OSError as error:\n        assert error.errno == 9\n"
            "    else:\n        raise AssertionError('inherited broker descriptor')\n"
        )
        argv = (*plan.argv[:-1], plan.argv[-1] + leak_check)
        result = subprocess.run(argv, pass_fds=plan.pass_fds, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    if denied:
        assert result.stdout == b"DENIED_READONLY\n"
