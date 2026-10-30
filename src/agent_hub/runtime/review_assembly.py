"""Server-owned fresh review assembly. Configuration alone never enables billing.

Deployment accounting contracts and the native four-mode verification record
must be supplied by the composition owner, outside settings/API/model arguments.
The default construction deliberately has neither and enrolls no paid runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Unpack, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import RuntimeCapabilityStack
from agent_hub.capabilities.model_review import (
    ModelApprovalReviewService,
    ModelReviewOutcome,
    _LiveReviewContext,
)
from agent_hub.capabilities.review_context import ConcreteModelReviewGate, ConcreteReviewRepository
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig, FrozenReviewAction
from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    ReviewRepository,
    _CurrentReviewBinding,
    _ReviewReservation,
    review_config_sha256,
)
from agent_hub.capabilities.review_settings import (
    ApprovalReviewReadiness,
    ApprovalReviewReadinessReason,
)
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.admission_bounds import KnownPricing, TrustedContextBound
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_budget import _run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    AttemptSnapshot,
    RunAdmissionRepository,
    _ExecutionArguments,
    _IssueArguments,
    _ReserveArguments,
    _WorkerLease,
)
from agent_hub.models.types import Deployment, ModelCapability, effective_deployment_capabilities
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.security.secrets import SecretService

_MODES = frozenset({TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID})
_PROFILE = "approval_review_profile"
REQUIRED_VERIFICATION_CHECKS = frozenset(
    {
        "original_pool",
        "original_lease",
        "physical_admission",
        "lazy_stream",
        "revoke",
        "root_backend",
        "exact_consume",
        "same_run_recovery",
    }
)


def runtime_source_fingerprint() -> str:
    """Pin the worker/gate/admission implementation, never a version label alone."""
    root = Path(__file__).resolve().parents[1]
    files = (
        "app.py",
        "api/routers/admin.py",
        "config/schema.py",
        "runtime/review_assembly.py",
        "runtime/worker_execution.py",
        "runtime/streams.py",
        "runtime/worker.py",
        "runtime/defaults.py",
        "runtime/contracts.py",
        "runtime/direct.py",
        "runtime/action_direct.py",
        "runtime/crew/adapter.py",
        "runtime/autogen/adapter.py",
        "runtime/autogen/project_repair_phase.py",
        "runtime/repair_projection.py",
        "runtime/hybrid.py",
        "runs/service.py",
        "runs/authorization.py",
        "runs/repository.py",
        "capabilities/defaults.py",
        "capabilities/gateway.py",
        "capabilities/policy.py",
        "capabilities/runtime.py",
        "capabilities/action_preparation.py",
        "capabilities/execution_authority.py",
        "capabilities/review_context.py",
        "capabilities/model_review.py",
        "capabilities/review_repository.py",
        "capabilities/review_settings.py",
        "harness/tool_gateway.py",
        "models/gateway.py",
        "models/litellm_client.py",
        "models/physical_budget.py",
        "models/physical_admission.py",
        "models/run_admission_repository.py",
        "models/admission_bounds.py",
        "files/workspace.py",
        "capabilities/python_resolution_preparation.py",
        "skills/sandbox/python_resolution_protocol.py",
        "skills/sandbox/python_resolution_store.py",
        "skills/sandbox/python_resolution_worker.py",
        "capabilities/python_dependency_resolution.py",
        "skills/sandbox/python_registry_protocol.py",
        "skills/sandbox/python_registry_read.py",
        "skills/sandbox/python_input_fetch.py",
        "skills/sandbox/python_dependency_metadata.py",
        "skills/sandbox/broker.py",
        "skills/sandbox/python_input_supply_catalog.py",
        "skills/sandbox/python_input_supply_worker.py",
        "skills/sandbox/python_input_publish.py",
        "skills/sandbox/python_dependency_index_store.py",
        "skills/sandbox/python_metadata_plan_protocol.py",
        "skills/sandbox/python_metadata_planning.py",
        "skills/sandbox/python_metadata_preparation_worker.py",
        "skills/sandbox/python_metadata_preparation_client.py",
        "skills/sandbox/project_contract_broker.py",
        "skills/sandbox/python_project_backend.py",
        "skills/sandbox/project.py",
        "capabilities/generated_project_service.py",
        "capabilities/python_metadata_preparation.py",
        "security/network.py",
        "capabilities/tools/http_read.py",
        "skills/sandbox/python_registry_fetch.py",
        "skills/sandbox/python_metadata_preparation_broker.py",
        "skills/sandbox/python_input_supply_broker.py",
        "skills/sandbox/python_project_broker.py",
        "skills/sandbox/base.py",
        "subprocess_cleanup.py",
        "skills/sandbox/python_resolution_overlay.py",
        "capabilities/python_generated_service.py",
        "capabilities/python_input_preparation.py",
        "runtime/generated_project_guidance.py",
        "skills/sandbox/python_project_profile.py",
        "skills/sandbox/python_dependency_inspection.py",
        "runtime/artifacts.py",
        "runs/artifacts.py",
    )
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


def catalog_fingerprint(config: PlatformConfig) -> str:
    return hashlib.sha256(
        json.dumps(
            config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderBillingContract:
    """An independently verified server record, pinned to the whole catalog.

    v1 means ALL billed prompt/completion/hidden tokens fit the configured
    context, both configured rates cover them, and internal proxy retries add
    no separately billed attempt. A context size or a client boolean is not
    evidence. Production currently has no issuer/source for these records.
    """

    tenant_id: UUID
    catalog_sha256: str
    deployment_id: str
    evidence_reference: str
    contract: Literal["single_physical_all_billed_tokens_v1"] = (
        "single_physical_all_billed_tokens_v1"
    )

    def __post_init__(self) -> None:
        if (
            type(self.tenant_id) is not UUID
            or len(self.catalog_sha256) != 64
            or not self.deployment_id
            or not self.evidence_reference.strip()
            or self.contract != "single_physical_all_billed_tokens_v1"
        ):
            raise ValueError("invalid server billing contract")


@dataclass(frozen=True, slots=True)
class WorkerReviewVerification:
    """Owner's native composition evidence; never read from tenant settings."""

    modes: frozenset[TaskMode]
    evidence_reference: str
    profile_version: Literal[1] = 1
    source_sha256: str = ""
    checks: frozenset[str] = frozenset()


