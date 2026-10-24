"""Real private PG hydration; fixed model/tools, not actual engineering delivery."""

from agent_hub.db.session import build_database
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import default_runtime_registry
from tests.integration.runs.test_action_service_native_recovery import RecordingQueue
from tests.unit.runtime.autogen.test_draft_action_progress import action_context
from tests.unit.runtime.autogen.test_writer_continuation_runtime import exercise_writer_continuation


async def test_multifile_continuation_restores_through_fresh_postgres_pool(database_url: str) -> None:
    original, fresh = build_database(database_url), build_database(database_url)
    try:
        context = action_context()
        assert context.actor_id is not None
        service = RunService(RunRepository(original.session_factory),
            runtime_registry=default_runtime_registry(), router=None, task_queue=RecordingQueue())
        submitted = await service.submit(tenant_id=context.tenant_id, actor_id=context.actor_id,
            actor_role=context.actor_role, mode=TaskMode.DISCUSS, message=context.request,
            allow_scale_mode_upgrade=False, permission_mode="read_only", sandbox_profile="read_only",
            requested_permissions=("workspace.read",))
        await exercise_writer_continuation(
            artifact_repository=PostgresArtifactRepository(original.session_factory),
            restore_repository=PostgresArtifactRepository(fresh.session_factory),
            context=context.model_copy(update={"run_id": submitted.id}))
    finally:
        await original.dispose()
        await fresh.dispose()
