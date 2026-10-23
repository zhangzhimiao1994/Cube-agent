from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.continuation_artifacts import save_continuation
from agent_hub.runtime.contracts import Artifact, JsonValue
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run

TENANT = UUID(int=1)
RUN = UUID(int=2)
USER = UUID(int=3)


class ReadTrackingRepository(InMemoryArtifactRepository):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[tuple[UUID, UUID]] = []

    async def get_many(
        self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
    ) -> tuple[Artifact, ...]:
        self.reads.append((tenant_id, run_id))
        return await super().get_many(tenant_id, run_id, references)


def run_repository(**routing: object) -> FakeRunRepository:
    record = stored_run(tenant=TENANT, run=RUN, actor_id=USER)
    return FakeRunRepository(replace(record, routing_decision={**(record.routing_decision or {}), **routing}))


def backend(
    tmp_path: Path, repository: FakeRunRepository, artifacts: ArtifactRepository | None,
) -> RuntimeCapabilityGateway:
    return RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=tmp_path / "workspaces",
        run_repository=repository, artifact_repository=artifacts,
    )


def harness(runtime: RuntimeCapabilityGateway, effect: PolicyEffect = PolicyEffect.ALLOW) -> HarnessToolGateway:
    policy = CapabilityGateway(
        CapabilityPolicy((CapabilityRule(
            tenant_id=TENANT, role=Role.OPERATOR, agent_id="writer", capability="file",
            operation="write", resource_prefix="workspace/current", effect=effect,
        ),)),
        ApprovalService(InMemoryApprovalStore()), object(),
    )
    return HarnessToolGateway(runtime, policy_gateway=policy, raise_backend_errors=True)


def request(arguments: Mapping[str, JsonValue], *, sandbox: str = "workspace_write") -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=RUN, actor="writer", tool_name="workspace.write_text", arguments=arguments,
        approval_required=False, sandbox=sandbox, idempotency_key="content-reference-write",
    )


async def invoke(
    gateway: HarnessToolGateway, arguments: Mapping[str, JsonValue], *,
    sandbox: str = "workspace_write", user_id: UUID | None = USER, role: Role = Role.OPERATOR,
) -> HarnessToolCallResult:
    return await gateway.invoke(TENANT, request(arguments, sandbox=sandbox), user_id=user_id, role=role)


async def reference(
    repository: ArtifactRepository, value: JsonValue = "reviewed file contents", *,
    tenant_id: UUID = TENANT, run_id: UUID = RUN,
) -> dict[str, JsonValue]:
    return await save_continuation(repository, tenant_id=tenant_id, run_id=run_id, value=value)


def target(tmp_path: Path, path: str = "src/result.txt") -> Path:
    return tmp_path / "workspaces" / str(TENANT) / "projects/project-a/sessions/session-a" / path


