"""Scoped offline Python recipe data; not runtime measurement or install authority.

The producer captures actual controls before construction. Consumers must re-read
the trusted snapshot, wheels and interpreter before execution or delivery; hashes
supplied by a model or a persisted payload are never those observations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.project_contracts import ProjectCheckContract
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectDeclaration,
    PythonProjectRecipeError,
)
from agent_hub.capabilities.python_project_wheels import (
    PythonWheelBinding,
    bind_python_project_wheels,
)
from agent_hub.capabilities.types import CapabilityRequest

_SHA = re.compile('[a-f0-9]{64}')
_PYTHON_RESOURCE = 'workspace/current/project-verification/python/'


def _need(condition: object) -> None:
    if not condition:
        raise PythonProjectRecipeError('python_project_recipe_invalid')


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')).hexdigest()


def _object(value: object) -> dict[str, object]:
    _need(type(value) is dict)
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class PythonInterpreterIdentity:
    implementation: str
    version: str
    abi: str
    platform: str
    binary_sha256: str
    base_runtime_sha256: str

    def __post_init__(self) -> None:
        _need(type(self.implementation) is str and self.implementation == 'cpython'
            and type(self.version) is str and re.fullmatch(r'3\.12\.[0-9]{1,5}', self.version) is not None
            and type(self.abi) is str and self.abi == 'cp312'
            and type(self.platform) is str and self.platform in {'linux_x86_64', 'linux_aarch64'}
            and all(type(value) is str and _SHA.fullmatch(value) is not None
                for value in (self.binary_sha256, self.base_runtime_sha256)))

    def to_payload(self) -> dict[str, object]:
        return {'implementation': self.implementation, 'version': self.version,
            'abi': self.abi, 'platform': self.platform, 'binary_sha256': self.binary_sha256,
            'base_runtime_sha256': self.base_runtime_sha256}

    @classmethod
    def from_payload(cls, value: object) -> PythonInterpreterIdentity:
        item = _object(value)
        _need(set(item) == {'implementation', 'version', 'abi', 'platform',
            'binary_sha256', 'base_runtime_sha256'})
        return cls(**cast(dict[str, str], item))


@dataclass(frozen=True, slots=True)
class PythonProjectInputs:
    contract: ProjectCheckContract
    original_reference: ProjectBaselineReference
    declaration: PythonProjectDeclaration
    interpreter: PythonInterpreterIdentity
    wheels: tuple[PythonWheelBinding, ...]

    def __post_init__(self) -> None:
        _need(type(self.contract) is ProjectCheckContract
            and type(self.original_reference) is ProjectBaselineReference
            and type(self.declaration) is PythonProjectDeclaration
            and type(self.interpreter) is PythonInterpreterIdentity)
        protected = self.contract.protected_files
        _need(self.contract.input_source_sha256 == self.original_reference.input_source_sha256
            and 'pyproject.toml' in protected)
        wheels = bind_python_project_wheels(self.declaration.requirements, self.wheels)
        if 'requirements.lock' not in protected:
            # Private locks bind data only. Root must still remeasure the
            # declaration, wheel selection and actual original before use.
            from agent_hub.capabilities.python_original_private_recipe import (
                canonical_original_lock,
            )

            pins = self.declaration.requirements
            if (any(pin.hashes != (wheel.sha256,) for pin, wheel in zip(pins, wheels, strict=True))
                    or hashlib.sha256(canonical_original_lock(pins)).hexdigest()
                    != self.declaration.lock_sha256):
                raise PythonProjectRecipeError('python_project_lock_invalid')
        object.__setattr__(self, 'wheels', wheels)

    def _binding(self) -> dict[str, object]:
        return {'schema_version': 1, 'kind': 'python_offline_wheel_v1',
            'contract': self.contract.to_payload(), 'original_reference': self.original_reference.to_payload(),
            'declaration': self.declaration.to_payload(), 'interpreter': self.interpreter.to_payload(),
            'wheels': [item.to_payload() for item in self.wheels]}

    @property
    def sha256(self) -> str:
        return _digest(self._binding())

    @property
    def environment_input_sha256(self) -> str:
        # Scope, file formatting and issuance are not changes to execution inputs.
        return _digest({'schema_version': 1, 'kind': 'python_offline_wheel_v1',
            'interpreter': self.interpreter.to_payload(),
            'wheels': [{'name': item.name, 'version': item.version, 'sha256': item.sha256}
                for item in self.wheels]})

    def to_payload(self) -> dict[str, object]:
        return self._binding() | {'recipe_sha256': self.sha256,
            'environment_input_sha256': self.environment_input_sha256}

    @classmethod
    def from_payload(cls, value: object) -> PythonProjectInputs:
        item = _object(value)
        _need(set(item) == {'schema_version', 'kind', 'contract', 'original_reference',
            'declaration', 'interpreter', 'wheels', 'recipe_sha256', 'environment_input_sha256'}
            and type(item['schema_version']) is int and item['schema_version'] == 1
            and type(item['kind']) is str and item['kind'] == 'python_offline_wheel_v1'
            and type(item['wheels']) is list)
        wheels: list[PythonWheelBinding] = []
        for wheel_value in cast(list[object], item['wheels']):
            wheel = _object(wheel_value)
            _need(set(wheel) == {'filename', 'name', 'version', 'sha256'})
            wheels.append(PythonWheelBinding(**cast(dict[str, str], wheel)))
        result = cls(contract=ProjectCheckContract.from_payload(item['contract']),
            original_reference=ProjectBaselineReference.from_payload(item['original_reference']),
            declaration=PythonProjectDeclaration.from_payload(item['declaration']),
            interpreter=PythonInterpreterIdentity.from_payload(item['interpreter']), wheels=tuple(wheels))
        _need(result.to_payload() == item)
        return result

    def _approval_arguments(self) -> dict[str, object]:
        return {'schema_version': 1, 'recipe_sha256': self.sha256,
            'environment_input_sha256': self.environment_input_sha256,
            'project_id': self.contract.project_id, 'workspace_session_id': self.contract.session_id}

    def approval_request(self, *, user_id: UUID, agent_id: str, idempotency_key: str) -> CapabilityRequest:
        """Build from trusted task scope, never expose these fields as tool arguments."""
        return CapabilityRequest(tenant_id=self.contract.tenant_id, run_id=self.contract.run_id,
            user_id=user_id, agent_id=agent_id, capability='command', operation='run',
            resource=_PYTHON_RESOURCE + self.sha256, arguments=self._approval_arguments(),
            idempotency_key=idempotency_key)

    def matches_approval_request(self, request: CapabilityRequest) -> bool:
        return ((request.tenant_id, request.run_id, request.capability, request.operation, request.resource)
            == (self.contract.tenant_id, self.contract.run_id, 'command', 'run', _PYTHON_RESOURCE + self.sha256)
            and type(request.arguments.get('schema_version')) is int
            and request.arguments == self._approval_arguments())


def bind_python_project_inputs(
    *, contract: ProjectCheckContract, original_reference: ProjectBaselineReference,
    declaration: PythonProjectDeclaration, interpreter: PythonInterpreterIdentity,
    wheels: tuple[PythonWheelBinding, ...], manifest: bytes, lock: bytes,
) -> PythonProjectInputs:
    """Bind controls read from the immutable original; still no installation grant."""
    _need(type(manifest) is bytes and type(lock) is bytes)
    for path, raw, expected in (('pyproject.toml', manifest, declaration.manifest_sha256),
                                ('requirements.lock', lock, declaration.lock_sha256)):
        _need(hashlib.sha256(raw).hexdigest() == expected)
        # The shared original-file map includes the executable-mode tag, not a raw SHA.
        _need(contract.protected_files.get(path) in {
            hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')})
    return PythonProjectInputs(contract=contract, original_reference=original_reference,
        declaration=declaration, interpreter=interpreter, wheels=wheels)
