"""Worker-only Python evidence for a locked checkpoint validation transaction."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import TaskContext, _freeze_object
from agent_hub.runtime.python_project_history import HISTORY_KEY, python_history_selected

if TYPE_CHECKING:
    from agent_hub.db.models import RunRow


def python_diagnostic_progress_context(
    authority: TaskContext, worker: TaskContext | None, row: RunRow,
) -> TaskContext | None:
    """Recover evidence, never route authority, from the current prepared worker.

    The caller holds the run row lock. Only the derived history and its two
    preparation artifacts enter this view; ledger artifacts remain DB-owned.
    """
    if not python_history_selected(authority):
        return None
    failure = 'diagnostic checkpoint context is invalid'

    def need(condition: object) -> None:
        if not condition:
            raise ValueError(failure)

    need(type(worker) is TaskContext)
    assert worker is not None
    worker = worker.validated_internal_clone()
    expiry = row.worker_lease_expires_at
    # SQLite does not preserve timezone metadata for the UTC database column.
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    need(worker.execution_token is not None and row.worker_lease_token is not None
         and worker.execution_token == row.worker_lease_token
         and isinstance(row.worker_id, str) and bool(row.worker_id)
         and expiry is not None and expiry > datetime.now(UTC)
         and row.status in {'running', 'waiting_approval'} and row.blocked_by_run_id is None)
    need(worker.run_id == authority.run_id and worker.tenant_id == authority.tenant_id
         and worker.actor_id == authority.actor_id and worker.actor_id is not None
         and worker.actor_role is authority.actor_role and worker.actor_role is not None
         and isinstance(row.actor_role, str) and worker.actor_role is Role(row.actor_role)
         and worker.mode is authority.mode is TaskMode.DISCUSS
         and worker.request == authority.request)
    for key in ('project_id', 'workspace_session_id', 'project_input', 'project_input_preparation'):
        need(key in authority.routing_decision
             and worker.routing_decision.get(key) == authority.routing_decision[key])
    history = worker.routing_decision.get(HISTORY_KEY)
    need(isinstance(history, Mapping))
    assert isinstance(history, Mapping)
    original_id = history.get('artifact_id')
    originals = tuple(a for a in worker.artifacts if str(a.id) == original_id)
    metadata = tuple(a for a in worker.artifacts if a.type == 'original_python_project_context')
    need(len(originals) == len(metadata) == 1)
    original = originals[0]
    need(original.type == 'original_project_observation'
         and original.producer == 'original_project_preparer'
         and original.content_sha256 == original.recompute_content_sha256()
         and original.content_sha256 == history.get('artifact_sha256')
         and original.id != metadata[0].id)
    view = authority.model_copy(update={
        'routing_decision': _freeze_object({**authority.routing_decision, HISTORY_KEY: history},
                                           name='diagnostic Python evidence'),
        'artifacts': (original, metadata[0]),
    }).validated_internal_clone()
    from agent_hub.runs.python_preparation import validate_python_preparation

    try:
        validate_python_preparation(view)
    except RuntimeError:
        raise ValueError(failure) from None
    return view
