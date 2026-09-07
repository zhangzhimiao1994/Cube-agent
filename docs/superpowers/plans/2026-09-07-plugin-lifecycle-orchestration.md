# Plugin Lifecycle Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class plugin lifecycle management layer so registered plugins can be listed, started, stopped, reloaded, audited, and surfaced in the runtime capability manifest.

**Architecture:** Use the existing admin resource pattern as the persistence boundary and the existing capability manifest pattern as the runtime visibility boundary. This slice manages installed/registered plugin metadata and lifecycle status; it does not download arbitrary external code or run untrusted plugin processes yet.

**Tech Stack:** FastAPI, Pydantic v2, existing `AdminResourceRow` persistence, `RuntimeCapabilityGateway`, pytest, ruff, mypy.

**Spec:** `HANDOFF.md` top sections for MCP/runtime/capability work plus the user requirement: implement Codex-harness stability and the DeepSeek-harness plugin idea for a multi-model agent, especially "everything can be a plugin" and "pluggable".

## Global Constraints

- Keep plugin execution fail-closed: a disabled or stopped plugin must not surface executable available capabilities.
- Reuse existing `plugin:read`, `plugin:use`, and `plugin:write` permissions; do not introduce new permissions in this slice.
- Do not implement external plugin download/install in this slice.
- Persist plugin resources through the existing admin resource table with tenant isolation.
- All new production behavior must start with failing tests.

---

### Task 1: Plugin Resource Models And Service Methods

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

**Interfaces:**
- Produces: `PluginCapabilityRequest`, `PluginResourceRequest`, `PluginResourceResponse`.
- Produces service methods: `list_plugins()`, `upsert_plugin(request)`, `start_plugin(plugin_id)`, `stop_plugin(plugin_id)`, `reload_plugin(plugin_id)`, `delete_plugin(plugin_id)`.
- Consumes: existing `InMemoryAdminResourceService` and `PersistentAdminResourceService` patterns.

- [ ] **Step 1: Write failing in-memory service test**

```python
@pytest.mark.asyncio
async def test_admin_plugin_lifecycle_updates_status_and_health() -> None:
    service = InMemoryAdminResourceService()

    created = await service.upsert_plugin(
        PluginResourceRequest(
            id="search",
            name="Search Plugin",
            capabilities=[
                PluginCapabilityRequest(
                    id="search.web",
                    permission_class="network.read",
                    sandbox_profile="remote_connector",
                    aliases=["search_web"],
                )
            ],
        )
    )
    started = await service.start_plugin("search")
    stopped = await service.stop_plugin("search")
    reloaded = await service.reload_plugin("search")

    assert created.status == "stopped"
    assert started.status == "running"
    assert started.health == "healthy"
    assert stopped.status == "stopped"
    assert stopped.health == "stopped"
    assert reloaded.status == "running"
```

- [ ] **Step 2: Run test to verify RED**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -q -k "plugin_lifecycle_updates_status"`

Expected: FAIL because plugin models/service methods are not defined.

- [ ] **Step 3: Implement minimal in-memory service behavior**

Add Pydantic models near the MCP models. Add `plugins: dict[str, PluginResourceResponse]` to `InMemoryAdminResourceService`. Implement status transitions:
- upsert: `enabled=True` -> `status="stopped", health="stopped"`; `enabled=False` -> `status="disabled", health="disabled"`.
- start: enabled plugin becomes `running/healthy`; disabled plugin raises 409 `plugin_disabled`.
- stop: enabled plugin becomes `stopped/stopped`.
- reload: enabled plugin becomes `running/healthy`.
- delete: missing plugin raises `KeyError`.

- [ ] **Step 4: Run test to verify GREEN**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -q -k "plugin_lifecycle_updates_status"`

Expected: PASS.

### Task 2: Persistent Plugin Storage

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

**Interfaces:**
- Consumes: Task 1 service signatures.
- Produces: persistent admin payload kind `"plugin"`.

- [ ] **Step 1: Write failing persistent service test**

```python
@pytest.mark.asyncio
async def test_persistent_admin_plugin_lifecycle_persists_status() -> None:
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),
        secret_service=FakeSecretService(),
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    await service.upsert_plugin(
        PluginResourceRequest(id="search", name="Search Plugin")
    )
    started = await service.start_plugin("search")
    listed = await service.list_plugins()

    assert started.status == "running"
    assert listed == (started,)
```

