"""Root FD staging of a service-owned Python recipe and its original snapshot.

Recipe data grants no installation authority. The caller authorizes the exact
recipe and the broker separately observes the interpreter, wheels and environment.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.python_project_input_store import _capture
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.skills.sandbox.project_contract_broker import (
    _CONTROL_LIMIT,
    _encoded,
    _object,
    _Observed,
    _open_directory,
    _read_control,
    _root,
    _seal_readonly,
    _write_all,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectInvocation,
    base_python_invocation,
)

_ERROR = 'python_project_staging_rejected'


class PythonProjectStageError(ValueError):
    """No broker-private path or source body leaves this boundary."""


def _need(condition: object) -> None:
    if not condition:
        raise PythonProjectStageError(_ERROR)


def decode_python_project_inputs(
    raw: bytes, *, invocation: PythonProjectInvocation, original: StoredProjectBaseline,
) -> PythonProjectInputs:
    """Decode canonical data from the stored recipe, never from a model envelope."""
    try:
        _need(type(raw) is bytes and len(raw) <= _CONTROL_LIMIT
            and type(invocation) is PythonProjectInvocation and type(original) is StoredProjectBaseline)
        invocation = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        item: object = json.loads(raw, object_pairs_hook=_object)
        inputs = PythonProjectInputs.from_payload(item)
        contract = inputs.contract
        _need(inputs.sha256 == invocation.recipe_sha256
            and inputs.environment_input_sha256 == invocation.environment_input_sha256
            and inputs.contract == original.contract
            and inputs.original_reference == original.reference
            and inputs.original_reference.to_payload() == invocation.original_reference.model_dump()
            and (contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
                == (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
            and _encoded(inputs.to_payload()) == raw)
        return inputs
    except (ValueError, TypeError, RecursionError, RuntimeError, AttributeError):
        raise PythonProjectStageError(_ERROR) from None


@dataclass(frozen=True, slots=True)
class StagedPythonProjectInputs:
    original: StoredProjectBaseline
    inputs: PythonProjectInputs
    recipe_path: Path


def stage_python_project_inputs(
    *, workspace_root: Path, invocation: PythonProjectInvocation, stage: Path, allowed_uid: int,
) -> StagedPythonProjectInputs:
    """Copy once under root0700; a partial stage is owned by the caller's UNKNOWN fence."""
    try:
        _need(type(allowed_uid) is int and 0 < allowed_uid < 2**32 - 1)
        if sys.platform != 'linux':
            raise PythonProjectStageError(_ERROR)
        _need(os.geteuid() == 0 and os.getegid() == 0 and type(invocation) is PythonProjectInvocation)
        invocation = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        original = stage_original_contract(workspace_root, base_python_invocation(invocation),
            ProjectBaselineReference.from_payload(invocation.original_reference.model_dump()), stage, allowed_uid)
        with ExitStack() as stack:
            observed: list[_Observed] = []
            target = _root(stage, 0, stack, observed, stage=True)
            controls = _root(workspace_root, allowed_uid, stack, observed)
            for name in ('.project-contracts', str(invocation.tenant_id), str(invocation.run_id)):
                controls = _open_directory(controls, name, allowed_uid, stack, observed, mode=0o700)
            raw = _read_control(controls, 'python-recipe.json', allowed_uid, observed)
            inputs = decode_python_project_inputs(raw, invocation=invocation, original=original)
            captured = _capture(original, interpreter=inputs.interpreter, wheels=inputs.wheels,
                max_control_bytes=min(_CONTROL_LIMIT, invocation.source_limit_bytes),
                max_requirements=len(inputs.declaration.requirements))
            _need(captured == inputs)
            for entry in observed:
                entry.check()
            descriptor = os.open('python-inputs.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=target)
            try:
                _write_all(descriptor, raw)
                _seal_readonly(descriptor, 0o444)
            finally:
                os.close(descriptor)
            os.fsync(target)
            for entry in observed:
                entry.check()
            return StagedPythonProjectInputs(original, inputs, stage / 'python-inputs.json')
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError, AttributeError):
        raise PythonProjectStageError(_ERROR) from None
