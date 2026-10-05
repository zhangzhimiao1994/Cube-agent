"""Strict public facts; these synthetic contracts are not native acceptance."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from agent_hub.previews.cleanup import BrokerCleanupObservation


def contract() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.cleanup"), "cleanup contract missing"
    return importlib.import_module("agent_hub.previews.cleanup")


def identity(kind: str = "static") -> Any:
    c = contract()
    return c.PreviewIdentityV1(
        preview_id="10000000-0000-0000-0000-000000000001", kind=kind,
        tenant_id="20000000-0000-0000-0000-000000000001",
        user_id="30000000-0000-0000-0000-000000000001", project_id="project-a",
        conversation_id="conversation-a", workspace_session_id="session-a",
        runtime_handle="a" * 32 if kind == "dynamic" else None,
        source_scheme="preview-broker-tree-v2" if kind == "dynamic" else "preview-static-tree-v1",
        source_sha256="b" * 64, display_root="dist", display_entrypoint="index.html",
    )


def broker_observation(bound: Any = None) -> BrokerCleanupObservation:
    c = contract()
    now = datetime.now(UTC)
    facts = []
    for resource in c.BROKER_RESOURCES:
        result = "absent"
        kwargs = {}
        if resource == "attachment":
            result = "exited"
        elif resource.startswith("unit_") or resource == "mount_unit":
            result = "inactive"
            kwargs = {"load_state": "not-found", "active_state": "inactive", "identity_match": True,
                      "main_pid": 0 if resource != "mount_unit" else None}
        facts.append(c.ResourceObservation(resource, "broker", now, result, "observed", **kwargs))
    return cast("BrokerCleanupObservation", c.BrokerCleanupObservation.create(
        bound or identity("dynamic"), tuple(facts), requested_at=now))


def test_identity_strict_roundtrip_and_immutable() -> None:
    c = contract()
    original = identity()
    assert c.PreviewIdentityV1.from_wire(original.to_wire()) == original
    with pytest.raises(AttributeError):
        original.project_id = "other"
    for key, value in (("secret", "no"), ("display_root", "../outside"),
                       ("source", {"scheme": "preview-broker-tree-v1", "sha256": "b" * 64})):
        with pytest.raises(ValueError):
            c.PreviewIdentityV1.from_wire(original.to_wire() | {key: value})


def test_receipt_derives_status_and_rejects_forged_summary() -> None:
    c = contract()
    now = datetime.now(UTC)
    receipt = c.CleanupReceiptV1.create(identity(), (), requested_at=now, reason="explicit")
    assert receipt.status == "unknown"
    assert c.CleanupReceiptV1.from_wire(receipt.to_wire()) == receipt
    patches: tuple[dict[str, object], ...] = ({"status": "confirmed"}, {"schema_version": True},
                                            {"observations": []}, {"unobserved": ["private_port"]})
    for patch in patches:
        with pytest.raises(ValueError):
            c.CleanupReceiptV1.from_wire(receipt.to_wire() | patch)


def test_broker_cannot_claim_manager_or_other_identity_facts() -> None:
    c = contract()
    now = datetime.now(UTC)
    fact = c.ResourceObservation("snapshot", "manager", now, "absent", "observed")
    with pytest.raises(ValueError):
        c.BrokerCleanupObservation.create(identity("dynamic"), (fact,), requested_at=now)


def test_malformed_unit_field_raises_contract_validation_error() -> None:
    c = contract()
    fact = broker_observation().observations[1].to_wire()
    with pytest.raises(ValueError):
        c.ResourceObservation.from_wire(fact | {"load_state": []})
