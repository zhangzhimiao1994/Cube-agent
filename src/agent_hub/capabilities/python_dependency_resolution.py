"""Deterministic bounded resolution from actual metadata; never installation authority."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.skills.sandbox.python_dependency_metadata import extract_python_wheel_metadata
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_registry_protocol import (
    DependencyNeed,
    ResolutionBudget,
    ResolutionDescriptor,
    ResolutionError,
    ResolutionScope,
    ResolvedWheel,
    need,
    requirement,
    target_environment,
)
from agent_hub.skills.sandbox.python_registry_read import RegistryReader
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


async def resolve_dependencies(
    needs: tuple[DependencyNeed, ...],
    *,
    scope: ResolutionScope,
    environment: Mapping[str, str],
    reader: RegistryReader,
    wheel_reader: Callable[[WheelDownload], Awaitable[bytes]],
    budget: ResolutionBudget,
) -> ResolutionDescriptor:
    """Greedy selection with explicit conflicts, not a general backtracking pip solver.

    wheel_reader is supplied by the future authorized download stage. This module
    itself never downloads wheel files, imports a distribution, writes or installs.
    """
    need(type(scope) is ResolutionScope)
    scope.__post_init__()
    need(
        type(needs) is tuple
        and 0 < len(needs) <= budget.limits.max_packages
        and all(type(item) is DependencyNeed for item in needs)
    )
    for item in needs:
        item.__post_init__()
    env = target_environment(environment)
    selected: dict[str, ResolvedWheel] = {}
    pending = deque((requirement(item.requirement), "", item.evidence_sha256) for item in needs)
    examined: set[tuple[str, str]] = set()
    edges: set[tuple[str, str]] = set()
    try:
        async with asyncio.timeout_at(budget.deadline):
            while pending:
                budget.check()
                query, parent, evidence = pending.popleft()
                name = str(canonicalize_name(query.name))
                if parent:
                    budget.consume("edges")
                    edges.add((parent, name))
                if name not in selected:
                    budget.consume("packages")
                    candidates = await reader.candidates(
                        DependencyNeed(str(query), evidence),
                        environment=env,
                        budget=budget,
                    )
                    need(len(candidates) == 1, "resolution_candidate_invalid")
                    candidate = candidates[0]
                    budget.consume("wheel_bytes", candidate.download.size_bytes)
                    raw = await wheel_reader(candidate.download)
                    budget.check()
                    metadata = extract_python_wheel_metadata(
                        candidate.download, raw, deadline=budget.deadline,
                    )
                    need(
                        SpecifierSet(metadata.requires_python or "").contains(
                            env["python_full_version"],
                            prereleases=True,
                        ),
                        "resolution_requires_python",
                    )
                    selected[name] = ResolvedWheel(candidate, metadata)
                wheel = selected[name]
                need(
                    query.specifier.contains(Version(wheel.candidate.download.version)),
                    "resolution_version_conflict",
                )
                for extra in sorted({"", *query.extras}):
                    context = (name, extra)
                    if context in examined:
                        continue
                    examined.add(context)
                    for text in wheel.metadata.requires_dist:
                        budget.consume("edges")
                        dependency = requirement(text)
                        if dependency.marker is None or dependency.marker.evaluate(
                            environment={**env, "extra": extra},
                            context="requirement",
                        ):
                            # Derived requirement carries the verified parent wheel hash.
                            dependency.marker = None
                            pending.append((dependency, name, wheel.candidate.download.sha256))
            budget.check()
            return ResolutionDescriptor(
                scope,
                needs,
                tuple(sorted(env.items())),
                tuple(selected[name] for name in sorted(selected)),
                tuple(sorted(edges)),
            )
    except ProcessCleanupIncomplete:
        # Cleanup uncertainty must survive even when the original deadline expired.
        raise
    except ResolutionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        budget.check()
        raise ResolutionError("resolution_input_invalid") from None
