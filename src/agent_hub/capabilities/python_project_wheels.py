"""Bind declared wheel identities to locks; no archive or installed-state verification.

Only build-tag-free ``py3-none-any.whl`` filenames are supported. Versions use
the recipe's grammar, excluding epochs and local separators requiring filename
normalization. Names and versions are compared without version normalization.
The supplied SHA is an assertion: this module never reads or hashes a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_hub.capabilities.python_project_recipe import (
    PythonLockedRequirement,
    PythonProjectRecipeError,
)

_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+!-]*')


@dataclass(frozen=True, slots=True)
class PythonWheelBinding:
    filename: str
    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if not all(type(value) is str for value in (
            self.filename, self.name, self.version, self.sha256,
        )):
            raise PythonProjectRecipeError('python_project_recipe_invalid')
        # Reuse the recipe's canonical identity, narrow version, and SHA contract.
        PythonLockedRequirement(self.name, self.version, (self.sha256,))
        if _FILENAME.fullmatch(self.filename) is None:
            raise PythonProjectRecipeError('python_project_recipe_invalid')
        if any(separator in self.version for separator in ('!', '_', '-')):
            raise PythonProjectRecipeError('python_project_recipe_unsupported')
        parts = self.filename.removesuffix('.whl').split('-')
        if (
            not self.filename.endswith('.whl')
            or len(parts) != 5
            or parts[2:] != ['py3', 'none', 'any']
        ):
            raise PythonProjectRecipeError('python_project_recipe_unsupported')
        distribution, version = parts[:2]
        canonical_name = re.sub(r'[-_.]+', '-', distribution).lower()
        if canonical_name != self.name or version != self.version:
            raise PythonProjectRecipeError('python_project_recipe_invalid')

    def to_payload(self) -> dict[str, str]:
        return {
            'filename': self.filename,
            'name': self.name,
            'version': self.version,
            'sha256': self.sha256,
        }


def bind_python_project_wheels(
    requirements: tuple[PythonLockedRequirement, ...],
    wheels: tuple[PythonWheelBinding, ...],
) -> tuple[PythonWheelBinding, ...]:
    """Require exactly one declared wheel per locked name, returned in name order."""
    if (
        type(requirements) is not tuple
        or type(wheels) is not tuple
        or not all(type(item) is PythonLockedRequirement for item in requirements)
        or not all(type(item) is PythonWheelBinding for item in wheels)
    ):
        raise PythonProjectRecipeError('python_project_recipe_invalid')

    locked: dict[str, PythonLockedRequirement] = {}
    for requirement in requirements:
        if requirement.name in locked:
            raise PythonProjectRecipeError('python_project_lock_invalid')
        locked[requirement.name] = requirement

    selected: dict[str, PythonWheelBinding] = {}
    filenames: set[str] = set()
    for wheel in wheels:
        if wheel.name in selected or wheel.filename in filenames:
            raise PythonProjectRecipeError('python_project_lock_invalid')
        locked_requirement = locked.get(wheel.name)
        if (
            locked_requirement is None
            or wheel.version != locked_requirement.version
            or wheel.sha256 not in locked_requirement.hashes
        ):
            raise PythonProjectRecipeError('python_project_lock_invalid')
        selected[wheel.name] = wheel
        filenames.add(wheel.filename)

    if selected.keys() != locked.keys():
        raise PythonProjectRecipeError('python_project_lock_invalid')
    return tuple(selected[name] for name in sorted(selected))
