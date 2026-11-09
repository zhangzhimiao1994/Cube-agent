"""Resolve a standard original's declared ranges using Root's wheel metadata."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.capabilities.python_dependency_closure import PythonWheelMetadata
from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntent,
    StandardPythonIntentError,
)
from agent_hub.capabilities.python_original_private_recipe import (
    PrivateOriginalPythonRecipe,
    _recipe_from_verified_selection,
    original_dependency_needs,
)
from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionBudget,
    ResolutionError,
    digest,
    require_sha,
    requirement,
    target_environment,
)


def resolve_original_from_catalog(
    intent: StandardPythonIntent, index: PythonDependencyMetadataIndex, *,
    catalog: tuple[WheelDownload, ...], catalog_sha256: str, source_sha256: str,
    environment: Mapping[str, str], budget: ResolutionBudget,
) -> PrivateOriginalPythonRecipe:
    """Select only catalog wheels. Cache reads, fetches and installs are separate effects."""
    try:
        require_sha(source_sha256)
        index.require_catalog(catalog, catalog_sha256)
        budget.check()
    except ResolutionError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise StandardPythonIntentError("python_original_catalog_untrusted") from None
    target = target_environment(environment)
    needs = original_dependency_needs(intent, target)
    choices: dict[str, list[PythonWheelMetadata]] = defaultdict(list)
    try:
        for item in index.records:
            budget.check()
            supported = SpecifierSet(item.requires_python or "")
            if any(part.operator == "===" for part in supported):
                raise ValueError
            if supported.contains(target["python_full_version"], prereleases=True):
                choices[item.wheel.name].append(item)
        for variants in choices.values():
            variants.sort(key=lambda item: Version(item.wheel.version), reverse=True)
    except ResolutionError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise StandardPythonIntentError("python_original_catalog_untrusted") from None

    def search(
        pending: deque[Requirement], selected: dict[str, PythonWheelMetadata],
        visited: set[tuple[str, str]],
    ) -> dict[str, PythonWheelMetadata] | None:
        while pending:
            budget.consume("edges")
            query = pending.popleft()
            name = canonicalize_name(query.name)
            current = selected.get(name)
            if current is None:
                if len(selected) >= budget.limits.max_packages:
                    raise ResolutionError("resolution_packages_limit")
                for choice in choices.get(name, ()):
                    budget.consume("candidates")
                    if not query.specifier.contains(Version(choice.wheel.version)):
                        continue
                    branch = search(deque((query, *pending)), selected | {name: choice}, visited.copy())
                    if branch is not None:
                        return branch
                return None
            if not query.specifier.contains(Version(current.wheel.version)):
                return None
            for extra in sorted({"", *query.extras}):
                key = (name, extra)
                if key in visited:
                    continue
                visited.add(key)
                for text in current.requires_dist:
                    budget.consume("edges")
                    dependency = requirement(text)
                    if dependency.marker is None or dependency.marker.evaluate(
                        environment={**target, "extra": extra}, context="requirement",
                    ):
                        pending.append(dependency)
        return selected

    try:
        selected = search(deque(requirement(need.requirement) for need in needs), {}, set())
        if not selected:
            raise StandardPythonIntentError("python_original_catalog_incomplete")
        records = tuple(selected[name] for name in sorted(selected))
        evidence = digest({
            "index_sha256": index.sha256, "source_sha256": source_sha256,
            "manifest_sha256": intent.manifest_sha256, "environment": target,
            "selected": [record.to_payload() for record in records],
        })
        budget.check()
        return _recipe_from_verified_selection(
            intent, records, environment=target, source_sha256=source_sha256, resolution_sha256=evidence,
        )
    except (ResolutionError, StandardPythonIntentError):
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise StandardPythonIntentError("python_original_catalog_untrusted") from None
