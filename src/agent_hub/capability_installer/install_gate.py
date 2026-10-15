from __future__ import annotations

import asyncio
import re
import stat
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from agent_hub.capability_installer.environment import CapabilityEnvironmentManager


class SdkInstallGateUnavailable(RuntimeError):
    pass


async def _complete_owned_operation[T](operation: Awaitable[T]) -> tuple[T, bool]:
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def _enter_gate(
    manager: CapabilityEnvironmentManager, tenant_id: UUID, plugin_id: str
) -> AbstractContextManager[None]:
    from agent_hub.plugins.handover_confirmation import _shared_fs_lock_at_root

    root = manager.root
    if (
        type(tenant_id) is not UUID
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", plugin_id) is None
        or not root.is_absolute()
        or root.parent == root
        or root.resolve(strict=True) != root
    ):
        raise ValueError("SDK gate identity unavailable")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError("SDK gate root unavailable")
    context = _shared_fs_lock_at_root(root, tenant_id, plugin_id)
    context.__enter__()
    try:
        current = root.lstat()
        if (current.st_dev, current.st_ino, current.st_mode) != (
            info.st_dev,
            info.st_ino,
            info.st_mode,
        ):
            raise ValueError("SDK gate root changed")
    except BaseException:
        context.__exit__(*sys.exc_info())
        raise
    return context


@asynccontextmanager
async def sdk_install_gate(
    manager: CapabilityEnvironmentManager, tenant_id: UUID, plugin_id: str
) -> AsyncIterator[None]:
    """Serialize cooperating SDK services sharing an existing configured local root."""
    from agent_hub.plugins.handover_storage import Conflict

    try:
        context, cancelled = await _complete_owned_operation(
            asyncio.to_thread(_enter_gate, manager, tenant_id, plugin_id)
        )
    except (Conflict, OSError, ValueError) as error:
        raise SdkInstallGateUnavailable("SDK install gate unavailable") from error
    failure: BaseException | None = None
    try:
        if cancelled:
            raise asyncio.CancelledError
        yield
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _, release_cancelled = await _complete_owned_operation(
                asyncio.to_thread(
                    context.__exit__,
                    # The operation error is already propagating; this context only owns the lock.
                    None,
                    None,
                    None,
                )
            )
        except (Conflict, OSError, ValueError) as error:
            raise SdkInstallGateUnavailable("SDK install gate release unavailable") from error
        if release_cancelled and failure is None:
            raise asyncio.CancelledError
