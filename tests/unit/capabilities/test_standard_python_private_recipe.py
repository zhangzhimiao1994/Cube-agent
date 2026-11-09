"""Require a real resolver contract before an ordinary project's lock can be planned."""

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.capabilities.python_dependency_closure import PythonWheelMetadata
from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntent,
    StandardPythonIntentError,
)
from agent_hub.capabilities.python_original_private_recipe import (
    derive_private_original_recipe,
    original_dependency_needs,
)
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_registry_protocol import (
    RegistryCandidate,
    RegistryProvenance,
    ResolutionDescriptor,
    ResolutionScope,
    ResolvedWheel,
)

ENVIRONMENT = {
    "implementation_name": "cpython", "implementation_version": "3.12.8", "os_name": "posix",
    "platform_machine": "x86_64", "platform_release": "6.8", "platform_system": "Linux",
    "platform_version": "6.8", "platform_python_implementation": "CPython",
    "python_full_version": "3.12.8", "python_version": "3.12", "sys_platform": "linux",
}
SCOPE = ResolutionScope(UUID(int=1), UUID(int=2), UUID(int=3), "main_agent", "project",
                        "session", "1" * 64, "2" * 64, "3" * 64)
INTENT = StandardPythonIntent("sample", "0.1.0", "sample", ">=3.11", "4" * 64,
                              ("setuptools>=68", "wheel"),
                              ('click>=8.1,<9', 'colorama; sys_platform == "win32"'), ())


def _wheel(name: str, version: str, *, dependencies: tuple[str, ...] = ()) -> ResolvedWheel:
    filename = name + "-" + version + "-py3-none-any.whl"
    sha = hashlib.sha256(filename.encode()).hexdigest()
    download = WheelDownload(filename, name, version, sha, 1024, "https://files.pythonhosted.org/packages/test/" + filename)
    return ResolvedWheel(
        RegistryCandidate(download, RegistryProvenance(f"https://pypi.org/pypi/{name}/{version}/json", "5" * 64),
                          RegistryProvenance(f"https://pypi.org/simple/{name}/", "6" * 64)),
        PythonWheelMetadata(PythonWheelBinding(filename, name, version, sha), ">=3.8", dependencies),
    )


def _descriptor() -> ResolutionDescriptor:
    return ResolutionDescriptor(
        SCOPE, original_dependency_needs(INTENT, ENVIRONMENT), tuple(sorted(ENVIRONMENT.items())),
        (_wheel("click", "8.1.8", dependencies=('colorama; sys_platform == "win32"',)),
         _wheel("setuptools", "75.0.0"), _wheel("wheel", "0.44.0")), (),
    )


def test_private_lock_retains_actual_original_manifest_and_bounds_environment() -> None:
    resolution = _descriptor()
    recipe = derive_private_original_recipe(
        INTENT, resolution, expected_scope=SCOPE, verified_descriptor_sha256=resolution.sha256,
    )
    assert recipe.declaration.manifest_sha256 == INTENT.manifest_sha256
    assert recipe.declaration.requires_python == "==3.12.*"
    assert recipe.declaration.lock_sha256 == hashlib.sha256(recipe.lock).hexdigest()
    assert recipe.declaration.runtime_dependencies == ("click",)
    assert recipe.resolution_sha256 == resolution.sha256 and recipe.source_sha256 == SCOPE.source_sha256
    assert len(recipe.lock.splitlines()) == 3
    assert b"click==8.1.8 --hash=sha256:" in recipe.lock
    assert b"colorama" not in recipe.lock


def test_old_or_unverified_resolution_cannot_become_a_private_recipe() -> None:
    resolution = _descriptor()
    with pytest.raises(StandardPythonIntentError, match="python_original_resolution_scope_mismatch"):
        derive_private_original_recipe(
            INTENT, resolution, expected_scope=replace(SCOPE, source_sha256="9" * 64),
            verified_descriptor_sha256=resolution.sha256,
        )
    with pytest.raises(StandardPythonIntentError, match="python_original_resolution_scope_mismatch"):
        derive_private_original_recipe(
            INTENT, resolution, expected_scope=SCOPE, verified_descriptor_sha256="9" * 64,
        )


def test_resolver_output_must_include_reachable_compatible_dependencies() -> None:
    resolution = _descriptor()
    missing = replace(resolution, wheels=(_wheel("click", "8.1.8", dependencies=("colorama",)), *resolution.wheels[1:]))
    outside = replace(resolution, wheels=(resolution.wheels[0], _wheel("colorama", "0.4.6"), *resolution.wheels[1:]))
    bad_version = replace(resolution, wheels=(_wheel("click", "9.0"), *resolution.wheels[1:]))
    for invalid in (missing, outside, bad_version):
        with pytest.raises(StandardPythonIntentError, match="python_original_resolution_incomplete"):
            derive_private_original_recipe(
                INTENT, invalid, expected_scope=SCOPE, verified_descriptor_sha256=invalid.sha256,
            )
