"""Permission repair for a quiescent broker-private stage, never a live workspace.

The caller must confirm all related cgroups quiet BEFORE calling, then remove
the stage with symlink-safe rmtree. This helper neither proves quiescence nor
deletes anything. Linux uses anchored descriptors; Windows is fixture-only.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


class ProjectStageCleanupError(RuntimeError):
    """Unsafe or changing private-stage metadata; preserve the caller's fence."""


def _need(value: bool) -> None:
    if not value:
        raise ProjectStageCleanupError('project_stage_cleanup_unsafe')


def _stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink, info.st_mtime_ns)


def _ordinary(info: os.stat_result, device: int) -> None:
    _need(info.st_dev == device and (
        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1))


def _same_fd(descriptor: int, expected: os.stat_result) -> os.stat_result:
    current = os.fstat(descriptor)
    _need(_stamp(current) == _stamp(expected))
    return current


def _prepare_fd(descriptor: int, expected: os.stat_result, *, owner: int, device: int) -> None:
    if sys.platform != 'linux':
        raise ProjectStageCleanupError('project_stage_cleanup_unsupported')
    current = _same_fd(descriptor, expected)
    _ordinary(current, device)
    if current.st_uid != owner:
        # CAP_CHOWN suffices; chmod first would require the absent CAP_FOWNER.
        os.fchown(descriptor, owner, -1)
        current = _same_fd(descriptor, expected)
        _ordinary(current, device)
    _need(current.st_uid == owner)
    mode = 0o700 if stat.S_ISDIR(current.st_mode) else 0o600
    os.fchmod(descriptor, mode)
    current = _same_fd(descriptor, expected)
    _need(current.st_uid == owner and stat.S_IMODE(current.st_mode) == mode)


def _directory_flags() -> int:
    if sys.platform != 'linux':
        raise ProjectStageCleanupError('project_stage_cleanup_unsupported')
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _same_member(parent: int, name: str, expected: os.stat_result, descriptor: int) -> None:
    _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _stamp(expected))
    _same_fd(descriptor, expected)


def _open_directory(path: Path) -> int:
    descriptor = os.open(path.anchor, _directory_flags())
    try:
        for name in path.parts[1:]:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            _need(stat.S_ISDIR(before.st_mode))
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                _same_fd(child, before)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _walk_fd(descriptor: int, before: os.stat_result, *, owner: int, device: int) -> None:
    if sys.platform != 'linux':
        raise ProjectStageCleanupError('project_stage_cleanup_unsupported')
    _prepare_fd(descriptor, before, owner=owner, device=device)
    for name in os.listdir(descriptor):
        member = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(member.st_mode):
            continue
        _ordinary(member, device)
        flags = (_directory_flags() if stat.S_ISDIR(member.st_mode) else
                 os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        child = os.open(name, flags, dir_fd=descriptor)
        try:
            _same_member(descriptor, name, member, child)
            if stat.S_ISDIR(member.st_mode):
                _walk_fd(child, member, owner=owner, device=device)
            else:
                _prepare_fd(child, member, owner=owner, device=device)
            _same_member(descriptor, name, member, child)
        finally:
            os.close(child)
    _need(_same_fd(descriptor, before).st_uid == owner)


def _linux_writable(stage: Path) -> None:
    if sys.platform != 'linux':
        raise ProjectStageCleanupError('project_stage_cleanup_unsupported')
    owner = os.geteuid()
    parent = _open_directory(stage.parent)
    try:
        parent_info = os.fstat(parent)
        _need(parent_info.st_uid == owner and not parent_info.st_mode & 0o077)
        before = os.stat(stage.name, dir_fd=parent, follow_symlinks=False)
        _need(stat.S_ISDIR(before.st_mode) and before.st_uid == owner
              and not before.st_mode & 0o077 and before.st_dev == parent_info.st_dev)
        descriptor = os.open(stage.name, _directory_flags(), dir_fd=parent)
        try:
            _same_member(parent, stage.name, before, descriptor)
            _walk_fd(descriptor, before, owner=owner, device=before.st_dev)
            _same_member(parent, stage.name, before, descriptor)
            _need(_same_fd(descriptor, before).st_uid == owner)
            current_parent = stage.parent.lstat()
            _need((current_parent.st_dev, current_parent.st_ino, current_parent.st_uid)
                  == (parent_info.st_dev, parent_info.st_ino, owner)
                  and stat.S_ISDIR(current_parent.st_mode) and not current_parent.st_mode & 0o077)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_fixture_writable(stage: Path) -> None:
    """Legacy local fixture cleanup only; never traverse a junction/reparse point."""
    root = stage.lstat()
    _need(stat.S_ISDIR(root.st_mode) and not _reparse(root))

    def walk(path: Path, before: os.stat_result) -> None:
        if stat.S_ISLNK(before.st_mode) or _reparse(before):
            return
        _ordinary(before, root.st_dev)
        current = path.lstat()
        _need(_stamp(current) == _stamp(before) and not _reparse(current))
        path.chmod(0o700 if stat.S_ISDIR(before.st_mode) else 0o600)
        if stat.S_ISDIR(before.st_mode):
            for member in path.iterdir():
                walk(member, member.lstat())
        _need(_stamp(path.lstat()) == _stamp(before))

    walk(stage, root)


def make_private_stage_writable(stage: Path) -> None:
    """Prepare only an owned, canonical private stage AFTER confirmed cgroup quiet.

    Foreign Linux entries are reclaimed with fchown to the actual euid before
    fchmod. Symlink targets, hardlinked files, other devices and special nodes
    are never modified. Ownership/permission errors propagate to the caller.
    """
    _need(stage.is_absolute() and stage.parent != stage and stage.resolve(strict=True) == stage)
    if sys.platform == 'linux':
        _linux_writable(stage)
    elif sys.platform == 'win32':
        _windows_fixture_writable(stage)
    else:
        raise ProjectStageCleanupError('project_stage_cleanup_unsupported')
