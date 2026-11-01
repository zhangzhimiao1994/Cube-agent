"""Dynamic generated Python consumers; synthetic IO is never product/native proof."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Unpack
from uuid import UUID

import pytest

from agent_hub.capabilities import python_input_preparation as prep
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectContext,
    GeneratedProjectService,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfileRequest,
    PythonProjectProfileResponse,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerResponse,
    PythonProjectProbeInvocation,
)
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionDescriptor
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
)
from tests.unit.capabilities import test_python_resolution_preparation as preparation_fixture
from tests.unit.capabilities.dynamic_resolution_typed_fixture import (
    CASArgs,
    FlowRepository,
    InspectArgs,
    PublishArgs,
    RejectEffects,
    ResolveArgs,
    RootState,
    draft_case,
    mapping,
    mutable,
    observation,
    present,
    strings,
    text,
)
from tests.unit.capabilities.test_python_dependency_resolution import resolve
from tests.unit.capabilities.test_python_resolution_preparation import Live
from tests.unit.skills import test_python_registry_read as registry

offline_network = registry.offline_network
live_fixture = preparation_fixture.live


def manifest(runtime: str="external-tool>=1", build: str="setuptools>=1") -> bytes:
    return ("[project]\nname='real-app'\nversion='1.0'\ndependencies=['" + runtime + "']\n"
            "[build-system]\nrequires=['" + build + "','wheel>=1']\n"
            "build-backend='setuptools.build_meta'\n").encode()


def test_declared_needs_are_generic_data_stable_across_unrelated_control_edits() -> None:
    needs = prep.dynamic_python_needs(manifest())
    assert tuple(n.requirement for n in needs) == ("external-tool>=1", "setuptools>=1", "wheel>=1")
    assert all(len(n.evidence_sha256) == 64 for n in needs)
    assert needs == prep.dynamic_python_needs(manifest() + b"# formatting changed\n")
    assert needs != prep.dynamic_python_needs(manifest(runtime="external-tool>=2"))


@pytest.mark.parametrize("requirement", ["demo @ https://elsewhere.example/a.whl", "../local", "--index-url=x"])
def test_declared_needs_cannot_authorize_arbitrary_input_sources(requirement: str) -> None:
    with pytest.raises(ValueError):
        prep.dynamic_python_needs(manifest(runtime=requirement))


async def test_resolved_pins_come_from_real_wheel_closure_not_model_hashes() -> None:
    wire, bodies = registry.Wire(), {}
    for name, deps in [("external-tool", ("helper>=1",)), ("helper", ())]:
        row, raw = wire.release(name, deps=deps)
        bodies[row["filename"]] = raw
    descriptor = await resolve(wire, bodies, ["external-tool>=1"])
    pins = prep.dynamic_python_pins(descriptor.to_payload())
    assert pins == "".join(
        f"{w.candidate.download.name}=={w.candidate.download.version} --hash=sha256:{w.candidate.download.sha256}\n"
        for w in descriptor.wheels
    )
    assert len(pins.splitlines()) == len(descriptor.wheels)
    assert "helper==1.0" in pins and not hasattr(pins, "install_allowed")
    tampered = descriptor.to_payload()
    wheels = tampered["wheels"]
    assert isinstance(wheels, (list, tuple))
    mutable(mutable(mutable(wheels[0])["metadata"])["wheel"])["sha256"] = "f" * 64
    with pytest.raises(ValueError):
        prep.dynamic_python_pins(tampered)
    assert descriptor.sha256 != replace(descriptor, scope=replace(descriptor.scope, source_sha256="f" * 64)).sha256


async def test_unknown_resolution_never_resolves_freezes_publishes_or_installs() -> None:
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from tests.unit.skills.test_python_resolution_store import claim

    calls = []
    class Client:
        async def inspect(self, **kwargs: Unpack[InspectArgs]) -> ResolutionStatus:
            calls.append("inspect")
            return ResolutionStatus(state="unknown", key="a" * 64)
        async def resolve(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("UNKNOWN cannot resolve again")
        async def freeze(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("UNKNOWN cannot freeze")
        async def publish(self, *args: object, **kwargs: Unpack[PublishArgs]) -> ResolutionStatus:
            raise AssertionError("UNKNOWN cannot publish")
    flow = prep.DynamicPythonResolutionFlow(Client())
    with pytest.raises(ValueError, match="inspect_only"):
        await flow.prepare(needs=prep.dynamic_python_needs(manifest()),
                           environment=registry.environment(), planning=claim().planning)
    assert calls == ["inspect"]


async def test_dynamic_resolution_uses_existing_preparation_exact_approval_fences(live_fixture: Live) -> None:
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.python_resolution_preparation import (
        PythonResolutionPreparation,
        ResolutionApprovalRequired,
    )
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    # Reuse actual typed worker witness setup; no new fake authority or Root body.
    live = live_fixture
    sent, approvals = [], []
    async def current() -> RunRecord:
        return live.row
    async def approved(request: CapabilityRequest) -> bool:
        approvals.append(request)
        return False
    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        sent.append(request.action)
        return ResolutionStatus(state="absent", key=request.key)
    client = PythonResolutionPreparation(current=current, approved=approved, transport=transport)
    flow = prep.DynamicPythonResolutionFlow(client)
    with (bind_execution_authority(live.context, worker_id=present(live.row.worker_id), actor="implementer"),
          pytest.raises(ResolutionApprovalRequired)):
        await flow.prepare(needs=prep.dynamic_python_needs(manifest()),
                           environment=registry.environment(), planning=live.planning)
    assert sent == ["inspect_python_resolution"]
    assert len(approvals) == 1 and approvals[0].arguments["install_allowed"] is False
    assert approvals[0].resource.startswith("workspace/current/project-resolution/python/")
    assert "lease_token" not in str(approvals[0].arguments)


async def test_actual_public_serialization_never_includes_private_plan_or_worker_token() -> None:
    import json
    from uuid import UUID

    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from agent_hub.skills.sandbox.python_resolution_store import freeze_plan
    from tests.unit.skills.test_python_resolution_store import claim, data

    descriptor, _ = await data()
    original = claim().model_copy(update={"lease_token": UUID("739b880b-110e-45b3-87a6-70ed74449521")})
    descriptor = replace(descriptor, scope=original.resolution_scope())
    plan = freeze_plan(descriptor, original=original, current=original, overlay_sha256="e" * 64)
    private = ResolutionStatus(state="frozen", key=plan.resolution_key,
                               descriptor=descriptor.to_payload(), plan=plan)
    assert str(original.lease_token) in private.model_dump_json()
    public = prep.dynamic_python_public_data(private)
    raw = json.dumps(public, sort_keys=True)
    assert str(original.lease_token) not in raw
    def check(value: object) -> None:
        if isinstance(value, dict):
            assert not {"plan", "claim", "lease_token", "worker_id", "witness", "original_deadline", "monotonic_deadline"} & value.keys()
            for item in value.values():
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check(item)
    check(public)
    assert set(public) == {"kind", "needs", "pins", "provenance", "descriptor_sha256", "data_ref"}
    assert public["descriptor_sha256"] == descriptor.sha256
    assert public["data_ref"] == "python-resolution:" + plan.resolution_key
    assert "artifact_ref" not in public  # no unregistered file artifact is fabricated



def test_profile_source_request_is_optional_and_response_must_bind_it() -> None:
    from agent_hub.skills.sandbox.python_project_profile import (
        PythonProjectProfileRequest,
        PythonProjectProfileResponse,
        PythonProjectProfileScope,
    )
    from tests.unit.capabilities.test_python_catalog_gateway import configured_profile
    from tests.unit.skills.sandbox.test_python_project_profile import invocation

    base = PythonProjectProfileRequest(invocation=invocation())
    assert base.expected_source_sha256 is None
    request = PythonProjectProfileRequest(invocation=invocation(), expected_source_sha256="a" * 64)
    profile = configured_profile(large=False)
    response = PythonProjectProfileResponse(scope=PythonProjectProfileScope.for_invocation(request.invocation),
                                           ok=True, profile=profile)
    response.require_for(base)
    with pytest.raises(ValueError):
        response.require_for(request)
    bound = profile.model_copy(update={"source_sha256": "a" * 64,
                                      "target_environment": registry.environment()})
    response = response.model_copy(update={"profile": bound})
    response.require_for(request)
    with pytest.raises(ValueError):
        response.require_for(request.model_copy(update={"expected_source_sha256": "b" * 64}))


async def test_actual_project_inspect_passes_measured_generated_source_without_a_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import python_project_profile as profile_api
    from agent_hub.skills.sandbox.project import project_source_fingerprint
    from tests.unit.capabilities.test_python_catalog_gateway import catalog_case

    case = catalog_case(tmp_path, monkeypatch, large=False)
    present(case.db.run.routing_decision).update({"generated_project_contract_version": 1,
                                        "python_dependency_preflight_version": 1})
    work = (present(case.backend._project_workspace_dir) / str(case.db.run.tenant_id) / "projects" /
            "catalog-project" / "sessions" / "catalog-session")
    work.mkdir(parents=True)
    (work / "pyproject.toml").write_bytes(manifest())
    actual_broker = profile_api.request_python_project_profile_broker
    async def broker(request: PythonProjectProfileRequest, *, socket_path: Path | None = None) -> PythonProjectProfileResponse:
        reply = await actual_broker(request, socket_path=socket_path)
        profile = present(reply.profile).model_copy(update={"source_sha256": request.expected_source_sha256,
                                                   "target_environment": registry.environment()})
        return reply.model_copy(update={"profile": profile})
    monkeypatch.setattr(profile_api, "request_python_project_profile_broker", broker)
    result = await case.inspect()
    assert result.status == "succeeded", result.failure_reason
    assert len(case.broker_calls) == 1
    request = case.broker_calls[0]
    expected = await project_source_fingerprint(present(case.backend._project_workspace_dir), request.invocation)
    assert request.expected_source_sha256 == expected
    assert case.db.approvals == {} and request.invocation.install_allowed is False


@pytest.mark.parametrize("publication_unknown", [False, True], ids=["await_exact_approval", "unknown_inspect_only"])
async def test_publication_resume_distinguishes_unstarted_from_unknown(publication_unknown: bool) -> None:
    from agent_hub.capabilities.python_resolution_preparation import (
        ResolutionApprovalRequired,
        resolution_action,
    )
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from agent_hub.skills.sandbox.python_resolution_store import freeze_plan
    from tests.unit.skills.test_python_resolution_store import claim, data

    descriptor, _ = await data()
    original = claim()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    needs = descriptor.needs
    env = dict(descriptor.environment)
    plan = freeze_plan(descriptor, original=original, current=original, overlay_sha256="e" * 64)
    calls, publication_calls = [], 0
    class Client:
        async def inspect(self, **kwargs: Unpack[InspectArgs]) -> ResolutionStatus:
            calls.append("inspect_plan" if kwargs.get("plan") else "inspect")
            if kwargs.get("plan"):
                return ResolutionStatus(state="unknown" if publication_unknown and publication_calls else "frozen",
                                        key=plan.resolution_key, plan=plan)
            return ResolutionStatus(state="resolved", key=plan.resolution_key, descriptor=descriptor.to_payload())
        async def resolve(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("known descriptor must not network again")
        async def freeze(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            calls.append("freeze")
            return ResolutionStatus(state="frozen", key=plan.resolution_key, descriptor=descriptor.to_payload(), plan=plan)
        async def publish(self, exact_plan: ResolutionPlan, **kwargs: Unpack[PublishArgs]) -> ResolutionStatus:
            nonlocal publication_calls
            assert exact_plan == plan
            publication_calls += 1
            if publication_calls == 1:
                if publication_unknown:
                    raise TimeoutError("synthetic lost publication receipt")
                raise ResolutionApprovalRequired(resolution_action(original, needs, env, plan=plan))
            return ResolutionStatus(state="published", key=plan.resolution_key, plan=plan)
    flow = prep.DynamicPythonResolutionFlow(Client())
    with pytest.raises(TimeoutError if publication_unknown else ResolutionApprovalRequired):
        await flow.prepare(needs=needs, environment=env, planning=original.planning)
    before = list(calls)
    if publication_unknown:
        with pytest.raises(ValueError, match="inspect_only"):
            await flow.prepare(needs=needs, environment=env, planning=original.planning)
        assert publication_calls == 1
    else:
        result = await flow.prepare(needs=needs, environment=env, planning=original.planning)
        assert result.state == "published" and publication_calls == 2
    assert calls == before + ["inspect_plan"]


async def test_resolved_data_is_available_before_freeze_and_legal_exact_edit_reuses_it() -> None:
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from tests.unit.skills.test_python_resolution_store import claim, data

    descriptor, _ = await data()
    original = claim()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    calls = []
    class Client:
        async def inspect(self, **kwargs: Unpack[InspectArgs]) -> ResolutionStatus:
            calls.append(kwargs["needs"])
            return ResolutionStatus(state="resolved", key="a" * 64, descriptor=descriptor.to_payload())
        async def resolve(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("verified bytes must not network again")
        async def freeze(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("data discovery must not freeze controls")
        async def publish(self, *args: object, **kwargs: Unpack[PublishArgs]) -> ResolutionStatus:
            raise AssertionError("data discovery must not publish/install")
    flow = prep.DynamicPythonResolutionFlow(Client())
    await flow.resolve_data(needs=descriptor.needs, environment=dict(descriptor.environment), planning=original.planning)
    exact = (registry.api()[0].DependencyNeed("demo==1.0", "b" * 64),)
    await flow.resolve_data(needs=exact, environment=dict(descriptor.environment), planning=original.planning)
    assert calls == [descriptor.needs, descriptor.needs]
    public = flow.public_data()
    assert public["descriptor_sha256"] == descriptor.sha256 and text(public["data_ref"]).endswith("a" * 64)
    with pytest.raises(ValueError, match="declaration_changed"):
        await flow.resolve_data(needs=(registry.api()[0].DependencyNeed("demo==99", "c" * 64),),
                                environment=dict(descriptor.environment), planning=original.planning)
    assert len(calls) == 2



def generated_dynamic_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GeneratedProjectService, FlowRepository, TaskContext, str]:
    from datetime import UTC, datetime

    from agent_hub.domain.runs import TaskMode
    from agent_hub.runs.repository import RunRecord
    from agent_hub.runtime.contracts import TaskContext
    from agent_hub.skills.sandbox import python_project_profile as profile_api
    from tests.unit.capabilities.test_python_catalog_gateway import configured_profile
    from tests.unit.capabilities.test_scoped_read import project_path

    service, repo, corrected = draft_case(tmp_path)
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    service.configure_resolution_artifacts(InMemoryArtifactRepository())
    row = repo.row
    present(row.routing_decision).update({"python_dependency_preflight_version": 1,
                                 "python_dependency_preflight_submission_version": 1})
    repo.row = RunRecord(id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id, actor_role=row.actor_role,
        request=row.request, mode=TaskMode.DIRECT, status=row.status, version=1, created_at=datetime.now(UTC),
        worker_id=row.worker_id, worker_lease_token=row.worker_lease_token,
        worker_lease_expires_at=row.worker_lease_expires_at, routing_decision=row.routing_decision)
    async def typed_get(tenant: UUID, run: UUID) -> RunRecord:
        from agent_hub.capabilities.generated_project_service import validate_generated_preparation
        assert (tenant, run) == (repo.row.tenant_id, repo.row.id)
        if 'generated_project_preparation' in present(repo.row.routing_decision):
            repo.row = replace(repo.row, routing_decision={**present(repo.row.routing_decision),
                'generated_project_preparation': validate_generated_preparation(present(repo.row.routing_decision)['generated_project_preparation'])})
        return repo.row
    repo.get = typed_get
    service.repository = repo
    ctx = TaskContext(run_id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id, actor_role=row.actor_role,
        execution_token=row.worker_lease_token, mode=TaskMode.DIRECT, request=row.request,
        routing_decision=_freeze_object(present(row.routing_decision), name="dynamic fixture routing"), timeout_seconds=120)
    manifest_path = service.root / project_path() / "pyproject.toml"
    manifest_path.write_bytes(manifest_path.read_bytes().replace(b"demo-dep==1.0", b"demo-dep>=1"))
    profile = configured_profile(large=False).model_copy(update={"root_config_sha256": "8" * 64})
    async def broker(request: PythonProjectProfileRequest, *, socket_path: Path | None = None) -> PythonProjectProfileResponse:
        selected = profile
        if request.expected_source_sha256 is not None:
            selected = profile.model_copy(update={"source_sha256": request.expected_source_sha256,
                                                   "target_environment": registry.environment()})
        return profile_api.PythonProjectProfileResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(request.invocation), ok=True, profile=selected)
    monkeypatch.setattr(profile_api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(profile_api, "request_python_project_profile_broker", broker)
    return service, repo, ctx, corrected


@pytest.fixture
def dynamic_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GeneratedProjectService, FlowRepository, TaskContext, str]:
    return generated_dynamic_case(tmp_path, monkeypatch)


async def test_actual_generated_prepare_requests_specific_resolution_before_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.generated_project_service import (
        PythonMetadataPreparationApprovalRequired,
    )
    from agent_hub.skills.sandbox import python_resolution_worker as wire
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    service, repo, ctx, _ = dynamic_case
    actions = []
    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        actions.append(request.action)
        assert request.claim.lease_token == ctx.execution_token
        return ResolutionStatus(state="absent", key=request.key)
    async def approved(*args: object) -> bool:
        return False
    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, "request_resolution", transport)
    with (bind_execution_authority(ctx, worker_id=present(repo.row.worker_id), actor="engineer"),
          pytest.raises(PythonMetadataPreparationApprovalRequired) as failure):
        await service.prepare(ctx.tenant_id, ctx.run_id, "engineer", user_id=present(ctx.actor_id))
    assert failure.value.approval_request.arguments["purpose"] == "python_dependency_resolution"
    assert actions == ["inspect_python_resolution"]
    assert "generated_project_preparation" not in present(repo.row.routing_decision) and repo.updates == 0



async def descriptor_with_build_inputs() -> ResolutionDescriptor:
    wire, bodies = registry.Wire(), {}
    for name, version, deps in [("demo-dep", "1.0", ("demo-helper>=1",)),
                                ("demo-helper", "1.0", ()), ("setuptools", "80.9.0", ()),
                                ("wheel", "0.45.1", ())]:
        row, raw = wire.release(name, version=version, deps=deps)
        bodies[row["filename"]] = raw
    return await resolve(wire, bodies, ["demo-dep>=1", "setuptools==80.9.0", "wheel==0.45.1"])


async def test_actual_manifest_lock_closure_must_match_verified_data_before_fresh_freeze() -> None:
    from tests.unit.capabilities.test_python_project_recipe import MANIFEST

    descriptor = await descriptor_with_build_inputs()
    manifest_bytes = MANIFEST.replace(b"packaging==25.0", b"demo-dep==1.0")
    lock = prep.dynamic_python_pins(descriptor.to_payload()).encode()
    prep.validate_dynamic_python_controls(manifest_bytes, lock, descriptor.to_payload())
    entry = descriptor.wheels[0].candidate.download
    with pytest.raises(ValueError, match="resolution_declaration_changed"):
        prep.validate_dynamic_python_controls(manifest_bytes,
            lock.replace(entry.sha256.encode(), b"f" * 64), descriptor.to_payload())
    missing = b"".join(line + b"\n" for line in lock.splitlines() if not line.startswith(b"demo-helper=="))
    with pytest.raises(ValueError, match="resolution_declaration_changed"):
        prep.validate_dynamic_python_controls(manifest_bytes, missing, descriptor.to_payload())


async def generated_projection_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str], *, cold: bool=False) -> tuple[RuntimeCapabilityGateway, FlowRepository, TaskContext, ResolutionDescriptor, ArtifactRepository, Mapping[str, object]]:
    import json

    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.runtime.artifacts import ArtifactReference
    from agent_hub.skills.sandbox import python_resolution_worker as wire
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    service, repo, ctx, _ = dynamic_case
    descriptor = await descriptor_with_build_inputs()
    saved: ResolutionDescriptor | None = None
    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        nonlocal saved
        if request.action == "inspect_python_resolution":
            return ResolutionStatus(state="absent" if saved is None else "resolved", key=request.key,
                                    descriptor=saved.to_payload() if saved else None)
        assert request.action == "resolve_python_inputs"
        saved = replace(descriptor, scope=request.claim.resolution_scope(), needs=request.needs)
        return ResolutionStatus(state="resolved", key=request.key, descriptor=saved.to_payload())
    async def approved(*args: object) -> bool:
        return True  # Synthetic exact approval store boundary; no external operation occurs.
    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, "request_resolution", transport)
    with (bind_execution_authority(ctx, worker_id=present(repo.row.worker_id), actor="engineer"),
          pytest.raises(GeneratedProjectDraftRejected)):
        await service.prepare(ctx.tenant_id, ctx.run_id, "engineer", user_id=present(ctx.actor_id))
    assert repo.updates == 0 and saved is not None
    artifacts = present(service._resolution_journal).artifacts
    if cold:
        from agent_hub.capabilities.generated_project_service import GeneratedProjectService
        service = GeneratedProjectService(root=service.root, repository=repo)
        service.configure_resolution_artifacts(artifacts)
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", run_repository=repo,
                                       project_workspace_dir=service.root, artifact_repository=artifacts)
    present(gateway._project_verification).generated = service
    public = await gateway._python_build_profile(ctx.tenant_id, ctx.run_id)
    projected = mapping(public["dependency_resolution"])
    assert "安装" in text(projected["summary"]) and "pins" not in projected
    reference = mapping(projected["artifact_ref"])
    artifact, = await artifacts.get_many(ctx.tenant_id, ctx.run_id,
        (ArtifactReference(UUID(text(reference["id"])), text(reference["sha256"])),))
    assert artifact.type == "python_dependency_resolution"
    assert artifact.content["descriptor_sha256"] == saved.sha256
    assert projected["data"] == artifact.content  # machine tool data, not a manual JSON handoff
    assert str(ctx.execution_token) not in json.dumps(artifact.to_payload())
    assert "generated_project_preparation" not in present(repo.row.routing_decision)
    return gateway, repo, ctx, saved, artifacts, projected


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
@pytest.mark.parametrize("scope_drift", ["permissions", "request"])
async def test_actual_generated_data_registers_safe_artifact_and_human_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold: bool, scope_drift: str, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    gateway, repo, ctx, saved, _artifacts, projected = await generated_projection_case(
        tmp_path, monkeypatch, dynamic_case, cold=cold)
    service = present(gateway._project_verification).generated
    # The source is allowed to change while the original needs remain data.
    # Inspect must not re-resolve, freeze or demand the old descriptor source SHA.
    from tests.unit.capabilities.test_scoped_read import project_path
    work = service.root / project_path()
    (work / "pyproject.toml").write_bytes((work / "pyproject.toml").read_bytes().replace(b"demo-dep>=1", b"demo-dep==1.0"))
    (work / "requirements.lock").write_text(prep.dynamic_python_pins(saved.to_payload()), encoding="utf-8")
    legal = await gateway._python_build_profile(ctx.tenant_id, ctx.run_id)
    assert legal["dependency_resolution"] == projected
    if scope_drift == "permissions":
        repo.row = replace(repo.row, routing_decision={**present(repo.row.routing_decision),
            "requested_permissions": ["workspace.read"]})
    else:
        repo.row = replace(repo.row, request="a different current request")
    with pytest.raises((ValueError, RuntimeError)):
        await gateway._python_build_profile(ctx.tenant_id, ctx.run_id)


async def test_projection_rechecks_current_scope_after_artifact_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    gateway, repo, ctx, _saved, artifacts, _projected = await generated_projection_case(tmp_path, monkeypatch, dynamic_case)
    put, get_many = artifacts.put, artifacts.get_many
    armed, writes = False, 0
    async def put_then_arm(tenant: UUID, run: UUID, artifact: Artifact, *, write_id: UUID | None = None) -> None:
        nonlocal armed, writes
        await put(tenant, run, artifact, write_id=write_id)
        armed, writes = True, writes + 1
    async def get_then_drift(tenant: UUID, run: UUID, references: tuple[ArtifactReference, ...]) -> tuple[Artifact, ...]:
        nonlocal armed
        result = await get_many(tenant, run, references)
        if armed:
            armed = False
            repo.row = replace(repo.row, request='request changed during artifact readback')
        return result
    monkeypatch.setattr(artifacts, 'put', put_then_arm)
    monkeypatch.setattr(artifacts, 'get_many', get_then_drift)
    with pytest.raises((ValueError, RuntimeError)):
        await gateway._python_build_profile(ctx.tenant_id, ctx.run_id)
    assert writes == 1  # Only immutable data storage; never a grant or a new resolve.


@pytest.mark.parametrize("defect", [None, "lease", "read_only", "private_field", "hash"])
async def test_actual_default_policy_requires_specific_resolution_approval(defect: str | None, live_fixture: Live) -> None:
    from types import SimpleNamespace

    from agent_hub.auth.models import Role
    from agent_hub.capabilities.defaults import _project_metadata_preparation_effect
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.python_resolution_preparation import (
        PythonResolutionPreparation,
        resolution_action,
    )
    from agent_hub.capabilities.types import PolicyEffect

    case = live_fixture
    async def current() -> RunRecord:
        return case.row
    client = PythonResolutionPreparation(current=current, approved=None)
    with bind_execution_authority(case.context, worker_id=present(case.row.worker_id), actor="implementer"):
        claim = await client._claim(case.planning)
        request = resolution_action(claim, prep.dynamic_python_needs(manifest()), registry.environment())
        if defect:
            args = dict(request.arguments)
            if defect == "lease":
                from uuid import UUID
                case.row = replace(case.row, worker_lease_token=UUID(int=999))
            elif defect == "private_field":
                args["lease_token"] = str(claim.lease_token)
            elif defect == "hash":
                args["source_sha256"] = "invalid"
            elif defect == "read_only":
                present(case.row.routing_decision)["requested_permissions"] = ["workspace.read"]
            request = request.model_copy(update={"arguments": args})
        effect = await _project_metadata_preparation_effect(SimpleNamespace(get=lambda *args: current()),
                                                            request, role=Role.OPERATOR)
    assert effect is (PolicyEffect.REQUIRE_APPROVAL if defect is None else PolicyEffect.DENY)


async def test_captured_supply_requires_the_same_preflight_effective_catalog(tmp_path: Path) -> None:
    from agent_hub.capabilities import python_generated_service as generated_api
    from agent_hub.capabilities.python_generated_service import _probe
    from tests.unit.capabilities.test_python_generated_service import configured

    _, _, _, _, supply, context = await configured(generated_api, tmp_path)
    probe = _probe(context, "test_supply_")
    plan = supply.plan_value(probe)
    prep.validate_supply_declaration(plan, probe, context.stored, expected_catalog_sha256="d" * 64)
    with pytest.raises(ValueError, match="plan_changed"):
        prep.validate_supply_declaration(plan, probe, context.stored, expected_catalog_sha256="e" * 64)


async def test_fresh_flow_recovers_immutable_scoped_artifact_and_unknown_is_inspect_only() -> None:
    import json
    from types import SimpleNamespace
    from uuid import UUID

    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from tests.unit.skills.test_python_resolution_store import claim

    original = claim()
    scope = original.planning.scope.model_dump(mode="json")
    scope.update(root_sha256=original.planning.expected_root_config_sha256,
                 catalog_sha256=original.planning.expected_catalog_sha256,
                 original_deadline=original.original_deadline.isoformat())
    artifacts = InMemoryArtifactRepository()
    refs = []
    journal = prep.DynamicResolutionJournal(artifacts)
    needs = prep.dynamic_python_needs(manifest())
    reference = await journal.save(scope, needs=needs, environment=registry.environment(),
                                   resolution_key="a" * 64, pending_plan_sha256="b" * 64)
    refs.append(reference)
    recovered_context = SimpleNamespace(scope=dict(scope), artifact_refs=tuple(refs),
        execution_token=UUID("7b3faf79-a37d-4c27-b365-34d9c5991859"))
    loaded = await prep.DynamicResolutionJournal(artifacts).load(recovered_context.scope)
    assert loaded is not None and "lease_token" not in json.dumps(loaded, default=dict)
    calls = []
    class Client:
        async def inspect(self, **kwargs: Unpack[InspectArgs]) -> ResolutionStatus:
            calls.append(kwargs)
            return ResolutionStatus(state="unknown", key="a" * 64)
        async def resolve(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("cold recovery must never network again")
        async def freeze(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise AssertionError("UNKNOWN must not freeze")
        async def publish(self, *args: object, **kwargs: Unpack[PublishArgs]) -> ResolutionStatus:
            raise AssertionError("UNKNOWN must not publish")
    fresh = prep.DynamicPythonResolutionFlow(Client())
    fresh.restore(loaded)
    with pytest.raises(ValueError, match="inspect_only"):
        await fresh.resolve_data(needs=(registry.api()[0].DependencyNeed("external-tool==1.0", "c" * 64),),
                                 environment=registry.environment(), planning=original.planning)
    assert len(calls) == 1 and calls[0]["needs"] == needs
    assert calls[0]["expected_plan_sha256"] == "b" * 64
    assert recovered_context.execution_token == UUID("7b3faf79-a37d-4c27-b365-34d9c5991859")
    foreign = dict(scope, run_id=str(UUID(int=999)))
    assert await prep.DynamicResolutionJournal(artifacts).load(foreign) is None


async def test_production_preflight_uses_source_bound_profile_and_same_overlay_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.capabilities.generated_project_service import _inspect_dependencies
    from agent_hub.skills.sandbox import python_dependency_protocol as closure
    from agent_hub.skills.sandbox import python_project_profile as profiles
    from tests.unit.capabilities.test_python_catalog_gateway import configured_profile
    from tests.unit.skills.sandbox.test_python_project_profile import invocation

    calls = []
    async def profile_wire(request: PythonProjectProfileRequest, *, socket_path: Path | None = None) -> PythonProjectProfileResponse:
        assert request.expected_source_sha256 == "a" * 64
        calls.append("profile")
        profile = configured_profile(large=False).model_copy(update={"catalog_sha256": "d" * 64,
            "source_sha256": "a" * 64, "target_environment": registry.environment()})
        return profiles.PythonProjectProfileResponse(
            scope=profiles.PythonProjectProfileScope.for_invocation(request.invocation), ok=True, profile=profile)
    async def closure_wire(request: closure.PythonDependencyClosureRequest, **kwargs: object) -> closure.PythonDependencyClosureResponse:
        assert request.expected_catalog_sha256 == "d" * 64 and request.expected_source_sha256 == "a" * 64
        calls.append("closure")
        return closure.PythonDependencyClosureResponse(
            scope=profiles.PythonProjectProfileScope.for_invocation(request.invocation), ok=True,
            observation=closure.PythonDependencyClosureObservation.model_validate(observation("complete"), strict=True))
    monkeypatch.setattr(profiles, "_systemd_supported", lambda: True)
    monkeypatch.setattr(closure, "_systemd_supported", lambda: True)
    monkeypatch.setattr(profiles, "request_python_project_profile_broker", profile_wire)
    monkeypatch.setattr(closure, "request_python_dependency_closure_broker", closure_wire)
    result = await _inspect_dependencies(invocation().model_copy(update={"sandbox_profile": "restricted"}), "a" * 64)
    assert result.catalog_sha256 == "d" * 64 and calls == ["profile", "closure"]


async def completed_generated_dynamic_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str], *, warm: bool=False) -> tuple[GeneratedProjectService, FlowRepository, TaskContext, GeneratedProjectContext, RootState, ArtifactRepository]:
    import hashlib
    import json
    from uuid import uuid4

    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.generated_project_service import (
        GeneratedProjectDraftRejected,
        GeneratedProjectService,
        validate_generated_preparation,
        validate_generated_transition,
    )
    from agent_hub.capabilities.python_dependency_closure import check_python_dependency_closure
    from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
    from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
    from agent_hub.skills.sandbox import python_resolution_worker as wire
    from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
    from agent_hub.skills.sandbox.python_dependency_protocol import (
        PythonDependencyClosureObservation,
    )
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from agent_hub.skills.sandbox.python_resolution_store import freeze_plan
    from tests.unit.capabilities.test_scoped_read import project_path

    service, repo, ctx, _ = dynamic_case
    artifacts = present(service._resolution_journal).artifacts
    descriptor = await descriptor_with_build_inputs()
    root_state = RootState()
    calls = []
    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        if request.action == "inspect_python_resolution":
            if request.expected_plan_sha256:
                return ResolutionStatus(state="published" if root_state.published else "frozen",
                                        key=request.key, plan=present(root_state.plan))
            return ResolutionStatus(state="resolved" if root_state.descriptor else "absent", key=request.key,
                                    descriptor=present(root_state.descriptor).to_payload() if root_state.descriptor else None)
        if request.action == "resolve_python_inputs":
            assert root_state.descriptor is None
            root_state.origin = request.claim
            root_state.descriptor = replace(descriptor, scope=request.claim.resolution_scope(), needs=request.needs)
            return ResolutionStatus(state="resolved", key=request.key, descriptor=present(root_state.descriptor).to_payload())
        if request.action == "freeze_python_resolution":
            plan = freeze_plan(present(root_state.descriptor), original=present(root_state.origin), current=request.claim,
                               overlay_sha256="d" * 64)
            root_state.plan = plan
            return ResolutionStatus(state="frozen", key=request.key, descriptor=present(root_state.descriptor).to_payload(), plan=plan)
        assert request.action == "publish_python_resolution" and request.expected_plan_sha256 == present(root_state.plan).sha256
        root_state.published = True
        return ResolutionStatus(state="published", key=request.key, plan=present(root_state.plan))
    async def approved(*args: object) -> bool:
        return True  # Approval storage boundary only; actual Root grant fences still execute.
    async def cas(tenant: UUID, run: UUID, **kw: Unpack[CASArgs]) -> RunRecord:
        before = present(repo.row.routing_decision).get("generated_project_preparation")
        if kw.get("before_reservation"):
            await kw["before_reservation"]()
        assert kw["lease_token"] == repo.row.worker_lease_token
        assert (None if before is None else validate_generated_preparation(before)) == kw["expected"]
        state = validate_generated_transition(before, kw["replacement"])
        repo.row = replace(repo.row, routing_decision={**present(repo.row.routing_decision),
                           "generated_project_preparation": json.loads(json.dumps(state, default=dict))})
        repo.updates += 1
        return repo.row
    repo.compare_and_set_generated_preparation = cas
    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, "request_resolution", transport)
    with (bind_execution_authority(ctx, worker_id=present(repo.row.worker_id), actor="engineer"),
          pytest.raises(GeneratedProjectDraftRejected)):
        await service.prepare(ctx.tenant_id, ctx.run_id, "engineer", user_id=present(ctx.actor_id))
    assert calls.count("resolve_python_inputs") == 1 and not root_state.published
    origin_source = present(root_state.origin).planning.draft.source_sha256
    work = service.root / project_path()
    (work / "pyproject.toml").write_bytes((work / "pyproject.toml").read_bytes().replace(b"demo-dep>=1", b"demo-dep==1.0"))
    (work / "requirements.lock").write_text(prep.dynamic_python_pins(present(root_state.descriptor).to_payload()), encoding="utf-8")
    token = uuid4()
    repo.row = replace(repo.row, worker_id="recovered-worker", worker_lease_token=token)
    current = ctx.model_copy(update={"execution_token": token})
    index = PythonDependencyMetadataIndex("d" * 64, tuple(w.metadata for w in present(root_state.descriptor).wheels))
    async def inspect(invocation: ProjectInvocation, source: str) -> PythonDependencyClosureObservation:
        manifest_bytes, lock = (work / "pyproject.toml").read_bytes(), (work / "requirements.lock").read_bytes()
        declaration = parse_python_project_declaration(manifest_bytes, lock, max_control_bytes=256 * 1024, max_requirements=128)
        values = observation("complete")
        values.update(source_sha256=source, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                      lock_sha256=hashlib.sha256(lock).hexdigest(), metadata_index_sha256=index.sha256,
                      closure=check_python_dependency_closure(declaration.requirements, index.records,
                                                               strings(values["target_environment"])).to_payload())
        return PythonDependencyClosureObservation.model_validate(values, strict=True)
    cold = service if warm else GeneratedProjectService(root=service.root, repository=repo, dependency_inspector=inspect)
    cold._dependency_inspector = inspect
    cold.configure_resolution_artifacts(artifacts)
    with bind_execution_authority(current, worker_id="recovered-worker", actor="engineer"):
        captured = await cold.prepare(ctx.tenant_id, ctx.run_id, "engineer", user_id=present(ctx.actor_id))
    assert captured is not None
    assert calls.count("resolve_python_inputs") == calls.count("publish_python_resolution") == 1
    assert present(root_state.plan).claim.lease_token == token
    assert present(root_state.plan).claim.original_deadline == present(root_state.origin).original_deadline
    assert captured.state["phase"] == "captured" and repo.updates == 2
    assert captured.stored.reference.input_source_sha256 == present(root_state.plan).claim.planning.draft.source_sha256
    assert captured.stored.reference.input_source_sha256 != origin_source
    assert {"pyproject.toml", "requirements.lock", "tests/test_billing.py"} <= captured.stored.contract.protected_files.keys()
    assert "python_recipe" not in captured.state  # Capture/publication are not installation receipts.
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", project_workspace_dir=service.root, run_repository=repo)
    with pytest.raises(RuntimeCapabilityError, match="original project checks are protected"):
        await gateway.execute(tenant_id=ctx.tenant_id, run_id=ctx.run_id, actor="engineer",
                              name="workspace.write_text", arguments={"path": "requirements.lock", "content": "weakened"},
                              idempotency_key="must-remain-frozen")

    return cold, repo, current, captured, root_state, artifacts


async def test_actual_generated_cold_exact_rebind_publishes_and_captures_once_without_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    await completed_generated_dynamic_case(tmp_path, monkeypatch, dynamic_case)


async def test_dynamic_cached_inputs_still_require_supply_then_recipe_exact_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from agent_hub.capabilities.project_verification import (
        ProjectVerificationError,
        ProjectVerificationService,
    )
    from agent_hub.capabilities.python_generated_service import (
        PythonGeneratedProjectService,
        PythonGeneratedRecipeApprovalRequired,
    )
    from agent_hub.capabilities.python_project_input_store import _capture
    from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
    from agent_hub.capabilities.types import CapabilityRequest
    from agent_hub.skills.sandbox.python_input_supply_protocol import (
        PythonInputSupplyPlan,
        PythonInputSupplyReceipt,
        PythonInputSupplyResponse,
        PythonInputSupplyStatus,
    )
    from agent_hub.skills.sandbox.python_project_protocol import (
        PythonEnvironmentReference,
        PythonProjectInputsObservation,
        PythonProjectInvocation,
    )
    from tests.unit.capabilities.test_python_catalog_gateway import configured_profile

    generated, repo, task, context, root_state, _ = await completed_generated_dynamic_case(tmp_path, monkeypatch, dynamic_case)
    inputs = _capture(context.stored, interpreter=PythonInterpreterIdentity.from_payload(configured_profile(large=False).interpreter.model_dump()),
                      wheels=tuple(w.metadata.wheel for w in present(root_state.descriptor).wheels),
                      max_control_bytes=256 * 1024, max_requirements=128)
    run_calls, probes, effects, approvals = [], [], [], set()
    class Executor:
        async def probe(self, invocation: PythonProjectProbeInvocation) -> PythonProjectInputsObservation:
            probes.append(invocation)
            return PythonProjectInputsObservation.model_validate({
                "scope": {name: getattr(invocation, name) for name in ("tenant_id", "run_id", "project_id", "session_id", "original_reference")},
                "declaration": inputs.declaration.to_payload(), "interpreter": inputs.interpreter.to_payload(),
                "wheels": tuple(w.to_payload() for w in inputs.wheels)}, strict=True)
        async def run(self, invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
            run_calls.append(invocation)
            raise ProjectVerificationError("synthetic_stop_at_native_install_boundary")
        async def inspect_environment(self, invocation: PythonProjectInvocation, *, expected_environment: PythonEnvironmentReference) -> PythonEnvironmentReference:
            raise AssertionError("no environment inspection before native install")
    class Supply:
        def plan_value(self, invocation: PythonProjectProbeInvocation) -> PythonInputSupplyPlan:
            return PythonInputSupplyPlan.model_validate({
                "scope": {name: getattr(invocation, name) for name in ("tenant_id", "run_id", "project_id", "session_id", "original_reference")},
                "declaration_sha256": inputs.declaration.sha256, "manifest_sha256": inputs.declaration.manifest_sha256,
                "lock_sha256": inputs.declaration.lock_sha256, "catalog_sha256": "d" * 64,
                "interpreter": inputs.interpreter.to_payload(),
                "artifacts": tuple(
                    {**w.metadata.wheel.to_payload(), "size_bytes": w.candidate.download.size_bytes} for w in present(root_state.descriptor).wheels)}, strict=True)
        async def plan(self, invocation: PythonProjectProbeInvocation) -> PythonInputSupplyResponse:
            return PythonInputSupplyResponse(action="plan_python_project_inputs", execution_id=invocation.execution_id,
                ok=True, plan=self.plan_value(invocation), missing_wheels=())
        async def supply(self, invocation: PythonProjectProbeInvocation, *, expected_plan_sha256: str) -> PythonInputSupplyReceipt:
            plan = self.plan_value(invocation)
            assert plan.sha256 == expected_plan_sha256
            effects.append(invocation)
            return PythonInputSupplyReceipt(plan=plan, supply_execution_id=invocation.execution_id,
                                             published=(), reused=tuple(w.filename for w in inputs.wheels))
        async def inspect(self, invocation: PythonProjectProbeInvocation, *, expected_plan_sha256: str, supply_execution_id: str) -> PythonInputSupplyStatus:
            raise AssertionError("no lost supply receipt in this case")
    async def approved(tenant: UUID, run: UUID, scope: str) -> bool:
        return scope in approvals
    repo.is_capability_approval_scope_approved = approved
    executor = Executor()
    owner = ProjectVerificationService(root=generated.root, repository=repo, executor=None, python_executor=executor)
    service = PythonGeneratedProjectService(owner, generated, executor)
    service.supply_client = Supply()
    request = CapabilityRequest(tenant_id=task.tenant_id, run_id=task.run_id, user_id=present(task.actor_id),
        agent_id="engineer", capability="command", operation="run", resource="workspace/current/project-verification",
        arguments={}, idempotency_key="dynamic-verify")
    action = await service.authorization_request(request, context)
    assert action.resource.startswith("workspace/current/project-inputs/python/")
    with pytest.raises(ProjectVerificationError, match="input_supply_approval_required"):
        await service.verify(context, "engineer")
    assert not effects and not run_calls
    approvals.add(capability_approval_scope(action))
    with pytest.raises(PythonGeneratedRecipeApprovalRequired) as needed:
        await service.verify(context, "engineer")
    assert len(effects) == 1 and not run_calls and all(not p.install_allowed for p in probes)
    assert needed.value.approval_request.resource.startswith("workspace/current/project-verification/python/")
    approvals.add(capability_approval_scope(needed.value.approval_request))
    with pytest.raises(ProjectVerificationError, match="synthetic_stop_at_native_install_boundary"):
        await service.verify(context, "engineer")
    assert len(run_calls) == 1 and run_calls[0].install_allowed is True and len(effects) == 1


async def test_actual_generated_warm_exact_edit_rebinds_current_callbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    await completed_generated_dynamic_case(tmp_path, monkeypatch, dynamic_case, warm=True)


async def test_recovered_resolution_rejects_a_different_root_data_key() -> None:
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus
    from tests.unit.skills.test_python_resolution_store import claim, data

    descriptor, _ = await data()
    class Client(RejectEffects):
        async def inspect(self, **kwargs: Unpack[InspectArgs]) -> ResolutionStatus:
            return ResolutionStatus(state='resolved', key='f' * 64, descriptor=descriptor.to_payload())
    flow = prep.DynamicPythonResolutionFlow(Client())
    flow.restore({'needs': [{'requirement': n.requirement, 'evidence_sha256': n.evidence_sha256} for n in descriptor.needs],
                  'environment': dict(descriptor.environment), 'resolution_key': 'a' * 64, 'pending_plan_sha256': None})
    with pytest.raises(ValueError, match='resolution_data_changed'):
        await flow.resolve_data(needs=descriptor.needs, environment=dict(descriptor.environment), planning=claim().planning)


async def test_actual_cold_approval_wait_can_resolve_only_after_root_absent_and_current_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str]) -> None:
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.generated_project_service import (
        GeneratedProjectDraftRejected,
        GeneratedProjectService,
        PythonMetadataPreparationApprovalRequired,
    )
    from agent_hub.skills.sandbox import python_resolution_worker as wire
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    service, repo, task, _ = dynamic_case
    artifacts = present(service._resolution_journal).artifacts
    descriptor = await descriptor_with_build_inputs()
    allowed = False
    saved: ResolutionDescriptor | None = None
    actions: list[str] = []
    async def approved(*args: object) -> bool:
        return allowed
    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        nonlocal saved
        actions.append(request.action)
        if request.action == 'inspect_python_resolution':
            return ResolutionStatus(state='resolved' if saved else 'absent', key=request.key,
                                    descriptor=saved.to_payload() if saved else None)
        assert allowed and request.action == 'resolve_python_inputs' and saved is None
        saved = replace(descriptor, scope=request.claim.resolution_scope(), needs=request.needs)
        return ResolutionStatus(state='resolved', key=request.key, descriptor=saved.to_payload())
    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, 'request_resolution', transport)
    with (bind_execution_authority(task, worker_id=present(repo.row.worker_id), actor='engineer'),
          pytest.raises(PythonMetadataPreparationApprovalRequired)):
        await service.prepare(task.tenant_id, task.run_id, 'engineer', user_id=present(task.actor_id))
    assert saved is None and actions == ['inspect_python_resolution']
    cold = GeneratedProjectService(root=service.root, repository=repo)
    cold.configure_resolution_artifacts(artifacts)
    allowed = True
    with (bind_execution_authority(task, worker_id=present(repo.row.worker_id), actor='engineer'),
          pytest.raises(GeneratedProjectDraftRejected)):
        await cold.prepare(task.tenant_id, task.run_id, 'engineer', user_id=present(task.actor_id))
    assert saved is not None and actions.count('resolve_python_inputs') == 1
    assert repo.updates == 0


async def test_checkpoint_consumer_reads_past_a_full_page_and_rejects_current_scope_drift() -> None:
    from uuid import UUID

    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.contracts import Artifact
    from tests.unit.skills.test_python_resolution_store import claim

    original = claim()
    scope = {**original.planning.scope.model_dump(mode='json'),
             'root_sha256': original.planning.expected_root_config_sha256,
             'catalog_sha256': original.planning.expected_catalog_sha256,
             'original_deadline': original.original_deadline.isoformat()}
    needs = prep.dynamic_python_needs(manifest())
    artifacts = InMemoryArtifactRepository()
    payload: dict[str, JsonValue] = {'schema_version': 1, 'scope': scope,
               'needs': tuple({'requirement': n.requirement, 'evidence_sha256': n.evidence_sha256} for n in needs),
               'environment': registry.environment(), 'resolution_key': 'a' * 64, 'pending_plan_sha256': None}
    for version in range(1, 130):
        await artifacts.put(original.planning.scope.tenant_id, original.planning.scope.run_id,
            Artifact(id=UUID(int=version), version=version, type='python_resolution_checkpoint',
                     producer='python_resolution', content={**payload, 'pending_plan_sha256': 'b' * 64 if version == 129 else None}))
    journal = prep.DynamicResolutionJournal(artifacts)
    loaded = await journal.load(scope)
    assert loaded is not None
    assert loaded['pending_plan_sha256'] == 'b' * 64
    for field in ('actor', 'user_id', 'request_sha256', 'permissions_sha256', 'project_id',
                  'session_id', 'root_sha256', 'catalog_sha256', 'original_deadline'):
        with pytest.raises(ValueError, match='checkpoint_scope_changed'):
            await journal.load({**scope, field: 'different-current-scope'})


async def test_generated_success_delivery_uses_human_summary_after_actual_decoder(tmp_path: Path) -> None:
    from agent_hub.capabilities import python_generated_service as generated_api
    from tests.unit.capabilities.test_python_generated_service import approve, configured, request

    service, _, repo, executor, _, context = await configured(generated_api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    result = await service.verify(context, 'tester')
    assert '离线' in result['summary'] and '测试' in result['summary']
    assert len(executor.calls) == 1 and result['verification']['generated_acceptance_sha256'] == context.envelope.sha256


@pytest.mark.parametrize("mutation", ["json_roundtrip", "scope_drift", "invalid_state"])
async def test_typed_repository_cas_normalizes_the_state_after_reservation_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str],
    mutation: str,
) -> None:
    """Actual generated capture/validator and JSON round trip; no native installation."""
    import hashlib
    import json

    from agent_hub.auth.models import Role
    from agent_hub.capabilities.generated_project_service import (
        GeneratedProjectError,
        validate_generated_preparation,
        validate_generated_transition,
    )

    _, captured_repo, _, captured, _, _ = await completed_generated_dynamic_case(
        tmp_path, monkeypatch, dynamic_case
    )
    expected = validate_generated_preparation(captured.state)
    replacement = validate_generated_transition(expected, expected)
    repo = FlowRepository(replace(captured_repo.row, routing_decision={
        **present(captured_repo.row.routing_decision), "generated_project_preparation": expected,
    }))
    before = repo.row
    calls: list[str] = []

    async def roundtrip_after_read() -> None:
        calls.append("before_reservation")
        assert present(repo.row.routing_decision)["generated_project_preparation"] == expected
        raw: object = json.loads(json.dumps(expected, default=dict))
        serialized = mutable(raw)
        assert serialized != expected  # JSON arrays are lists; validated arrays are tuples.
        assert validate_generated_preparation(serialized) == expected
        if mutation == "scope_drift":
            serialized["manifest_raw_sha256"] = "f" * 64
        elif mutation == "invalid_state":
            serialized["phase"] = "not-a-generated-phase"
        repo.row = replace(repo.row, routing_decision={
            **present(repo.row.routing_decision), "generated_project_preparation": serialized,
        })

    async def compare_and_set() -> RunRecord:
        return await repo.compare_and_set_generated_preparation(
            before.tenant_id, before.id,
            actor_id=present(before.actor_id), actor_role=Role(present(before.actor_role)),
            lease_token=present(before.worker_lease_token),
            request_sha256=hashlib.sha256(before.request.encode()).hexdigest(),
            project_id=captured.stored.contract.project_id,
            session_id=captured.stored.contract.session_id,
            expected=expected, replacement=replacement, before_reservation=roundtrip_after_read,
        )

    if mutation == "json_roundtrip":
        updated = await compare_and_set()
        assert updated is repo.row and repo.updates == 1
        assert present(updated.routing_decision)["generated_project_preparation"] == replacement
    else:
        error = GeneratedProjectError if mutation == "invalid_state" else AssertionError
        with pytest.raises(error, match="^generated_acceptance_state_invalid$" if mutation == "invalid_state" else None):
            await compare_and_set()
        assert repo.updates == 0
    assert calls == ["before_reservation"]
    assert (repo.row.worker_id, repo.row.worker_lease_token) == (before.worker_id, before.worker_lease_token)
