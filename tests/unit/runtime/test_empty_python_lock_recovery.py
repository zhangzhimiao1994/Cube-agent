"""Real Direct/Harness/recipe/capture; synthetic model, catalog and native transport.

No provider, wheel download, install, build or test subprocess runs in this test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfile,
    PythonProjectProfileInterpreter,
    PythonProjectProfileResponse,
    PythonProjectProfileSandbox,
    PythonProjectProfileScope,
    PythonProjectProfileWheel,
)
from tests.unit.capabilities import test_python_project_service as native_fixture
from tests.unit.capabilities.test_generated_project_service import USER, setup
from tests.unit.capabilities.test_python_generated_service import Approvals, Executor, approve
from tests.unit.capabilities.test_python_project_input_store import interpreter, selected
from tests.unit.capabilities.test_python_project_recipe import MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_direct_project_verification import Policy, batch


async def test_empty_lock_draft_is_corrected_from_catalog_before_capture_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _ = setup(tmp_path)
    root = seed.root
    source = root / project_path()
    for name in ("ACCEPTANCE.json", "package.json", "src/index.js", "test/business.test.cjs"):
        (source / name).unlink()
    acceptance = json.dumps({
        "schema_version": 1, "stack": "python", "editable_paths": ["src"],
        "test_files": ["tests/test_billing.py"], "criteria": [{
            "id": "AC-001", "description": "Returns the requested totals",
            "tests": ["test_billing.BillingTests.test_total"],
        }],
    })
    initial_manifest = MANIFEST.replace(b"packaging==25.0", b"click==8.1.7")
    for name, body in {
        "ACCEPTANCE.json": acceptance, "pyproject.toml": initial_manifest.decode(),
        "requirements.lock": "", "src/billing/__init__.py": "def total(): return 12\n",
        "tests/test_billing.py": (
            "import unittest\nfrom billing import total\n"
            "class BillingTests(unittest.TestCase):\n"
            "    def test_total(self):\n        self.assertEqual(total(), 12)\n"
        ),
    }.items():
        write_file(root, f"{project_path()}/{name}", body)
    lock = source / "requirements.lock"
    assert lock.read_bytes() == b"" and lock.stat().st_size == 0

    wheels = (PythonWheelBinding("click-8.1.8-py3-none-any.whl", "click", "8.1.8", "a" * 64),
              *selected()[1:])
    monkeypatch.setattr(native_fixture, "selected", lambda: wheels)
    catalog = PythonProjectProfile(
        interpreter=PythonProjectProfileInterpreter.model_validate(interpreter().to_payload()),
        catalog_sha256="d" * 64,
        catalog=tuple(PythonProjectProfileWheel(**wheel.to_payload(), size_bytes=100)
                      for wheel in wheels),
    )

    async def inspect(_self: object, invocation: Any) -> PythonProjectProfileResponse:
        return PythonProjectProfileResponse(ok=True,
            scope=PythonProjectProfileScope.for_invocation(invocation), profile=catalog)

    monkeypatch.setattr(PythonProjectProfileSandbox, "inspect", inspect)
    repo = Approvals()
    repo.row.routing_decision.update(project_scale="small", project_delivery="workspace",
                                     artifact_strategy="workspace_bundle")
    permissions = list(repo.row.routing_decision["requested_permissions"])
    executor = Executor(root)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills",
        project_workspace_dir=root, generated_artifact_dir=tmp_path / "artifacts",
        run_repository=repo)
    # Use the existing native-report transport fixture with the actual service/parser.
    verifier = ProjectVerificationService(root=root, repository=repo, executor=None,
        python_executor=executor)
    backend._project_verification = verifier
    backend._project_executor_configured = True
    context = TaskContext(tenant_id=TENANT, run_id=RUN, actor_id=USER,
        actor_role=repo.row.actor_role, mode=TaskMode.DIRECT, request=repo.row.request,
        token_budget=50_000, timeout_seconds=120, routing_decision=repo.row.routing_decision)

    class ApprovalPolicy(Policy):
        async def invoke(self, request: CapabilityRequest, **kwargs: Any) -> CapabilityResult:
            decision = await super().invoke(request, **kwargs)
            if request.resource.startswith("workspace/current/project-verification/python/"):
                approve(repo, request)
            return decision

    policy = ApprovalPolicy(context, CapabilityStatus.ALLOWED)
    results: list[HarnessToolCallResult] = []

    class ObservedHarness(HarnessToolGateway):
        async def invoke(self, tenant_id: Any, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            result = await super().invoke(tenant_id, request, **kwargs)
            results.append(result)
            return result

    class RepairModel(Model):
        repaired_manifest = ""
        repaired_lock = ""

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if not self.requests:
                return await super().complete_with_context(request)
            assert len(self.requests) == 1
            assert lock.read_bytes() == b""
            assert repo.updates == 0
            assert "generated_project_preparation" not in repo.row.routing_decision
            assert not (root / ".project-contracts").exists()
            assert not executor.probes and not executor.calls
            assert not verifier._python_generated.receipts
            assert not list((tmp_path / "artifacts").rglob("*.zip"))
            rejected = [r for r in results if r.tool_name == "project.verify"]
            assert len(rejected) == 1
            failure = rejected[0]
            assert failure.status == "failed" and not failure.truncated
            assert failure.failure_reason == "generated project draft requires correction"
            data = cast(Mapping[str, JsonValue], failure.payload["generated_draft_rejection"])
            assert data["reason"] == "generated_acceptance_python_declaration_invalid"
            assert data["issues"] == ("python_declaration_invalid",)
            assert data["execution_started"] is False and data["phase"] == "before_capture"
            assert data["run_id"] == str(RUN) and data["tenant_id"] == str(TENANT)
            assert data["project_id"] == "project-a" and data["session_id"] == "session-a"
            prompt = "\n".join(cast(str, m.content) for m in request.messages)
            assert "UNTRUSTED_GENERATED_DRAFT_JSON" in prompt
            assert "python_declaration_invalid" in prompt
            assert "no acceptance was frozen" in prompt
            inspection = next(r for r in results if r.tool_name == "project.inspect")
            profile = cast(Mapping[str, Any], inspection.payload["python_build_profile"])
            assert profile["status"] == "configured_not_probed"
            pins = {row["name"]: row for row in profile["catalog"]}
            assert pins["click"]["version"] in prompt and pins["click"]["sha256"] in prompt
            self.repaired_manifest = initial_manifest.decode().replace(
                "click==8.1.7", "click==" + pins["click"]["version"])
            self.repaired_lock = "".join(
                f"{name}=={row['version']} --hash=sha256:{row['sha256']}\n"
                for name, row in sorted(pins.items()))
            self.responses.append(response(batch({
                "pyproject.toml": self.repaired_manifest, "requirements.lock": self.repaired_lock,
            })))
            return await super().complete_with_context(request)

    model = RepairModel(response(batch({"README.md": "Python dependency draft.\n"})))
    harness = ObservedHarness(backend, policy_gateway=policy, require_actor_identity=True)
    runtime = DirectRuntime(model, logical_model="fixture", capability_gateway=backend,
                            harness_tool_gateway=harness)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 2 and not model.responses
    assert lock.read_text(encoding="utf-8") == model.repaired_lock
    assert (source / "pyproject.toml").read_text(encoding="utf-8") == model.repaired_manifest
    assert repo.row.routing_decision["requested_permissions"] == permissions
    assert len(executor.calls) == 1
    assert all(call.run_id == RUN and call.tenant_id == TENANT for call in executor.calls)
    captured = await verifier.generated.load(TENANT, RUN, "main_agent")
    assert captured is not None and captured.state["phase"] == "python_ready"
    assert captured.stored.contract.protected_files["requirements.lock"]
    verified = [r for r in results if r.tool_name == "project.verify" and r.status == "succeeded"]
    assert len(verified) == 1
    receipt = cast(Mapping[str, JsonValue], verified[0].payload["verification"])
    assert receipt["status"] == "passed" and receipt["test_count"] == 1
    inputs = executor.inputs(executor.calls[0])
    assert inputs.declaration.lock_sha256 == hashlib.sha256(model.repaired_lock.encode()).hexdigest()
    assert inputs.declaration.runtime_dependencies == ("click",)
    assert {(w.name, w.version, w.sha256) for w in inputs.wheels} == {
        (w.name, w.version, w.sha256) for w in wheels}
    # Direct's batch adapter calls bundle on the backend, outside Harness.invoke.
    assert len(list((tmp_path / "artifacts").rglob("*.zip"))) == 1
