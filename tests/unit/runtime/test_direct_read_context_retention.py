"""Bounded read observations survive rounds without becoming progress or write authority."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.types import ModelRequest
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


def read_record(path: str, text: str) -> dict[str, JsonValue]:
    return {
        'path': path, 'text': text, 'truncated': False, 'reader_truncated': False,
        'gateway_truncated': False, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
    }


def cache(max_bytes: int = 4096) -> Any:
    return direct.WorkspaceReadContext(max_bytes=max_bytes)


def payload(value: Any) -> dict[str, Any]:
    raw = value.payload()
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert isinstance(decoded, dict) and isinstance(decoded.get('items'), list)
    assert type(decoded.get('entries_complete')) is bool
    assert type(decoded.get('omitted_count')) is int
    return cast(dict[str, Any], decoded)


def prompt(request: ModelRequest) -> str:
    return '\n'.join(str(message.content) for message in request.messages)


def read_payloads(request: ModelRequest) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in re.findall(
        r'<UNTRUSTED_WORKSPACE_READ_JSON>(.*?)</UNTRUSTED_WORKSPACE_READ_JSON>',
        prompt(request), re.DOTALL,
    ):
        decoded = json.loads(raw)
        if isinstance(decoded, list):
            # The predecessor emits a per-round list; this keeps RED on retention,
            # rather than on the new cache envelope's incidental schema.
            result.append({'items': decoded, 'entries_complete': True, 'omitted_count': 0})
        else:
            assert isinstance(decoded, dict) and isinstance(decoded.get('items'), list)
            result.append(decoded)
    return result


def read_items(request: ModelRequest) -> dict[str, dict[str, Any]]:
    return {item['path']: item for body in read_payloads(request) for item in body['items']}


def read_batch(*paths: str) -> str:
    return json.dumps({'workspace_batch': {'files': {}, 'read_paths': list(paths),
        'complete': False, 'continuation': 'Use the inspected files to repair this same project.'}})


class ReadHarness(Harness):
    def __init__(
        self, context: TaskContext, verified: Mapping[str, JsonValue],
        bundle: Mapping[str, JsonValue], files: dict[str, str],
    ) -> None:
        super().__init__(context, verified, bundle)
        self.files = dict(files)

    async def invoke(self, tenant: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
        if request.tool_name != 'read_context':
            return await super().invoke(tenant, request, **kwargs)
        assert tenant == self.context.tenant_id and request.run_id == self.context.run_id
        assert request.actor == 'main_agent' and kwargs['user_id'] == self.context.actor_id
        assert kwargs['role'] == self.context.actor_role
        path = request.arguments['path']
        assert isinstance(path, str) and path in self.files
        self.calls.append('read_context')
        return HarnessToolCallResult(call_id=request.call_id, tool_name='read_context',
            status='succeeded', payload={'path': path, 'text': self.files[path], 'truncated': False})

    async def execute(self, *, name: str, **kwargs: Any) -> Mapping[str, JsonValue]:
        result = await super().execute(name=name, **kwargs)
        if name == 'workspace.write_text':
            args = kwargs['arguments']
            assert isinstance(args['path'], str) and isinstance(args['content'], str)
            self.files[args['path']] = args['content']
        return result


def test_pure_cache_accumulates_confirmed_reads_and_copies_observations() -> None:
    value = cache()
    a = read_record('src/a.py', 'A_CONFIRMED_READ')
    b = read_record('src/b.py', 'B_CONFIRMED_READ')
    assert value.observe([a]) is None
    a['text'] = 'MUTATED_AFTER_OBSERVATION'
    assert value.observe([b]) is None
    body = payload(value)
    assert {item['path']: item['text'] for item in body['items']} == {
        'src/a.py': 'A_CONFIRMED_READ', 'src/b.py': 'B_CONFIRMED_READ',
    }
    assert body['entries_complete'] is True and body['omitted_count'] == 0
    other = cache()
    assert other.payload() is None or not payload(other)['items']


def test_pure_cache_duplicate_observation_is_not_a_new_entry() -> None:
    value = cache()
    record = read_record('src/a.py', 'SAME_CONFIRMED_READ')
    assert value.observe([record]) is None
    first = payload(value)
    assert value.observe([record, record]) is None
    assert payload(value) == first


def test_pure_cache_invalidate_removes_old_source_before_fresh_read() -> None:
    value = cache()
    value.observe([read_record('src/a.py', 'STALE_A'), read_record('src/b.py', 'KEEP_B')])
    assert value.invalidate(['src/a.py']) is None
    body = payload(value)
    assert [item['path'] for item in body['items']] == ['src/b.py']
    assert 'STALE_A' not in cast(str, value.payload())
    value.observe([read_record('src/a.py', 'FRESH_A')])
    assert {item['path']: item['text'] for item in payload(value)['items']} == {
        'src/a.py': 'FRESH_A', 'src/b.py': 'KEEP_B',
    }


def test_pure_cache_budget_counts_serialized_utf8_and_reports_omissions() -> None:
    value = cache(max_bytes=768)
    records = [read_record(f'src/file_{i}.py', f'OBSERVATION_{i}_' + '中文' * 80) for i in range(8)]
    value.observe(records)
    raw = value.payload()
    assert isinstance(raw, str) and len(raw.encode('utf-8')) <= 768
    body = payload(value)
    assert body['entries_complete'] is False and body['omitted_count'] > 0
    supplied = {str(item['path']): item for item in records}
    for item in body['items']:
        # Budgeting may omit a record, never invent or silently shorten confirmed bytes.
        assert item == supplied[item['path']]


async def test_runtime_read_a_then_b_then_repeat_a_retains_both(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    files = {'src/a.py': 'A_READ_BODY_UNIQUE\n', 'src/b.py': 'B_READ_BODY_UNIQUE\n'}
    harness = ReadHarness(context, verified, bundle, files)
    model = Model(response(read_batch('src/a.py')), response(read_batch('src/b.py')),
        response(read_batch('src/a.py')), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 4
    for request in model.requests[2:]:
        records = read_items(request)
        assert records['src/a.py']['text'] == files['src/a.py']
        assert records['src/b.py']['text'] == files['src/b.py']
    assert '"validation_error":"no_progress"' in prompt(model.requests[3])
    assert harness.calls[:4] == ['project.inspect', 'read_context', 'read_context', 'read_context']
    assert all(not request.tools for request in model.requests)


async def test_cached_repeat_does_not_buy_progress_tokens_or_more_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = ReadHarness(context, verified, bundle, {'src/a.py': 'ONE_READ_ONLY\n'})
    model = Model(*(response(read_batch('src/a.py')) for _ in range(3)), response(batch()))
    progress: list[int] = []
    limits: list[dict[str, Any]] = []
    from agent_hub.runtime.adaptive_budget import deadline_from_routing

    original_deadline, original_limit = deadline_from_routing, direct._workspace_delivery_token_limit

    class Deadline:
        def __init__(self, target: Any) -> None:
            self.target = target

        @property
        def deadline(self) -> float:
            return cast(float, self.target.deadline)

        def observe(self, *, progress_units: int, now: float) -> Any:
            progress.append(progress_units)
            return self.target.observe(progress_units=progress_units, now=now)

    def deadline(*args: Any, **kwargs: Any) -> Any:
        return Deadline(original_deadline(*args, **kwargs))

    def limit(*args: Any, **kwargs: Any) -> int:
        limits.append(dict(kwargs))
        return original_limit(*args, **kwargs)

    monkeypatch.setattr(direct, 'deadline_from_routing', deadline)
    monkeypatch.setattr(direct, '_workspace_delivery_token_limit', limit)
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='workspace batch made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and harness.calls == ['project.inspect', *(['read_context'] * 3)]
    assert progress == [1, 1]
    assert all(row['completed_files'] == 0 and row['completed_batches'] <= 1 for row in limits)
    assert model.requests[2].max_output_tokens <= model.requests[1].max_output_tokens
    assert all(0 < request.timeout_seconds <= context.timeout_seconds for request in model.requests)


async def test_successful_write_invalidates_old_cached_read_in_next_prompt(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = ReadHarness(context, verified, bundle, {
        'src/a.py': 'STALE_BEFORE_SUCCESSFUL_WRITE\n', 'src/b.py': 'KEEP_B_AFTER_WRITE\n',
    })
    write = json.dumps({'workspace_batch': {'files': {'src/a.py': 'NEW_WRITTEN_A\n'},
        'complete': False, 'continuation': 'Inspect the remaining dependency before verification.'}})
    model = Model(response(read_batch('src/a.py')), response(write),
        response(read_batch('src/b.py')), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 4
    assert 'STALE_BEFORE_SUCCESSFUL_WRITE' in prompt(model.requests[1])
    for request in model.requests[2:]:
        assert 'STALE_BEFORE_SUCCESSFUL_WRITE' not in prompt(request)
        assert 'src/a.py' not in read_items(request)
    assert read_items(model.requests[3])['src/b.py']['text'] == 'KEEP_B_AFTER_WRITE\n'
    assert harness.files['src/a.py'] == 'NEW_WRITTEN_A\n'


async def test_runtime_read_cache_stays_bounded_inside_original_context_window(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    window = 8192
    context = context.model_copy(update={'token_budget': 100_000, 'timeout_seconds': 60,
        'routing_decision': {**context.routing_decision, 'main_agent_context_window_tokens': window}})
    files = {f'src/read_{i}.py': f'READ_{i}_' + 'bounded-reference-' * 600 for i in range(8)}
    harness = ReadHarness(context, verified, bundle, files)
    model = Model(*(response(read_batch(path)) for path in files), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 9
    last = read_payloads(model.requests[-1])
    assert last and last[-1]['entries_complete'] is False and last[-1]['omitted_count'] > 0
    for request in model.requests[1:]:
        text = json.dumps([{'role': message.role, 'content': message.content} for message in request.messages],
            ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        estimate = direct._estimate_text_tokens(text)
        assert estimate + request.max_output_tokens <= window
        assert context.request in prompt(request)
        assert not request.tools
