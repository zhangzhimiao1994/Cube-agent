"""Local workspace text validation only; no native runner, provider, or network."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.tool_gateway import _capability_request
from agent_hub.models.physical_budget import _bind_run_budget
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import save_continuation
from agent_hub.runtime.contracts import JsonValue
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run
from tests.unit.capabilities.workspace_empty_text_fixture import invoke, setup


class _SchemaValidator(Protocol):
    def validate(self, instance: object) -> None: ...

    def is_valid(self, instance: object) -> bool: ...


TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN = UUID("11111111-1111-4111-8111-111111111111")


def backend(
    tmp_path: Path, **updates: object,
) -> tuple[RuntimeCapabilityGateway, ProjectWorkspaceStore, InMemoryArtifactRepository]:
    artifacts = InMemoryArtifactRepository()
    record = stored_run(tenant=TENANT, run=RUN)
    assert record.routing_decision is not None
    record = replace(record, routing_decision={**record.routing_decision, **updates})
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path/"skills", project_workspace_dir=tmp_path/"workspaces",
        run_repository=FakeRunRepository(record),
        artifact_repository=artifacts, skill_sandboxes={},
    )
    return gateway, ProjectWorkspaceStore(tmp_path/"workspaces"), artifacts


async def write(
    gateway: RuntimeCapabilityGateway, arguments: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    return await gateway.execute(
        tenant_id=TENANT, run_id=RUN, actor="main_agent", name="workspace.write_text",
        arguments=arguments, idempotency_key="empty-text-regression",
    )


@pytest.mark.parametrize("referenced", [False, True], ids=["inline", "reference"])
@pytest.mark.parametrize("path,content", [
    ("requirements.lock", ""), ("src/expense_report/__init__.py", ""),
    ("requirements.lock", " \t\r\n"),
], ids=["blank_lock", "blank_python_init", "whitespace_lock"])
async def test_explicit_empty_and_whitespace_text_round_trip_unchanged(
    tmp_path: Path, referenced: bool, path: str, content: str,
) -> None:
    gateway, store, artifacts = backend(tmp_path)
    arguments: dict[str, JsonValue] = {"path": path, "content": content}
    if referenced:
        ref = await save_continuation(artifacts, tenant_id=TENANT, run_id=RUN, value=content)
        arguments = {"path": path, "content_reference": cast(JsonValue, ref)}
    result = await write(gateway, arguments)
    metadata = result["file"]
    assert isinstance(metadata, Mapping)
    expected = content.encode("utf-8")
    assert metadata["size_bytes"] == len(expected)
    assert metadata["sha256"] == hashlib.sha256(expected).hexdigest()
    destination = store.session_root(TENANT, "project-a", "session-a")/path
    assert destination.read_bytes() == expected


def test_model_schema_accepts_explicit_empty_but_requires_typed_content_and_nonblank_path(
    tmp_path: Path,
) -> None:
    gateway, _, _ = backend(tmp_path)
    manifest = gateway.capability_manifest(TENANT)
    items = manifest["capabilities"]
    assert isinstance(items, tuple | list)
    schema = next(item["input_schema"] for item in items
                  if isinstance(item, Mapping) and item.get("id") == "workspace.write_text")
    assert isinstance(schema, Mapping)
    # Attest the two exercised APIs of the installed untyped third-party library.
    # Legacy test helpers remain ordinary static typed imports, not dynamic imports.
    factory = cast(Callable[[Mapping[str, object]], _SchemaValidator],
        import_module("jsonschema").Draft202012Validator)
    validator = factory(dict(schema))
    for content in ("", " \t\n"):
        validator.validate({"path": "requirements.lock", "content": content})
    for invalid in [
        {"path": "requirements.lock"}, {"path": "requirements.lock", "content": None},
        {"path": "requirements.lock", "content": False}, {"path": "", "content": ""},
        {"path": "requirements.lock", "content": "", "unexpected": True},
    ]:
        assert not validator.is_valid(invalid)


@pytest.mark.parametrize("arguments", [
    {"path": "requirements.lock"}, {"path": "requirements.lock", "content": None},
    {"path": "requirements.lock", "content": False},
    {"path": "requirements.lock", "content": 0},
], ids=["content_missing", "content_null", "content_bool", "content_number"])
async def test_missing_or_nonstring_inline_content_never_creates_a_file(
    tmp_path: Path, arguments: dict[str, JsonValue],
) -> None:
    gateway, store, _ = backend(tmp_path)
    with pytest.raises(RuntimeCapabilityError, match="content must be a string"):
        await write(gateway, arguments)
    assert store.list_files(TENANT, "project-a", "session-a") == ()


@pytest.mark.parametrize("path", ["", " ", "../outside.txt", ".env", "hidden/.config", "/outside.txt"],
    ids=["path_empty", "path_whitespace", "path_traversal", "hidden_file", "hidden_directory", "absolute_path"])
async def test_empty_content_does_not_bypass_path_guards(tmp_path: Path, path: str) -> None:
    gateway, store, _ = backend(tmp_path)
    with pytest.raises(RuntimeCapabilityError):
        await write(gateway, {"path": path, "content": ""})
    assert store.list_files(TENANT, "project-a", "session-a") == ()


@pytest.mark.parametrize("updates", [
    {"sandbox_profile": "read_only"},
    {"requested_permissions": ["workspace.read"]},
    {"workspace_session_id": "../outside"},
], ids=["sandbox_read_only", "write_permission_missing", "session_traversal"])
async def test_empty_content_does_not_bypass_persisted_write_scope(
    tmp_path: Path, updates: dict[str, object],
) -> None:
    gateway, store, _ = backend(tmp_path, **updates)
    with pytest.raises(RuntimeCapabilityError):
        await write(gateway, {"path": "requirements.lock", "content": ""})
    assert store.list_files(TENANT, "project-a", "session-a") == ()


async def test_empty_content_does_not_cross_tenant_scope(tmp_path: Path) -> None:
    gateway, store, _ = backend(tmp_path)
    with pytest.raises(RuntimeCapabilityError):
        await gateway.execute(tenant_id=OTHER, run_id=RUN, actor="main_agent",
            name="workspace.write_text", arguments={"path": "requirements.lock", "content": ""},
            idempotency_key="foreign-empty")
    assert store.list_files(TENANT, "project-a", "session-a") == ()
    assert store.list_files(OTHER, "project-a", "session-a") == ()


async def test_empty_write_keeps_protected_controls_and_file_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, store, _ = backend(tmp_path)
    protected = SimpleNamespace(is_editable=lambda path: False)
    with monkeypatch.context() as scoped:
        scoped.setattr(gateway, "_original_project_contract", AsyncMock(return_value=protected))
        with pytest.raises(RuntimeCapabilityError, match="protected from modification"):
            await write(gateway, {"path": "requirements.lock", "content": ""})
    assert store.list_files(TENANT, "project-a", "session-a") == ()
    limited = ProjectWorkspaceStore(tmp_path/"workspaces", max_file_bytes=0)
    monkeypatch.setattr(gateway, "_project_workspace_store", limited)
    await write(gateway, {"path": "requirements.lock", "content": ""})
    with pytest.raises(RuntimeCapabilityError, match="workspace file is too large"):
        await write(gateway, {"path": "nonempty.txt", "content": " "})


async def test_empty_reference_keeps_exclusive_sources_digest_and_tenant_binding(
    tmp_path: Path,
) -> None:
    gateway, store, artifacts = backend(tmp_path)
    ref = await save_continuation(artifacts, tenant_id=TENANT, run_id=RUN, value="")
    foreign = await save_continuation(artifacts, tenant_id=OTHER, run_id=RUN, value="")
    damaged = dict(ref, content_sha256="f"*64)
    for invalid in [
        {"path": "requirements.lock", "content": "", "content_reference": ref},
        {"path": "requirements.lock", "content_reference": foreign},
        {"path": "requirements.lock", "content_reference": damaged},
    ]:
        with pytest.raises(RuntimeCapabilityError):
            await write(gateway, cast(Mapping[str, JsonValue], invalid))
    assert store.list_files(TENANT, "project-a", "session-a") == ()


@pytest.mark.parametrize("value", [None, 0, False, {"text": ""}],
    ids=["reference_null", "reference_number", "reference_bool", "reference_object"])
async def test_reference_must_still_resolve_to_a_string(
    tmp_path: Path, value: JsonValue,
) -> None:
    gateway, store, artifacts = backend(tmp_path)
    ref = await save_continuation(artifacts, tenant_id=TENANT, run_id=RUN, value=value)
    with pytest.raises(RuntimeCapabilityError):
        await write(gateway, {"path": "requirements.lock", "content_reference": cast(JsonValue, ref)})
    assert store.list_files(TENANT, "project-a", "session-a") == ()


@pytest.mark.parametrize("content", ["", " \t\n"], ids=["blank", "whitespace"])
async def test_reviewed_empty_text_executes_exact_reviewed_effect_without_trimming(
    tmp_path: Path, content: str,
) -> None:
    state = setup(tmp_path)
    state.request = state.request.__class__(
        state.task.run_id, "main_agent", "workspace.write_text",
        {"path": "requirements.lock", "content": content}, True, "workspace_write", "empty-review",
    )
    result = await invoke(state)
    assert result.status == "succeeded", result
    files = ProjectWorkspaceStore(tmp_path).list_files(state.task.tenant_id, "demo", "session")
    assert len(files) == 1
    assert files[0].size_bytes == len(content.encode())
    assert files[0].sha256 == hashlib.sha256(content.encode()).hexdigest()
    snapshot = state.reviewer.calls[0][0]
    reviewed_content = snapshot.request().arguments["content"]
    assert isinstance(reviewed_content, str)
    assert reviewed_content == content
    if content == "":
        assert files[0].sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hashlib.sha256(reviewed_content.encode()).hexdigest() == files[0].sha256
    effect = snapshot.to_payload()["normalized_effect"]
    assert isinstance(effect, Mapping)
    assert effect["content"] == content
    target = ProjectWorkspaceStore(tmp_path).session_root(state.task.tenant_id, "demo", "session")
    assert (target/"requirements.lock").read_bytes() == content.encode()


async def test_review_effect_digest_distinguishes_empty_from_whitespace(tmp_path: Path) -> None:
    state = setup(tmp_path)
    assert state.task.actor_id is not None and state.task.actor_role is not None
    first = _capability_request(state.task.tenant_id, state.task.actor_id, state.request)
    empty = first.model_copy(update={"arguments": {"path": "requirements.lock", "content": ""}})
    whitespace = first.model_copy(update={"arguments": {"path": "requirements.lock", "content": " "}})
    with bind_execution_authority(state.task, worker_id=state.budget.lease.worker_id), _bind_run_budget(state.budget):
        policy, _ = await state.policy._current_review_policy(empty, state.task.actor_role)
        a, _, _ = await state.loader.load(empty, state.task.actor_role, policy, tool_name="workspace.write_text")
        b, _, _ = await state.loader.load(whitespace, state.task.actor_role, policy, tool_name="workspace.write_text")
    assert a.effect_digest != b.effect_digest
    assert a.action_digest != b.action_digest


@pytest.mark.parametrize("content", ["", " \t\n"], ids=["blank", "whitespace"])
async def test_stored_empty_lock_still_fails_python_dependency_validation(
    tmp_path: Path, content: str,
) -> None:
    from agent_hub.capabilities.python_project_recipe import (
        PythonProjectRecipeError,
        parse_python_project_declaration,
    )
    from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST

    gateway, store, _ = backend(tmp_path)
    await write(gateway, {"path": "requirements.lock", "content": content})
    raw = (store.session_root(TENANT, "project-a", "session-a")/"requirements.lock").read_bytes()
    assert raw == content.encode()
    valid = parse_python_project_declaration(
        MANIFEST, LOCK, max_control_bytes=256*1024, max_requirements=128,
    )
    assert len(valid.requirements) == 3
    with pytest.raises(PythonProjectRecipeError):
        parse_python_project_declaration(
            MANIFEST, raw, max_control_bytes=256*1024, max_requirements=128,
        )
