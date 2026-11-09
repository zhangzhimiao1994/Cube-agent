"""A trusted catalog can solve ranges without mutating the original project."""

import time
from dataclasses import replace

import pytest

from agent_hub.capabilities.python_original_catalog import resolve_original_from_catalog
from agent_hub.capabilities.python_original_metadata import StandardPythonIntentError
from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionBudget,
    ResolutionLimits,
    ResolvedWheel,
)

from .test_standard_python_private_recipe import ENVIRONMENT, INTENT, SCOPE, _wheel


def _index(*wheels: ResolvedWheel) -> tuple[tuple[WheelDownload, ...], PythonDependencyMetadataIndex]:
    downloads = tuple(wheel.candidate.download for wheel in wheels)
    index = PythonDependencyMetadataIndex(SCOPE.catalog_sha256, tuple(wheel.metadata for wheel in wheels))
    return downloads, index


def test_transitive_conflict_selects_older_compatible_catalog_wheel() -> None:
    catalog, index = _index(
        _wheel("click", "8.1.8", dependencies=("shared<2",)),
        _wheel("click", "8.2.1", dependencies=("shared>=2",)),
        _wheel("setuptools", "75.0.0", dependencies=("shared<2",)),
        _wheel("shared", "1.5"), _wheel("shared", "2.0"), _wheel("wheel", "0.44.0"),
    )
    result = resolve_original_from_catalog(
        INTENT, index, catalog=catalog, catalog_sha256=SCOPE.catalog_sha256,
        source_sha256=SCOPE.source_sha256, environment=ENVIRONMENT,
        budget=ResolutionBudget(ResolutionLimits(), time.monotonic() + 10),
    )
    assert b"click==8.1.8 " in result.lock
    assert b"shared==1.5 " in result.lock
    assert b"click==8.2.1" not in result.lock and b"colorama" not in result.lock
    assert result.declaration.manifest_sha256 == INTENT.manifest_sha256
    assert result.source_sha256 == SCOPE.source_sha256


def test_catalog_mismatch_and_missing_active_dependency_remain_distinct() -> None:
    catalog, index = _index(
        _wheel("click", "8.1.8"), _wheel("setuptools", "75.0.0"), _wheel("wheel", "0.44.0"),
    )
    for intent, digest, reason in (
        (INTENT, "9" * 64, "python_original_catalog_untrusted"),
        (replace(INTENT, runtime_requirements=("click>=9",)), SCOPE.catalog_sha256,
         "python_original_catalog_incomplete"),
    ):
        with pytest.raises(StandardPythonIntentError, match=reason):
            resolve_original_from_catalog(
                intent, index, catalog=catalog, catalog_sha256=digest,
                source_sha256=SCOPE.source_sha256, environment=ENVIRONMENT,
                budget=ResolutionBudget(ResolutionLimits(), time.monotonic() + 10),
            )
