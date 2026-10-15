"""Actual generated file envelopes through the persistent download service."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.api.routers.admin import PersistentAdminResourceService
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.files.generated import GeneratedFileStore
from agent_hub.runtime.contracts import Artifact, JsonValue
from tests.api.test_admin_resources import (
    ACTOR_ID,
    TENANT_ID,
    FakeConfigService,
    FakeSecretService,
    GeneratedArtifactRunRepository,
)


@pytest.mark.parametrize("envelope", ["result", "workspace_delivery"])
@pytest.mark.parametrize("selector", [
    "outer", "inner", "disagreeing-id", "invalid-id", "foreign-run", "foreign-tenant",
    "ambiguous-envelope",
])
async def test_generated_zip_download_resolves_only_matching_published_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, envelope: str, selector: str,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    run_id, outer_id = uuid4(), uuid4()
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", generated_artifact_dir=tmp_path / "generated",
        skill_sandboxes={},
    )
    result = await gateway.execute(
        tenant_id=TENANT_ID, run_id=run_id, actor="main", name="project.generate_zip",
        arguments={"title": "Source ZIP", "files": {"main.py": "print('source bytes')\n"}},
        idempotency_key="download-envelope",
    )
    inner_id = UUID(cast(str, result["artifact_id"]))
    file = cast(dict[str, JsonValue], result["file"])
    metadata = cast(dict[str, JsonValue], result["metadata"])
    if selector == "disagreeing-id":
        file["artifact_id"] = str(uuid4())
    elif selector == "invalid-id":
        for item in (cast(dict[str, JsonValue], result), file, metadata):
            item["artifact_id"] = "not-a-uuid"
    elif selector in ("foreign-run", "foreign-tenant"):
        foreign = GeneratedFileStore(tmp_path / "generated").store_bytes(
            uuid4() if selector == "foreign-tenant" else TENANT_ID,
            uuid4() if selector == "foreign-run" else run_id,
            inner_id, cast(str, metadata["filename"]), "application/zip", b"foreign bytes",
        )
        metadata["storage_key"] = foreign.storage_key
    content: dict[str, JsonValue] = {envelope: result}
    if selector == "ambiguous-envelope":
        content["workspace_delivery" if envelope == "result" else "result"] = result
    artifact = Artifact(id=outer_id, type="tool_result", producer="main", content=content)
    repository = GeneratedArtifactRunRepository(run_id, (artifact.to_payload(),))
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID, actor_id=ACTOR_ID,
        run_repository=repository,  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path / "generated",
    )
    requested = outer_id if selector == "outer" else inner_id
    if selector not in ("outer", "inner"):
        with pytest.raises(KeyError):
            await service.download_run_artifact(run_id, outer_id, tenant_id=TENANT_ID)
        return
    download = await service.download_run_artifact(run_id, requested, tenant_id=TENANT_ID)
    assert download.mime_type == "application/zip"
    assert download.path.relative_to(tmp_path / "generated").as_posix() == metadata["storage_key"]
    assert download.path.read_bytes().startswith(b"PK\x03\x04")
