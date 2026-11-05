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
    guidance = (
        'Use only tools assigned to your current role; planning-only roles pass this contract to the writer. '
        'For this new project, define executable acceptance covering the actual user requirements, '
        'including error cases, before claiming completion. Inspect the workspace with project.inspect. '
        'Use workspace.write_text to implement real source, tests and ACCEPTANCE.json with exactly '
        'schema_version=1, stack="node" or "python", editable_paths (implementation only), test_files, '
        'criteria=[{"id":"AC-001","description":"requirement being checked","tests":["exact test ID"]}]. '
        'Each real test must be assigned once. Node tests use node:test and exact leaf titles, not file names; '
        'Python tests use unittest IDs such as test_app.FeatureTests.test_feature, without a tests. prefix. '
        'Use the standard built-in test APIs; skipped/todo/zero tests and echoed success do not pass. '
        'Successful preparation in project.verify freezes acceptance, tests and dependency controls. '
        'A typed before_capture draft rejection means no capture or execution occurred: correct the '
        'reported draft issues and call project.verify again, without claiming tests passed. '
        'Never include pyproject.toml, requirements.lock, package.json or tests in editable_paths. '
        'After freezing repair '
        'only implementation paths; do not weaken frozen criteria or rewrite tests to hide failures. '
        'Use the returned diagnostics, repair the implementation, and rerun project.verify after the final '
        'source change, then workspace.bundle. Planning text or code snippets are not a completed project. '
        'For Python, project.inspect returns python_build_profile with actual interpreter/catalog pins; '
        'never invent versions or SHA hashes, and configured_not_probed is not an installation receipt. '
        'The supported recipe is python_offline_wheel_v1: pinned [project] dependencies and requires-python, '
        'with Python 3.12 only: set requires-python to ">=3.12,<3.13" or "==3.12.*". '
        '[build-system] requires pinned setuptools and wheel with build-backend="setuptools.build_meta"; '
        'Every fixed project.dependencies and build-system.requires pin must use the same name/version '
        'as its requirements.lock entry. '
        '[tool.setuptools] package-dir={"":"src"}; [tool.setuptools.packages.find] where=["src"]; '
        '[tool.agent-hub.python-project] recipe="python_offline_wheel_v1", import-module=<actual package>, '
        'test-directory="tests". Write requirements.lock entries as name==version --hash=sha256:<actual hash> '
        'for all selected direct and transitive build/runtime wheels. '
        'Use same-scope verified dependency pin feedback when present, or project.inspect '
        'python_build_profile; copy the actual sha256 for the matching name/version, never invent hashes. '
        'Do not assume every catalog entry '
        'is required. Missing trusted inputs require their own approval before installation; dependency '
        'supply and recipe installation have separate approvals. Unsupported inputs are a precise blocker, '
        'not permission to run host commands or manufacture successful evidence.'
    )
    if (type(route.get('python_dependency_preflight_version')) is int
            and route['python_dependency_preflight_version'] == 1):
        guidance += (
            ' Before capture, project.verify checks dependency_closure against trusted wheel metadata. '
            'For required registry packages outside the base catalog, project.verify requests a specific '
            'resolution action, then project.inspect exposes trusted dependency data through a real artifact_ref. '
            'Use its structured machine data (identical to the registered artifact) and write actual exact pins and hashes to pyproject.toml and requirements.lock '
            'before capture; never invent hashes, preinstall global packages or replace a dependency with a fake module. '
            'Legal control edits require a fresh source freeze while reusing the original verified bytes/needs data. '
            'Publication, input supply and offline recipe installation retain their own concrete approvals. '
            'An UNKNOWN operation is inspect-only; do not retry a download or extend the original budget/deadline. '
            'Keep Main replies readable: explain the next concrete action and cite the real artifact_ref, '
            'not raw descriptor JSON, private Root plans or worker credentials. '
            'A dependency observation is not installation, build or test evidence, an approval, '
            'or an extension of the original task deadline.'
        )
    return guidance
