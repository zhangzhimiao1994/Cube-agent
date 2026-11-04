"""Strict Python project declarations; data integrity, never install authority."""

from __future__ import annotations

import hashlib
import json
import keyword
import re
import sys
import tomllib
from dataclasses import dataclass
from typing import Literal, cast

from agent_hub.capability_installer.environment import (
    EnvironmentBuildError,
    _validate_hashed_requirements,
)

_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?')
_VERSION = re.compile(
    r'(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?'
    r'(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?'
)
_MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')
_SHA = re.compile(r'[a-f0-9]{64}')
_CODE = Literal['python_project_recipe_invalid', 'python_project_recipe_unsupported',
    'python_project_lock_invalid', 'python_project_declaration_limit']


class PythonProjectRecipeError(ValueError):
    def __init__(self, code: _CODE) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: _CODE = 'python_project_recipe_invalid') -> None:
    if not value:
        raise PythonProjectRecipeError(code)


def python_declaration_issue(lock: bytes, error: ValueError | TypeError) -> str:
    """Finite repair data from our parser; never expose arbitrary exception text."""
    fallback = 'python_declaration_invalid'
    if (type(error) is not PythonProjectRecipeError or error.args != (error.code,)
            or error.code not in {'python_project_lock_invalid', 'python_project_declaration_limit'}):
        return fallback
    if not lock:
        return 'python_lock_empty'
    if error.code != 'python_project_lock_invalid':
        return fallback
    try:
        _validate_hashed_requirements(lock.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n'))
    except EnvironmentBuildError as cause:
        if type(cause) is not EnvironmentBuildError or len(cause.args) != 1:
            return fallback
        return {
            'remote or editable requirements are forbidden': 'python_lock_external_source_forbidden',
            'Python lock file is empty': 'python_lock_empty',
            'Python requirements must use fixed versions': 'python_lock_fixed_versions_required',
            'every Python requirement must include a SHA256 hash': 'python_lock_hashes_required',
        }.get(cause.args[0], fallback) if type(cause.args[0]) is str else fallback
    except UnicodeError:
        return fallback
    return fallback


def _name(value: str) -> str:
    _need(type(value) is str and _NAME.fullmatch(value) is not None)
    return re.sub(r'[-_.]+', '-', value).lower()


def _pin(value: object) -> tuple[str, str]:
    _need(type(value) is str)
    parts = cast(str, value).split('==')
    _need(len(parts) == 2 and _VERSION.fullmatch(parts[1]) is not None,
        'python_project_recipe_unsupported')
    return _name(parts[0]), parts[1]


def _object(value: object) -> dict[str, object]:
    _need(type(value) is dict)
    return cast(dict[str, object], value)


def _pins(value: object) -> dict[str, str]:
    _need(type(value) is list)
    result: dict[str, str] = {}
    for item in cast(list[object], value):
        name, version = _pin(item)
        _need(name not in result)
        result[name] = version
    return result


def _python_constraint(value: str) -> str:
    if re.fullmatch(r'[ \t]*>=[ \t]*3\.12[ \t]*,[ \t]*<[ \t]*3\.13[ \t]*', value):
        return '>=3.12,<3.13'
    if re.fullmatch(r'[ \t]*==[ \t]*3\.12\.\*[ \t]*', value):
        return '==3.12.*'
    raise PythonProjectRecipeError('python_project_recipe_unsupported')


@dataclass(frozen=True, slots=True)
class PythonLockedRequirement:
    name: str
    version: str
    hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _need(_name(self.name) == self.name and type(self.version) is str
            and _VERSION.fullmatch(self.version) is not None)
        _need(type(self.hashes) is tuple and self.hashes
            and all(type(item) is str and _SHA.fullmatch(item) for item in self.hashes))
        _need(self.hashes == tuple(sorted(set(self.hashes))))

    def to_payload(self) -> dict[str, object]:
        return {'name': self.name, 'version': self.version, 'hashes': list(self.hashes)}


@dataclass(frozen=True, slots=True)
class PythonProjectDeclaration:
    project_name: str
    project_version: str
    import_module: str
    requires_python: str
    manifest_sha256: str
    lock_sha256: str
    requirements: tuple[PythonLockedRequirement, ...]
    runtime_dependencies: tuple[str, ...]
    build_dependencies: tuple[str, ...]
    kind: Literal['python_offline_wheel_v1'] = 'python_offline_wheel_v1'

    def __post_init__(self) -> None:
        _need(type(self.kind) is str and self.kind == 'python_offline_wheel_v1')
        _need(_name(self.project_name) == self.project_name
            and type(self.project_version) is str and _VERSION.fullmatch(self.project_version) is not None)
        _need(type(self.import_module) is str and len(self.import_module) <= 255
            and _MODULE.fullmatch(self.import_module) is not None
            and not any(keyword.iskeyword(part) for part in self.import_module.split('.')))
        _need(type(self.requires_python) is str and self.requires_python in ('>=3.12,<3.13', '==3.12.*'),
            'python_project_recipe_unsupported')
        _need(all(type(value) is str and _SHA.fullmatch(value)
            for value in (self.manifest_sha256, self.lock_sha256)))
        _need(type(self.requirements) is tuple and self.requirements
            and all(type(item) is PythonLockedRequirement for item in self.requirements))
        names = tuple(item.name for item in self.requirements)
        _need(names == tuple(sorted(set(names))) and {'setuptools', 'wheel'} <= set(names))
        _need(type(self.runtime_dependencies) is tuple
            and all(type(name) is str for name in self.runtime_dependencies)
            and self.runtime_dependencies == tuple(sorted(set(self.runtime_dependencies)))
            and set(self.runtime_dependencies) <= set(names))
        _need(type(self.build_dependencies) is tuple
            and all(type(name) is str for name in self.build_dependencies)
            and self.build_dependencies == tuple(sorted(set(self.build_dependencies)))
            and {'setuptools', 'wheel'} <= set(self.build_dependencies) <= set(names))
        root = self.import_module.split('.')[0]
        _need(root not in sys.stdlib_module_names and root not in {'pip', 'setuptools', 'wheel'}
            and re.sub(r'[-_.]+', '-', root).lower() not in names, 'python_project_recipe_unsupported')

    def to_payload(self) -> dict[str, object]:
        return {'schema_version': 1, 'kind': self.kind, 'project_name': self.project_name,
            'project_version': self.project_version, 'import_module': self.import_module,
            'requires_python': self.requires_python, 'manifest_sha256': self.manifest_sha256,
            'lock_sha256': self.lock_sha256,
            'requirements': [item.to_payload() for item in self.requirements],
            'runtime_dependencies': list(self.runtime_dependencies),
            'build_dependencies': list(self.build_dependencies),
            'build_recipe': 'setuptools_pep517_wheel_v1', 'test_recipe': 'unittest_discover_v1',
            'source_directory': 'src', 'test_directory': 'tests'}

    @property
    def sha256(self) -> str:
        raw = json.dumps(self.to_payload(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False).encode('ascii')
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_payload(cls, value: object) -> PythonProjectDeclaration:
        item = _object(value)
        _need(set(item) == {'schema_version', 'kind', 'project_name', 'project_version',
            'import_module', 'requires_python', 'manifest_sha256', 'lock_sha256',
            'requirements', 'runtime_dependencies', 'build_dependencies', 'build_recipe', 'test_recipe',
            'source_directory', 'test_directory'})
        _need(type(item['schema_version']) is int and item['schema_version'] == 1)
        requirements = item['requirements']
        dependencies = item['runtime_dependencies']
        build_dependencies = item['build_dependencies']
        _need(type(requirements) is list and type(dependencies) is list and type(build_dependencies) is list)
        locked: list[PythonLockedRequirement] = []
        for requirement in cast(list[object], requirements):
            pin = _object(requirement)
            _need(set(pin) == {'name', 'version', 'hashes'} and type(pin['hashes']) is list)
            locked.append(PythonLockedRequirement(cast(str, pin['name']), cast(str, pin['version']),
                tuple(cast(list[str], pin['hashes']))))
        result = cls(project_name=cast(str, item['project_name']), project_version=cast(str, item['project_version']),
            import_module=cast(str, item['import_module']), requires_python=cast(str, item['requires_python']),
            manifest_sha256=cast(str, item['manifest_sha256']), lock_sha256=cast(str, item['lock_sha256']),
            requirements=tuple(locked), runtime_dependencies=tuple(cast(list[str], dependencies)),
            build_dependencies=tuple(cast(list[str], build_dependencies)),
            kind=cast(Literal['python_offline_wheel_v1'], item['kind']))
        _need(result.to_payload() == item)
        return result


def parse_python_project_declaration(
    manifest: bytes, lock: bytes, *, max_control_bytes: int, max_requirements: int,
) -> PythonProjectDeclaration:
    _need(type(max_control_bytes) is int and max_control_bytes > 0
        and type(max_requirements) is int and max_requirements > 0,
        'python_project_declaration_limit')
    _need(type(manifest) is bytes and type(lock) is bytes)
    _need(0 < len(manifest) and 0 < len(lock)
        and len(manifest) + len(lock) <= max_control_bytes, 'python_project_declaration_limit')
    try:
        text = lock.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        _validate_hashed_requirements(text)
    except (UnicodeError, EnvironmentBuildError):
        raise PythonProjectRecipeError('python_project_lock_invalid') from None
    try:
        project_data = tomllib.loads(manifest.decode('utf-8-sig'))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise PythonProjectRecipeError('python_project_recipe_invalid') from None
    except RecursionError:
        raise PythonProjectRecipeError('python_project_declaration_limit') from None
    locked: dict[str, PythonLockedRequirement] = {}
    for line in text.replace('\\\n', ' ').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        tokens = line.split()
        name, version = _pin(tokens[0])
        _need(name not in locked, 'python_project_lock_invalid')
        hashes = tuple(sorted({token.removeprefix('--hash=sha256:').lower() for token in tokens[1:]}))
        locked[name] = PythonLockedRequirement(name, version, hashes)
        _need(len(locked) <= max_requirements, 'python_project_declaration_limit')
    project = _object(project_data.get('project'))
    build = _object(project_data.get('build-system'))
    tools = _object(project_data.get('tool'))
    _need(('dynamic' not in project or type(project['dynamic']) is list and project['dynamic'] == [])
        and ('optional-dependencies' not in project
             or type(project['optional-dependencies']) is dict and project['optional-dependencies'] == {})
        and 'dependency-groups' not in project_data
        and not {'uv', 'poetry', 'pdm'}.intersection(tools), 'python_project_recipe_unsupported')
    _need(set(build) == {'requires', 'build-backend'}
        and build.get('build-backend') == 'setuptools.build_meta', 'python_project_recipe_unsupported')
    selection = _object(_object(tools.get('agent-hub')).get('python-project'))
    _need(set(selection) == {'recipe', 'import-module', 'test-directory'}
        and selection.get('recipe') == 'python_offline_wheel_v1'
        and selection.get('test-directory') == 'tests', 'python_project_recipe_unsupported')
    setuptools = _object(tools.get('setuptools'))
    _need(set(setuptools) == {'package-dir', 'packages'} and setuptools.get('package-dir') == {'': 'src'}
        and setuptools.get('packages') == {'find': {'where': ['src']}}, 'python_project_recipe_unsupported')
    runtime_pins = _pins(project.get('dependencies', []))
    build_pins = _pins(build.get('requires'))
    _need({'setuptools', 'wheel'} <= set(build_pins), 'python_project_recipe_unsupported')
    _need(all(name in locked and locked[name].version == version
        for pins in (runtime_pins, build_pins) for name, version in pins.items()), 'python_project_lock_invalid')
    _need(type(project.get('name')) is str and type(project.get('version')) is str
        and type(project.get('requires-python')) is str and type(selection.get('import-module')) is str)
    return PythonProjectDeclaration(
        project_name=_name(cast(str, project['name'])), project_version=cast(str, project['version']),
        import_module=cast(str, selection['import-module']),
        requires_python=_python_constraint(cast(str, project['requires-python'])),
        manifest_sha256=hashlib.sha256(manifest).hexdigest(), lock_sha256=hashlib.sha256(lock).hexdigest(),
        requirements=tuple(locked[name] for name in sorted(locked)),
        runtime_dependencies=tuple(sorted(runtime_pins)),
        build_dependencies=tuple(sorted(build_pins)),
    )
