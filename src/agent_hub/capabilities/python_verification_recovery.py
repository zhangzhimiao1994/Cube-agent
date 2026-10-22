"""Restore Python receipts for the private, DAO-authenticated verifier coordinator.

The caller must first authenticate the private Artifact and its live DAO lease,
generation, actor, source and execution metadata. Payload validation alone never
establishes execution provenance. This helper grants no installation or execution
rights and reuses the services' current delivery checks before returning.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from agent_hub.capabilities.generated_project_service import generated_verification_binding
from agent_hub.capabilities.project_verification import ProjectVerificationError, _OriginalScope
from agent_hub.capabilities.python_generated_service import _Receipt as _GeneratedReceipt
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_receipts import validate_python_passed_receipt
from agent_hub.capabilities.python_project_reports import PythonProjectReport
from agent_hub.capabilities.python_project_service import (
    _encoded,
    python_original_invocation,
    python_recipe_marker,
)
from agent_hub.capabilities.python_project_service import (
    _Receipt as _OriginalReceipt,
)
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import project_source_fingerprint, project_source_manifest
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
    _reference_matches,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import ProjectVerificationService

_ERROR = "project verification required before delivery"


def _need(value: object) -> None:
    if not value:
        raise ProjectVerificationError(_ERROR)


def _clear(owner: ProjectVerificationService, key: tuple[UUID, UUID]) -> None:
    owner._receipts.pop(key, None)
    owner._original_receipts.pop(key, None)
    owner._generated_receipts.pop(key, None)
    owner._python.receipts.pop(key, None)
    owner._python.environments.pop(key, None)
    owner._python_generated.receipts.pop(key, None)


def _passed(
    payload: Mapping[str, JsonValue], *, inputs: PythonProjectInputs,
    invocation: PythonProjectInvocation, reference: PythonEnvironmentReference,
    source: str, generated_acceptance: str | None = None,
) -> PythonProjectReport:
    _need(_reference_matches(invocation, reference))
    _need(payload["python_recipe"] == python_recipe_marker(inputs))
    value = payload["verification"]
    _need(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    if generated_acceptance is not None:
        _need(value.get("generated_acceptance_sha256") == generated_acceptance)
        value = {key: item for key, item in value.items()
                 if key != "generated_acceptance_sha256"}
    report = validate_python_passed_receipt(
        value, expected_source=source, expected_recipe=inputs.sha256,
        expected_environment_inputs=inputs.environment_input_sha256,
        expected_environment=reference.environment_sha256,
    )
    _need(report["execution_id"] == invocation.execution_id
          and report["runtime"] == inputs.interpreter.to_payload())
    return PythonProjectReport(report, reference, source)


async def restore_python_verification(
    owner: ProjectVerificationService, *,
    kind: Literal["python_original", "python_generated"], tenant_id: UUID, run_id: UUID,
    actor: str, payload: Mapping[str, JsonValue], source_sha256: str, execution_id: str,
) -> None:
    """Rehydrate authenticated history, then inspect current delivery eligibility.

    Private coordinator use only: the Artifact/DAO check must precede this call.
    Neither model tool input nor replayed public results may call this helper as
    their own authority. No preparation, supply, run or route update occurs here.
    """
    key = (tenant_id, run_id)
    _clear(owner, key)
    try:
        _need(kind in ("python_original", "python_generated")
              and type(tenant_id) is UUID and type(run_id) is UUID
              and type(actor) is str and bool(actor))
        payload = _freeze_object(payload, name="private Python verification recovery")
        binding_field = ("original_check_contract" if kind == "python_original"
                         else "generated_project_acceptance")
        _need(set(payload) == {"verification", binding_field, "python_recipe",
                               "python_environment_reference", "summary"})
        reference = PythonEnvironmentReference.model_validate_json(
            _encoded(payload["python_environment_reference"]), strict=True)
        if kind == "python_original":
            service = owner._python
            context = await service.select(tenant_id, run_id, actor, approve=True)
            _need(context is not None)
            assert context is not None
            invocation = PythonProjectInvocation.model_validate({
                **python_original_invocation(context.invocation, context.inputs).model_dump(),
                "execution_id": execution_id, "source_kind": "candidate", "install_allowed": False,
            }, strict=True)
            context.stored.contract.require_current(
                await project_source_manifest(owner.root, context.invocation))
            _need(await project_source_fingerprint(owner.root, context.invocation) == source_sha256)
            references = await asyncio.to_thread(service._read_references, context)
            if reference not in references:
                # select already validated this DB-owned original observation.
                observation = cast(Mapping[str, JsonValue], context.state["observation"])
                historical = observation["environment_reference"]
                _need(historical is not None and reference == (
                    PythonEnvironmentReference.model_validate_json(_encoded(historical), strict=True)))
            _passed(payload, inputs=context.inputs, invocation=invocation,
                    reference=reference, source=source_sha256)
            binding = _OriginalScope(context.stored, context.observation_sha256).binding(source_sha256)
            _need(payload[binding_field] == binding)
            service.receipts[key] = _OriginalReceipt(context, invocation, source_sha256, reference, actor)
            service.environments[key] = reference
            owner._original_receipts[key] = binding
            _need(await service.require_current(tenant_id, run_id) == source_sha256)
        else:
            generated = owner._python_generated
            current = await generated.generated.load(tenant_id, run_id, actor, readonly=True)
            _need(current is not None)
            assert current is not None
            _need(current.envelope.manifest.stack == "python"
                  and current.state["phase"] == "python_ready")
            inputs = await generated._inputs(current)
            current.envelope.require_current(await project_source_manifest(owner.root, current.invocation))
            _need(await project_source_fingerprint(owner.root, current.invocation) == source_sha256)
            invocation = PythonProjectInvocation.model_validate({
                **python_original_invocation(current.invocation, inputs).model_dump(),
                "execution_id": execution_id, "source_kind": "candidate", "install_allowed": False,
            }, strict=True)
            _need(reference in await asyncio.to_thread(generated._read_references, current, inputs))
            report = _passed(payload, inputs=inputs, invocation=invocation,
                             reference=reference, source=source_sha256,
                             generated_acceptance=current.envelope.sha256)
            saved_binding = payload[binding_field]
            _need(isinstance(saved_binding, Mapping))
            assert isinstance(saved_binding, Mapping)
            evidence = saved_binding["test_execution"]
            _need(isinstance(evidence, Mapping))
            assert isinstance(evidence, Mapping)
            binding = generated_verification_binding(current, source_sha256, evidence)
            _need(saved_binding == binding and report.report["test_count"] == evidence["count"])
            generated.receipts[key] = _GeneratedReceipt(
                current, invocation, inputs, source_sha256, reference, actor, binding)
            _need(await generated.require_current(tenant_id, run_id) == source_sha256)
    except (asyncio.CancelledError, ProcessCleanupIncomplete):
        _clear(owner, key)
        raise
    except Exception:  # noqa: BLE001 - untrusted/stale data cannot become delivery authority.
        _clear(owner, key)
        raise ProjectVerificationError(_ERROR) from None
