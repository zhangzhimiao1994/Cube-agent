"""Shared broker cleanup cannot change objects outside its private stage."""

import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation, _Execution
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


@pytest.mark.parametrize("link", ["symbolic", "hard"])
async def test_shared_finish_preserves_external_link_target(tmp_path: Path, link: str) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"private test sentinel")
    outside.chmod(0o444)
    original = outside.stat()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    alias = stage / "alias"
    try:
        if link == "symbolic":
            alias.symlink_to(outside)
        else:
            os.link(outside, alias)
    except OSError:
        pytest.skip("platform cannot create the selected link fixture")
    invocation = ProjectInvocation(execution_id="cleanup_link", tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="project", session_id="session",
        sandbox_profile="workspace_write", timeout_seconds=3)
    job = _Execution(invocation, None, stage=stage, launched=False)
    broker = ProjectExecutionBroker(BrokerPolicy())
    if link == "hard":
        with pytest.raises(ProcessCleanupIncomplete):
            await broker._finish(job)
        assert stage.exists() and not job.quiet
    else:
        await broker._finish(job)
        assert not stage.exists() and job.quiet
    current = outside.stat()
    assert outside.read_bytes() == b"private test sentinel"
    assert stat.S_IMODE(current.st_mode) == stat.S_IMODE(original.st_mode)
    assert current.st_uid == original.st_uid and current.st_gid == original.st_gid
