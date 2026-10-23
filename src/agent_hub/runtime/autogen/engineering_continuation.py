"""Opt-in, ledger-derived scheduling hints; never capture or execution authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.engineering_delivery import _READ_ONLY, engineering_progress
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext, _canonical_json_bytes
from agent_hub.runtime.crew.workspace_maintenance_delivery import _file_metadata
from agent_hub.runtime.engineering_actions import EngineeringProgress
from agent_hub.runtime.progress import workspace_progress_key

ENGINEERING_CONTINUATION_POLICY = 'engineering_writer_continuation_version'
ENGINEERING_CONTINUATION_VERSION = 1

type _Metadata = tuple[str, str, int]
type _Outcome = tuple[str, str, str, Mapping[str, JsonValue]]


def engineering_continuation_selected(context: TaskContext) -> bool:
    return context.mode is TaskMode.DISCUSS and all(
        type(context.routing_decision.get(key)) is int and context.routing_decision[key] == 1
        for key in ('generated_project_contract_version', 'engineering_action_version',
                    ENGINEERING_CONTINUATION_POLICY)
    )


def _inventory(result: Mapping[str, JsonValue]) -> dict[str, _Metadata]:
    values = result.get('workspace_files')
    if not isinstance(values, tuple | list) or result.get('truncated', False) is not False:
        raise ValueError('engineering continuation inventory is invalid')
    files = {}
    for value in values:
        item = _file_metadata(value)
        if item is None or item[0] in files:
            raise ValueError('engineering continuation inventory is invalid')
        files[item[0]] = item
    return files


def _written(result: Mapping[str, JsonValue]) -> _Metadata:
    files = result.get('workspace_files')
    if isinstance(files, tuple | list):
        item = _file_metadata(files[0]) if len(files) == 1 else None
    else:
        item = _file_metadata(result)
    if item is None:
        raise ValueError('engineering continuation write metadata is invalid')
    return item


def _validated(
    context: TaskContext, tool_entries: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact], allowed_tools: Mapping[str, Sequence[str]],
) -> tuple[EngineeringProgress, tuple[_Outcome, ...]]:
    progress = engineering_progress(context, tool_entries=tool_entries, artifacts=artifacts,
                                    allowed_tools=allowed_tools)
    if progress.uncertain:
        raise ValueError('engineering continuation evidence is invalid or uncertain')
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    outcomes = []
    for row in tool_entries:
        artifact = by_id[cast(str, row['artifact_id'])]
        result = cast(Mapping[str, JsonValue], artifact.content['result'])
        name, actor, status = (cast(str, row[key]) for key in ('name', 'actor', 'status'))
        if name == 'workspace.list':
            _inventory(result)
        elif name == 'workspace.write_text':
            _written(result)
        elif name in _READ_ONLY and result.get('truncated', False) is not False:
            raise ValueError('engineering continuation observation is incomplete')
        if name in _READ_ONLY:
            _read_key(name, result)
        outcomes.append((actor, name, status, result))
    return progress, tuple(outcomes)


def draft_action(
    context: TaskContext, tool_entries: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact], allowed_tools: Mapping[str, Sequence[str]],
) -> str | None:
    if not engineering_continuation_selected(context):
        return None
    progress, outcomes = _validated(context, tool_entries, artifacts, allowed_tools)
    if (not progress.inspected or progress.existing_project or progress.verification_seen
            or 'generated_project_preparation' in context.routing_decision):
        return None
    writers = [actor for actor, tools in allowed_tools.items() if 'workspace.write_text' in tools]
    if len(writers) != 1:
        return None
    tools = allowed_tools[writers[0]]
    files: dict[str, _Metadata] | None = None
    for _, name, _, result in outcomes:
        if name == 'workspace.list':
            files = _inventory(result)
        elif name == 'workspace.write_text' and files is not None:
            item = _written(result)
            files[item[0]] = item
    if files is None:
        return 'workspace.list' if 'workspace.list' in tools else None
    return 'workspace.write_text' if 'ACCEPTANCE.json' not in files else None


def _read_key(name: str, result: Mapping[str, JsonValue]) -> object:
    if name in {'workspace.read', 'workspace_read'} and (
        type(result.get('path')) is not str or type(result.get('text')) is not str
    ):
        raise ValueError('engineering continuation read metadata is invalid')
    key = workspace_progress_key(name, '', result)
    if key is not None:
        return key
    # Descriptive summaries are not new workspace information.
    return name, _canonical_json_bytes({key: value for key, value in result.items() if key != 'summary'})


def writer_can_continue(
    context: TaskContext, owner: str, tool_entries: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact], allowed_tools: Mapping[str, Sequence[str]],
) -> bool:
    if not engineering_continuation_selected(context):
        return False
    _, outcomes = _validated(context, tool_entries, artifacts, allowed_tools)
    writers = {actor for actor, tools in allowed_tools.items() if 'workspace.write_text' in tools}
    if not outcomes or writers != {owner} or outcomes[-1][0] != owner:
        return False
    _, name, status, result = outcomes[-1]
    if name == 'workspace.bundle':
        return False
    if status == 'failed':
        if (name != 'project.verify' or 'generated_project_preparation' in context.routing_decision
                or not isinstance(result.get('generated_draft_rejection'), Mapping)):
            return False
        rejection = cast(Mapping[str, JsonValue], result['generated_draft_rejection'])
        observation = rejection['observation_sha256']
        for _, _, prior_status, prior in outcomes[:-1]:
            value = prior.get('generated_draft_rejection')
            if prior_status == 'failed' and isinstance(value, Mapping) and value.get('observation_sha256') == observation:
                return False
        return True
    if name == 'workspace.write_text':
        known: dict[str, _Metadata] = {}
        for _, prior_name, _, prior in outcomes[:-1]:
            if prior_name == 'workspace.list':
                known = _inventory(prior)
            elif prior_name == 'workspace.write_text':
                item = _written(prior)
                known[item[0]] = item
        latest = _written(result)
        return known.get(latest[0]) != latest
    if name in _READ_ONLY:
        latest_key = _read_key(name, result)
        return all(_read_key(prior_name, prior) != latest_key
                   for _, prior_name, _, prior in outcomes[:-1] if prior_name in _READ_ONLY)
    return False


__all__ = [
    'ENGINEERING_CONTINUATION_POLICY',
    'ENGINEERING_CONTINUATION_VERSION',
    'draft_action',
    'engineering_continuation_selected',
    'writer_can_continue',
]
