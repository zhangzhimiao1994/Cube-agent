"""Recognize the durable runtime receipt for a pending capability approval."""

from collections.abc import Mapping
from typing import cast

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import RuntimeCheckpoint


def checkpoint_waits_for_approval(
    checkpoint: RuntimeCheckpoint | None, approval_id: str,
) -> bool:
    """Match a receipt, not general replayability; callers must still check event order."""
    if checkpoint is None or type(approval_id) is not str or not approval_id.strip():
        return False
    try:
        if checkpoint.state_sha256 != checkpoint.recompute_state_sha256():
            return False
        # to_payload thaws the contract's immutable mappings for strict JSON validation.
        checkpoint = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        if checkpoint.runtime_type == "hybrid":
            if (
                checkpoint.runtime_version != "2"
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
        # Only Crew v11 defines waiting_approval receipts. Never recursively search
        # arbitrary state, or accept nested Hybrid/direct/discussion ledgers.
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
