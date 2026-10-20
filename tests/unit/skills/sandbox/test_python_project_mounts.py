"""Python phase mounts cannot expose writable source or a host cache to hooks."""

import importlib
import importlib.util
from contextlib import ExitStack
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def mounts() -> Any:
    name = 'agent_hub.skills.sandbox.python_project_mounts'
    assert importlib.util.find_spec(name) is not None, 'Python isolated mount adapter missing'
    return importlib.import_module(name)


@pytest.mark.parametrize('phase,environment,output,python', [
    ('install', '--bind', '--ro-bind', '/runtime/bin/python3.12'),
    ('build', '--ro-bind', '--bind', '/environment/bin/python'),
    ('test', '--ro-bind', '--ro-bind', '/environment/bin/python'),
    ('check_environment', '--ro-bind', '--ro-bind', '/environment/bin/python'),
])
def test_fixed_phase_mount_permissions_and_commands(mounts: Any, phase: str, environment: str, output: str, python: str) -> None:
    args = mounts.python_phase_mount_arguments(phase, environment_fd=20, output_fd=21,
        phase_fd=22, import_module='billing.core')
    assert args[:6] == (environment, '/proc/self/fd/20', '/environment', output, '/proc/self/fd/21', '/output')
    assert args[6:10] == ('--ro-bind-data', '22', '/trusted/phase.py', '--')
    assert args[10:] == (python, '-I', '-B', '/trusted/phase.py', phase, '--import-module', 'billing.core')


@pytest.mark.parametrize('phase', ['shell', 'pip', '../build'])
def test_no_arbitrary_execution_phase(mounts: Any, phase: str) -> None:
    with pytest.raises(ValueError):
        mounts.python_phase_mount_arguments(phase, environment_fd=20, output_fd=21, phase_fd=22, import_module='billing')


@pytest.mark.parametrize('value', [True, -1, '20'])
def test_descriptor_inputs_are_strict_not_paths(mounts: Any, value: object) -> None:
    with pytest.raises(ValueError):
        mounts.python_phase_mount_arguments('build', environment_fd=value, output_fd=21, phase_fd=22, import_module='billing')


def test_networkless_prefix_has_only_readonly_input_bindings(mounts: Any) -> None:
    args = mounts.python_phase_prefix(work_root=PurePosixPath('/run/work/source'), runtime_fd=30, wheel_fd=31)
    assert args[0] == '/usr/bin/bwrap'
    assert '--unshare-net' in args and '--clearenv' in args
    assert args[args.index('--bind'):args.index('--bind') + 3] == ('--bind', '/run/work/source', '/workspace')
    assert args.count('--bind') == 1  # Reconstructed read-only by the contract helper before execution.
    assert ('--ro-bind', '/proc/self/fd/30', '/runtime') == args[args.index('/runtime') - 2:args.index('/runtime') + 1]
    assert '/var/lib/agent-hub' not in args
    assert 'LD_LIBRARY_PATH' in args and '/runtime/lib' in args
    assert args[-2:] == ('--', '/usr/bin/true')  # Only a placeholder until the source contract is reconstructed.


@pytest.mark.parametrize('module', ['../billing', 'billing;exec', '', 'class'])
def test_module_is_a_qualified_identifier_not_command_text(mounts: Any, module: str) -> None:
    with pytest.raises(ValueError):
        mounts.python_phase_mount_arguments('test', environment_fd=20, output_fd=21, phase_fd=22, import_module=module)


def test_script_read_may_change_atime_but_not_content_identity(mounts: Any) -> None:
    values = {'st_dev': 1, 'st_ino': 2, 'st_mode': 0o100600, 'st_uid': 0, 'st_gid': 0,
        'st_nlink': 1, 'st_size': 3, 'st_mtime_ns': 4, 'st_ctime_ns': 5, 'st_atime_ns': 6}
    first = SimpleNamespace(**values)
    accessed = SimpleNamespace(**(values | {'st_atime_ns': 99}))
    modified = SimpleNamespace(**(values | {'st_mtime_ns': 99}))
    assert mounts._file_stamp(first) == mounts._file_stamp(accessed)
    assert mounts._file_stamp(first) != mounts._file_stamp(modified)


def test_all_inherited_directory_descriptors_are_closed_before_project_code(mounts: Any) -> None:
    command = ('/environment/bin/python', '-I', '-B', '/trusted/phase.py', 'test')
    code = 'trusted-close-and-exec'
    suffix = mounts._closed_execution(command, (20, 21, 22, 23, 24), code)
    assert suffix[:7] == ('--', '/usr/bin/python3', '-I', '-S', '-B', '-c', code)
    assert suffix[7] == '20,21,22,23,24'
    assert suffix[8:] == command
    with pytest.raises(ValueError):
        mounts._closed_execution(command, (20, True), code)


@pytest.mark.parametrize('operation', ['_pin_directory', '_seal_script'])
def test_native_descriptor_helpers_fail_closed_on_windows(
    mounts: Any, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    monkeypatch.setattr(mounts.sys, 'platform', 'win32')
    with ExitStack() as stack, pytest.raises(ValueError, match='python_project_mounts_rejected'):
        getattr(mounts, operation)(PurePosixPath('/host/path'), stack, [])


def test_owned_output_subdirectories_do_not_change_directory_identity(mounts: Any) -> None:
    values = {'st_dev': 1, 'st_ino': 2, 'st_mode': 0o40700, 'st_uid': 3, 'st_gid': 3,
        'st_nlink': 2, 'st_size': 3, 'st_mtime_ns': 4, 'st_ctime_ns': 5}
    before = SimpleNamespace(**values)
    after = SimpleNamespace(**(values | {'st_nlink': 5, 'st_size': 6, 'st_mtime_ns': 7, 'st_ctime_ns': 8}))
    replaced = SimpleNamespace(**(values | {'st_ino': 99}))
    assert mounts._identity(before) == mounts._identity(after)
    assert mounts._identity(before) != mounts._identity(replaced)
    linked_file = SimpleNamespace(**(values | {'st_nlink': 3}))
    assert mounts._file_stamp(before) != mounts._file_stamp(linked_file)
