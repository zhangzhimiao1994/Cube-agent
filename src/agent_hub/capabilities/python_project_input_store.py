"""Once-only recipe metadata next to, never inside, the original v1 envelope."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from agent_hub.capabilities.project_baselines import (
    _CONTROL_LIMIT,
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
    _encoded,
    _object,
)
from agent_hub.capabilities.python_project_inputs import (
    PythonInterpreterIdentity,
    PythonProjectInputs,
    bind_python_project_inputs,
)
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectRecipeError,
    parse_python_project_declaration,
)
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
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


def _capture(
    stored: StoredProjectBaseline, *, interpreter: PythonInterpreterIdentity,
    wheels: tuple[PythonWheelBinding, ...], max_control_bytes: int, max_requirements: int,
) -> PythonProjectInputs:
    raw = {name: _read_no_links(stored.snapshot, (name,), max_bytes=max_control_bytes)
        for name in ('pyproject.toml', 'requirements.lock')}
    declaration = parse_python_project_declaration(raw['pyproject.toml'], raw['requirements.lock'],
        max_control_bytes=max_control_bytes, max_requirements=max_requirements)
    module = 'src/' + declaration.import_module.replace('.', '/')
    paths = stored.source_files.keys()
    if ('setup.py' in paths or 'setup.cfg' in paths
            or module + '/__init__.py' not in paths
            or any(path.startswith('tests/') and path not in stored.contract.protected_files for path in paths)
            or not _discoverable_tests(stored.source_files)):
        raise PythonProjectRecipeError('python_project_recipe_unsupported')
    return bind_python_project_inputs(contract=stored.contract, original_reference=stored.reference,
        declaration=declaration, interpreter=interpreter, wheels=wheels,
        manifest=raw['pyproject.toml'], lock=raw['requirements.lock'])


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
    ) -> PythonProjectInputs:
        """Recover only against inputs independently remeasured by the root probe.

        Existing complete or partial files are never removed or overwritten.
        This is not baseline recapture and does not trust a stored recipe digest.
        """
        try:
            invocation = self.baselines._invocation(invocation, require_workspace=False)
            if (type(max_control_bytes) is not int
                    or not 0 < max_control_bytes <= min(_CONTROL_LIMIT, invocation.source_limit_bytes)):
                raise PythonProjectRecipeError('python_project_declaration_limit')
            stored = self.baselines.load(invocation, reference)
            expected = _capture(stored, interpreter=interpreter, wheels=wheels,
                max_control_bytes=max_control_bytes, max_requirements=max_requirements)
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
                    max_requirements=len(result.declaration.requirements))
                if observed != result:
                    raise ValueError
                controls.check()
                return result
        except PythonProjectRecipeError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError):
            raise PythonProjectRecipeError('python_project_recipe_invalid') from None
