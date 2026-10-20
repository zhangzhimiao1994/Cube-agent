"""Read-only observations of a broker-configured, root-owned flat wheel catalog.

The root comes from trusted broker policy, never model arguments. Nothing here
executes, imports, or extracts wheels, creates a cache, or grants installation.
Observations are not an immutability lease: the broker must revalidate bytes and
identity when copying selected wheels, and mount only those copies, not this
catalog. Returned bindings and finite errors contain no host paths.
"""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from agent_hub.capabilities.python_project_recipe import (
    PythonLockedRequirement,
    PythonProjectRecipeError,
)
from agent_hub.capabilities.python_project_wheels import (
    PythonWheelBinding,
    bind_python_project_wheels,
)
from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.python_project_runtime import (
    PythonDistributionError,
    PythonDistributionLimitError,
    PythonDistributionTimeoutError,
    _Budget,
    _check,
    _directory,
    _names,
    _regular,
    _safe,
    _stamp,
)

_Code = Literal[
    'wheel_cache_invalid', 'wheel_cache_lock_invalid', 'wheel_cache_unsupported',
    'wheel_cache_missing', 'wheel_cache_wheel_missing', 'wheel_cache_hash_mismatch',
    'wheel_cache_ambiguous', 'wheel_cache_unsafe', 'wheel_cache_limit', 'wheel_cache_timeout',
]


class PythonWheelCacheError(ValueError):
    """A finite, path-free reason why an observation could not be completed."""

    def __init__(self, code: _Code) -> None:
        self.code = code
        super().__init__(code)


def _need(condition: object, code: _Code = 'wheel_cache_unsafe') -> None:
    if not condition:
        raise PythonWheelCacheError(code)


def _candidate(
    filename: str, locked: dict[str, PythonLockedRequirement],
) -> PythonLockedRequirement | None:
    parts = filename.split('-', 2)
    if len(parts) < 2:
        return None
    requirement = locked.get(re.sub(r'[-_.]+', '-', parts[0]).lower())
    if requirement is None or parts[1] != requirement.version:
        return None
    try:
        # Reuse filename eligibility only. The actual digest is measured below.
        PythonWheelBinding(filename, requirement.name, requirement.version, '0' * 64)
    except PythonProjectRecipeError:
        return None
    return requirement


def _observe(
    parent: int, locked: dict[str, PythonLockedRequirement], limits: ScanLimits, deadline: float,
) -> tuple[PythonWheelBinding, ...]:
    budget = _Budget(limits)
    members: dict[str, os.stat_result] = {}
    for filename in _names(parent, (), budget, deadline):
        _check(deadline)
        info = os.stat(filename, dir_fd=parent, follow_symlinks=False)
        _safe(info)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        members[filename] = info

    candidates: set[str] = set()
    selected: dict[str, PythonWheelBinding] = {}
    snapshots: dict[str, tuple[int, ...]] = {}
    for filename, info in members.items():
        _check(deadline)
        requirement = _candidate(filename, locked)
        if requirement is None:
            continue
        candidates.add(requirement.name)
        measured = _regular(parent, filename, info, budget, deadline)
        if measured.sha256 not in requirement.hashes:
            continue
        _need(requirement.name not in selected, 'wheel_cache_ambiguous')
        selected[requirement.name] = PythonWheelBinding(
            filename, requirement.name, requirement.version, measured.sha256,
        )
        snapshots[filename] = _stamp(info)

    for name in locked:
        _check(deadline)
        _need(name in candidates, 'wheel_cache_wheel_missing')
        _need(name in selected, 'wheel_cache_hash_mismatch')
    result = bind_python_project_wheels(tuple(locked.values()), tuple(selected.values()))
    # Catch changes to an early selection while later candidates were being read.
    for filename, identity in snapshots.items():
        _check(deadline)
        _need(_stamp(os.stat(filename, dir_fd=parent, follow_symlinks=False)) == identity)
    _check(deadline)
    return result


def observe_python_wheel_cache(
    root: Path, requirements: tuple[PythonLockedRequirement, ...], *,
    limits: ScanLimits, deadline: float,
) -> tuple[PythonWheelBinding, ...]:
    """Select exactly one hash-approved universal wheel per locked requirement.

    ``deadline`` is a finite positive absolute ``time.monotonic()`` value.
    ``max_entries`` bounds all catalog entries, and ``max_bytes`` bounds the
    combined bytes hashed across eligible candidates, including rejected hashes.
    Unrequested safe regular files receive filename/stat checks only.
    """
    try:
        _need(isinstance(root, Path), 'wheel_cache_invalid')
        _need(root.is_absolute() and '..' not in root.parts and '\0' not in str(root)
              and len(os.fsencode(root)) <= 4096, 'wheel_cache_invalid')
        _need(type(limits) is ScanLimits, 'wheel_cache_invalid')
        _need(all(type(value) is int and value > 0 for value in (
            limits.max_bytes, limits.max_entries, limits.max_depth, limits.max_path_bytes,
        )), 'wheel_cache_invalid')
        _need(type(deadline) in {float, int} and math.isfinite(deadline) and deadline > 0,
              'wheel_cache_invalid')
        _need(type(requirements) is tuple
              and all(type(item) is PythonLockedRequirement for item in requirements),
              'wheel_cache_lock_invalid')
        _need(len(requirements) <= limits.max_entries, 'wheel_cache_limit')
        locked: dict[str, PythonLockedRequirement] = {}
        for requirement in requirements:
            _check(deadline)
            PythonLockedRequirement(requirement.name, requirement.version, requirement.hashes)
            _need(requirement.name not in locked, 'wheel_cache_lock_invalid')
            locked[requirement.name] = requirement
        if (sys.platform != 'linux' or os.name != 'posix'
                or not all(getattr(os, name, 0) for name in (
                    'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_CLOEXEC'))
                or not {os.open, os.stat} <= os.supports_dir_fd
                or os.stat not in os.supports_follow_symlinks
                or os.scandir not in os.supports_fd):
            raise PythonWheelCacheError('wheel_cache_unsupported')
        _need(root.anchor == '/', 'wheel_cache_invalid')
        _check(deadline)
        with ExitStack() as stack:
            try:
                descriptor = stack.enter_context(_directory(None, '/', deadline))
                for name in root.parts[1:]:
                    descriptor = stack.enter_context(_directory(descriptor, name, deadline))
            except FileNotFoundError:
                raise PythonWheelCacheError('wheel_cache_missing') from None
            result = _observe(descriptor, locked, limits, deadline)
        _check(deadline)
        return result
    except PythonWheelCacheError:
        raise
    except PythonDistributionTimeoutError:
        raise PythonWheelCacheError('wheel_cache_timeout') from None
    except PythonDistributionLimitError:
        raise PythonWheelCacheError('wheel_cache_limit') from None
    except PythonProjectRecipeError:
        raise PythonWheelCacheError('wheel_cache_lock_invalid') from None
    except (PythonDistributionError, OSError, RuntimeError, ValueError, OverflowError):
        # OS and codec errors may contain broker paths or catalog contents.
        raise PythonWheelCacheError('wheel_cache_unsafe') from None
