from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from agent_hub.runtime.action_direct import _binding, _json
from agent_hub.runtime.engineering_admission import _fingerprint
from tests.unit.runs.reviewed_report_revision_typed_fixture import string_value
from tests.unit.runtime.test_direct_project_verification import Model, context, runner


def test_legacy_action_binding_stays_byte_identical_without_diagnostic_opt_in() -> None:
    ctx = context(routing_decision={})
    expected = hashlib.sha256(_json({
        'scope': _fingerprint(ctx), 'route': {}, 'contracts': {}, 'guidance': {},
        'resolved_model': 'main', 'selected_model': {}, 'instructions': None, 'sources': [],
    })).hexdigest()
    assert _binding(ctx, 'main') == expected


@pytest.mark.parametrize('changed', [None, True, '1', 2, 0])
def test_action_binding_freezes_presence_and_type_of_diagnostic_read_policy(changed: Any) -> None:
    ctx = context(routing_decision={'engineering_diagnostic_reads_version': 1})
    route = {} if changed is None else {'engineering_diagnostic_reads_version': changed}
    assert _binding(ctx, 'main') != _binding(ctx.model_copy(update={'routing_decision': route}), 'main')


@pytest.mark.parametrize('version', [1, None, True, '1', 2])
def test_direct_prompt_advertises_only_selected_diagnostic_read_protocol(
    tmp_path: Path, version: Any,
) -> None:
    ctx = context()
    route = dict(ctx.routing_decision)
    if version is not None:
        route['engineering_diagnostic_reads_version'] = version
    ctx = ctx.model_copy(update={'routing_decision': route})
    runtime, _, _, _ = runner(tmp_path, ctx, Model())
    prompt = runtime._build_prompt(ctx)
    assert prompt.messages is not None
    text = '\n'.join(string_value(message.content) for message in prompt.messages)
    assert ('read_paths' in text) is (type(version) is int and version == 1)
