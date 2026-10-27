"""Verifier-owned issuance and recovery; replayed tool output is never authority."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.execution_authority import get_verification_authority
from agent_hub.capabilities.project_prerequisites import (
    ProjectPrerequisiteObservation,
    validated_project_prerequisite_payload,
)
from agent_hub.runs.project_verification_receipts import (
    RECEIPT_KEY,
    ProjectVerificationReceiptRepository,
    VerificationAuthority,
    VerificationRecord,
)
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, _freeze_object
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import (
        ProjectVerificationError,
        ProjectVerificationService,
    )

Kind = Literal['node_plain', 'node_original', 'node_generated', 'python_original', 'python_generated']
_TYPE = 'project_verification_receipt'
_PRODUCER = 'trusted_project_verifier'
_ERROR = 'project verification receipt is unavailable or changed'
PublicationDisposition = Literal['private', 'writing', 'aborted', 'completing', 'committed', 'unknown']


@dataclass(slots=True)
class _Attempt:
    tenant: UUID
    run: UUID
    actor: str
    action: str
    issued: VerificationRecord | None = None
    active: bool = True
    product: Mapping[str, JsonValue] | None = None
    publication_disposition: PublicationDisposition = 'private'
    artifact_reference: ArtifactReference | None = None


@dataclass(frozen=True, slots=True)
class _RetainedProductPublication:
    """Non-authorizing reconciliation data, separate from current receipt caches."""

    product: Mapping[str, JsonValue]
    artifact_reference: ArtifactReference | None
    disposition: Literal['unknown'] = 'unknown'


@dataclass(slots=True)
class _Recovery:
    scope: tuple[UUID, UUID]
    active: bool = True


def _known_manifest_rejection(error: ProjectVerificationError, issued: VerificationRecord) -> bool:
    observation = error.project_prerequisite
    if (type(observation) is not ProjectPrerequisiteObservation or error.diagnostic is not None
            or error.project_failure is not None or error.python_project_failure is not None):
        return False
    report = validated_project_prerequisite_payload(observation.to_payload())
    return (report is not None
            and observation.source_sha256 == issued.source_sha256 == report['source_sha256']
            and observation.reason == report['reason'] == 'invalid_package_manifest'
            and observation.category == report['category'] == 'integrity'
            and report['checks'] == tuple({'phase': phase, 'status': 'not_run'}
                                         for phase in ('install', 'build', 'test')))


class DurableVerification:
    def __init__(self, owner: ProjectVerificationService, receipts: ProjectVerificationReceiptRepository,
                 artifacts: ArtifactRepository) -> None:
        self.owner, self.receipts, self.artifacts = owner, receipts, artifacts
        self._attempt: ContextVar[_Attempt | None] = ContextVar('project_verification_attempt', default=None)
        self._restoring: ContextVar[_Recovery | None] = ContextVar('verification_restoring', default=None)
        self._issued: dict[tuple[UUID, UUID], VerificationRecord] = {}
        self._retained_product_publications: dict[
            tuple[UUID, UUID, str], _RetainedProductPublication,
        ] = {}

    def clear(self, tenant: UUID, run: UUID) -> None:
        self._issued.pop((tenant, run), None)

    def _finish_product_publication(self, attempt: _Attempt) -> None:
        from agent_hub.capabilities.python_product_delivery import discard_product

        product = attempt.product
        if product is None:
            return
        key = (attempt.tenant, attempt.run)
        if self.owner._pending_python_products.get(key) is product:
            self.owner._pending_python_products.pop(key, None)
        if attempt.publication_disposition in {'private', 'aborted'}:
            discard_product(self.owner.generated_file_store, product)
        elif attempt.publication_disposition != 'committed':
            # No new publication or execution is attempted here. The existing
            # possibly written body must remain intact; retain its small file
            # locator even when put/abort acknowledgements were unavailable.
            artifact_id = cast(str, product['artifact_id'])
            self._retained_product_publications[(*key, artifact_id)] = _RetainedProductPublication(
                product, attempt.artifact_reference)

    async def require_current(self, tenant: UUID, run: UUID) -> None:
        attempt = self._attempt.get()
        recovery = self._restoring.get()
        if (attempt is not None and attempt.active and (attempt.tenant, attempt.run) == (tenant, run)
                or recovery is not None and recovery.active and recovery.scope == (tenant, run)):
            return
        issued = self._issued.get((tenant, run))
        if issued is None:
            raise ValueError(_ERROR)
        authority = await self._authority(tenant, run, issued.authority.actor)
        if await self.receipts.load(authority, generation=issued.generation) != issued:
            raise ValueError(_ERROR)

    async def _authority(self, tenant: UUID, run: UUID, actor: str) -> VerificationAuthority:
        getter = getattr(self.owner.repository, 'get', None)
        if not callable(getter):
            raise TypeError(_ERROR)
        record = await getter(tenant, run)
        authority = get_verification_authority(tenant_id=tenant, run_id=run,
            user_id=record.actor_id, role=record.actor_role, current_record=record)
        # Actor comes from the approved verifier invocation, never a stored worker lease.
        return replace(authority, actor=actor)

    async def begin(self, *, tenant_id: UUID, run_id: UUID, actor: str,
                    source_sha256: str, execution_id: str) -> None:
        attempt = self._attempt.get()
        if (attempt is None or not attempt.active
                or (attempt.tenant, attempt.run, attempt.actor) != (tenant_id, run_id, actor)):
            raise ValueError(_ERROR)
        if attempt.issued is not None:
            raise ValueError(_ERROR)
        authority = await self._authority(tenant_id, run_id, actor)
        attempt.issued = await self.receipts.begin(authority, action_key=attempt.action,
            execution_id=execution_id, source_sha256=source_sha256)

    def _kind(self, tenant: UUID, run: UUID) -> Kind:
        key = (tenant, run)
        owner = self.owner
        if key in owner._python_generated.receipts:
            return 'python_generated'
        if key in owner._generated_receipts:
            return 'node_generated'
        if key in owner._python.receipts:
            return 'python_original'
        if key in owner._original_receipts:
            return 'node_original'
        if key in owner._receipts:
            return 'node_plain'
        raise ValueError(_ERROR)

    @staticmethod
    def _body(issued: VerificationRecord, kind: Kind, result: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return _freeze_object({'schema_version': 1, 'scope': issued.authority.stable_scope(),
            'action_key': issued.action_key, 'generation': issued.generation,
            'execution_id': issued.execution_id, 'source_sha256': issued.source_sha256,
            'kind': kind, 'result': result}, name='private verification receipt')

    async def _publish(self, issued: VerificationRecord, result: Mapping[str, JsonValue]) -> None:
        tenant, run = issued.authority.tenant_id, issued.authority.run_id
        attempt = self._attempt.get()
        if attempt is None or attempt.issued != issued or not attempt.active:
            raise ValueError(_ERROR)
        if await self.owner.require_current_receipt(tenant, run) != issued.source_sha256:
            raise ValueError(_ERROR)
        artifact = Artifact(id=uuid4(), type=_TYPE, producer=_PRODUCER,
            content=self._body(issued, self._kind(tenant, run), result))
        reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
        attempt.artifact_reference = reference
        write_id = uuid4()
        completing = False
        try:
            attempt.publication_disposition = 'writing'
            await self.artifacts.reserve_write(tenant, run, reference, write_id=write_id)
            await self.artifacts.put(tenant, run, artifact, write_id=write_id)
            if await self.owner.require_current_receipt(tenant, run) != issued.source_sha256:
                raise ValueError(_ERROR)
            completing = True
            attempt.publication_disposition = 'completing'
            complete = await self.receipts.complete(issued.authority, expected=issued, status='passed',
                                                   artifact_references=(reference,))
            attempt.publication_disposition = 'committed'
            self._issued[(tenant, run)] = complete
        except BaseException:
            # After CAS starts the outcome may be unknown: retain a possibly committed body.
            attempt.publication_disposition = 'unknown'
            if not completing:
                cleanup = asyncio.create_task(self.artifacts.abort_write(tenant, run, reference, write_id=write_id))
                aborted = False
                while True:
                    try:
                        aborted = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.done():
                            if not cleanup.cancelled():
                                try:
                                    aborted = cleanup.result()
                                except Exception:  # noqa: BLE001 - a missing acknowledgement is UNKNOWN.
                                    logging.getLogger(__name__).warning('private verification write cleanup unavailable')
                            else:
                                logging.getLogger(__name__).warning('private verification write cleanup cancelled')
                            break
                    except Exception:  # noqa: BLE001 - never expose storage content while draining.
                        logging.getLogger(__name__).warning('private verification write cleanup unavailable')
                        break
                if aborted is True:
                    attempt.publication_disposition = 'aborted'
            raise

    async def verify(self, *, tenant_id: UUID, run_id: UUID, actor: str,
                     action_key: str) -> Mapping[str, JsonValue]:
        from agent_hub.capabilities.generated_project_service import GeneratedProjectError
        from agent_hub.capabilities.project_verification import ProjectVerificationError

        attempt = _Attempt(tenant_id, run_id, actor, action_key)
        token = self._attempt.set(attempt)
        self.owner._clear_receipts(tenant_id, run_id)
        try:
            if not action_key or action_key != action_key.strip():
                raise ValueError(_ERROR)
            await self._authority(tenant_id, run_id, actor)
            getter = getattr(self.owner.repository, 'get', None)
            if not callable(getter):
                raise TypeError(_ERROR)
            record = await getter(tenant_id, run_id)
            previous = record.routing_decision.get(RECEIPT_KEY)
            if isinstance(previous, Mapping) and (
                previous.get('status') == 'running' or previous.get('action_key') == action_key
            ):
                raise ValueError(_ERROR)
            result = await self.owner._verify(tenant_id=tenant_id, run_id=run_id, actor=actor)
            attempt.product = self.owner._pending_python_products.get((tenant_id, run_id))
            if attempt.product != result.get('python_product'):
                raise ValueError(_ERROR)
            if attempt.issued is None:
                raise ValueError(_ERROR)
            await self._publish(attempt.issued, result)
            return result
        except BaseException as error:
            self.owner._clear_receipts(tenant_id, run_id)
            if (attempt.issued is not None and isinstance(error, ProjectVerificationError)
                    and ((error.project_prerequisite is None
                          and (error.project_failure is not None or error.python_project_failure is not None))
                         or _known_manifest_rejection(error, attempt.issued))):
                try:
                    await self.receipts.complete(attempt.issued.authority, expected=attempt.issued, status='failed')
                except Exception:  # noqa: BLE001 - failed CAS is UNKNOWN, not an alternate failure result.
                    raise ProjectVerificationError(_ERROR) from None
            if isinstance(error, (asyncio.CancelledError, ProcessCleanupIncomplete,
                                  ProjectVerificationError, GeneratedProjectError)):
                raise
            raise ProjectVerificationError(_ERROR) from None
        finally:
            try:
                self._finish_product_publication(attempt)
            finally:
                attempt.active = False
                self._attempt.reset(token)

    async def restore(self, *, tenant_id: UUID, run_id: UUID, actor: str,
                      action_key: str) -> Mapping[str, JsonValue]:
        from agent_hub.capabilities.node_verification_recovery import restore_node_verification
        from agent_hub.capabilities.project_verification import ProjectVerificationError
        from agent_hub.capabilities.python_verification_recovery import restore_python_verification

        self.owner._clear_receipts(tenant_id, run_id)
        recovery = _Recovery((tenant_id, run_id))
        token = self._restoring.set(recovery)
        try:
            authority = await self._authority(tenant_id, run_id, actor)
            issued = await self.receipts.load(authority)
            if issued is None or issued.action_key != action_key or len(issued.artifact_references) != 1:
                raise ValueError(_ERROR)
            artifacts = await self.artifacts.get_many(tenant_id, run_id, issued.artifact_references)
            if len(artifacts) != 1:
                raise ValueError(_ERROR)
            artifact = artifacts[0]
            reference = issued.artifact_references[0]
            body = artifact.content
            kind, result = body.get('kind'), body.get('result')
            if (type(artifact) is not Artifact or artifact.id != reference.id
                    or artifact.content_sha256 != reference.sha256
                    or artifact.recompute_content_sha256() != reference.sha256
                    or (artifact.type, artifact.producer, artifact.version) != (_TYPE, _PRODUCER, 1)
                    or artifact.source_ids or artifact.provenance is not None
                    or type(body.get('schema_version')) is not int
                    or type(body.get('generation')) is not int
                    or kind not in ('node_plain', 'node_original', 'node_generated', 'python_original', 'python_generated')
                    or not isinstance(result, Mapping)
                    or body != self._body(issued, cast(Kind, kind), result)):
                raise ValueError(_ERROR)
            if kind in ('python_original', 'python_generated'):
                await restore_python_verification(self.owner, kind=cast(Literal['python_original', 'python_generated'], kind),
                    tenant_id=tenant_id, run_id=run_id, actor=actor, payload=result,
                    source_sha256=issued.source_sha256, execution_id=issued.execution_id)
            else:
                await restore_node_verification(self.owner, kind=cast(Literal['node_plain', 'node_original', 'node_generated'], kind),
                    tenant_id=tenant_id, run_id=run_id, actor=actor, payload=result,
                    source_sha256=issued.source_sha256, execution_id=issued.execution_id)
            if (await self.owner.require_current_receipt(tenant_id, run_id) != issued.source_sha256
                    or await self.receipts.load(authority, generation=issued.generation) != issued):
                raise ValueError(_ERROR)
            self._issued[(tenant_id, run_id)] = issued
            return result
        except BaseException as error:
            self.owner._clear_receipts(tenant_id, run_id)
            if isinstance(error, (asyncio.CancelledError, ProcessCleanupIncomplete)):
                raise
            raise ProjectVerificationError(_ERROR) from None
        finally:
            recovery.active = False
            self._restoring.reset(token)
