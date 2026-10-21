"""Generated baselines can never acquire pre-model original execution identity."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox import python_project_backend as api
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest
from tests.unit.skills.sandbox.test_python_project_backend import (
    configuration_payload,
    stubbed_context,
)


@pytest.mark.parametrize('kind', ['original', 'candidate'])
def test_generated_baseline_does_not_become_original(kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recipe, invocation, original, stage, events = stubbed_context(api, monkeypatch, tmp_path)
    original = replace(original, generated_metadata={'schema_version': 1, 'purpose': 'generated_acceptance',
        'capture_phase': 'post_model', 'user_id': str(invocation.tenant_id), 'request_sha256': 'a' * 64,
        'manifest_sha256': 'b' * 64})
    monkeypatch.setattr(api, 'stage_python_project_inputs', lambda **kw: SimpleNamespace(original=original, inputs=recipe))
    request = PythonProjectBrokerRequest(invocation=invocation.model_copy(update={'source_kind': kind}))
    configured = api.RootConfig.from_payload(configuration_payload())
    if kind == 'original':
        with pytest.raises(api.PythonProjectBackendError):
            api._prepared(request, BrokerPolicy(), configured, stage, 10**12)
        assert events == []
    else:
        selected: Any = api._prepared(request, BrokerPolicy(), configured, stage, 10**12)
        assert selected.inputs == recipe and events == ['runtime', 'wheels']
