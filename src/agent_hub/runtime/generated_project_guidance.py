"""Shared new-project instructions; never execution or approval authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_hub.runtime.contracts import TaskContext


def generated_project_guidance(context: TaskContext) -> str:
    route = context.routing_decision
    if (type(route.get('generated_project_contract_version')) is not int
            or route['generated_project_contract_version'] != 1 or route.get('project_delivery') != 'workspace'):
        return ''
    return (
        'Use only tools assigned to your current role; planning-only roles pass this contract to the writer. '
        'For this new project, define executable acceptance covering the actual user requirements, '
        'including error cases, before claiming completion. Inspect the workspace with project.inspect. '
        'Use workspace.write_text to implement real source, tests and ACCEPTANCE.json with exactly '
        'schema_version=1, stack="node" or "python", editable_paths (implementation only), test_files, '
        'criteria=[{"id":"AC-001","description":"requirement being checked","tests":["exact test ID"]}]. '
        'Each real test must be assigned once. Node tests use node:test and exact leaf titles, not file names; '
        'Python tests use unittest IDs such as test_app.FeatureTests.test_feature, without a tests. prefix. '
        'Use the standard built-in test APIs; skipped/todo/zero tests and echoed success do not pass. '
        'First project.verify freezes acceptance, tests and dependency controls. After freezing repair '
        'only implementation paths; do not weaken frozen criteria or rewrite tests to hide failures. '
        'Use the returned diagnostics, repair the implementation, and rerun project.verify after the final '
        'source change, then workspace.bundle. Planning text or code snippets are not a completed project. '
        'For Python, project.inspect returns python_build_profile with actual interpreter/catalog pins; '
        'never invent versions or SHA hashes, and configured_not_probed is not an installation receipt. '
        'The supported recipe is python_offline_wheel_v1: pinned [project] dependencies and requires-python, '
        '[build-system] requires pinned setuptools and wheel with build-backend="setuptools.build_meta"; '
        '[tool.setuptools] package-dir={"":"src"}; [tool.setuptools.packages.find] where=["src"]; '
        '[tool.agent-hub.python-project] recipe="python_offline_wheel_v1", import-module=<actual package>, '
        'test-directory="tests". Write requirements.lock entries as name==version --hash=sha256:<actual hash> '
        'for all selected direct and transitive build/runtime wheels. Do not assume every catalog entry '
        'is required. Missing trusted inputs require their own approval before installation; dependency '
        'supply and recipe installation have separate approvals. Unsupported inputs are a precise blocker, '
        'not permission to run host commands or manufacture successful evidence.'
    )
