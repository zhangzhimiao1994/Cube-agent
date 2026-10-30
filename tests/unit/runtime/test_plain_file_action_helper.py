"""Shared plain-note input and local-only capacity boundary; no native grant."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease


def plain_note_submission():
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = f"plain-note-{timestamp}.txt"
    content = "A plain note saved at the user's explicit request.\n"
    body = CreateRunRequest(
        message=(
            f"Save a plain note in this workspace to the UTC-timestamped file {path}. "
            f"Write exactly these UTF-8 bytes: {json.dumps(content)}. "
            "After saving, tell me the filename in a readable reply."
        ),
        mode=TaskMode.DIRECT,
        conversation_id="task7c-" + uuid4().hex,
        project_id="plain-notes",
        workspace_session_id="note-session",
        sandbox_profile="workspace_write",
        permission_mode="auto_review",
        requested_permissions=("workspace.read", "command.run"),
        execution_backend="systemd",
        runtime_timeout_seconds=180,
    )
    return body, path, content


class LocalCapacityBoundary:
    """Explicit no-Redis diagnostic seam; never a native CapacityPool proof."""

    def __init__(self):
        self.acquired = []
        self.released = []

    async def initialize(self):
        pass

    def validate_configuration(self, deployments):
        self.deployments = tuple(deployments)

    async def acquire(self, candidates, wait_timeout, *, estimated_tokens):
        selected = candidates[0]
        lease = CapacityLease(
            str(uuid4()),
            selected.id,
            selected.quota_scope_id,
            datetime.now(UTC) + timedelta(seconds=120),
            30,
        )
        self.acquired.append(lease)
        return lease

    async def release(self, lease):
        self.released.append(lease)
        return True

    async def renew(self, lease):
        return replace(lease, expires_at=datetime.now(UTC) + timedelta(seconds=120))

    async def record_outcome(self, quota_scope_id, *, status_code, latency_seconds, succeeded):
        pass
