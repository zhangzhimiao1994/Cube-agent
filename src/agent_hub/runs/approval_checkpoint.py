"""Recognize the durable runtime receipt for a pending capability approval."""

import hashlib
import re
from collections.abc import Mapping
from typing import cast

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.contracts import RuntimeCheckpoint


def _autogen_waits_for_approval(checkpoint: RuntimeCheckpoint, approval_id: str) -> bool:
    # Keep the driver independent of AutoGen imports; its formal runtime version is 2.
    if (checkpoint.runtime_version != '2' or checkpoint.mode is not TaskMode.DISCUSS
            or checkpoint.state.get('terminal') is not False
            or re.fullmatch(r'approval_[A-Za-z0-9_-]{1,100}', approval_id) is None):
        return False
    ledger = checkpoint.state.get('tool_ledger')
    if not isinstance(ledger, tuple) or not ledger:
        return False
    if not all(isinstance(row, Mapping) and row.get('status') in {'succeeded', 'failed'}
               for row in ledger[:-1]):
        return False
    receipt = ledger[-1]
    fields = {'status', 'approval_id', 'replay_safe', 'actor', 'name', 'position',
              'arguments_hash', 'idempotency_key'}
    if (not isinstance(receipt, Mapping) or set(receipt) != fields
            or receipt.get('status') != 'waiting_approval'
            or receipt.get('approval_id') != approval_id
            or type(receipt.get('replay_safe')) is not bool):
        return False
    actor, name, position = receipt['actor'], receipt['name'], receipt['position']
    arguments_hash = receipt['arguments_hash']
    if (type(actor) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,127}', actor) is None
            or type(name) is not str or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) is None
            or type(position) is not int or position != len(ledger) - 1
            or type(arguments_hash) is not str
            or re.fullmatch(r'[a-f0-9]{64}', arguments_hash) is None):
        return False
    identity = f'{checkpoint.run_id}:{actor}:{position}:{name}:{arguments_hash}'
    return (type(receipt['idempotency_key']) is str
            and receipt['idempotency_key'] == hashlib.sha256(identity.encode()).hexdigest())


def checkpoint_waits_for_approval(
    checkpoint: RuntimeCheckpoint | None, approval_id: str,
) -> bool:
    """Match a receipt, not general replayability; callers must still check event order."""
    if type(checkpoint) is not RuntimeCheckpoint or type(approval_id) is not str or not approval_id.strip():
        return False
    try:
        if checkpoint.state_sha256 != checkpoint.recompute_state_sha256():
            return False
        # to_payload thaws the contract's immutable mappings for strict JSON validation.
        checkpoint = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        if checkpoint.runtime_type == 'engineering_admission':
            from agent_hub.runtime.engineering_admission import admission_checkpoint_valid

            return admission_checkpoint_valid(checkpoint, approval_id)
        if checkpoint.runtime_type == 'direct_action':
            return action_checkpoint_valid(checkpoint, approval_id)
        if checkpoint.runtime_type == 'autogen':
            return _autogen_waits_for_approval(checkpoint, approval_id)
        if checkpoint.runtime_type == "hybrid":
            if (
                checkpoint.runtime_version not in {"2", "3"}
                or checkpoint.mode is not TaskMode.HYBRID
                or checkpoint.state.get("terminal") is not False
                or type(checkpoint.state.get("next_stage")) is not int
                or checkpoint.state["next_stage"] not in {0, 1}
            ):
                return False
            state_payload = cast(dict[str, object], checkpoint.to_payload()["state"])
            payload = state_payload.get("child_checkpoint")
            if not isinstance(payload, dict) or not payload.get("state_sha256"):
                return False
            child = RuntimeCheckpoint.from_payload(payload)
            if child.run_id != checkpoint.run_id or child.tenant_id != checkpoint.tenant_id:
                return False
            checkpoint = child
        # Hybrid Direct actions remain unsupported until the actual plan's Direct
        # stage is validated. Preserve the existing one-level Crew v11 rule only;
        # never recursively search arbitrary references or nested Hybrid ledgers.
        if (
            checkpoint.runtime_type != "crew"
            or checkpoint.runtime_version != "11"
            or checkpoint.mode is not TaskMode.DISPATCH
        ):
            return False
        tools = checkpoint.state.get("tools")
        return isinstance(tools, Mapping) and any(
            isinstance(receipt, Mapping)
            and receipt.get("status") == "waiting_approval"
            and receipt.get("approval_id") == approval_id
            for receipt in tools.values()
        )
    except (KeyError, TypeError, ValueError):
        return False