- [ ] **Step 2: Run test to verify RED**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -q -k "persistent_admin_plugin_lifecycle"`

Expected: FAIL because persistent methods are not implemented.

- [ ] **Step 3: Implement persistent service methods**

Use `_list_admin_payloads("plugin")`, `_upsert_admin_payload("plugin", ...)`, `_delete_admin_payload("plugin", ...)`, and `_record_audit()` with actions `plugin.upsert`, `plugin.start`, `plugin.stop`, `plugin.reload`, and `plugin.delete`.

- [ ] **Step 4: Run test to verify GREEN**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -q -k "persistent_admin_plugin_lifecycle"`

Expected: PASS.

### Task 3: Plugin Admin API And Capability Manifest Source

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Create or modify: `src/agent_hub/capabilities/tools/registry.py`
- Test: `tests/api/test_admin_resources.py`
- Test: `tests/unit/capabilities/tools/test_registry.py`

**Interfaces:**
- Consumes: `list_plugins()` and plugin response capabilities.
- Produces: admin endpoints `GET/POST /api/v1/admin/plugins`, `POST /api/v1/admin/plugins/{plugin_id}/start`, `stop`, `reload`, and `DELETE /api/v1/admin/plugins/{plugin_id}`.
- Produces: a manifest source that turns running plugin capabilities into `kind="plugin"` manifest items.

- [ ] **Step 1: Write failing endpoint and manifest test**

```python
def test_plugin_admin_api_exposes_running_plugin_capabilities_in_manifest() -> None:
    api = client()
    cast(Any, api.app).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "capabilities": [
                {
                    "id": "search.web",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                    "aliases": ["search_web"],
                }
            ],
        },
    )
    started = api.post("/api/v1/admin/plugins/search/start", headers=headers())
    manifest = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert created.status_code == 200
    assert started.json()["status"] == "running"
    capabilities = {item["id"]: item for item in manifest.json()["capabilities"]}
    assert capabilities["search.web"]["available"] is True
    assert capabilities["search.web"]["kind"] == "plugin"
```

- [ ] **Step 2: Run test to verify RED**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -q -k "plugin_admin_api_exposes_running_plugin_capabilities"`

Expected: FAIL because endpoints and manifest source do not exist.

- [ ] **Step 3: Implement endpoints and manifest source**

Add router handlers requiring `plugin:read` for list, `plugin:write` for create/update/start/stop/reload/delete. Add plugin manifest source to `capability_manifest()` alongside the existing MCP config source.

- [ ] **Step 4: Run test to verify GREEN**

Run: `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py tests\unit\capabilities\tools\test_registry.py -q -k "plugin"`

Expected: PASS.

### Task 4: Verification, Commit, CI, Deploy, Handoff

**Files:**
- Modify: `HANDOFF.md`

**Interfaces:**
- Consumes: completed code from Tasks 1-3.
- Produces: pushed commit, passing CI, prod-web-03 deployment, updated handoff.

- [ ] **Step 1: Run focused verification**

Run:
- `.\.tmp\test-venv\Scripts\python.exe -m ruff check src tests --no-cache`
- `.\.tmp\test-venv\Scripts\python.exe -m mypy --strict src tests --cache-dir E:\code_x\codex-pytest-tmp\mofang-mypy-20260907-plugin-lifecycle-1`
- `.\.tmp\test-venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py tests\unit\capabilities\tools\test_registry.py tests\unit\capabilities tests\unit\runtime\test_configured_runtime.py -q -k "plugin or mcp or capability_manifest or capability_inventory"`

- [ ] **Step 2: Commit and push**

Run:
- `git add src/agent_hub/api/routers/admin.py src/agent_hub/capabilities/tools/registry.py tests/api/test_admin_resources.py tests/unit/capabilities/tools/test_registry.py docs/superpowers/plans/2026-09-07-plugin-lifecycle-orchestration.md`
- `git commit -m "Add plugin lifecycle orchestration"`
- `git push origin codex/project-workspace-permissions`

- [ ] **Step 3: Check GitHub Actions**

Use GitHub Actions run for the pushed SHA. If it fails, fetch failure details, fix locally with tests, commit, push again, and repeat.

- [ ] **Step 4: Deploy to prod-web-03**

Build a tar package from CI-passed source plus `web/dist`, upload it to `prod-web-03`, deploy to `/opt/agent-hub/releases/<timestamp>-plugin-lifecycle-<sha>`, run `alembic upgrade head`, restart services, run doctor, health, OpenAPI, Caddy asset, journal, and systemd failed-unit checks.

- [ ] **Step 5: Update handoff**

Prepend `HANDOFF.md` with the commit SHA, CI run, release path, verification commands, prod checks, and residual risks.
