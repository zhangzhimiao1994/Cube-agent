"""Concrete review at the existing Harness boundary; opt-in, never a broad grant.

The loader reads only the private worker binding, persisted authenticated input,
current server configuration and the injected real backend's Root reader. The
per-invocation state stays private to Harness, never in CapabilityResult.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import _trusted_review_context
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.model_review import ModelReviewOutcome, _LiveReviewContext
from agent_hub.capabilities.policy import CapabilityPolicy
from agent_hub.capabilities.review_contracts import (
    _CREDENTIAL,
    ApprovalReviewConfig,
    FrozenReviewAction,
    freeze_review_action,
)
from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    _CurrentReviewBinding,
    authorization_binding_sha256,
    review_config_sha256,
)
from agent_hub.capabilities.scoped_read import _path_parts
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.files.workspace import (
    _EXCLUDED_WORKSPACE_DIRECTORIES,
    _safe_workspace_path,
    project_private_entry,
)
from agent_hub.models.physical_budget import _run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import _WorkerLease
from agent_hub.runs.authorization import _AuthenticatedInput
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import (
    JsonValue,
    TaskContext,
    _canonical_json_bytes,
    _freeze_object,
)


class ReviewRunRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord: ...
    async def load_authenticated_input(
        self, tenant_id: UUID, run_id: UUID
    ) -> _AuthenticatedInput | None: ...


class ConcreteReviewService(Protocol):
    async def review(
        self, snapshot: FrozenReviewAction, live_context: _LiveReviewContext
    ) -> ModelReviewOutcome: ...


class ConcreteReviewRepository(Protocol):
    async def lookup_effect(
        self, tenant_id: UUID, run_id: UUID, authorization_turn_id: UUID, effect_digest: str
    ) -> ReviewIntent | None: ...
    async def consume_permit(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool: ...


PreparationReader = Callable[[TaskContext], Awaitable[Mapping[str, JsonValue]]]
ReviewConfigGetter = Callable[[UUID], Awaitable[ApprovalReviewConfig]]
ReviewPolicyReader = Callable[[], Awaitable[tuple[CapabilityPolicy, bool]]]


def _check_cancelled() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class _Fence:
    data: bytes = field(repr=False)
    context: TaskContext = field(repr=False)
    worker_id: str
    evidence: _AuthenticatedInput = field(repr=False)
    config: ApprovalReviewConfig = field(repr=False)


class TrustedReviewContextLoader:
    def __init__(
        self,
        repository: ReviewRunRepository,
        *,
        preparation_reader: PreparationReader,
        config_getter: ReviewConfigGetter,
    ) -> None:
        self._repository = repository
        self._preparation_reader = preparation_reader
        self._config_getter = config_getter

    def _root_identity(self) -> dict[str, object]:
        from agent_hub.capabilities.runtime import RuntimeCapabilityGateway

        backend = getattr(self._preparation_reader, "__self__", None)
        if (
            not isinstance(backend, RuntimeCapabilityGateway)
            or getattr(self._preparation_reader, "__func__", None)
            is not RuntimeCapabilityGateway.action_preparation_snapshot
            or backend._project_verification is None
        ):
            raise ValueError("review Root unavailable")
        root = backend._project_verification.root.resolve(strict=True)
        # Require the reader and actual executor to refer to the same real Root.
        if (
            backend._project_workspace_dir is None
            or backend._project_workspace_dir.resolve(strict=True) != root
            or backend._project_workspace_store is None
            or backend._project_workspace_store._root.resolve(strict=True) != root
        ):
            raise ValueError("review Root changed")
        stat = root.stat()
        return {
            "location_sha256": hashlib.sha256(
                os.path.normcase(str(root)).encode("utf-8")
            ).hexdigest(),
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }

    async def source(self, fence: _Fence) -> Mapping[str, JsonValue]:
        identity = self._root_identity()
        source = _freeze_object(await self._preparation_reader(fence.context), name="review source")
        if (
            set(source) != {"schema_version", "scope", "contracts", "workspace_sha256"}
            or source["schema_version"] != 1
            or self._root_identity() != identity
        ):
            raise ValueError("review source unavailable")
        return _freeze_object({**source, "root_identity": identity}, name="review Root source")

    def _effect_target(self, fence: _Fence, path: str) -> str:
        from agent_hub.capabilities.runtime import RuntimeCapabilityGateway

        backend = getattr(self._preparation_reader, "__self__", None)
        if (
            not isinstance(backend, RuntimeCapabilityGateway)
            or backend._project_workspace_store is None
        ):
            raise ValueError("review Root unavailable")
        project = fence.context.routing_decision.get("project_id")
        session = fence.context.routing_decision.get("workspace_session_id")
        if not isinstance(project, str) or not isinstance(session, str):
            raise TypeError("review scope unavailable")
        store = backend._project_workspace_store
        target = store._resolve_candidate(
            fence.context.tenant_id, project, session, path, must_exist=False
        )
        relative = target.relative_to(store.session_root(fence.context.tenant_id, project, session))
        return os.path.normcase(relative.as_posix()).replace("\\", "/")

    async def fence(self, request: CapabilityRequest, role: Role) -> _Fence:
        _check_cancelled()
        # Resolve the original lease BEFORE any IO. Current DB tokens are comparison only.
        _trusted_review_context(
            tenant_id=request.tenant_id, run_id=request.run_id, user_id=request.user_id, role=role
        )
        record = await self._repository.get(request.tenant_id, request.run_id)
        context, worker = _trusted_review_context(
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            user_id=request.user_id,
            role=role,
            current_record=record,
        )
        expiry = record.worker_lease_expires_at
        if (
            record.worker_id != worker
            or record.worker_lease_token != context.execution_token
            or expiry is None
            or expiry.tzinfo is None
            or expiry <= datetime.now(UTC)
        ):
            raise ValueError("review context unavailable")
        evidence = await self._repository.load_authenticated_input(
            request.tenant_id, request.run_id
        )
        if evidence is None or (evidence.tenant_id, evidence.actor_id, evidence.actor_role) != (
            request.tenant_id,
            request.user_id,
            role,
        ):
            raise ValueError("review context unavailable")
        auth = authorization_binding_sha256(evidence)
        config = ApprovalReviewConfig.from_payload(
            (await self._config_getter(request.tenant_id)).to_payload()
        )
        # Re-read after the auth/config awaits; do not trust an earlier detached row.
        current = await self._repository.get(request.tenant_id, request.run_id)
        current_context, current_worker = _trusted_review_context(
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            user_id=request.user_id,
            role=role,
            current_record=current,
        )
        current_expiry = current.worker_lease_expires_at
        if (
            current_worker != worker
            or current.worker_id != worker
            or current.worker_lease_token != context.execution_token
            or current_expiry is None
            or current_expiry.tzinfo is None
            or current_expiry <= datetime.now(UTC)
            or _canonical_json_bytes(current_context.routing_decision)
            != _canonical_json_bytes(context.routing_decision)
        ):
            raise ValueError("review context changed")
        _check_cancelled()
        _trusted_review_context(
            tenant_id=request.tenant_id, run_id=request.run_id, user_id=request.user_id, role=role
        )
        data = _canonical_json_bytes(
            _freeze_object(
                {
                    "authorization": auth,
                    "config": review_config_sha256(config),
                    "worker": worker,
                    "lease": str(context.execution_token),
                    "route": context.routing_decision,
                    "mode": context.mode.value,
                    "request": context.request,
                },
                name="private live review fence",
            )
        )
        return _Fence(data, context, worker, evidence, config)

    async def load(
        self, request: CapabilityRequest, role: Role, policy: CapabilityPolicy, *, tool_name: str
    ) -> tuple[FrozenReviewAction, _LiveReviewContext, _Fence]:
        fence = await self.fence(request, role)
        budget = _run_budget.get()
        if type(budget) is not _RunBudgetContext:
            raise ValueError("review context unavailable")
        budget.check()
        if (
            (budget.tenant_id, budget.run_id, budget.lease.worker_id, budget.lease.token)
            != (request.tenant_id, request.run_id, fence.worker_id, fence.context.execution_token)
            or authorization_binding_sha256(budget.evidence)
            != authorization_binding_sha256(fence.evidence)
            or review_config_sha256(budget.config) != review_config_sha256(fence.config)
            or fence.config.engine != "rules_then_model"
        ):
            raise ValueError("review context unavailable")
        source = await self.source(fence)
        # Only one fully interpretable action in this slice. No guessed shell/plugin equivalence.
        if (
            tool_name != "workspace.write_text"
            or request.tool_origin != "builtin"
            or (request.capability, request.operation, request.resource)
            != ("file", "write", "workspace/current")
            or set(request.arguments) != {"path", "content"}
            or type(request.arguments["path"]) is not str
            or type(request.arguments["content"]) is not str
        ):
            raise ValueError("review action requires human")
        path, content = request.arguments["path"], request.arguments["content"]
        assert isinstance(path, str) and isinstance(content, str)
        if (
            "/".join(_path_parts(path)) != path
            or _safe_workspace_path(path) != path
            or not content.strip()
        ):
            raise ValueError("review action requires human")
        # Source digests are continuity, never an editable-file permission. The backend
        # still enforces protected paths/contracts. Unsupported contract tools stay human.
        if source["contracts"] or path == "DELIVERY_MANIFEST.json":
            raise ValueError("review action requires human")
        target = self._effect_target(fence, path)
        if any(
            project_private_entry(part) or part.casefold() in _EXCLUDED_WORKSPACE_DIRECTORIES
            for part in (*_path_parts(path), *_path_parts(target))
        ):
            # The existing source manifest intentionally excludes these trees.
            # Do not assert completeness or send private targets to the reviewer.
            raise ValueError("review action requires human")
        effect = {
            "operation": "workspace.write_text",
            "scope": {
                "root": self._root_identity()["location_sha256"],
                "project": fence.context.routing_decision.get("project_id"),
                "session": fence.context.routing_decision.get("workspace_session_id"),
            },
            "target": target,
            "content": content,
        }
        snapshot = freeze_review_action(
            request,
            role=role,
            authorization_turn_id=fence.evidence.turn_id,
            authorization_sha256=authorization_binding_sha256(fence.evidence),
            policy_sha256=policy.policy_sha256(),
            config_sha256=review_config_sha256(fence.config),
            source_binding=source,
            normalized_effect=effect,
        )
        if snapshot.request() != request:
            raise ValueError("review projection changed")
        material = json.dumps(
            {
                "action": snapshot.to_payload(),
                "message": fence.evidence.message,
                "scope": fence.evidence.input_scope,
            },
            ensure_ascii=False,
        )
        if _CREDENTIAL.search(material) is not None:
            raise ValueError("review action requires human")
        if (await self.fence(request, role)).data != fence.data:
            raise ValueError("review context changed")
        budget.check()
        return snapshot, _LiveReviewContext(budget, True, False), fence


class ConcreteModelReviewGate:
    """Install only on fresh Harness instances. None preserves the old path."""

    def __init__(
        self,
        loader: TrustedReviewContextLoader,
        service: ConcreteReviewService,
        repository: ConcreteReviewRepository,
    ) -> None:
        self.loader, self.service, self.repository = loader, service, repository

    def invocation(self, *, tool_name: str, backend: object) -> _ConcreteReviewCall:
        return _ConcreteReviewCall(self, tool_name, backend)

    def supports_model_review(self, tool_name: str) -> bool:
        """Semantic support only; live context/budget/config are checked per call."""
        return tool_name == "workspace.write_text"


class _ConcreteReviewCall:
    """Private per-actual-invocation state, never serialized or reused by discovery."""

    def __init__(self, gate: ConcreteModelReviewGate, tool_name: str, backend: object) -> None:
        self.gate, self.tool_name = gate, tool_name
        self._backend = backend
        self.policy_reader: ReviewPolicyReader | None = None
        self.exact_reader: Callable[[], Awaitable[bool]] | None = None
        self._check: Callable[[], Awaitable[bool]] | None = None
        self._dispatched = False
        self._identity_check: Callable[[], object] | None = None
        self._budget: _RunBudgetContext | None = None

    async def authorize(
        self,
        request: CapabilityRequest,
        role: Role,
        policy: CapabilityPolicy,
        *,
        inbound: bool,
        human_wait: Callable[[], Awaitable[CapabilityResult]],
    ) -> CapabilityResult:
        denied = CapabilityResult(
            CapabilityStatus.DENIED, request.run_id, reason="capability denied"
        )
        route = policy.evaluate(request, role).route
        if route == "deny":
            return denied
        self._identity_check = lambda: _trusted_review_context(
            tenant_id=request.tenant_id, run_id=request.run_id, user_id=request.user_id, role=role
        )
        try:
            fence = await self.gate.loader.fence(request, role)
        except Exception:  # noqa: BLE001 - missing context cannot reach legacy broad/rules shortcuts.
            return await human_wait()
        exact = self.exact_reader is not None and await self.exact_reader()
        # Current human-only policy precedes submission/conversation/broad authority.
        if route == "allowed" or inbound and route == "model_required":

            async def allowed_check() -> bool:
                return (
                    await self._same_policy(policy)
                    and (await self.gate.loader.fence(request, role)).data == fence.data
                )

            self._check = allowed_check
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if exact:
            # A precise current human authorization can override model denial, but
            # is rechecked against the live worker/config/policy at dispatch.
            try:
                self._require_same_backend()
                source = await self.gate.loader.source(fence)
            except Exception:  # noqa: BLE001 - exact authorization still needs live Root continuity.
                return await human_wait()

            async def exact_check() -> bool:
                self._require_same_backend()
                return (
                    await self._same_policy(policy)
                    and (await self.gate.loader.fence(request, role)).data == fence.data
                    and await self.gate.loader.source(fence) == source
                    and self.exact_reader is not None
                    and await self.exact_reader()
                )

            self._check = exact_check
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if route == "human_only":
            return await human_wait()
        try:
            self._require_same_backend()
            snapshot, live, captured = await self.gate.loader.load(
                request, role, policy, tool_name=self.tool_name
            )
        except Exception:  # noqa: BLE001 - unsupported/missing materials require precise human consent.
            return await human_wait()
        try:
            prior = await self.gate.repository.lookup_effect(
                request.tenant_id,
                request.run_id,
                live.budget.evidence.turn_id,
                snapshot.effect_digest,
            )
            if prior is not None:
                return denied  # any durable effect is occupied; never pay/reissue/replay.
            outcome = await self.gate.service.review(snapshot, live)
            if (
                outcome.status != "allow"
                or outcome.review_id is None
                or outcome.action_digest != snapshot.action_digest
                or outcome.effect_digest != snapshot.effect_digest
            ):
                return denied if outcome.status in {"deny", "unknown"} else await human_wait()

            async def model_check() -> bool:
                self._require_same_backend()
                live.budget.check()
                if not await self._same_policy(policy):
                    return False
                current, current_live, current_fence = await self.gate.loader.load(
                    request, role, policy, tool_name=self.tool_name
                )
                return (
                    current.action_digest == snapshot.action_digest
                    and current.effect_digest == snapshot.effect_digest
                    and current_fence.data == captured.data
                    and current_live.budget is live.budget
                    and await self._same_policy(policy)
                )

            if not await model_check():
                return denied
            if not await self.gate.repository.consume_permit(
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                review_id=outcome.review_id,
                current_binding=_CurrentReviewBinding(
                    snapshot,
                    captured.config,
                    _WorkerLease(captured.worker_id, live.budget.lease.token),
                ),
            ):
                return denied
            self._check = model_check
            self._budget = live.budget
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        except Exception:  # noqa: BLE001 - stale/unknown/consume failures never downgrade to legacy grants.
            return denied

    async def _same_policy(self, policy: CapabilityPolicy) -> bool:
        if self.policy_reader is None:
            return False
        current, _ = await self.policy_reader()
        return current.policy_sha256() == policy.policy_sha256()

    def _require_same_backend(self) -> None:
        if getattr(self.gate.loader._preparation_reader, "__self__", None) is not self._backend:
            raise ValueError("review Root backend mismatch")

    async def before_dispatch(self) -> bool:
        if self._dispatched or self._check is None:
            return False
        self._dispatched = True
        try:
            _check_cancelled()
            valid = await self._check()
            # Nothing asynchronous may follow these revocation checks before
            # entering the actual backend. Children share the original frame.
            _check_cancelled()
            if self._identity_check is None:
                return False
            self._identity_check()
            current_budget = _run_budget.get()
            if current_budget is not None:
                current_budget.check()
            if self._budget is not None:
                self._budget.check()
            return valid
        except Exception:  # noqa: BLE001 - final live check never exposes private evidence.
            return False
