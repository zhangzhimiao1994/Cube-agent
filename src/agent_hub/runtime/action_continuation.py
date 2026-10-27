"""Read-only Direct v2 continuation decoding and write ownership, never replay/grants."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import quote
from uuid import UUID

from agent_hub.files.workspace import ProjectWorkspaceFile
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.continuation_artifacts import _checkpoint, _encode, load_continuation
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.engineering_admission import _fingerprint

_ERROR = 'action continuation is unavailable'


class _ContextChanged(Exception):
    pass


def _need(value: object) -> None:
    if not value:
        raise RuntimeExecutionError(_ERROR)


@dataclass(frozen=True, slots=True)
class ActionRecord:
    request: HarnessToolCallRequest
    result: HarnessToolCallResult | None
    content_sha256: str | None
    content_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class ActionContinuation(Mapping[str, JsonValue]):
    value: Mapping[str, JsonValue]
    snapshot: TaskContext
    actions: tuple[ActionRecord, ...]

    def __getitem__(self, key: str) -> JsonValue:
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


async def _action(repository: ArtifactRepository, context: TaskContext, value: object, *, pending: bool) -> ActionRecord:
    _need(isinstance(value, Mapping) and set(value) == ({'raw_sha256', 'request'} if pending else
                                                      {'raw_sha256', 'request', 'result'}))
    assert isinstance(value, Mapping)
    raw = value['request']
    _need(isinstance(raw, Mapping))
    assert isinstance(raw, Mapping)
    request = HarnessToolCallRequest(**{**dict(raw), 'run_id': UUID(raw['run_id'])})
    _need(_encode(request.to_payload()) == _encode(raw) and request.run_id == context.run_id
          and request.actor == 'main_agent' and request.sandbox == context.routing_decision.get('sandbox_profile', 'none'))
    arguments = dict(request.arguments)
    content = None
    if request.tool_name == 'workspace.write_text':
        _need(set(arguments) in ({'path', 'content'}, {'path', 'content_reference'}))
        if 'content_reference' in arguments:
            reference = arguments.pop('content_reference')
            _need(isinstance(reference, Mapping))
            arguments['content'] = await load_continuation(repository, tenant_id=context.tenant_id,
                run_id=context.run_id, reference=cast(Mapping[str, JsonValue], reference))
        _need(type(arguments['path']) is str and type(arguments['content']) is str)
        content = cast(str, arguments['content'])
    hashes = {hashlib.sha256(_encode({'name': request.tool_name, 'arguments': arguments,
                                     'key': request.idempotency_key})).hexdigest(),
              hashlib.sha256(_encode(request.to_payload())).hexdigest()}
    _need(type(value['raw_sha256']) is str and value['raw_sha256'] in hashes)
    result = None
    if not pending:
        raw_result = value['result']
        _need(isinstance(raw_result, Mapping))
        assert isinstance(raw_result, Mapping)
        result = HarnessToolCallResult(**dict(raw_result))
        _need(_encode(result.to_payload()) == _encode(raw_result)
              and (result.call_id, result.tool_name) == (request.call_id, request.tool_name))
    content_sha256, content_size = None, None
    if content is not None:
        raw_content = content.encode('utf-8')
        content_sha256, content_size = hashlib.sha256(raw_content).hexdigest(), len(raw_content)
    return ActionRecord(request, result, content_sha256, content_size)


async def read_action_continuation(repository: ArtifactRepository, context: TaskContext) -> ActionContinuation:
    """Decode private data only; runtime still validates resolved model/preparation/budget/replay."""
    try:
        checkpoint = context.checkpoint
        _need(checkpoint is not None and action_checkpoint_valid(checkpoint)
              and (checkpoint.tenant_id, checkpoint.run_id, checkpoint.mode) ==
                  (context.tenant_id, context.run_id, context.mode))
        assert checkpoint is not None
        value = await load_continuation(repository, tenant_id=context.tenant_id, run_id=context.run_id,
            reference=cast(Mapping[str, JsonValue], checkpoint.state['reference']))
        _need(isinstance(value, Mapping) and set(value) == {
            'context', 'models', 'actions', 'pending', 'emitted', 'elapsed', 'inspection', 'preparation'})
        assert isinstance(value, Mapping)
        snapshot = TaskContext.from_payload(json.loads(_encode(value['context'])))
        snapshot = snapshot.model_copy(update={'instruction_context': context.instruction_context})
        if (_fingerprint(snapshot) != _fingerprint(context)
                or snapshot.routing_decision.get('conversation_id') != context.routing_decision.get('conversation_id')):
            raise _ContextChanged('action continuation context changed')
        _need(snapshot.execution_token is None and snapshot.checkpoint is None)
        sources = {item.id: item for item in context.artifacts}
        if any(item.id not in sources or _encode(sources[item.id].to_payload()) != _encode(item.to_payload())
               for item in snapshot.artifacts):
            raise _ContextChanged('action continuation sources changed')
        if value['inspection'] is not None:
            inspection = Artifact.from_payload(json.loads(_encode(value['inspection'])))
            _need(inspection.id in sources and _encode(sources[inspection.id].to_payload()) == _encode(inspection.to_payload()))
        _need(type(value['emitted']) is int and value['emitted'] >= 0
              and type(value['elapsed']) in (int, float) and math.isfinite(cast(float, value['elapsed']))
              and 0 <= cast(float, value['elapsed']) <= cast(float, checkpoint.state['elapsed_seconds']))
        preparation = value['preparation']
        _need(isinstance(preparation, Mapping) and (not preparation or set(preparation) == {
            'schema_version', 'scope', 'contracts', 'workspace_sha256'}))
        models, actions = value['models'], value['actions']
        _need(isinstance(models, (list, tuple)) and len(models) <= 16384
              and isinstance(actions, (list, tuple)) and len(actions) <= 65536)
        assert isinstance(models, (list, tuple)) and isinstance(actions, (list, tuple))
        for reference in models:
            _need(isinstance(reference, Mapping))
            _checkpoint(cast(Mapping[str, JsonValue], reference))
        parsed = tuple([await _action(repository, context, action, pending=False) for action in actions])
        pending = await _action(repository, context, value['pending'], pending=True)
        _need(pending.request.tool_name == checkpoint.state['tool_name'])
        return ActionContinuation(value, snapshot, parsed)
    except asyncio.CancelledError:
        raise
    except _ContextChanged as exc:
        raise RuntimeExecutionError(str(exc)) from None
    except Exception:  # noqa: BLE001, S110 - never expose private frame or parse diagnostics.
        pass
    raise RuntimeExecutionError(_ERROR) from None


async def writes_own_inventory(
    repository: ArtifactRepository, context: TaskContext, files: Sequence[ProjectWorkspaceFile],
    preparation: Mapping[str, JsonValue],
) -> bool:
    """Prove current bytes came from successful writes; this does not admit any action."""
    frame = await read_action_continuation(repository, context)
    _need(_encode(frame.value['preparation']) == _encode(preparation))
    current = {(f.path, f.sha256, f.size_bytes) for f in files}
    _need(len(current) == len(files))
    workspace = hashlib.sha256(json.dumps([(f.path, f.sha256, f.size_bytes) for f in files],
        ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
    _need(preparation['workspace_sha256'] == workspace)
    owned: dict[str, tuple[str, int]] = {}
    for action in frame.actions:
        request, result = action.request, action.result
        if request.tool_name != 'workspace.write_text':
            continue
        path = cast(str, request.arguments['path'])
        owned.pop(path, None)
        if result is None or result.status != 'succeeded' or result.truncated or result.failure_reason is not None:
            continue
        metadata = result.payload.get('file')
        _need(isinstance(metadata, Mapping) and action.content_sha256 is not None
              and action.content_size_bytes is not None)
        assert isinstance(metadata, Mapping) and action.content_sha256 is not None
        assert action.content_size_bytes is not None
        digest, size = action.content_sha256, action.content_size_bytes
        expected_url = ('/api/v1/workspaces/projects/' + cast(str, context.routing_decision['project_id'])
            + '/sessions/' + cast(str, context.routing_decision['workspace_session_id'])
            + '/files/download?path=' + quote(path, safe=''))
        _need(metadata.get('path') == path and metadata.get('filename') == PurePosixPath(path).name
              and metadata.get('sha256') == digest and type(metadata.get('size_bytes')) is int
              and metadata['size_bytes'] == size and metadata.get('download_url') == expected_url
              and _encode(result.payload.get('workspace_files')) == _encode([metadata]))
        owned[path] = (digest, size)
    _need(current == {(path, sha, size) for path, (sha, size) in owned.items()})
    return True
