# Plugin Input Schema Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate plugin invocation arguments against each capability's declared `input_schema` before adapter execution.

**Architecture:** Keep validation in `RuntimePluginService.invoke()` after capability resolution and adapter lookup, before calling the adapter. Use `jsonschema` as a direct runtime dependency and translate schema/argument failures into deterministic `RuntimeCapabilityError` messages without leaking argument values.

**Tech Stack:** Python 3.12, Pydantic, jsonschema, pytest.

**Spec:** `HANDOFF.md` entry `2026-09-07 15:22 CST - prod-web-03 deployed 723589c multi-capability schema UI`.

## Global Constraints

- Communicate in Chinese by default.
- Use TDD: write failing tests before production code.
- Do not create new top-level Codex tasks for delegation.
- Update `HANDOFF.md` after completion.
- After GitHub push, verify CI and fix failures before deployment.
- Keep validation user-safe: error strings may name schema paths/fields, but must not echo raw argument values or credentials.

---

### Task 1: Runtime Input Schema Enforcement

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/agent_hub/plugins/runtime.py`
- Test: `tests/unit/plugins/test_runtime.py`

**Interfaces:**
- Consumes: `PluginCapabilityRequest.input_schema: dict[str, JsonValue] | None`
- Produces: `_validate_plugin_arguments(arguments: Mapping[str, JsonValue], schema: Mapping[str, JsonValue] | None) -> None`

- [x] **Step 1: Write failing test for invalid arguments**

```python
async def test_runtime_plugin_service_rejects_arguments_that_violate_input_schema() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    input_schema={
                        "type": "object",
                        "required": ("title",),
                        "properties": {"title": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin arguments do not match input schema"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": 123, "secret": "do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
```

- [x] **Step 2: Verify RED**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\unit\plugins\test_runtime.py -q -k "violate_input_schema"`

Expected: FAIL because invalid arguments currently reach the adapter.

- [x] **Step 3: Write failing test for invalid declared schema**

```python
async def test_runtime_plugin_service_rejects_invalid_input_schema_before_adapter_execution() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", input_schema={"type": "not-a-json-schema-type"}),)
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin input schema is invalid"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
```

- [x] **Step 4: Verify RED**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\unit\plugins\test_runtime.py -q -k "input_schema"`

Expected: FAIL for the new runtime enforcement tests.

- [x] **Step 5: Implement minimal validation**

Add a direct `jsonschema` dependency, import `SchemaError` and `ValidationError`, call `jsonschema.validators.validator_for(schema).check_schema(schema)`, instantiate the selected validator, and validate `dict(arguments)`. Convert schema errors to `RuntimeCapabilityError("Plugin input schema is invalid")` and argument errors to `RuntimeCapabilityError("Plugin arguments do not match input schema: <path/message>")` with no raw argument values.

- [x] **Step 6: Verify GREEN**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\unit\plugins\test_runtime.py -q -k "input_schema or registered_adapter"`

Expected: PASS and adapter is not invoked on validation failures.

### Task 2: Verification, Commit, CI, Deploy

**Files:**
- Modify: `HANDOFF.md`

**Interfaces:**
- Consumes: local verification output and CI result.
- Produces: pushed commit and deployed prod release.

- [x] Run ruff, mypy, targeted backend plugin tests, relevant gateway tests, `git diff --check`, and any dependency lock verification required by the local toolchain.
- [ ] Commit and push.
- [ ] Check GitHub Actions. Fix and repeat if needed.
- [ ] Deploy to `prod-web-03`.
- [ ] Run prod doctor, service checks, OpenAPI/dependency checks, and recent log checks.
- [ ] Update `HANDOFF.md`.
