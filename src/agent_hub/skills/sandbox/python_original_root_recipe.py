"""Root-only lookup of a private recipe for a sealed standard Python original."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from agent_hub.capabilities.python_original_catalog import resolve_original_from_catalog
from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntent,
    StandardPythonIntentError,
    standard_python_intent,
)
from agent_hub.capabilities.python_original_private_recipe import (
    PrivateOriginalPythonRecipe,
    derive_private_original_recipe,
)
from agent_hub.skills.sandbox.python_dependency_index_store import (
    PythonDependencyIndexError,
    read_dependency_metadata_index,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    read_supply_configuration,
)
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionBudget,
    ResolutionError,
    ResolutionLimits,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.project_baselines import StoredProjectBaseline
    from agent_hub.skills.sandbox.project import ProjectInvocation
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


def standard_original_intent(
    original: StoredProjectBaseline, manifest: bytes,
) -> StandardPythonIntent | None:
    if original.generated_metadata is not None:
        return None
    intent = standard_python_intent(
        manifest, paths=original.source_files.keys(), protected=original.contract.protected_files.keys(),
    )
    if intent is not None and original.contract.protected_files.get("pyproject.toml") not in {
        hashlib.sha256(tag + manifest).hexdigest() for tag in (b"f\0", b"x\0")
    }:
        raise StandardPythonIntentError("python_original_metadata_invalid")
    return intent


def resolve_standard_original_inputs(
    original: StoredProjectBaseline, intent: StandardPythonIntent, config: RootConfig,
    *, deadline: float, supply: RootSupplyConfig | None = None,
    invocation: ProjectInvocation | None = None,
) -> PrivateOriginalPythonRecipe:
    """Resolve against the configured index, with no registry, install or write authority."""
    from agent_hub.skills.sandbox.python_dependency_inspection import _target_environment

    configured = read_supply_configuration() if supply is None else supply
    environment = _target_environment(config)
    if invocation is not None:
        from agent_hub.skills.sandbox.python_resolution_overlay import (
            verified_resolution_publication,
        )
        from agent_hub.skills.sandbox.python_resolution_protocol import (
            ProtectedOriginalResolutionPlanning,
        )

        try:
            published = verified_resolution_publication(
                configured, invocation, source_sha256=original.reference.input_source_sha256, deadline=deadline,
            )
        except ResolutionError as error:
            if str(error) == "resolution_overlay_pending":
                # Worker may resume the same transaction by inspection. It may
                # not admit a fallback from the base catalog while IO is pending.
                raise StandardPythonIntentError("python_original_catalog_incomplete") from None
            raise
        if published is not None:
            planning = published.claim.planning
            if (not isinstance(planning, ProtectedOriginalResolutionPlanning)
                    or planning.draft.original_reference.model_dump() != original.reference.to_payload()
                    or planning.draft.protected_sha256 != original.contract.protected_sha256
                    or planning.draft.manifest_sha256 != intent.manifest_sha256
                    or dict(published.descriptor.environment) != environment):
                raise StandardPythonIntentError("python_original_catalog_untrusted")
            return derive_private_original_recipe(
                intent, published.descriptor, expected_scope=published.claim.resolution_scope(),
                verified_descriptor_sha256=published.descriptor_sha256,
            )
    try:
        index = read_dependency_metadata_index(configured, deadline=deadline)
    except PythonDependencyIndexError as error:
        if error.code == "timeout":
            raise TimeoutError from None
        code = ("python_original_catalog_unavailable" if error.code == "metadata_unavailable"
                else "python_original_catalog_untrusted")
        raise StandardPythonIntentError(code) from None
    return resolve_original_from_catalog(
        intent, index, catalog=configured.catalog, catalog_sha256=configured.digest,
        source_sha256=original.reference.input_source_sha256, environment=environment,
        budget=ResolutionBudget(ResolutionLimits(), deadline),
    )
