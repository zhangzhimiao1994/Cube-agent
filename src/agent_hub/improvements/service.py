from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class ImprovementStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHING = "publishing"
    PUBLISHED = "published"


@dataclass(frozen=True, slots=True)
class ProposedDiff:
    target_type: Literal["prompt", "workflow", "skill"]
    target_name: str
    diff: str

    def __post_init__(self) -> None:
        if self.target_type not in {"prompt", "workflow", "skill"}:
            raise ValueError("target type is invalid")
        _safe_identifier(self.target_name, name="target name")
        _safe_text(self.diff, name="diff", max_bytes=65_536)


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    name: str
    input: str
    expected_behavior: str | None = None
    expected: str | None = None

    def __post_init__(self) -> None:
        _safe_identifier(self.name, name="evaluation case name")
        _safe_text(self.input, name="evaluation input", max_bytes=8192)
        expected = self.expected_behavior if self.expected_behavior is not None else self.expected
        if expected is None:
            raise ValueError("evaluation case expected behavior is required")
        _safe_text(expected, name="evaluation expected behavior", max_bytes=8192)
        object.__setattr__(self, "expected_behavior", expected)


@dataclass(frozen=True, slots=True)
class ImprovementDraftInput:
    tenant_id: UUID
    created_by: UUID
    source_run_id: UUID
    rationale: str
    proposed_diffs: tuple[ProposedDiff, ...]
    evaluation_cases: tuple[EvaluationCase, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _safe_text(self.rationale, name="rationale", max_bytes=8192)
        if not self.proposed_diffs:
            raise ValueError("improvement draft requires proposed diffs")
        if not self.evaluation_cases:
            raise ValueError("improvement draft requires evaluation cases")
        _safe_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class ImprovementProposal:
    tenant_id: UUID
    created_by: UUID
    source_run_id: UUID
    rationale: str
    evaluation_cases: tuple[EvaluationCase, ...]
    proposed_diffs: tuple[ProposedDiff, ...] = ()
    idempotency_key: str = ""
    target_type: Literal["prompt", "workflow", "skill"] | None = None
    target_id: str = ""
    title: str = ""
    diff: Mapping[str, object] | None = None
    id: UUID = field(default_factory=uuid4)
    status: ImprovementStatus = ImprovementStatus.DRAFT
    published_config_version: int | str | None = None
    approved_by: UUID | None = None
    reviewed_by: UUID | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        proposed_diffs = self.proposed_diffs
        idempotency_key = self.idempotency_key
        if not proposed_diffs and self.target_type is not None:
            _safe_identifier(self.target_id, name="target id")
            _safe_text(self.title, name="proposal title", max_bytes=512)
            if self.diff is None:
                raise ValueError("improvement proposal requires a diff")
            proposed_diffs = (
                ProposedDiff(
                    target_type=self.target_type,
                    target_name=self.target_id,
                    diff=json.dumps(dict(self.diff), ensure_ascii=False, sort_keys=True),
                ),
            )
            object.__setattr__(self, "proposed_diffs", proposed_diffs)
        if not proposed_diffs:
            raise ValueError("improvement proposal requires proposed diffs")
        if not self.evaluation_cases:
            raise ValueError("improvement proposal requires evaluation cases")
        if not idempotency_key:
            idempotency_key = (
                f"{self.target_type or proposed_diffs[0].target_type}:"
                f"{self.target_id or proposed_diffs[0].target_name}:{self.source_run_id}"
            )
            object.__setattr__(self, "idempotency_key", idempotency_key)
        _safe_idempotency_key(idempotency_key)
        digest = self.fingerprint or _fingerprint(
            self.tenant_id,
            self.source_run_id,
            idempotency_key,
            proposed_diffs,
            self.evaluation_cases,
        )
        object.__setattr__(self, "fingerprint", digest)


class AdminPublisher(Protocol):
    async def publish(self, proposal_id: UUID, *, tenant_id: UUID, admin_id: UUID) -> str: ...


class ImprovementPublisher(Protocol):
    async def publish(self, proposal: ImprovementProposal, *, actor_id: UUID) -> int: ...


class PermissionDenied(RuntimeError):
    pass


class ProposalConflict(RuntimeError):
    pass


class ImprovementService:
    """Stores reviewed improvement drafts and gates publishing behind administrator approval."""

    def __init__(
        self,
        publisher: ImprovementPublisher | AdminPublisher | None = None,
        *,
        admin_publisher: AdminPublisher | None = None,
        administrator_ids: Iterable[UUID] | None = None,
    ) -> None:
        if publisher is not None and admin_publisher is None and _looks_like_admin_publisher(publisher):
            admin_publisher = cast(AdminPublisher, publisher)
            publisher = None
        if administrator_ids is not None:
            admin_ids = frozenset(administrator_ids)
            if not admin_ids:
                raise ValueError("administrator approval path requires administrators")
            self._administrator_ids: frozenset[UUID] | None = admin_ids
        else:
            self._administrator_ids = None
        self._admin_publisher = admin_publisher
        self._legacy_publisher = cast(ImprovementPublisher | None, publisher)
        self._proposals: dict[tuple[UUID, UUID], ImprovementProposal] = {}
        self._fingerprints: dict[tuple[UUID, str], UUID] = {}
        self._lock = asyncio.Lock()

    async def create_draft(self, draft: ImprovementDraftInput) -> ImprovementProposal:
        proposal = ImprovementProposal(
            tenant_id=draft.tenant_id,
            created_by=draft.created_by,
            source_run_id=draft.source_run_id,
            rationale=draft.rationale,
            evaluation_cases=draft.evaluation_cases,
            proposed_diffs=draft.proposed_diffs,
            idempotency_key=draft.idempotency_key,
        )
        return await self._store_draft(proposal)

    async def _store_draft(self, proposal: ImprovementProposal) -> ImprovementProposal:
        async with self._lock:
            existing_id = self._fingerprints.get((proposal.tenant_id, proposal.fingerprint))
            if existing_id is not None:
                return self._proposals[(proposal.tenant_id, existing_id)]
            self._proposals[(proposal.tenant_id, proposal.id)] = proposal
            self._fingerprints[(proposal.tenant_id, proposal.fingerprint)] = proposal.id
            return proposal

    async def get(self, tenant_id: UUID, proposal_id: UUID) -> ImprovementProposal:
        async with self._lock:
            proposal = self._proposals.get((tenant_id, proposal_id))
            if proposal is None:
                raise KeyError("proposal not found")
            return proposal

    async def list_proposals(self, *, tenant_id: UUID) -> tuple[ImprovementProposal, ...]:
        async with self._lock:
            return tuple(
                proposal
                for key, proposal in self._proposals.items()
                if key[0] == tenant_id
            )

    async def publish(
        self,
        *,
        tenant_id: UUID,
        proposal_id: UUID,
        admin_id: UUID | None = None,
        actor_id: UUID | None = None,
        actor_is_admin: bool | None = None,
    ) -> ImprovementProposal:
        effective_admin_id = admin_id if admin_id is not None else actor_id
        if effective_admin_id is None:
            raise PermissionDenied("administrator approval is required")
        if self._administrator_ids is not None and effective_admin_id not in self._administrator_ids:
            raise PermissionDenied("only administrators can publish improvement drafts")
        if self._administrator_ids is None and actor_is_admin is not True:
            raise PermissionDenied("explicit administrator proof is required")
        if actor_is_admin is False:
            raise PermissionDenied("only administrators can publish improvement drafts")
        async with self._lock:
            proposal = self._proposals.get((tenant_id, proposal_id))
            if proposal is None:
                raise KeyError("proposal not found")
            if proposal.status is not ImprovementStatus.DRAFT:
                raise ProposalConflict("proposal is not publishable")
            proposal = replace(proposal, status=ImprovementStatus.PUBLISHING)
            self._proposals[(tenant_id, proposal_id)] = proposal
        try:
            if self._admin_publisher is not None:
                version: int | str = await self._admin_publisher.publish(
                    proposal_id,
                    tenant_id=tenant_id,
                    admin_id=effective_admin_id,
                )
            elif self._legacy_publisher is not None:
                legacy_version = await self._legacy_publisher.publish(
                    proposal,
                    actor_id=effective_admin_id,
                )
                version = legacy_version
            else:
                raise ProposalConflict("no administrator publisher is configured")
        except Exception:
            async with self._lock:
                current = self._proposals.get((tenant_id, proposal_id))
                if current is not None and current.status is ImprovementStatus.PUBLISHING:
                    self._proposals[(tenant_id, proposal_id)] = replace(
                        current,
                        status=ImprovementStatus.DRAFT,
                    )
            raise
        published = replace(
            proposal,
            status=ImprovementStatus.PUBLISHED,
            published_config_version=version,
            approved_by=effective_admin_id,
            reviewed_by=effective_admin_id,
        )
        async with self._lock:
            current = self._proposals.get((tenant_id, proposal_id))
            if current is None:
                raise KeyError("proposal not found")
            if current.status is not ImprovementStatus.PUBLISHING:
                raise ProposalConflict("proposal is not publishable")
            self._proposals[(tenant_id, proposal_id)] = published
            return published

    async def propose_prompt_change(
        self,
        *,
        tenant_id: UUID,
        created_by: UUID,
        source_run_id: UUID,
        target_id: str,
        title: str,
        rationale: str,
        diff: dict[str, object],
        evaluation_cases: tuple[EvaluationCase, ...],
    ) -> ImprovementProposal:
        proposal = ImprovementProposal(
            tenant_id=tenant_id,
            created_by=created_by,
            source_run_id=source_run_id,
            rationale=rationale,
            evaluation_cases=evaluation_cases,
            proposed_diffs=(
                ProposedDiff(
                    target_type="prompt",
                    target_name=target_id,
                    diff=json.dumps(diff, ensure_ascii=False, sort_keys=True),
                ),
            ),
            idempotency_key=f"prompt:{target_id}:{source_run_id}",
            target_type="prompt",
            target_id=target_id,
            title=title,
            diff=diff,
        )
        return await self._store_draft(proposal)


def _fingerprint(
    tenant_id: UUID,
    source_run_id: UUID,
    idempotency_key: str,
    proposed_diffs: tuple[ProposedDiff, ...],
    evaluation_cases: tuple[EvaluationCase, ...],
) -> str:
    payload = {
        "tenant_id": str(tenant_id),
        "source_run_id": str(source_run_id),
        "idempotency_key": idempotency_key,
        "proposed_diffs": [
            {
                "target_type": diff.target_type,
                "target_name": diff.target_name,
                "diff": diff.diff,
            }
            for diff in proposed_diffs
        ],
        "evaluation_cases": [
            {
                "name": item.name,
                "input": item.input,
                "expected_behavior": item.expected_behavior,
            }
            for item in evaluation_cases
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_identifier(value: str, *, name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _safe_idempotency_key(value: str) -> str:
    if _SAFE_KEY.fullmatch(value) is None:
        raise ValueError("idempotency key must be safe")
    return value


def _safe_text(value: str, *, name: str, max_bytes: int) -> str:
    if not value.strip() or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} must be nonblank and bounded")
    return value


def _looks_like_admin_publisher(candidate: object) -> bool:
    publish = getattr(candidate, "publish", None)
    if publish is None:
        return False
    parameters = inspect.signature(publish).parameters
    return "tenant_id" in parameters and "admin_id" in parameters