class _EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    catalog_sha256: StrictStr = Field(pattern="^[a-f0-9]{64}$")
    deployment_id: StrictStr = Field(min_length=1, max_length=256)
    evidence_reference: StrictStr = Field(min_length=1, max_length=2048)
    contract: Literal["single_physical_all_billed_tokens_v1"]


class _VerificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_version: StrictInt
    source_sha256: StrictStr = Field(pattern="^[a-f0-9]{64}$")
    modes: list[StrictStr] = Field(min_length=4, max_length=4)
    checks: list[StrictStr] = Field(min_length=8, max_length=8)
    evidence_reference: StrictStr = Field(min_length=1, max_length=2048)


class _EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: StrictInt
    valid_until: StrictStr
    billing_contracts: list[_EvidenceItem] = Field(min_length=1, max_length=1024)
    worker_verification: _VerificationItem


def load_server_review_evidence(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[tuple[ProviderBillingContract, ...], WorkerReviewVerification]:
    """Read an ops-installed, digest-pinned document. Never an admin/API upload.

    File integrity is not a provider proof: its installer is the trusted issuer
    responsible for the referenced contract and native verification reports.
    Re-read before admission so removal, expiry or replacement revokes use.
    """

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if not path.is_absolute() or len(expected_sha256) != 64:
            raise ValueError
        with path.open("rb") as stream:
            body = stream.read(131073)
        if len(body) > 131072 or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError
        document = _EvidenceDocument.model_validate(json.loads(body, object_pairs_hook=unique))
        expiry = datetime.fromisoformat(document.valid_until)
        item = document.worker_verification
        if (
            document.schema_version != 1
            or expiry.utcoffset() != timedelta(0)
            or expiry <= datetime.now(UTC)
            or item.profile_version != 1
            or item.source_sha256 != runtime_source_fingerprint()
            or len(set(item.modes)) != 4
            or frozenset(TaskMode(mode) for mode in item.modes) != _MODES
            or frozenset(item.checks) != REQUIRED_VERIFICATION_CHECKS
            or not item.evidence_reference.strip()
        ):
            raise ValueError
        contracts = tuple(
            ProviderBillingContract(**entry.model_dump()) for entry in document.billing_contracts
        )
        if len({(entry.tenant_id, entry.deployment_id) for entry in contracts}) != len(contracts):
            raise ValueError
        return contracts, WorkerReviewVerification(
            _MODES,
            item.evidence_reference,
            source_sha256=item.source_sha256,
            checks=frozenset(item.checks),
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("invalid server review evidence") from None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    tenant_id: UUID
    config: ApprovalReviewConfig
    platform: PlatformConfig
    catalog_version: int
    catalog_sha256: str
    contracts: tuple[TrustedContextBound, ...]


@dataclass(frozen=True, slots=True)
class ReviewExecution:
    budget: _RunBudgetContext
    snapshot: _Snapshot


_execution: ContextVar[ReviewExecution | None] = ContextVar(
    "assembled_review_execution", default=None
)


def current_review_platform(tenant_id: UUID) -> PlatformConfig | None:
    execution = _execution.get()
    if execution is None:
        return None
    execution.budget.check()
    if execution.budget.tenant_id != tenant_id:
        raise AdmissionConflict("review_tenant_changed")
    return execution.snapshot.platform.model_copy(deep=True)


def review_deployments(deployments: tuple[Deployment, ...]) -> tuple[Deployment, ...]:
    """Limit real capacity selection, including fallback, to verified contracts."""
    execution = _execution.get()
    if execution is None:
        return deployments
    execution.budget.check()
    identifiers = {contract.deployment_id for contract in execution.snapshot.contracts}
    return tuple(deployment for deployment in deployments if deployment.id in identifiers)


class ReviewAssemblyUnavailable(RuntimeError):
    """Explicit unsupported execution, never an unbudgeted paid downgrade."""


class _CurrentAdmission(RunAdmissionRepository):
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], check: Callable[[], Awaitable[None]]
    ) -> None:
        super().__init__(sessions)
        self._check_configuration = check

    async def require_current(self, **a: Unpack[_ExecutionArguments]) -> None:
        await self._check_configuration()
        await super().require_current(**a)
        await self._check_configuration()

    async def reserve(self, **a: Unpack[_ReserveArguments]) -> AttemptSnapshot:
        await self._check_configuration()
        result = await super().reserve(**a)
        await self._check_configuration()
        return result

    async def mark_issued(self, **a: Unpack[_IssueArguments]) -> bool:
        await self._check_configuration()
        result = await super().mark_issued(**a)
        await self._check_configuration()
        return result


