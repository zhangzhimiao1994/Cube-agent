"""Once-only recipe metadata next to, never inside, the original v1 envelope."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.capabilities.project_baselines import (
    _CONTROL_LIMIT,
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
    _encoded,
    _object,
)
from agent_hub.capabilities.python_original_metadata import standard_python_intent
from agent_hub.capabilities.python_original_private_recipe import _active, canonical_original_lock
from agent_hub.capabilities.python_project_inputs import (
    PythonInterpreterIdentity,
    PythonProjectInputs,
    bind_python_project_inputs,
)
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectDeclaration,
    PythonProjectRecipeError,
    parse_python_project_declaration,
)
from agent_hub.capabilities.python_project_wheels import (
    PythonWheelBinding,
    bind_python_project_wheels,
)
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox.project import ProjectInvocation

_FILENAME = 'python-recipe.json'


def _discoverable_tests(files: Mapping[str, str]) -> bool:
    tests = [path for path in files if path.startswith('tests/')
        and path.rsplit('/', 1)[-1].startswith('test') and path.endswith('.py')]
    if not tests:
        return False
    for path in tests:
        parts = path.split('/')
        if (re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', parts[-1]) is None
                or any(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', part) is None for part in parts[1:-1])
                or any('/'.join(parts[:index]) + '/__init__.py' not in files
                       for index in range(2, len(parts)))):
            return False
    return True


def _private_inputs(
    stored: StoredProjectBaseline, manifest: bytes, declaration: PythonProjectDeclaration, *,
    interpreter: PythonInterpreterIdentity, wheels: tuple[PythonWheelBinding, ...],
    max_control_bytes: int, max_requirements: int,
) -> PythonProjectInputs:
    if (stored.generated_metadata is not None
            or stored.contract.protected_files.get('pyproject.toml') not in {
                hashlib.sha256(tag + manifest).hexdigest() for tag in (b'f\0', b'x\0')}):
        raise PythonProjectRecipeError('python_project_recipe_invalid')
    intent = standard_python_intent(
        manifest, paths=stored.source_files.keys(), protected=stored.contract.protected_files.keys(),
    )
    if (intent is None or type(declaration) is not PythonProjectDeclaration
            or type(interpreter) is not PythonInterpreterIdentity):
        raise PythonProjectRecipeError('python_project_recipe_unsupported')
    if ((declaration.project_name, declaration.project_version, declaration.import_module,
         declaration.manifest_sha256, declaration.requires_python)
            != (intent.project_name, intent.project_version, intent.import_module,
                intent.manifest_sha256, '==3.12.*')
            or not intent.supports_interpreter(interpreter.version)):
        raise PythonProjectRecipeError('python_project_recipe_invalid')
    if type(max_requirements) is not int or len(declaration.requirements) > max_requirements:
        raise PythonProjectRecipeError('python_project_declaration_limit')
    selected = bind_python_project_wheels(declaration.requirements, wheels)
    if any(pin.hashes != (wheel.sha256,) for pin, wheel in zip(declaration.requirements, selected, strict=True)):
        raise PythonProjectRecipeError('python_project_lock_invalid')
    lock = canonical_original_lock(declaration.requirements)
    if len(manifest) + len(lock) > max_control_bytes:
        raise PythonProjectRecipeError('python_project_declaration_limit')
    if hashlib.sha256(lock).hexdigest() != declaration.lock_sha256:
        raise PythonProjectRecipeError('python_project_lock_invalid')
    # Use the measured interpreter, not the Worker's host. The identity does
    # not carry kernel marker values, so recipes depending on them fail closed.
    environment = {
        'implementation_name': interpreter.implementation,
        'implementation_version': interpreter.version,
        'os_name': 'posix', 'platform_machine': interpreter.platform.removeprefix('linux_'),
        'platform_release': '', 'platform_system': 'Linux', 'platform_version': '',
        'platform_python_implementation': 'CPython', 'python_full_version': interpreter.version,
        'python_version': '3.12', 'sys_platform': 'linux',
    }
    versions = {wheel.name: Version(wheel.version) for wheel in selected}
    for declared, roots in ((declaration.runtime_dependencies, intent.runtime_requirements),
                            (declaration.build_dependencies, intent.build_requirements)):
        if any(re.search(r'\b(?:platform_release|platform_version)\b', text.partition(';')[2])
               for text in roots):
            raise PythonProjectRecipeError('python_project_recipe_unsupported')
        active = {canonicalize_name(item.name): item.specifier for item in _active(roots, environment)}
        if (set(declared) != set(active) or any(
                name not in versions or not specifier.contains(versions[name])
                for name, specifier in active.items())):
            raise PythonProjectRecipeError('python_project_lock_invalid')
    # The store checks declared roots and canonical pins. Root independently
    # remeasures the entire resolution, including transitive wheel metadata.
    return PythonProjectInputs(contract=stored.contract, original_reference=stored.reference,
        declaration=declaration, interpreter=interpreter, wheels=selected)


def _capture(
    stored: StoredProjectBaseline, *, interpreter: PythonInterpreterIdentity,
    wheels: tuple[PythonWheelBinding, ...], max_control_bytes: int, max_requirements: int,
    private_declaration: PythonProjectDeclaration | None = None,
) -> PythonProjectInputs:
    manifest = _read_no_links(stored.snapshot, ('pyproject.toml',), max_bytes=max_control_bytes)
    if private_declaration is not None:
        tool = tomllib.loads(manifest.decode('utf-8-sig')).get('tool', {})
        agent = tool.get('agent-hub', {}) if isinstance(tool, dict) else {}
        if not isinstance(agent, dict) or 'python-project' not in agent:
            return _private_inputs(stored, manifest, private_declaration, interpreter=interpreter,
                wheels=wheels, max_control_bytes=max_control_bytes, max_requirements=max_requirements)
    lock = _read_no_links(stored.snapshot, ('requirements.lock',), max_bytes=max_control_bytes)
    declaration = parse_python_project_declaration(manifest, lock,
        max_control_bytes=max_control_bytes, max_requirements=max_requirements)
    if private_declaration is not None and declaration != private_declaration:
        raise PythonProjectRecipeError('python_project_recipe_invalid')
    module = 'src/' + declaration.import_module.replace('.', '/')
    paths = stored.source_files.keys()
    if ('setup.py' in paths or 'setup.cfg' in paths
            or module + '/__init__.py' not in paths
            or any(path.startswith('tests/') and path not in stored.contract.protected_files for path in paths)
            or not _discoverable_tests(stored.source_files)):
        raise PythonProjectRecipeError('python_project_recipe_unsupported')
    return bind_python_project_inputs(contract=stored.contract, original_reference=stored.reference,
        declaration=declaration, interpreter=interpreter, wheels=wheels,
        manifest=manifest, lock=lock)


class PythonProjectInputStore:
    def __init__(self, workspace_root: Path) -> None:
        self.baselines = ProjectBaselineStore(workspace_root)

    def capture_once(
        self, invocation: ProjectInvocation, reference: ProjectBaselineReference, *,
        interpreter: PythonInterpreterIdentity, wheels: tuple[PythonWheelBinding, ...],
        max_control_bytes: int, max_requirements: int,
    ) -> PythonProjectInputs:
        try:
            invocation = self.baselines._invocation(invocation, require_workspace=False)
            if (type(max_control_bytes) is not int
                    or not 0 < max_control_bytes <= min(_CONTROL_LIMIT, invocation.source_limit_bytes)):
                raise PythonProjectRecipeError('python_project_declaration_limit')
            stored = self.baselines.load(invocation, reference)
            result = _capture(stored, interpreter=interpreter, wheels=wheels,
                max_control_bytes=max_control_bytes, max_requirements=max_requirements)
            with ExitStack() as stack:
                controls = self.baselines._controls(invocation, stack, create=False)
                # O_EXCL preserves a partial record as a consumed fence too.
                self.baselines._write_once(controls, _FILENAME, _encoded(result.to_payload()))
                controls.check()
            return result
        except PythonProjectRecipeError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError):
            raise PythonProjectRecipeError('python_project_recipe_invalid') from None

    def capture_or_load_matching(
        self, invocation: ProjectInvocation, reference: ProjectBaselineReference, *,
        interpreter: PythonInterpreterIdentity, wheels: tuple[PythonWheelBinding, ...],
        max_control_bytes: int, max_requirements: int,
        private_declaration: PythonProjectDeclaration | None = None,
    ) -> PythonProjectInputs:
        """Recover only against inputs independently remeasured by the root probe.

        Existing complete or partial files are never removed or overwritten.
        This is not baseline recapture and does not trust a stored recipe digest.
        A private declaration must come from the independently measured Root recipe.
        """
        try:
            invocation = self.baselines._invocation(invocation, require_workspace=False)
            if (type(max_control_bytes) is not int
                    or not 0 < max_control_bytes <= min(_CONTROL_LIMIT, invocation.source_limit_bytes)):
                raise PythonProjectRecipeError('python_project_declaration_limit')
            stored = self.baselines.load(invocation, reference)
            expected = _capture(stored, interpreter=interpreter, wheels=wheels,
                max_control_bytes=max_control_bytes, max_requirements=max_requirements,
                private_declaration=private_declaration)
            with ExitStack() as stack:
                controls = self.baselines._controls(invocation, stack, create=False)
                try:
                    controls.info(_FILENAME)
                except FileNotFoundError:
                    try:
                        self.baselines._write_once(controls, _FILENAME, _encoded(expected.to_payload()))
                    except FileExistsError:
                        pass
                controls.check()
            return self.load(invocation, reference, expected_sha256=expected.sha256)
        except PythonProjectRecipeError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError):
            raise PythonProjectRecipeError('python_project_recipe_invalid') from None

    def load(
        self, invocation: ProjectInvocation, reference: ProjectBaselineReference, *, expected_sha256: str,
    ) -> PythonProjectInputs:
        try:
            invocation = self.baselines._invocation(invocation, require_workspace=False)
            stored = self.baselines.load(invocation, reference)
            with ExitStack() as stack:
                controls = self.baselines._controls(invocation, stack, create=False)
                raw = controls.read(_FILENAME, _CONTROL_LIMIT)
                payload: object = json.loads(raw, object_pairs_hook=_object)
                result = PythonProjectInputs.from_payload(payload)
                if (type(expected_sha256) is not str or result.sha256 != expected_sha256
                        or result.contract != stored.contract or result.original_reference != reference
                        or _encoded(result.to_payload()) != raw):
                    raise ValueError
                observed = _capture(stored, interpreter=result.interpreter, wheels=result.wheels,
                    max_control_bytes=min(_CONTROL_LIMIT, invocation.source_limit_bytes),
                    max_requirements=len(result.declaration.requirements),
                    private_declaration=result.declaration)
                if observed != result:
                    raise ValueError
                controls.check()
                return result
        except PythonProjectRecipeError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError):
            raise PythonProjectRecipeError('python_project_recipe_invalid') from None
