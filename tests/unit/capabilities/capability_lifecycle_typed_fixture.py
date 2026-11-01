"""Fail-closed unused lifecycle methods and real untrusted-call boundaries."""

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from importlib import import_module
from importlib.machinery import ExtensionFileLoader
from importlib.metadata import distribution
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_service import PythonOriginalContext
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonFailedMaintenanceInvocation,
    PythonFailedReservationSnapshot,
    PythonProjectInvocation,
)
from tests.unit.capabilities.test_python_project_service import PythonExecutor


def native_asyncpg_uuid_class() -> type[UUID]:
    """Checked C-extension FFI boundary; asyncpg has no public UUID export.

    The returned class is the installed native object's identity, never a local
    stand-in or a generated stub for the untyped extension package.
    """
    name = "asyncpg.pgproto.pgproto"
    module = import_module(name)
    spec = module.__spec__
    assert spec is not None and spec.name == name
    assert isinstance(spec.loader, ExtensionFileLoader) and spec.origin is not None
    origin = Path(spec.origin).resolve()
    package = Path(str(distribution("asyncpg").locate_file("asyncpg/pgproto"))).resolve()
    assert origin.parent == package and origin.is_file()
    assert Path(module.__file__ or "").resolve() == origin
    kind: object = vars(module)["UUID"]
    assert isinstance(kind, type) and issubclass(kind, UUID) and kind is not UUID
    assert kind.__module__ == name and kind.__name__ == "UUID"
    exported: object = module.UUID
    assert kind is exported
    return kind


@dataclass(frozen=True)
class EnvironmentInspection:
    invocation: PythonProjectInvocation
    expected: PythonEnvironmentReference
    observed: PythonEnvironmentReference


def record_environment_inspections(
    executor: PythonExecutor, patch: pytest.MonkeyPatch,
) -> list[EnvironmentInspection]:
    actual = executor.inspect_environment
    observations: list[EnvironmentInspection] = []

    async def inspect(
        invocation: PythonProjectInvocation, *, expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference:
        result: object = await actual(invocation, expected_environment=expected_environment)
        assert isinstance(result, PythonEnvironmentReference)
        observations.append(EnvironmentInspection(invocation, expected_environment, result))
        return result

    patch.setattr(executor, "inspect_environment", inspect)
    return observations


class UnusedContextReader:
    async def python_original_context(
        self, tenant_id: UUID, run_id: UUID,
    ) -> PythonOriginalContext | None:
        raise AssertionError("unused original context lookup")

    async def python_expired_environment_invocation(
        self, tenant_id: UUID, run_id: UUID,
    ) -> PythonProjectInvocation | None:
        raise AssertionError("unused expired invocation lookup")


class UnusedReleaser:
    async def release_expired(self, invocation: PythonProjectInvocation) -> bool:
        raise AssertionError("unused expired release")

    async def inspect_failed(self, invocation: PythonFailedMaintenanceInvocation) -> PythonFailedReservationSnapshot | None:
        raise AssertionError("unused failed inspection")

    async def commit_failed(
        self, invocation: PythonFailedMaintenanceInvocation,
        snapshot: PythonFailedReservationSnapshot,
    ) -> bool:
        raise AssertionError("unused failed commit")

    async def release_completed(
        self, invocation: PythonProjectInvocation, *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference:
        raise AssertionError("unused completed release")


def string_map(value: object) -> Mapping[str, str]:
    assert isinstance(value, Mapping)
    assert all(isinstance(key, str) and isinstance(item, str) for key, item in value.items())
    return value


async def untrusted_authorization(backend: RuntimeCapabilityGateway, request: object) -> object:
    target: object = backend.project_verification_authorization
    assert callable(target)
    pending: object = target(request)
    assert isinstance(pending, Awaitable)
    result: object = await pending
    return result


def mutate_mapping(value: object, key: str, replacement: object) -> None:
    # Preserve the deliberate assignment to the actual MappingProxyType, whose
    # own __setitem__ failure (not a fixture error) is the tested boundary.
    from collections.abc import MutableMapping

    if isinstance(value, MutableMapping):
        value[key] = replacement
    else:
        target: object = value
        # operator.setitem performs the actual attempted assignment unchanged.
        import operator

        setter: object = operator.setitem
        assert callable(setter)
        setter(target, key, replacement)


def set_failure_fields(failure: Exception) -> None:
    for key, value in [('phase', 'profile'), ('code', 'metadata_prepare_unavailable')]:
        setattr(failure, key, value)


async def verified_direct_expiry_with_session_fixture(session: object, row: object) -> bool:
    """Run the actual SQL/read-only validator on the original minimal DB double."""
    from agent_hub.capabilities.python_direct_expiry import verified_direct_expiry

    target: object = verified_direct_expiry
    assert callable(target)
    pending: object = target(session, row)
    assert isinstance(pending, Awaitable)
    result: object = await pending
    assert isinstance(result, bool)
    return result
