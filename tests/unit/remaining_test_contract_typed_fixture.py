"""Exclusive fixtures for the final test-contract typing slice."""

from dataclasses import dataclass
from types import ModuleType
from uuid import UUID

from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.db.session import Database
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainInvocation


@dataclass
class HistoryCase:
    db: Database
    tenant: UUID
    actor: UUID
    main: UUID
    other_actor: UUID
    other_tenant: UUID
    legacy: UUID
    ids: list[UUID]
    foreign_ids: list[UUID]
    statements: list[str]


def caches(owner: ProjectVerificationService) -> list[object]:
    # The six original cache identities from test_python_verification_recovery.
    return [owner._receipts, owner._original_receipts, owner._generated_receipts,
            owner._python.receipts, owner._python.environments, owner._python_generated.receipts]


def invocation(api: ModuleType) -> ToolchainInvocation:
    # Preserve test_toolchain_protocol's envelope and the concrete factory identity.
    assert api.ToolchainInvocation is ToolchainInvocation
    scope = ToolchainPlanScope(
        tenant_id=UUID(int=1),
        user_id=UUID(int=2),
        run_id=UUID(int=3),
        project_id="demo",
        session_id="session",
        actor_id="main_agent",
        actor_role="operator",
        source_sha256="b" * 64,
        contract_sha256="c" * 64,
    )
    return ToolchainInvocation(
        execution_id="sdk_test",
        tenant_id=scope.tenant_id,
        run_id=scope.run_id,
        project_id=scope.project_id,
        session_id=scope.session_id,
        sandbox_profile="workspace_write",
        install_allowed=False,
        timeout_seconds=600,
        scope=scope,
        original_reference=OriginalProjectReference.model_validate({
            "schema_version": 1,
            "input_source_sha256": "b" * 64,
            "contract_sha256": "c" * 64,
            "file_count": 3,
        }),
    )
