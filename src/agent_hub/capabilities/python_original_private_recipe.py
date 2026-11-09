"""Build private Python inputs from a verified, source-bound resolver result."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.capabilities.python_dependency_closure import (
    PythonWheelMetadata,
    check_python_dependency_closure,
)
from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntent,
    StandardPythonIntentError,
)
from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_recipe import (
    PythonLockedRequirement,
    PythonProjectDeclaration,
)
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.skills.sandbox.python_registry_protocol import (
    DependencyNeed,
    ResolutionDescriptor,
    ResolutionScope,
    target_environment,
)


@dataclass(frozen=True, slots=True)
class PrivateOriginalPythonRecipe:
    declaration: PythonProjectDeclaration
    lock: bytes
    wheels: tuple[PythonWheelBinding, ...]
    source_sha256: str
    resolution_sha256: str


def _active(requirements: tuple[str, ...], environment: Mapping[str, str]) -> tuple[Requirement, ...]:
    selected: list[Requirement] = []
    for text in requirements:
        requirement = Requirement(text)
        if requirement.url is not None or any(spec.operator == "===" for spec in requirement.specifier):
            raise StandardPythonIntentError("python_original_unsafe_requirement")
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment={**environment, "extra": ""}, context="requirement",
        ):
            continue
        requirement.marker = None
        selected.append(requirement)
    return tuple(selected)


def original_dependency_needs(
    intent: StandardPythonIntent, environment: Mapping[str, str],
) -> tuple[DependencyNeed, ...]:
    """Produce bounded exact requests; markers use the target, not this host."""
    try:
        selected = target_environment(environment)
        if not intent.supports_interpreter(selected["python_full_version"]):
            raise StandardPythonIntentError("python_original_interpreter_unsupported")
        active = _active(intent.build_requirements, selected) + _active(intent.runtime_requirements, selected)
        return tuple(DependencyNeed(text, intent.manifest_sha256)
                     for text in sorted({str(requirement) for requirement in active}))
    except StandardPythonIntentError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise StandardPythonIntentError("python_original_resolution_input_invalid") from None


def canonical_original_lock(requirements: tuple[PythonLockedRequirement, ...]) -> bytes:
    """Serialize a private lock. The source manifest and any source lock stay intact."""
    return "".join(
        f"{item.name}=={item.version}"
        + "".join(f" --hash=sha256:{sha}" for sha in item.hashes) + "\n"
        for item in requirements
    ).encode("ascii")


def original_pin_environment(interpreter: PythonInterpreterIdentity) -> dict[str, str]:
    """Reconstruct the supported direct marker inputs from a pinned interpreter."""
    return {
        "implementation_name": interpreter.implementation,
        "implementation_version": interpreter.version,
        "os_name": "posix", "platform_machine": interpreter.platform.removeprefix("linux_"),
        "platform_release": "", "platform_system": "Linux", "platform_version": "",
        "platform_python_implementation": "CPython", "python_full_version": interpreter.version,
        "python_version": ".".join(interpreter.version.split(".")[:2]), "sys_platform": "linux",
    }


def _recipe_from_verified_selection(
    intent: StandardPythonIntent, metadata: tuple[PythonWheelMetadata, ...], *,
    environment: Mapping[str, str], source_sha256: str, resolution_sha256: str,
) -> PrivateOriginalPythonRecipe:
    """Lower an exact dependency selection after its origin and reachability checks."""
    incomplete = "python_original_resolution_incomplete"
    wheels = tuple(item.wheel for item in metadata)
    if not 0 < len(wheels) <= 128:
        raise StandardPythonIntentError(incomplete)
    locks = tuple(PythonLockedRequirement(wheel.name, wheel.version, (wheel.sha256,)) for wheel in wheels)
    closure = check_python_dependency_closure(locks, metadata, environment)
    if closure.status != "complete":
        raise StandardPythonIntentError(incomplete)
    declaration = declaration_for_original_pins(intent, locks, environment=environment)
    return PrivateOriginalPythonRecipe(declaration, canonical_original_lock(locks), wheels,
                                       source_sha256, resolution_sha256)


def declaration_for_original_pins(
    intent: StandardPythonIntent, locks: tuple[PythonLockedRequirement, ...], *,
    environment: Mapping[str, str],
) -> PythonProjectDeclaration:
    """Check direct source ranges and canonically bind a trusted pin set.

    Does not verify transitive metadata or prove pins came from Root. The supply
    worker rederives these exact pins from the sealed baseline and Root catalog.
    """
    runtime = _active(intent.runtime_requirements, environment)
    build = _active(intent.build_requirements, environment)
    pins = {item.name: Version(item.version) for item in locks}
    if (not intent.supports_interpreter(environment["python_full_version"])
            or len(pins) != len(locks)
            or any((pin := pins.get(canonicalize_name(item.name))) is None or not item.specifier.contains(pin)
                   for item in (*runtime, *build))):
        raise StandardPythonIntentError("python_original_resolution_incomplete")
    lock = canonical_original_lock(locks)
    return PythonProjectDeclaration(
        project_name=intent.project_name,
        project_version=intent.project_version,
        import_module=intent.import_module,
        requires_python="==3.12.*",
        manifest_sha256=intent.manifest_sha256,
        lock_sha256=hashlib.sha256(lock).hexdigest(),
        requirements=locks,
        runtime_dependencies=tuple(sorted({canonicalize_name(item.name) for item in runtime})),
        build_dependencies=tuple(sorted({canonicalize_name(item.name) for item in build})),
    )


def derive_private_original_recipe(
    intent: StandardPythonIntent, descriptor: ResolutionDescriptor, *,
    expected_scope: ResolutionScope, verified_descriptor_sha256: str,
) -> PrivateOriginalPythonRecipe:
    """Lower a trusted resolver receipt without writing user or sandbox files.

    The caller must obtain the verified digest and scope from the trusted
    resolution ledger. This value itself does not authorize installs or IO.
    """
    mismatch = "python_original_resolution_scope_mismatch"
    incomplete = "python_original_resolution_incomplete"
    try:
        if (type(descriptor) is not ResolutionDescriptor or type(expected_scope) is not ResolutionScope
                or descriptor.scope != expected_scope or descriptor.sha256 != verified_descriptor_sha256):
            raise StandardPythonIntentError(mismatch)
        environment = target_environment(dict(descriptor.environment))
        if descriptor.needs != original_dependency_needs(intent, environment):
            raise StandardPythonIntentError(mismatch)
        runtime = _active(intent.runtime_requirements, environment)
        build = _active(intent.build_requirements, environment)
        wheels = tuple(item.metadata.wheel for item in descriptor.wheels)
        if not 0 < len(wheels) <= 128:
            raise StandardPythonIntentError(incomplete)
        by_name = {wheel.name: wheel for wheel in wheels}
        roots: set[str] = set()
        for item in (*build, *runtime):
            name = canonicalize_name(item.name)
            roots.add(name)
            wheel = by_name.get(name)
            if wheel is None or not item.specifier.contains(Version(wheel.version)):
                raise StandardPythonIntentError(incomplete)
        edges: dict[str, set[str]] = {}
        for parent, child in descriptor.edges:
            edges.setdefault(parent, set()).add(child)
        reachable: set[str] = set()
        pending = deque(roots)
        while pending:
            current = pending.popleft()
            if current not in reachable:
                reachable.add(current)
                pending.extend(edges.get(current, ()))
        if reachable != set(by_name):
            raise StandardPythonIntentError(incomplete)
        return _recipe_from_verified_selection(
            intent, tuple(item.metadata for item in descriptor.wheels), environment=environment,
            source_sha256=expected_scope.source_sha256, resolution_sha256=descriptor.sha256,
        )
    except StandardPythonIntentError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise StandardPythonIntentError(incomplete) from None
