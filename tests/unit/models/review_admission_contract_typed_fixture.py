"""Typed guards that preserve deliberately invalid codec/storage inputs unchanged."""

from collections.abc import Awaitable
from typing import Literal

from agent_hub.capabilities import review_contracts
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.models import run_admission_repository as admission


def raw_call(target: object, *args: object, **kwargs: object) -> object:
    # Tests intentionally cross the public type boundary to exercise runtime guards.
    assert callable(target)
    return target(*args, **kwargs)


def raw_lease(worker: object, token: object) -> admission._WorkerLease:
    result = raw_call(admission._WorkerLease, worker, token)
    assert isinstance(result, admission._WorkerLease)
    return result


def raw_usage(
    prompt: object, completion: object, total: object, cost: object,
) -> admission.UsageObservation:
    result = raw_call(admission.UsageObservation, prompt, completion, total, cost)
    assert isinstance(result, admission.UsageObservation)
    return result


def raw_freeze(
    request: CapabilityRequest, **bindings: object,
) -> review_contracts.FrozenReviewAction:
    result = raw_call(review_contracts.freeze_review_action, request, **bindings)
    assert isinstance(result, review_contracts.FrozenReviewAction)
    return result


async def invalid_storage_call(
    operation: Literal["open", "reserve"], arguments: dict[str, object],
) -> object:
    # None remains the original deliberately absent factory/session. The real
    # repository must reject invalid bindings before accessing either one.
    repository = raw_call(admission.RunAdmissionRepository, None)
    assert isinstance(repository, admission.RunAdmissionRepository)
    method = (repository._open_pool_in_session if operation == "open"
              else repository._reserve_in_session)
    pending = raw_call(method, None, **arguments)
    assert isinstance(pending, Awaitable)
    result: object = await pending
    return result


def object_map(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return value


def object_array(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def string(value: object) -> str:
    assert isinstance(value, str)
    return value


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def attempt_assignment(target: object, field: str, value: object) -> None:
    """Perform the real frozen DTO assignment; its own guard must raise."""
    setattr(target, field, value)
