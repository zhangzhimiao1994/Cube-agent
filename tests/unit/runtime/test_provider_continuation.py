from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from importlib import import_module
from typing import Any
from uuid import uuid4

import pytest


def snapshot(**changes: Any) -> Any:
    module = import_module("agent_hub.runtime.provider_continuation")
    native = import_module("agent_hub.models.native_messages")
    from agent_hub.runtime.contracts import GatewayProvenance

    values = {
        "id": uuid4(), "tenant_id": uuid4(), "run_id": uuid4(), "ledger_key": "a" * 64,
        "request_sha256": "b" * 64, "model_artifact_sha256": "c" * 64,
        "provenance": GatewayProvenance(
            logical_model="deepseek", deployment_id="deployment-1", provider_id="deepseek",
            provider_model="deepseek/deepseek-chat",
        ),
        "assistant": native.NativeAssistantMessage(
            deployment_id="deployment-1", content=None,
            tool_calls=(native.NativeToolCall(
                id="call-1", name="workspace.read", arguments='{ "path" : "file.ts" }',
            ),), reasoning_content="PRIVATE_REASONING_SENTINEL",
        ),
    }
    return module.ProviderContinuationSnapshot(**(values | changes))


def test_contract_exists_separately_from_artifacts() -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    value = snapshot()
    assert not hasattr(value, "content_sha256")
    assert isinstance(value, module.ProviderContinuationSnapshot)


def test_snapshot_roundtrip_hash_reference_immutability_and_private_repr() -> None:
    value = snapshot()
    payload = value.to_payload()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode()
    assert value.sha256 == hashlib.sha256(canonical).hexdigest()
    assert value.reference() == {"id": str(value.id), "sha256": value.sha256}
    assert type(value).from_payload(payload) == value
    assert value.assistant.to_wire()["tool_calls"][0]["function"]["arguments"] == '{ "path" : "file.ts" }'
    assert "PRIVATE" not in repr(value) and "file.ts" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.ledger_key = "changed"
    payload["assistant"]["reasoning_content"] = "mutated"
    assert value.assistant.reasoning_content == "PRIVATE_REASONING_SENTINEL"


@pytest.mark.parametrize("field,bad", [
    ("id", "not-uuid"), ("tenant_id", None), ("run_id", "PRIVATE"),
    ("ledger_key", "PRIVATE\nKEY"), ("request_sha256", "PRIVATE"),
    ("model_artifact_sha256", "A" * 64), ("assistant", {"reasoning_content": "PRIVATE"}),
    ("provenance", {"provider_model": "PRIVATE"}),
])
def test_invalid_construction_is_strict_and_errors_are_private(field: str, bad: object) -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    with pytest.raises(module.ProviderContinuationError) as caught:
        snapshot(**{field: bad})
    assert "PRIVATE" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("field,bad", [
    ("version", True), ("version", 2), ("protocol", "responses"),
    ("id", "00000000000040008000000000000001"),
    ("provenance", {"PRIVATE": "PRIVATE"}), ("assistant", {"PRIVATE": "PRIVATE"}),
    ("extra", "PRIVATE"),
])
def test_payload_exact_keys_version_canonical_uuid_and_nested_contract(field: str, bad: object) -> None:
    value = snapshot()
    payload = value.to_payload()
    payload[field] = bad
    from agent_hub.runtime.provider_continuation import ProviderContinuationError

    with pytest.raises(ProviderContinuationError) as caught:
        type(value).from_payload(payload)
    assert "PRIVATE" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None


def test_deployment_provenance_must_match_native_envelope() -> None:
    value = snapshot()
    from agent_hub.runtime.provider_continuation import ProviderContinuationError

    with pytest.raises(ProviderContinuationError):
        replace(value, assistant=replace(value.assistant, deployment_id="different"))


async def test_memory_roundtrip_idempotent_and_scope_digest_missing_fail_closed() -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    repo = module.InMemoryProviderContinuationRepository()
    value = snapshot()
    await repo.put(value)
    await repo.put(value)
    assert await repo.get(value.tenant_id, value.run_id, value.id, value.sha256) == value
    for tenant, run, ident, digest in (
        (uuid4(), value.run_id, value.id, value.sha256),
        (value.tenant_id, uuid4(), value.id, value.sha256),
        (value.tenant_id, value.run_id, uuid4(), value.sha256),
        (value.tenant_id, value.run_id, value.id, "0" * 64),
    ):
        with pytest.raises(module.ProviderContinuationRepositoryError):
            await repo.get(tenant, run, ident, digest)


async def test_memory_immutable_id_and_unique_ledger_binding() -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    repo = module.InMemoryProviderContinuationRepository()
    value = snapshot()
    await repo.put(value)
    for conflict in (replace(value, request_sha256="d" * 64), replace(value, id=uuid4()),
                     replace(value, run_id=uuid4()), replace(value, ledger_key="other")):
        with pytest.raises(module.ProviderContinuationRepositoryError):
            await repo.put(conflict)
    assert await repo.get(value.tenant_id, value.run_id, value.id, value.sha256) == value


async def test_memory_quota_idempotency_and_concurrent_duplicate_ledger() -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    value = snapshot()
    size = len(module.encode_snapshot(value).encode())
    repo = module.InMemoryProviderContinuationRepository(
        max_snapshots_per_run=1, max_total_bytes_per_run=size, max_snapshot_bytes=size,
    )
    results = await asyncio.gather(repo.put(value), repo.put(replace(value, id=uuid4())),
                                   return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, module.ProviderContinuationRepositoryError) for result in results) == 1
    await repo.put(value)
    with pytest.raises(module.ProviderContinuationRepositoryError):
        await repo.put(replace(value, id=uuid4(), ledger_key="other"))
    for limits in ({"max_snapshot_bytes": size - 1}, {"max_total_bytes_per_run": size - 1}):
        with pytest.raises(module.ProviderContinuationRepositoryError):
            await module.InMemoryProviderContinuationRepository(**limits).put(value)


@pytest.mark.parametrize("operation", ["put", "get"])
async def test_memory_cancellation_propagates_without_writing(operation: str) -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    repo = module.InMemoryProviderContinuationRepository()
    value = snapshot()
    async with repo._lock:
        task = asyncio.create_task(repo.put(value) if operation == "put" else repo.get(
            value.tenant_id, value.run_id, value.id, value.sha256,
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(module.ProviderContinuationRepositoryError):
        await repo.get(value.tenant_id, value.run_id, value.id, value.sha256)


async def test_forged_dataclass_and_corrupt_memory_storage_are_rejected() -> None:
    module = import_module("agent_hub.runtime.provider_continuation")
    value = snapshot()
    repo = module.InMemoryProviderContinuationRepository()
    await repo.put(value)
    forged = snapshot()
    object.__setattr__(forged, "request_sha256", "PRIVATE")
    with pytest.raises(module.ProviderContinuationRepositoryError) as caught:
        await repo.put(forged)
    assert "PRIVATE" not in repr(caught.value)
    repo._payloads[value.id] += " "
    with pytest.raises(module.ProviderContinuationRepositoryError):
        await repo.get(value.tenant_id, value.run_id, value.id, value.sha256)
