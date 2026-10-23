"""Fixed offline Go phases for root CLI projects, using sealed contract source.

Only the trusted outer systemd runner supplies host paths. toolchain_root is
the GoRoot itself (bin/go), already mounted readable by DynamicUser, e.g. from
the published 0555 go leaf onto /run/agent-hub-go-sdk. Never relax permissions
on its root-owned publication parent. This adapter does not validate libraries.

Mount permissions are not quotas: the caller must enforce outer cgroup limits
and quota-tmpfs storage for work, cache and output, pass only plan.pass_fds,
and reap the subprocess before leaving the context.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import cast

from agent_hub.capabilities.go_project_recipe import _module_path

from .go_project_canary import GoOverlayCanary
from .project_contract_mounts import (
    _CLOSE_FDS_AND_EXEC,
    ContractMountPlan,
    _File,
    _seal,
    build_contract_mounts,
)
from .python_project_mounts import _closed_execution, _pin_directory, _Pinned, _seal_script


def _need(condition: object) -> None:
    if not condition:
        raise ValueError('go_project_mounts_rejected')


def go_phase_command(phase: str) -> tuple[str, ...]:
    """Select a fixed command; no caller-supplied command, URL or flags."""
    _need(type(phase) is str and phase in {'packages', 'build', 'test'})
    if phase == 'packages':
        return ('/sdk/bin/go', 'list', '-json', './...')
    if phase == 'build':
        return ('/sdk/bin/go', 'build', '-trimpath', '-mod=readonly', '-o', '/output/project', '.')
    return ('/sdk/bin/go', 'test', '-json', '-count=1', '-vet=off', '-mod=readonly', './...')


@dataclass(frozen=True, slots=True)
class GoInspectionParameters:
    module_path: str
    file_limit: int
    byte_limit: int
    timeout_ms: int
    max_packages: int = 256
    max_tests: int = 1000

    def __post_init__(self) -> None:
        _module_path(self.module_path)
        for value, ceiling in ((self.file_limit, 100000), (self.byte_limit, 500 * 1024**2),
                               (self.timeout_ms, 3600000)):
            _need(type(value) is int and 0 < value <= ceiling)
        for value in (self.max_packages, self.max_tests):
            _need(type(value) is int and value > 0)


def go_inspection_command(parameters: GoInspectionParameters) -> tuple[str, ...]:
    _need(type(parameters) is GoInspectionParameters)
    return (
        '/sdk/bin/go', 'run', '-mod=readonly', '/trusted/go_project_inspect.go',
        '--root', '/workspace', '--module', parameters.module_path,
        '--max-files', str(parameters.file_limit), '--max-bytes', str(parameters.byte_limit),
        '--timeout-ms', str(parameters.timeout_ms),
        '--max-packages', str(parameters.max_packages), '--max-tests', str(parameters.max_tests),
    )


@dataclass(frozen=True, slots=True)
class GoPhaseMountPlan:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]


def _prefix(work_root: Path, toolchain_fd: int, parallelism: int) -> tuple[str, ...]:
    argv: tuple[str, ...] = (
        '/usr/bin/bwrap', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin', '--ro-bind', '/lib', '/lib',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--tmpfs', '/var',
        '--bind', str(work_root), '/workspace',
        '--ro-bind', f'/proc/self/fd/{toolchain_fd}', '/sdk',
        '--chdir', '/workspace', '--clearenv',
    )
    if Path('/lib64').exists():
        argv += ('--ro-bind', '/lib64', '/lib64')
    for name, value in (
        ('PATH', '/sdk/bin:/usr/bin:/bin'), ('LANG', 'C.UTF-8'),
        ('GOROOT', '/sdk'), ('GOTOOLCHAIN', 'local'), ('GOWORK', 'off'), ('GOENV', 'off'),
        ('GOTELEMETRY', 'off'), ('GOFLAGS', ''),
        ('GOPROXY', 'off'), ('GOSUMDB', 'off'), ('GOVCS', '*:off'), ('CGO_ENABLED', '0'),
        ('GOOS', 'linux'), ('GOARCH', 'amd64'), ('GOCACHE', '/cache/go-build'),
        ('GOMODCACHE', '/cache/mod'), ('GOPATH', '/cache/gopath'), ('GOTMPDIR', '/cache/tmp'),
        ('HOME', '/cache/home'), ('TMPDIR', '/cache/tmp'),
        ('GOMAXPROCS', str(parallelism)),
    ):
        argv += ('--setenv', name, value)
    return (*argv, '--', '/usr/bin/true')


@contextmanager
def build_go_phase_mounts(
    phase: str, *, work_root: Path, toolchain_root: Path, output_root: Path, cache_root: Path,
    contract_arguments: Mapping[str, object], inspection: GoInspectionParameters | None = None,
    canary: GoOverlayCanary | None = None, parallelism: int = 1,
) -> Iterator[GoPhaseMountPlan]:
    """Hold pinned mount and sealed source FDs until the child is reaped."""
    _need((phase == 'inspect' and type(inspection) is GoInspectionParameters)
          or (phase != 'inspect' and inspection is None))
    _need((phase == 'canary' and type(canary) is GoOverlayCanary)
          or (phase != 'canary' and canary is None))
    # The outer invocation admits at most four CPU cores; never infer host capacity.
    _need(type(parallelism) is int and 1 <= parallelism <= 4)
    command = (canary.command if canary is not None else go_inspection_command(inspection)
               if inspection is not None else go_phase_command(phase))
    _need(sys.platform == 'linux'
          and os.environ.get('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX') == 'systemd')
    arguments = dict(contract_arguments)
    _need(arguments.get('work_root') == work_root and arguments.get('ephemeral_dirs', ()) == ())
    if canary is not None:
        _need(canary.source_sha256 == arguments.get('expected_source_sha256')
              and canary.protected_sha256 == arguments.get('expected_contract_sha256'))
    original = arguments.get('original_root')
    _need(isinstance(original, Path))
    roots = (work_root, toolchain_root, output_root, cache_root, cast(Path, original))
    _need(all(isinstance(root, Path) and root.is_absolute() and root != Path(root.anchor)
              and '..' not in root.parts and '\0' not in str(root) for root in roots))
    _need(all(left != right and not left.is_relative_to(right) and not right.is_relative_to(left)
              for left, right in combinations(roots, 2)))
    with ExitStack() as stack:
        pins: list[_Pinned] = []
        descriptors = tuple(_pin_directory(root, stack, pins) for root in roots)
        _need(len({(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors}) == len(roots))
        _, sdk, output, cache, _ = descriptors
        helper = (_seal_script(Path(__file__).with_name('go_project_inspect.go'), stack, pins)
                  if phase == 'inspect' else None)
        canary_files = (() if canary is None else tuple(
            (name, _seal(_File(name, data), stack)) for name, data in (
                ('go-canary.go', canary.source_bytes),
                ('go-canary-overlay.json', canary.overlay_bytes),
            )
        ))
        # Keep the opaque argument schema and all contract policy in the shared adapter.
        source_builder = cast(Callable[..., AbstractContextManager[ContractMountPlan]],
                              build_contract_mounts)
        with source_builder(_prefix(work_root, sdk, parallelism), **arguments) as source:
            for pin in pins:
                pin.check()
            split = source.argv.index('--')
            mounts: tuple[str, ...] = (
                '--bind', f'/proc/self/fd/{cache}', '/cache',
                '--bind' if phase == 'build' else '--ro-bind', f'/proc/self/fd/{output}', '/output',
            )
            for directory in ('go-build', 'mod', 'gopath', 'tmp', 'home'):
                mounts += ('--dir', '/cache/' + directory)
            inherited = (*source.pass_fds, sdk, output, cache)
            if helper is not None:
                mounts += ('--dir', '/trusted', '--ro-bind-data', str(helper),
                           '/trusted/go_project_inspect.go')
                inherited += (helper,)
            if canary_files:
                mounts += ('--dir', '/trusted')
                for name, fd in canary_files:
                    mounts += ('--ro-bind-data', str(fd), '/trusted/' + name)
                    inherited += (fd,)
            argv = (*source.argv[:split], *mounts,
                    *_closed_execution(command, inherited, _CLOSE_FDS_AND_EXEC))
            yield GoPhaseMountPlan(argv, inherited)
            for pin in pins:
                pin.check()
