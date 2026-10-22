"""Pure Direct action v2 schema checks shared by runtime and approval drain.

Hashes and references identify data, not permission to execute. Actual resume
must still load and validate the private continuation and current authority.
This module deliberately does not depend on the action runtime or its codec.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import RuntimeCheckpoint

# Wire limits of continuation JSON v1; kept independent of its storage codec.
_SEGMENT_BYTES = 64 * 1024
_MAX_BYTES = 64 * 1024 * 1024
_MAX_SEGMENTS = 1024
_SHA256 = re.compile(r'[a-f0-9]{64}')
_STATE_FIELDS = frozenset({'phase', 'completed', 'approval_id', 'tool_name',
                          'context_sha256', 'reference', 'elapsed_seconds', 'timeout_seconds'})
_TOOLS = frozenset({'workspace.write_text', 'workspace.bundle', 'project.verify',
                    'workspace.read', 'read_context', 'project.inspect'})


def _digest(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _reference_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        'version', 'content_sha256', 'total_bytes', 'segments',
    }:
        return False
    total = value['total_bytes']
    segments = value['segments']
    if (type(value['version']) is not int or value['version'] != 1
            or not _digest(value['content_sha256'])
            or type(total) is not int or not 1 <= total <= _MAX_BYTES
            or not isinstance(segments, list | tuple)
            or not 1 <= len(segments) <= _MAX_SEGMENTS
            or len(segments) != (total + _SEGMENT_BYTES - 1) // _SEGMENT_BYTES):
        return False
    seen: set[UUID] = set()
    for segment in segments:
        if not isinstance(segment, Mapping) or set(segment) != {'id', 'sha256'}:
            return False
        identity = segment['id']
        if type(identity) is not str or len(identity) != 36 or not _digest(segment['sha256']):
            return False
        parsed = UUID(identity)
        if str(parsed) != identity or parsed in seen:
            return False
        seen.add(parsed)
    return True


def action_checkpoint_valid(checkpoint: RuntimeCheckpoint, approval_id: str | None = None) -> bool:
    """Recognize a strict v2 boundary; do not authenticate its referenced data."""
    try:
        if (type(checkpoint) is not RuntimeCheckpoint
                or any(type(value) is not UUID for value in
                       (checkpoint.id, checkpoint.tenant_id, checkpoint.run_id))
                or checkpoint.state_sha256 != checkpoint.recompute_state_sha256()):
            return False
        # Also reject model_construct/model_copy states that bypassed JSON validation.
        checkpoint = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        state = checkpoint.state
        elapsed, timeout = state.get('elapsed_seconds'), state.get('timeout_seconds')
        approval = state.get('approval_id')
        tool = state.get('tool_name')
        return bool(
            checkpoint.runtime_type == 'direct_action' and checkpoint.runtime_version == '2'
            and checkpoint.mode is TaskMode.DIRECT and set(state) == _STATE_FIELDS
            and state['phase'] == 'concrete_action' and state['completed'] is False
            and type(approval) is str
            and re.fullmatch(r'approval_[A-Za-z0-9_-]{1,100}', approval) is not None
            and (approval_id is None or type(approval_id) is str and approval_id == approval)
            and type(tool) is str and tool in _TOOLS and _digest(state['context_sha256'])
            and type(elapsed) in (int, float) and type(timeout) in (int, float)
            # Bounded comparisons also reject NaN/infinity without coercing huge integers.
            and 0 <= cast(float, elapsed) < cast(float, timeout) <= 3600
            and _reference_valid(state['reference'])
        )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return False


__all__ = ['action_checkpoint_valid']
