"""Real spool/store/source archive integration; fixture bytes are not a built wheel."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.capabilities.python_product_delivery import consume_response, persist_product
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.files.generated import GeneratedFileStore
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerResponse
from tests.unit.skills.sandbox.test_python_product_transport import descriptor
from tests.unit.skills.sandbox.test_python_project_client import invocation


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_source", [False, True])
async def test_exact_source_and_measured_product_share_one_downloadable_zip(
    tmp_path: Path, stale_source: bool,
) -> None:
    inv = invocation()
    content = b"Use the installed CLI.\n"
    fingerprints = {"README.md": hashlib.sha256(b"f\0" + content).hexdigest()}
    source = hashlib.sha256(json.dumps(fingerprints, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    product = descriptor().model_copy(update={"source_sha256": source})
    report: dict[str, JsonValue] = {"status": "passed", "execution_id": inv.execution_id,
        "source_sha256": source, "recipe_sha256": inv.recipe_sha256,
        "wheel": {"filename": product.filename, "sha256": product.sha256, "size": product.size}}
    response = PythonProjectBrokerResponse(action="run_python_project", execution_id=inv.execution_id,
        ok=True, product=product,
        result=SkillResult(exit_code=0, stdout=json.dumps(report), stderr="", timed_out=False))
    files = GeneratedFileStore(tmp_path / "artifacts")
    spool = (tmp_path / "product-spool.bin").open("w+b")  # consume_response owns it.
    spool.write(b"wheel")
    spool.seek(0)
    response._product_stream = spool
    with consume_response(response, inv, source) as (public, stream):
        ref = await persist_product(files, public, stream, inv, source, report)
    assert spool.closed and response._product_stream is None
    workspace = ProjectWorkspaceStore(tmp_path / "workspace")
    workspace.write_bytes(inv.tenant_id, inv.project_id, inv.session_id, "README.md", content, "text/markdown")
    if stale_source:
        workspace.write_bytes(inv.tenant_id, inv.project_id, inv.session_id, "README.md", b"changed", "text/markdown")
        with pytest.raises(ValueError, match="bundle does not match verified source"):
            RuntimeCapabilityGateway._bundle_workspace(store=workspace, generated_store=files,
                tenant_id=inv.tenant_id, run_id=inv.run_id, project_id=inv.project_id,
                session_id=inv.session_id, artifact_id=uuid4(), filename="project.zip",
                verified_source_sha256=source, python_product=ref)
        assert not list((tmp_path / "artifacts").rglob("*.zip"))
    else:
        artifact_id = uuid4()
        metadata, _ = RuntimeCapabilityGateway._bundle_workspace(store=workspace, generated_store=files,
            tenant_id=inv.tenant_id, run_id=inv.run_id, project_id=inv.project_id,
            session_id=inv.session_id, artifact_id=artifact_id, filename="project.zip",
            verified_source_sha256=source, python_product=ref)
        with zipfile.ZipFile(files.resolve_for(inv.tenant_id, inv.run_id, artifact_id, metadata.storage_key)) as archive:
            assert archive.namelist() == ["README.md", "dist/" + product.filename]
            assert archive.read("dist/" + product.filename) == b"wheel"
            assert archive.read("README.md") == content
    assert [f.path for f in workspace.list_files(inv.tenant_id, inv.project_id, inv.session_id)] == ["README.md"]
