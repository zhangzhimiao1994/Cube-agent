"""The normal persistent-cache broker result still needs original-only authorization."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.runs.test_original_python_offline_review import setup


class LocalControls:
    """Test only. Use real exclusive file writes with the platform ACL boundary stubbed."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def open(self, name: str, flags: int) -> int:
        return os.open(self.path / name, flags | getattr(os, 'O_BINARY', 0), 0o600)

    def read(self, name: str, maximum: int) -> bytes:
        with (self.path / name).open('rb') as stream:
            raw = stream.read(maximum + 1)
        assert len(raw) <= maximum
        return raw

    def check(self) -> None:
        assert self.path.is_dir()

    def sync(self) -> None:
        pass


@pytest.mark.parametrize('wrong_digest', [False, True], ids=['persistent', 'wrong-environment-digest'])
async def test_original_offline_review_validates_and_records_persistent_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_digest: bool,
) -> None:
    persistent_run = PythonExecutor.run
    preparer, verifier, repository, executor, context, _ = await setup(tmp_path, monkeypatch, standard=True)
    directory = tmp_path / 'controls'
    directory.mkdir()
    controls = LocalControls(directory)
    monkeypatch.setattr(ProjectBaselineStore, '_controls', lambda *_args, **_kwargs: controls)
    real_read = type(verifier._python)._read_references.__get__(verifier._python)
    monkeypatch.setattr(verifier._python, '_read_references', real_read)

    issued: list[PythonEnvironmentReference] = []

    async def run(invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
        assert invocation.source_kind == 'original'
        assert repository.receipts[-1].status == 'consumed'
        response = await persistent_run(executor, invocation)
        reference = response.environment_reference
        assert reference is not None and response.ephemeral_environment is None
        issued.append(reference)
        if wrong_digest:
            return response.model_copy(update={
                'environment_reference': reference.model_copy(update={'environment_sha256': '0' * 64}),
            })
        return response

    monkeypatch.setattr(executor, 'run', run)
    with bind_execution_authority(context, worker_id='local-worker'):
        if wrong_digest:
            with pytest.raises(ProjectVerificationError):
                await preparer(context)
        else:
            await preparer(context)

    state = repository.row.routing_decision['project_input_preparation']
    assert len(issued) == len(executor.calls) == 1
    assert repository.receipts[-1].status == 'consumed'
    if wrong_digest:
        assert not list(directory.iterdir())
        assert state['phase'] == 'executing'
        return
    reference = issued[0]
    assert state['phase'] == 'observed'
    assert state['observation']['environment_reference'] == reference.model_dump(mode='json')
    assert [path.name for path in directory.iterdir()] == [
        'python-environment-' + reference.origin_execution_id + '.json',
    ]
    history = await verifier.python_original_context(context.tenant_id, context.run_id)
    assert history is not None and history.environment_reference == reference
    assert history.environment_references == (reference,)
    with pytest.raises(ProjectVerificationError, match='install_approval_required'):
        await verifier._python.select(context.tenant_id, context.run_id, 'main_agent', approve=True)
    assert len(executor.calls) == 1
