"""Typed fields for the existing concrete-review fixture; no execution grant."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, TypedDict, TypeGuard
from uuid import UUID

import pytest

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.capabilities import review_context
from agent_hub.capabilities.review_repository import ReviewRepository, _CurrentReviewBinding
from tests.unit.capabilities.test_concrete_model_review import FixedLedgerReview, FocusedDB
from tests.unit.harness.test_review_dispatch import ReviewSetup

type PermissionMode = Literal['default', 'auto_review', 'read_only']
type SandboxProfile = Literal['none', 'read_only', 'restricted', 'workspace_write']


def permission_sequence(value: object) -> tuple[object, ...]:
    def is_sequence(item: object) -> TypeGuard[list[object] | tuple[object, ...]]:
        return isinstance(item, (list, tuple))
    assert is_sequence(value)
    return tuple(value)


@contextmanager
def forged_dispatch(value: object) -> Iterator[None]:
    """Inject invalid data at get(); never claim bool is a valid typed authority.

    A temporary real ContextVar presents the same forged object to production
    code while the valid parent slot remains untouched and is restored exactly.
    """
    parent = review_context._WRITE_DISPATCH
    slot = ContextVar[object]('untrusted_concrete_write_dispatch', default=value)
    assert slot.get() is value
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(review_context, '_WRITE_DISPATCH', slot)
        yield
    assert review_context._WRITE_DISPATCH is parent


class ConsumeArguments(TypedDict):
    tenant_id: UUID
    run_id: UUID
    review_id: UUID
    current_binding: _CurrentReviewBinding


@dataclass
class ReachabilitySetup(ReviewSetup):
    db: FocusedDB = field(init=False)
    ledger: ReviewRepository = field(init=False)
    fixed: FixedLedgerReview = field(init=False)
    body: CreateRunRequest = field(init=False)


def reachability_setup(base: ReviewSetup) -> ReachabilitySetup:
    return ReachabilitySetup(
        base.task, base.budget, base.runs, base.backend, base.calls, base.settings,
        base.policy, base.permits, base.reviewer, base.loader, base.gate, base.harness,
        base.request,
    )
