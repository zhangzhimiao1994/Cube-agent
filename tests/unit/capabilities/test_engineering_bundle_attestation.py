"""Bundle attestation comes from checked ZIP bytes, never model parameters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from tests.unit.capabilities.test_project_verification import Executor, gateway, verify
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


async def test_real_verified_bundle_attests_exact_receipt_and_source(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    write_file(tmp_path / "projects", f"{project_path()}/protected.test.js", "original test")
    verified = await verify(backend)
    receipt = verified["verification"]
    assert isinstance(receipt, Mapping)
    bundle = await backend.execute(tenant_id=TENANT, run_id=RUN, actor="engineer", name="workspace.bundle",
                                   arguments={}, idempotency_key="attested-bundle")
    assert bundle["verification_source_sha256"] == receipt["source_sha256"]
    assert bundle["verification_receipt_sha256"] == hashlib.sha256(json.dumps(
        receipt, default=dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    assert (tmp_path / "projects" / project_path() / "protected.test.js").read_text() == "original test"


async def test_failure_invalidates_old_attestation(tmp_path: Path) -> None:
    executor = Executor()
    backend = gateway(tmp_path, executor)
    await verify(backend)
    executor.failed = True
    with pytest.raises(RuntimeCapabilityError):
        await verify(backend)
    with pytest.raises(RuntimeCapabilityError, match="verification required"):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor="engineer", name="workspace.bundle",
                              arguments={}, idempotency_key="failed-attestation")