async def test_large_referenced_text_writes_through_real_harness_without_raising_inline_limit(
    tmp_path: Path,
) -> None:
    artifacts = ReadTrackingRepository()
    text = "\u6587\u4ef6\n" * 40_000
    ref = await reference(artifacts, text)
    call = request({"path": "src/result.txt", "content_reference": ref})
    assert len(text.encode("utf-8")) > 65_536
    assert len(json.dumps(dict(call.to_payload()), ensure_ascii=False).encode("utf-8")) < 65_536
    with pytest.raises(ValueError):
        request({"path": "src/result.txt", "content": text})

    result = await harness(backend(tmp_path, run_repository(), artifacts)).invoke(
        TENANT, call, user_id=USER, role=Role.OPERATOR,
    )
    assert result.status == "succeeded"
    assert target(tmp_path).read_bytes() == text.encode("utf-8")
    assert artifacts.reads == [(TENANT, RUN)]
    file = cast(Mapping[str, JsonValue], result.payload["file"])
    assert file["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert text not in json.dumps(dict(result.to_payload()), ensure_ascii=False)


@pytest.mark.parametrize("injected", [False, True])
async def test_small_inline_write_preserves_existing_behavior(tmp_path: Path, injected: bool) -> None:
    artifacts = ReadTrackingRepository()
    runtime = backend(tmp_path, run_repository(), artifacts if injected else None)
    text = "keep whitespace and CRLF\r\n"
    result = await invoke(harness(runtime), {
        "path": "src/result.txt", "content": text,
        "project_id": "ignored-project", "workspace_session_id": "ignored-session",
    })
    assert result.status == "succeeded"
    assert target(tmp_path).read_bytes() == text.encode()
    assert artifacts.reads == []
    assert not (tmp_path / "workspaces" / str(TENANT) / "projects/ignored-project").exists()


@pytest.mark.parametrize("reason", ["deny-policy", "wrong-role", "missing-actor", "sandbox"])
async def test_valid_reference_never_grants_write_authority(tmp_path: Path, reason: str) -> None:
    artifacts = ReadTrackingRepository()
    ref = await reference(artifacts)
    gateway = harness(backend(tmp_path, run_repository(), artifacts),
                      PolicyEffect.DENY if reason == "deny-policy" else PolicyEffect.ALLOW)
    result = await invoke(gateway, {"path": "src/result.txt", "content_reference": ref},
        sandbox="read_only" if reason == "sandbox" else "workspace_write",
        user_id=None if reason == "missing-actor" else USER,
        role=Role.VIEWER if reason == "wrong-role" else Role.OPERATOR)
    assert result.status == "failed"
    assert artifacts.reads == []
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("routing", [
    {"sandbox_profile": "read_only"}, {"requested_permissions": ["workspace.read"]},
])
async def test_runtime_write_scope_still_required_after_harness_allows(
    tmp_path: Path, routing: dict[str, object],
) -> None:
    artifacts = ReadTrackingRepository()
    ref = await reference(artifacts)
    result = await invoke(harness(backend(tmp_path, run_repository(**routing), artifacts)),
                          {"path": "src/result.txt", "content_reference": ref})
    assert result.status == "failed"
    assert artifacts.reads == []
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("extra", [
    {"content": "mixed"}, {"content": None}, {"extra": True},
    {"tenant_id": str(uuid4())}, {"run_id": str(uuid4())},
    {"project_id": "forged"}, {"workspace_session_id": "forged"},
])
async def test_reference_request_rejects_mixed_and_extra_arguments_before_read(
    tmp_path: Path, extra: dict[str, JsonValue],
) -> None:
    artifacts = ReadTrackingRepository()
    ref = await reference(artifacts)
    result = await invoke(harness(backend(tmp_path, run_repository(), artifacts)),
                          {"path": "src/result.txt", "content_reference": ref, **extra})
    assert result.status == "failed"
    assert artifacts.reads == []
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("value", [None, True, 1, (), {"text": "object"}, "", " \n\t"])
async def test_reference_must_decode_to_nonblank_string(tmp_path: Path, value: JsonValue) -> None:
    artifacts = InMemoryArtifactRepository()
    ref = await reference(artifacts, value)
    result = await invoke(harness(backend(tmp_path, run_repository(), artifacts)),
                          {"path": "src/result.txt", "content_reference": ref})
    assert result.status == "failed"
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("ref", [None, True, "reference", (), {}])
async def test_reference_requires_mapping_and_configured_repository(tmp_path: Path, ref: JsonValue) -> None:
    result = await invoke(harness(backend(tmp_path, run_repository(), InMemoryArtifactRepository())),
                          {"path": "src/result.txt", "content_reference": ref})
    assert result.status == "failed"
    assert not (tmp_path / "workspaces").exists()


async def test_reference_has_no_implicit_repository_fallback(tmp_path: Path) -> None:
    ref = await reference(InMemoryArtifactRepository())
    result = await invoke(harness(backend(tmp_path, run_repository(), None)),
                          {"path": "src/result.txt", "content_reference": ref})
    assert result.status == "failed"
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("kind", ["tenant", "run", "hash", "bytes", "segment", "order", "extra"])
async def test_foreign_or_tampered_reference_cannot_overwrite_existing_file(
    tmp_path: Path, kind: str,
) -> None:
    artifacts = InMemoryArtifactRepository()
    ref = await reference(artifacts, "x" * 100_000,
        tenant_id=uuid4() if kind == "tenant" else TENANT,
        run_id=uuid4() if kind == "run" else RUN)
    mutable = cast(dict[str, object], json.loads(json.dumps(ref)))
    segments = cast(list[dict[str, object]], mutable["segments"])
    if kind == "hash":
        mutable["content_sha256"] = "0" * 64
    elif kind == "bytes":
        mutable["total_bytes"] = 100_001
    elif kind == "segment":
        segments[0]["sha256"] = "0" * 64
    elif kind == "order":
        segments.reverse()
    elif kind == "extra":
        mutable["unexpected"] = True
    path = target(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing file")
    result = await invoke(harness(backend(tmp_path, run_repository(), artifacts)),
        {"path": "src/result.txt", "content_reference": cast(JsonValue, mutable)})
    assert result.status == "failed"
    assert path.read_bytes() == b"existing file"


@pytest.mark.parametrize("path", ["../escape.txt", "/absolute.txt", "src\\wrong.txt", ".private.txt"])
async def test_reference_preserves_workspace_path_checks(tmp_path: Path, path: str) -> None:
    artifacts = InMemoryArtifactRepository()
    ref = await reference(artifacts)
    result = await invoke(harness(backend(tmp_path, run_repository(), artifacts)),
                          {"path": path, "content_reference": ref})
    assert result.status == "failed"
    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("swallow", [False, True])
async def test_cancelled_reference_resolution_never_writes(tmp_path: Path, swallow: bool) -> None:
    entered = asyncio.Event()

    class BlockingRepository(InMemoryArtifactRepository):
        async def get_many(
            self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
        ) -> tuple[Artifact, ...]:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise
            return await super().get_many(tenant_id, run_id, references)

    artifacts = BlockingRepository()
    ref = await reference(artifacts)
    path = target(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing file")
    task = asyncio.create_task(invoke(harness(backend(tmp_path, run_repository(), artifacts)),
        {"path": "src/result.txt", "content_reference": ref}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert path.read_bytes() == b"existing file"


@pytest.mark.parametrize("drift", ["permissions", "session"])
async def test_scope_change_during_reference_load_cannot_write(tmp_path: Path, drift: str) -> None:
    runs = run_repository()

    class ChangingRepository(InMemoryArtifactRepository):
        async def get_many(
            self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
        ) -> tuple[Artifact, ...]:
            artifacts = await super().get_many(tenant_id, run_id, references)
            record = runs.records[(TENANT, RUN)]
            route = dict(record.routing_decision or {})
            route.update({"requested_permissions": ["workspace.read"]} if drift == "permissions"
                         else {"workspace_session_id": "changed-session"})
            runs.records[(TENANT, RUN)] = replace(record, routing_decision=route)
            return artifacts

    artifacts = ChangingRepository()
    ref = await reference(artifacts)
    result = await invoke(harness(backend(tmp_path, runs, artifacts)),
                          {"path": "src/result.txt", "content_reference": ref})
    assert result.status == "failed"
    assert not (tmp_path / "workspaces").exists()


async def test_reference_cannot_override_original_protected_contract(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    invocation, source = source_tree(root)
    selection = ManagedProjectInput(("src",))
    baseline = await ProjectBaselineStore(root).capture_once(invocation, selection)
    runs = run_repository(
        project_input_contract_version=1, project_input=selection.to_payload(),
        project_input_preparation={"schema_version": 1, "phase": "observed",
                                   "reference": baseline.to_payload()},
    )
    artifacts = InMemoryArtifactRepository()
    ref = await reference(artifacts, "replacement content\n")
    gateway = harness(backend(tmp_path, runs, artifacts))
    protected = source / "tests/original.js"
    before = protected.read_bytes()
    denied = await invoke(gateway, {"path": "tests/original.js", "content_reference": ref})
    assert denied.status == "failed"
    assert protected.read_bytes() == before
    allowed = await invoke(gateway, {"path": "src/index.js", "content_reference": ref})
    assert allowed.status == "succeeded"
    assert (source / "src/index.js").read_bytes() == b"replacement content\n"


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.VIEWER])
async def test_default_stack_passes_repository_and_still_applies_real_policy(
    tmp_path: Path, role: Role,
) -> None:
    artifacts = ReadTrackingRepository()
    ref = await reference(artifacts, "default stack text\n")
    stack = build_runtime_capability_stack(
        tenant_id=TENANT, run_repository=run_repository(), skill_store_dir=tmp_path / "skills",
        workspace_root=None, project_workspace_dir=tmp_path / "workspaces",
        artifact_repository=artifacts,
    )
    result = await invoke(stack.harness_tool_gateway,
                          {"path": "src/result.txt", "content_reference": ref}, role=role)
    if role is Role.OPERATOR:
        assert result.status == "succeeded"
        assert target(tmp_path).read_bytes() == b"default stack text\n"
    else:
        assert result.status == "failed"
        assert artifacts.reads == []
        assert not (tmp_path / "workspaces").exists()


def test_injected_repository_manifest_advertises_mutually_exclusive_content_sources(tmp_path: Path) -> None:
    runtime = backend(tmp_path, run_repository(), InMemoryArtifactRepository())
    items = cast(tuple[Mapping[str, JsonValue], ...], runtime.capability_manifest(TENANT)["capabilities"])
    item = next(item for item in items if item["id"] == "workspace.write_text")
    schema = cast(Mapping[str, object], item["input_schema"])
    validator = Draft202012Validator(json.loads(json.dumps(schema)))
    ref = {"version": 1, "content_sha256": "0" * 64, "total_bytes": 4,
           "segments": [{"id": str(uuid4()), "sha256": "0" * 64}]}
    assert validator.is_valid({"path": "a.txt", "content": "text"})
    assert validator.is_valid({"path": "a.txt", "content_reference": ref})
    assert not validator.is_valid({"path": "a.txt"})
    assert not validator.is_valid({"path": "a.txt", "content": "text", "content_reference": ref})
    assert not validator.is_valid({"path": "a.txt", "content_reference": ref, "extra": True})