class _AssembledReviewRepository(ReviewRepository):
    """Run unchanged ledger transactions behind the same worker resource fence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        current_check: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(session_factory)
        self._current_check = current_check

    async def _current(self, tenant_id: UUID, run_id: UUID) -> None:
        execution = _execution.get()
        if execution is None or (execution.budget.tenant_id, execution.budget.run_id) != (
            tenant_id,
            run_id,
        ):
            raise ReviewAssemblyUnavailable("original_budget_binding_missing")
        await self._current_check()

    async def begin_intent(
        self,
        *,
        snapshot: FrozenReviewAction,
        config: ApprovalReviewConfig,
        reservation: _ReviewReservation,
        review_id: UUID,
    ) -> ReviewIntent:
        request = snapshot.request()
        await self._current(request.tenant_id, request.run_id)
        result = await super().begin_intent(
            snapshot=snapshot, config=config, reservation=reservation, review_id=review_id
        )
        await self._current(request.tenant_id, request.run_id)
        return result

    async def mark_issued(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        physical_identity: UUID,
        lease: _WorkerLease,
    ) -> bool:
        await self._current(tenant_id, run_id)
        result = await super().mark_issued(
            tenant_id=tenant_id,
            run_id=run_id,
            review_id=review_id,
            physical_identity=physical_identity,
            lease=lease,
        )
        await self._current(tenant_id, run_id)
        return result

    async def consume_permit(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool:
        await self._current(tenant_id, run_id)
        consumed = await super().consume_permit(
            tenant_id=tenant_id, run_id=run_id, review_id=review_id, current_binding=current_binding
        )
        # A consumed permit stays consumed if activation changes during the DAO
        # await. Never refund/reissue the settled review or dispatch its allow.
        await self._current(tenant_id, run_id)
        return consumed


class ReviewAssemblyBroker:
    """One versioned getter/compiler for API, worker, reviewer and readiness."""

    def __init__(
        self,
        *,
        config_service: ConfigService,
        config_getter: Callable[[UUID], Awaitable[ApprovalReviewConfig]],
        run_repository: RunRepository,
        session_factory: async_sessionmaker[AsyncSession],
        secret_service: SecretService,
        capacity_factory: Callable[[UUID, tuple[Deployment, ...]], Awaitable[CapacityPool]],
        transport: ModelTransport | None = None,
        billing_contracts: tuple[ProviderBillingContract, ...] = (),
        verification: WorkerReviewVerification | None = None,
        evidence_loader: Callable[
            [], tuple[tuple[ProviderBillingContract, ...], WorkerReviewVerification] | None
        ]
        | None = None,
    ) -> None:
        self._configs, self._get_config = config_service, config_getter
        self._runs, self._sessions = run_repository, session_factory
        self._secrets, self._capacity = secret_service, capacity_factory
        self._transport = transport or LiteLLMClient()
        self._contracts, self._verification = tuple(billing_contracts), verification
        self._evidence_loader = evidence_loader
        self.review_repository = _AssembledReviewRepository(session_factory, self._dispatch_check)

    async def current_config(self, tenant_id: UUID) -> ApprovalReviewConfig:
        return ApprovalReviewConfig.from_payload((await self._get_config(tenant_id)).to_payload())

    def _require_activation_current(self, snapshot: _Snapshot) -> None:
        """Final synchronous evidence fence after all awaited control-plane IO."""
        try:
            records, verification = self._contracts, self._verification
            if self._evidence_loader is not None:
                installed = self._evidence_loader()
                records, verification = ((), None) if installed is None else installed
            if (
                type(verification) is not WorkerReviewVerification
                or verification.profile_version != 1
                or verification.modes != _MODES
                or not verification.evidence_reference.strip()
                or verification.checks != REQUIRED_VERIFICATION_CHECKS
                or verification.source_sha256 != runtime_source_fingerprint()
            ):
                raise ValueError
            available = {
                record.deployment_id
                for record in records
                if type(record) is ProviderBillingContract
                and record.tenant_id == snapshot.tenant_id
                and record.catalog_sha256 == snapshot.catalog_sha256
            }
            if not {bound.deployment_id for bound in snapshot.contracts}.issubset(available):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ReviewAssemblyUnavailable("review_activation_changed") from None

    async def _dispatch_check(self) -> None:
        """Use the original binding at consumption and the last async gate seam."""
        execution = _execution.get()
        if execution is None or execution.budget is not _run_budget.get():
            raise ReviewAssemblyUnavailable("original_budget_binding_missing")
        await execution.budget.require_current()
        # Read catalog last: secret/DAO awaits in require_current must not hide
        # a newly published catalog with otherwise identical review settings.
        current = await self._configs.get_current(execution.budget.tenant_id)
        if (
            current is None
            or current.version != execution.snapshot.catalog_version
            or catalog_fingerprint(PlatformConfig.model_validate(current.document))
            != execution.snapshot.catalog_sha256
        ):
            raise ReviewAssemblyUnavailable("review_configuration_changed")
        # No await may follow this file hash/expiry/source check in this callback.
        self._require_activation_current(execution.snapshot)
        execution.budget.check()

    async def _compile(
        self, tenant_id: UUID
    ) -> tuple[_Snapshot | None, ApprovalReviewReadinessReason]:
        from agent_hub.runtime.defaults import _deployments

        config = await self.current_config(tenant_id)
        if config.engine == "rules":
            return None, "rules_only"
        current = await self._configs.get_current(tenant_id)
        if current is None:
            return None, "model_alias_missing"
        platform = PlatformConfig.model_validate(current.document)
        if config.reviewer_logical_model not in platform.models:
            return None, "model_alias_missing"
        fingerprint = catalog_fingerprint(platform)
        records, verification = self._contracts, self._verification
        if self._evidence_loader is not None:
            try:
                installed = self._evidence_loader()
            except ValueError:
                return None, "evidence_invalid"
            records, verification = ((), None) if installed is None else installed
        deployments = _deployments(platform)
        contracts = []
        rejected: dict[str, ApprovalReviewReadinessReason] = {}
        for deployment in deployments:
            alias, ordinal = deployment.id.rsplit("_", 1)
            definition = platform.models[alias].deployments[int(ordinal) - 1]
            # to_deployment's legacy 65536 default is not trustworthy metadata.
            if definition.context_window_tokens is None or definition.max_output_tokens is None:
                rejected[alias] = "metadata_missing"
                continue
            if (
                deployment.input_per_million_usd is None
                or deployment.output_per_million_usd is None
            ):
                rejected[alias] = "pricing_missing"
                continue
            KnownPricing(deployment.input_per_million_usd, deployment.output_per_million_usd)
            capabilities = effective_deployment_capabilities(deployment)
            if ModelCapability.TEXT not in capabilities or (
                alias == config.reviewer_logical_model
                and not {ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}.issubset(
                    capabilities
                )
            ):
                rejected[alias] = "capability_missing"
                continue
            if alias == config.reviewer_logical_model:
                numerator, denominator = max(
                    deployment.input_per_million_usd, deployment.output_per_million_usd
                ).as_integer_ratio()
                micros = (
                    definition.context_window_tokens * numerator + denominator - 1
                ) // denominator
                hold = Decimal(f"{micros // 1_000_000}.{micros % 1_000_000:06d}")
                if hold > Decimal(config.max_cost_usd_per_run):
                    rejected[alias] = "review_budget_insufficient"
                    continue
            if not any(
                type(c) is ProviderBillingContract
                and c.tenant_id == tenant_id
                and c.catalog_sha256 == fingerprint
                and c.deployment_id == deployment.id
                for c in records
            ):
                rejected[alias] = "provider_contract_missing"
                continue
            try:
                if not await self._secrets.fingerprint(tenant_id, deployment.secret_ref):
                    raise ValueError
            except Exception:  # noqa: BLE001 - metadata only, no key decryption/provider RPC.
                rejected[alias] = "secret_unavailable"
                continue
            contracts.append(
                TrustedContextBound(
                    deployment.id,
                    alias,
                    deployment.provider_model,
                    definition.context_window_tokens,
                )
            )
        if not any(
            contract.logical_model == config.reviewer_logical_model for contract in contracts
        ):
            return None, rejected.get(config.reviewer_logical_model or "", "model_alias_missing")
        if (
            type(verification) is not WorkerReviewVerification
            or verification.profile_version != 1
            or verification.modes != _MODES
            or not verification.evidence_reference.strip()
            or verification.source_sha256 != runtime_source_fingerprint()
            or verification.checks != REQUIRED_VERIFICATION_CHECKS
        ):
            return None, "current_mode_fence_unverified"
        if type(self._transport) is not LiteLLMClient:
            return None, "transport_unsupported"
        return _Snapshot(
            tenant_id, config, platform, current.version, fingerprint, tuple(contracts)
        ), "ready"

    async def readiness(
        self, tenant_id: UUID, *, expected_config: ApprovalReviewConfig | None = None
    ) -> ApprovalReviewReadiness:
        try:
            snapshot, reason = await self._compile(tenant_id)
            if (
                snapshot is not None
                and expected_config is not None
                and snapshot.config != expected_config
            ):
                reason = "configuration_changed"
        except Exception:  # noqa: BLE001 - safe projection, never expose secrets/errors.
            reason = "runtime_not_ready"
        return ApprovalReviewReadiness(
            status="ready" if reason == "ready" else "not_ready",
            reason_code=reason,
            supported_scope=("fresh_original_run", "workspace.write_text")
            if reason == "ready"
            else (),
        )

    async def submission_profile(
        self, tenant_id: UUID, *, timeout_seconds: float
    ) -> dict[str, object]:
        snapshot, _ = await self._compile(tenant_id)
        if snapshot is None:
            return {}
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise ValueError("invalid original review timeout")
        return {
            _PROFILE: {
                "version": 1,
                "config_sha256": review_config_sha256(snapshot.config),
                "catalog_version": snapshot.catalog_version,
                "catalog_sha256": snapshot.catalog_sha256,
                "timeout_seconds": timeout_seconds,
            },
            "runtime_timeout_absolute_seconds": timeout_seconds,
            "runtime_plan_timeout_seconds": timeout_seconds,
        }

    async def prepare(self, context: TaskContext, *, worker_id: str) -> ReviewExecution | None:
        captured = time.monotonic()
        route = context.routing_decision
        profile = route.get(_PROFILE)
        if profile is None:
            current = await self._runs.get(context.tenant_id, context.run_id)
            persisted = current.routing_decision or {}
            if (
                route.get("source") == "self_repair"
                or route.get("source_run_id") is not None
                or persisted.get("source") == "self_repair"
                or persisted.get("source_run_id") is not None
            ):
                # Internal run creation does not carry private resource scope.
                # Inspect only persisted provenance; never adopt a parent lease
                # or mint a new pool when the parent's profile was dropped.
                ancestors: set[UUID] = {context.run_id}
                for _ in range(32):
                    if (
                        persisted.get("source_run_id") is None
                        and persisted.get("source") != "self_repair"
                    ):
                        break
                    try:
                        source = UUID(str(persisted.get("source_run_id")))
                        if source in ancestors:
                            raise ReviewAssemblyUnavailable(
                                "derived_run_pool_continuity_unsupported"
                            )
                        ancestors.add(source)
                        parent = await self._runs.get(context.tenant_id, source)
                    except (ValueError, LookupError):
                        if (
                            await self.current_config(context.tenant_id)
                        ).engine == "rules_then_model":
                            raise ReviewAssemblyUnavailable(
                                "derived_run_pool_continuity_unsupported"
                            ) from None
                        break
                    if _PROFILE in (parent.routing_decision or {}):
                        raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
                    persisted = parent.routing_decision or {}
                else:
                    raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
            return None  # No upgrade of old runs, profiles or checkpoints.
        if (
            not isinstance(profile, Mapping)
            or type(profile.get("version")) is not int
            or profile.get("version") != 1
            or context.mode not in _MODES
        ):
            raise ReviewAssemblyUnavailable("review_profile_unsupported")
        snapshot, reason = await self._compile(context.tenant_id)
        if snapshot is None:
            raise ReviewAssemblyUnavailable(reason)
        if (
            profile.get("config_sha256") != review_config_sha256(snapshot.config)
            or profile.get("catalog_version") != snapshot.catalog_version
            or profile.get("catalog_sha256") != snapshot.catalog_sha256
        ):
            raise ReviewAssemblyUnavailable("review_configuration_changed")
        if (
            context.execution_token is None
            or context.actor_id is None
            or context.actor_role is None
        ):
            raise ReviewAssemblyUnavailable("original_worker_context_missing")
        record = await self._runs.get(context.tenant_id, context.run_id)
        evidence = await self._runs.load_authenticated_input(context.tenant_id, context.run_id)
        if (
            evidence is None
            or record.actor_id != context.actor_id
            or record.actor_role != context.actor_role
            or record.status is not RunStatus.RUNNING
            or record.mode is not context.mode
            or record.worker_id != worker_id
            or record.worker_lease_token != context.execution_token
            or record.routing_decision is None
            or record.routing_decision.get(_PROFILE) != dict(profile)
        ):
            raise ReviewAssemblyUnavailable("original_authenticated_context_missing")
        # A genuine accepted repair requeues the SAME RunRow. Children still have
        # no trusted parent-pool continuity and cannot borrow their parent's frame.
        repair = None
        if (
            route.get("source_run_id") is not None
            or record.routing_decision.get("source_run_id") is not None
        ):
            raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
        if (
            route.get("source") == "self_repair"
            or record.routing_decision.get("source") == "self_repair"
        ):
            repair = await self._runs.load_same_run_repair_continuity(
                context.tenant_id, context.run_id
            )
            if repair is None or not repair.matches(context):
                raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")

        async def check() -> None:
            from agent_hub.capabilities.execution_authority import _trusted_review_context

            def local_worker_scope() -> None:
                if _execution.get() is not None:
                    # Reuse the actual revocable worker/child binding. A child
                    # exit must stop inherited paid work while the parent run
                    # retains its original shared balance and stays active.
                    assert context.actor_id is not None and context.actor_role is not None
                    _trusted_review_context(
                        tenant_id=context.tenant_id,
                        run_id=context.run_id,
                        user_id=context.actor_id,
                        role=context.actor_role,
                    )

            local_worker_scope()
            if repair is not None:
                # Pin the proved resume boundary, not the latest in-flight
                # checkpoint. New work has its own journal/UNKNOWN fences.
                current_run = await self._runs.get(context.tenant_id, context.run_id)
                if not repair.matches_routing(current_run.routing_decision or {}):
                    raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
            current, current_reason = await self._compile(context.tenant_id)
            local_worker_scope()
            if (
                current is None
                or current.catalog_version != snapshot.catalog_version
                or current.catalog_sha256 != snapshot.catalog_sha256
                or current.config != snapshot.config
                or current.contracts != snapshot.contracts
            ):
                raise ReviewAssemblyUnavailable(
                    current_reason if current is None else "review_configuration_changed"
                )
            self._require_activation_current(snapshot)

        repository = _CurrentAdmission(self._sessions, check)
        lease = _WorkerLease(worker_id, context.execution_token)
        pool = await repository.get_pool(context.tenant_id, context.run_id)
        if pool is None:
            if repair is not None or context.checkpoint is not None:
                raise ReviewAssemblyUnavailable("recovery_original_pool_missing")
            absolute, plan = (
                route.get("runtime_token_absolute_tokens"),
                route.get("runtime_plan_token_budget"),
            )
            timeout = profile.get("timeout_seconds")
            envelope = route.get("runtime_timeout_absolute_seconds")
            if (
                type(absolute) is not int
                or type(plan) is not int
                or not 0 < min(absolute, plan) <= 10_000_000
                or type(timeout) not in (int, float)
                or type(envelope) not in (int, float)
                or timeout != envelope
            ):
                raise ReviewAssemblyUnavailable("original_hard_envelope_missing")
            original_timeout = cast(int | float, timeout)
            if not math.isfinite(original_timeout) or not 0 < original_timeout <= 3600:
                raise ReviewAssemblyUnavailable("original_hard_envelope_missing")
            # Anchored to persisted creation, not this (possibly late) factory call.
            deadline = record.created_at + timedelta(seconds=float(original_timeout))
            pool = await repository.open_pool(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                authorization_turn_id=evidence.turn_id,
                lease=lease,
                config_sha256=review_config_sha256(snapshot.config),
                original_token_grant=min(absolute, plan),
                original_deadline=deadline,
                review_token_limit=min(absolute, plan),
                review_count_limit=snapshot.config.max_reviews_per_run,
                review_cost_limit=Decimal(snapshot.config.max_cost_usd_per_run),
            )
        await repository.require_current(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            authorization_turn_id=evidence.turn_id,
            lease=lease,
            config_sha256=review_config_sha256(snapshot.config),
        )
        budget = _RunBudgetContext(
            repository=repository,
            pool=pool,
            evidence=evidence,
            lease=lease,
            config=snapshot.config,
            deadline=captured + context.timeout_seconds,
            trusted_contexts=snapshot.contracts,
        )
        return ReviewExecution(budget, snapshot)

    def gate_factory(self, backend: object) -> ConcreteModelReviewGate:
        from agent_hub.capabilities.review_context import (
            ConcreteModelReviewGate,
            TrustedReviewContextLoader,
        )
        from agent_hub.capabilities.runtime import RuntimeCapabilityGateway

        if not isinstance(backend, RuntimeCapabilityGateway):
            raise TypeError("review backend must be the actual runtime backend")
        return ConcreteModelReviewGate(
            TrustedReviewContextLoader(
                self._runs,
                preparation_reader=backend.action_preparation_snapshot,
                config_getter=self.current_config,
            ),
            # 4B's async Protocol is narrower than 2C's sync factory returning
            # Awaitable. Both await the same real repository; no method replaced.
            self,
            cast(ConcreteReviewRepository, self.review_repository),
            dispatch_check=self._dispatch_check,
        )

    async def review(
        self, snapshot: FrozenReviewAction, live_context: _LiveReviewContext
    ) -> ModelReviewOutcome:
        from agent_hub.runtime.defaults import _gateway_for_config

        execution = _execution.get()
        if execution is None or execution.budget is not _run_budget.get():
            raise ReviewAssemblyUnavailable("original_budget_binding_missing")
        await execution.budget.require_current()
        gateway, _, _ = await _gateway_for_config(
            execution.snapshot.platform,
            tenant_id=execution.budget.tenant_id,
            secret_service=self._secrets,
            capacity_factory=self._capacity,
            transport=self._transport,
        )
        return await ModelApprovalReviewService(gateway, self.review_repository).review(
            snapshot, live_context
        )


def build_review_broker(
    *,
    config_service: ConfigService,
    config_getter: Callable[[UUID], Awaitable[ApprovalReviewConfig]],
    run_repository: RunRepository,
    session_factory: async_sessionmaker[AsyncSession],
    secret_service: SecretService,
    redis_client: object,
) -> ReviewAssemblyBroker:
    # Deployment environment is a server control plane. Neither field exists
    # in settings, model catalog payloads, action arguments or app.state maps.
    path = os.environ.get("AGENT_HUB_REVIEW_EVIDENCE_FILE")
    digest = os.environ.get("AGENT_HUB_REVIEW_EVIDENCE_SHA256")

    def evidence() -> tuple[tuple[ProviderBillingContract, ...], WorkerReviewVerification] | None:
        if path is None and digest is None:
            return None
        if not path or not digest:
            raise ValueError("invalid server review evidence")
        return load_server_review_evidence(Path(path), expected_sha256=digest)

    async def capacity(tenant_id: UUID, deployments: tuple[Deployment, ...]) -> CapacityPool:
        async def fingerprint(secret_ref: str) -> str:
            return await secret_service.fingerprint(tenant_id, secret_ref)

        return CapacityPool(redis_client, deployments=deployments, fingerprint_resolver=fingerprint)

    return ReviewAssemblyBroker(
        config_service=config_service,
        config_getter=config_getter,
        run_repository=run_repository,
        session_factory=session_factory,
        secret_service=secret_service,
        capacity_factory=capacity,
        evidence_loader=evidence,
    )


class _ProfiledHarness(HarnessToolGateway):
    """Two policy paths, one actual backend/Root/receipt/cleanup lifecycle."""

    def __init__(self, stack: RuntimeCapabilityStack, **options: Any) -> None:
        super().__init__(
            stack.runtime_gateway,
            policy_gateway=stack.policy_gateway,
            raise_backend_errors=True,
            **options,
        )
        self._fresh = stack.harness_tool_gateway

    def _selected(self, tenant_id: UUID, run_id: UUID) -> HarnessToolGateway | None:
        execution = _execution.get()
        if execution is None:
            return None
        execution.budget.check()
        if (execution.budget.tenant_id, execution.budget.run_id) != (tenant_id, run_id):
            raise ReviewAssemblyUnavailable("derived_run_pool_continuity_unsupported")
        return self._fresh

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        fresh = self._selected(tenant_id, request.run_id)
        if fresh is None:
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        return await fresh.invoke(tenant_id, request, user_id=user_id, role=role)

    async def authorize(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        fresh = self._selected(tenant_id, request.run_id)
        if fresh is None:
            return await super().authorize(tenant_id, request, user_id=user_id, role=role)
        return await fresh.authorize(tenant_id, request, user_id=user_id, role=role)


def assemble_review_capability_stack(
    broker: ReviewAssemblyBroker,
    builder: Callable[..., RuntimeCapabilityStack],
    **options: Any,
) -> RuntimeCapabilityStack:
    stack = builder(review_gate_factory=broker.gate_factory, **options)
    harness = _ProfiledHarness(
        stack, mcp_backend=options.get("mcp_backend"), plugin_backend=options.get("plugin_backend")
    )
    # Build only a preparer referencing the profiled dispatcher; the actual
    # RuntimeCapabilityGateway, verifier, caches and terminal hook stay identical.
    preparer = stack.runtime_gateway.original_project_preparer(harness)
    if preparer is not None and stack.project_cache_cleanup_hook is not None:
        preparer.capacity_maintenance = stack.project_cache_cleanup_hook.maintain_capacity
    return replace(stack, harness_tool_gateway=harness, project_input_preparer=preparer)
