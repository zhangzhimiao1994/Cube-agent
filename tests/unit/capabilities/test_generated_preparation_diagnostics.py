"""Unexpected preparation errors retain a finite stage, never private details."""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic_core import PydanticSerializationError

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import GeneratedProjectError
from agent_hub.runtime.artifacts import ArtifactRepositoryError
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.dynamic_resolution_typed_fixture import present
from tests.unit.capabilities.test_dynamic_python_resolution_flow import generated_dynamic_case


class PrivateFailure(Exception):
    pass


@pytest.mark.parametrize('kind,expected', [
    (RuntimeError, 'RuntimeError'), (ValueError, 'ValueError'),
    (OSError, 'OSError'), (PrivateFailure, 'Exception'),
    (KeyError, 'KeyError'), (AssertionError, 'AssertionError'),
    (ArtifactRepositoryError, 'ArtifactRepositoryError'),
    (PydanticSerializationError, 'PydanticSerializationError'),
    (ConnectionError, 'ConnectionError'), (BrokenPipeError, 'BrokenPipeError'),
    (EOFError, 'EOFError'), (IndexError, 'IndexError'),
    (ConnectionResetError, 'ConnectionResetError'),
    (ConnectionAbortedError, 'ConnectionAbortedError'),
    (ConnectionRefusedError, 'ConnectionRefusedError'),
    (ProcessCleanupIncomplete, 'ProcessCleanupIncomplete'),
    (lambda private: asyncio.IncompleteReadError(private.encode(), len(private) + 1),
     'IncompleteReadError'),
])
async def test_unexpected_resolution_error_logs_stage_without_private_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    kind: Callable[[str], Exception], expected: str,
) -> None:
    service, repo, context, _ = generated_dynamic_case(tmp_path, monkeypatch)
    private = 'private-credential-token /private/workspace/generated-source'

    async def fail(*args: object, **kwargs: object) -> bool:
        raise kind(private)

    monkeypatch.setattr(service, '_resolve_dependency_draft', fail)
    with (
        caplog.at_level(logging.WARNING, logger='agent_hub.capabilities.generated_project_service'),
        bind_execution_authority(context, worker_id=present(repo.row.worker_id), actor='main_agent'),
        pytest.raises(GeneratedProjectError, match='^generated_acceptance_unavailable_or_changed$'),
    ):
        await service.prepare(context.tenant_id, context.run_id, 'main_agent',
                              user_id=present(context.actor_id))
    records = [record for record in caplog.records
               if record.name == 'agent_hub.capabilities.generated_project_service']
    assert len(records) == 1
    prefix = (
        'generated_project_prepare_failed stage=dependency_resolution_read '
        f'error_kind={expected} run_id={context.run_id}'
    )
    assert records[0].getMessage().startswith(prefix + ' error_site=')
    assert re.fullmatch(r'capabilities/generated_project_service[.]py:[1-9][0-9]*',
                        records[0].getMessage().removeprefix(prefix + ' error_site='))
    assert records[0].exc_info is None and records[0].stack_info is None
    assert private not in caplog.text and 'PrivateFailure' not in caplog.text
    assert repo.updates == 0
    assert 'generated_project_preparation' not in present(repo.row.routing_decision)
