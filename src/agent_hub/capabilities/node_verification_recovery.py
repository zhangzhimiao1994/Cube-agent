"""Rebuild Node caches from evidence authenticated by the trusted coordinator.

This is an internal callback, never a decoder for model/replay observations.
It only reads current scope and frozen controls; it cannot execute or prepare.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import ProjectVerificationService


def _clear(owner: ProjectVerificationService, key: tuple[UUID, UUID]) -> None:
    owner._receipts.pop(key, None)
    owner._original_receipts.pop(key, None)
    owner._generated_receipts.pop(key, None)
    owner._python.receipts.pop(key, None)
    owner._python_generated.receipts.pop(key, None)


def _validate_receipt(receipt: Mapping[str, JsonValue], source: str) -> None:
    from agent_hub.capabilities.project_verification import (
        _validate_original_diagnostic,
        _validate_report,
    )

    if (set(receipt) != {'schema_version', 'status', 'source_sha256', 'checks', 'runtime'}
            or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1
            or receipt.get('status') != 'passed' or receipt.get('source_sha256') != source):
        raise ValueError
    checks = receipt['checks']
    if not isinstance(checks, tuple) or len(checks) != 3:
        raise ValueError
    raw_checks: list[dict[str, object]] = []
    for phase, check in zip(('install', 'build', 'test'), checks, strict=True):
        if (not isinstance(check, Mapping)
                or set(check) != {'phase', 'status', 'execution_diagnostic'}
                or check.get('phase') != phase or check.get('status') != 'passed'):
            raise ValueError
        diagnostic = check['execution_diagnostic']
        _validate_original_diagnostic(diagnostic)
        if not isinstance(diagnostic, Mapping):
            raise TypeError
        raw_checks.append({'phase': phase, 'status': 'passed',
            **{name: diagnostic[name] for name in ('exit_code', 'timed_out', 'stdout', 'stderr')}})
    # Check the native success protocol without replacing persisted diagnostic hashes.
    _validate_report(json.dumps({'schema_version': 1, 'status': 'passed',
        'source_sha256': source, 'checks': raw_checks, 'runtime': receipt['runtime']}, default=dict),
        expected_source=source)


async def restore_node_verification(
    owner: ProjectVerificationService, *,
    kind: Literal['node_plain', 'node_original', 'node_generated'],
    tenant_id: UUID, run_id: UUID, actor: str, payload: Mapping[str, JsonValue],
    source_sha256: str, execution_id: str,
) -> None:
    """Restore authenticated success, then recheck the live delivery gate.

    The coordinator must validate DAO/private artifact provenance and the persisted
    execution identity before calling. Payload fields confer no authority here.
    """
    from agent_hub.capabilities.generated_project_service import (
        generated_selected,
        generated_verification_binding,
    )
    from agent_hub.capabilities.project_verification import (
        ProjectVerificationError,
        _GeneratedReceipt,
    )
    from agent_hub.skills.sandbox.project import (
        ProjectInvocation,
        project_source_fingerprint,
        project_source_manifest,
    )

    key = (tenant_id, run_id)
    _clear(owner, key)
    try:
        if (kind not in {'node_plain', 'node_original', 'node_generated'}
                or type(tenant_id) is not UUID or type(run_id) is not UUID
                or type(actor) is not str or not actor
                or type(source_sha256) is not str
                or re.fullmatch(r'[a-f0-9]{64}', source_sha256) is None):
            raise ValueError
        data = _freeze_object(payload, name='Node verification recovery')
        expected_fields = {'verification', 'summary'}
        if kind == 'node_original':
            expected_fields.add('original_check_contract')
        elif kind == 'node_generated':
            expected_fields.add('generated_project_acceptance')
        if set(data) != expected_fields or type(data['summary']) is not str:
            raise ValueError
        receipt = data['verification']
        if not isinstance(receipt, Mapping):
            raise TypeError

        record = await owner.repository.get(tenant_id, run_id)  # type: ignore[attr-defined]
        if generated_selected(record) != (kind == 'node_generated'):
            raise ValueError
        context = await owner.generated.load(tenant_id, run_id, actor)
        if (context is not None) != (kind == 'node_generated'):
            raise ValueError
        if context is not None and context.envelope.manifest.stack != 'node':
            raise ValueError
        if await owner._python.select(tenant_id, run_id, actor) is not None:
            raise ValueError
        current = await owner._invocation(tenant_id, run_id, actor)
        if type(current) is not ProjectInvocation:
            raise ValueError
        invocation = ProjectInvocation.model_validate(
            {**current.model_dump(), 'execution_id': execution_id}, strict=True)
        if await project_source_fingerprint(owner.root, invocation) != source_sha256:
            raise ValueError

        original_binding: Mapping[str, JsonValue] | None = None
        if context is not None:
            node_receipt = dict(receipt)
            if node_receipt.pop('generated_acceptance_sha256', None) != context.envelope.sha256:
                raise ValueError
            _validate_receipt(node_receipt, source_sha256)
            binding = data['generated_project_acceptance']
            if not isinstance(binding, Mapping) or not isinstance(binding.get('test_execution'), Mapping):
                raise ValueError
            expected_binding = generated_verification_binding(context, source_sha256,
                cast(Mapping[str, object], binding['test_execution']))
            if binding != expected_binding:
                raise ValueError
            await owner._current_generated(context, actor)
        else:
            _validate_receipt(receipt, source_sha256)
            original = await owner._original_scope(invocation)
            if (original is not None) != (kind == 'node_original'):
                raise ValueError
            if original is not None:
                original.stored.contract.require_current(await project_source_manifest(owner.root, invocation))
                original_binding = original.binding(source_sha256)
                if data['original_check_contract'] != original_binding:
                    raise ValueError

        latest = await owner._invocation(tenant_id, run_id, actor)
        if latest.model_copy(update={'execution_id': execution_id}) != invocation:
            raise ValueError
        owner._receipts[key] = (invocation, source_sha256)
        if context is not None:
            owner._generated_receipts[key] = _GeneratedReceipt(context, source_sha256, actor)
        elif original_binding is not None:
            owner._original_receipts[key] = original_binding
        if await owner.require_current_receipt(tenant_id, run_id) != source_sha256:
            raise ValueError
        return
    except (asyncio.CancelledError, ProcessCleanupIncomplete):
        _clear(owner, key)
        raise
    except Exception:  # noqa: BLE001 - no private store or filesystem details escape.
        _clear(owner, key)
    raise ProjectVerificationError('Node verification recovery is unavailable or changed') from None


__all__ = ['restore_node_verification']
