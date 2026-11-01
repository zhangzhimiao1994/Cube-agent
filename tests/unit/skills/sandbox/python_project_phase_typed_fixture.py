"""Real standalone-phase module protocol and checked mock/stat boundaries."""
from __future__ import annotations

from collections.abc import MutableMapping
from os import stat_result as StatResult
from pathlib import Path as FilePath
from typing import Protocol, runtime_checkable
from unittest.mock import Mock


class SystemState(Protocol):
    platform: str
    executable: str
    prefix: str
    path: list[str]
    modules: MutableMapping[str, object]


class OSCalls(Protocol):
    def fstat(self, descriptor: int) -> StatResult: ...


class ProcessCalls(Protocol):
    run: object


@runtime_checkable
class PhaseModule(Protocol):
    _RUNTIME: FilePath
    _ENVIRONMENT: FilePath
    _WHEELHOUSE: FilePath
    _WORKSPACE: FilePath
    _OUTPUT: FilePath
    PhaseError: type[RuntimeError]
    Path: type[FilePath]
    sys: SystemState
    os: OSCalls
    subprocess: ProcessCalls
    importlib: object
    unittest: object

    def _stamp(self, info: StatResult) -> tuple[int, ...]: ...
    def _regular(self, path: FilePath, *, sealed_input: bool = False) -> StatResult: ...
    def run_phase(self, phase: str, *, import_module: str,
                  generated_manifest_sha256: str | None = None) -> dict[str, object]: ...
    def main(self, argv: list[str] | None = None) -> int: ...


def process_mock(module: PhaseModule) -> Mock:
    value = module.subprocess.run
    assert isinstance(value, Mock)
    return value


def modified_stat(info: StatResult, *, mode: int | None = None,
                  links: int | None = None, inode: int | None = None) -> StatResult:
    values = list(info)
    if mode is not None:
        values[0] = mode
    if inode is not None:
        values[1] = inode
    if links is not None:
        values[3] = links
    return StatResult(values, {'st_atime_ns': info.st_atime_ns,
        'st_mtime_ns': info.st_mtime_ns, 'st_ctime_ns': info.st_ctime_ns})
