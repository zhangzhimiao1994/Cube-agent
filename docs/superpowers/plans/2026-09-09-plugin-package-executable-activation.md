# Plugin Package Executable Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first safe executable activation semantics for adapter packages without executing package-provided code.

**Architecture:** Keep arbitrary package code blocked. Add an execution-capable install mode that is eligible only when the package is signed, trusted, approved, bound to a host-registered adapter, uses a supported SDK API version, and declares an isolation profile the current runtime can enforce.

**Tech Stack:** Python, FastAPI/Pydantic, pytest, TypeScript, Zod.

**Spec:** `HANDOFF.md` next recommended work item 1.

## Global Constraints

- Do not execute, extract, or dynamically import package-provided adapter code in this slice.
- Keep `scan_only` fail-closed behavior unchanged.
- Any executable activation state must require verified signature and approved package metadata.
- Runtime availability must still depend on `activation_state == "eligible"`.
- Frontend schema must accept the new install mode and activation state returned by the backend.

---

### Task 1: Backend Activation Semantics

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`
- Test: `tests/unit/plugins/test_runtime.py`

**Interfaces:**
- Produces: `PluginPackageMetadata.install_mode` accepts `runtime_registered`.
- Produces: `_plugin_package_activation_state()` returns `eligible` only for approved, verified `runtime_registered` adapter packages.
- Produces: archive validation rejects `runtime_registered` packages that are not bound to the package adapter contract.

- [ ] **Step 1: Write failing admin tests**

```python
def test_runtime_registered_adapter_package_requires_capabilities_to_use_package_adapter() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post("/api/v1/admin/plugins/signing-keys", headers=headers(), json={
        "key_id": "calendar-prod",
        "algorithm": "ed25519",
        "public_key": plugin_public_key_value(private_key),
    })
    archive_bytes = signed_plugin_archive(
        private_key,
        package_overrides={"install_mode": "runtime_registered", "isolation": "in_process"},
        capabilities=[{"id": "calendar.create_event", "adapter": "other_adapter"}],
    )

    response = api.post("/api/v1/admin/plugins/install", headers={
        **headers(),
        "Content-Type": "application/zip",
        "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
    }, content=archive_bytes)

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == (
        "runtime-registered adapter packages must route capabilities through package adapter_id"
    )
```

- [ ] **Step 2: Write failing eligible-path tests**

```python
def test_runtime_registered_adapter_package_can_be_approved_and_started() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post("/api/v1/admin/plugins/signing-keys", headers=headers(), json={
        "key_id": "calendar-prod",
        "algorithm": "ed25519",
        "public_key": plugin_public_key_value(private_key),
    })
    archive_bytes = signed_plugin_archive(
        private_key,
        package_overrides={"install_mode": "runtime_registered", "isolation": "in_process"},
        capabilities=[{
            "id": "calendar.create_event",
            "adapter": "calendar_python",
            "sandbox_profile": "in_process",
        }],
    )

    install = api.post("/api/v1/admin/plugins/install", headers={
        **headers(),
        "Content-Type": "application/zip",
        "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
    }, content=archive_bytes)
    approved = api.post("/api/v1/admin/plugins/calendar/package/approve", headers=headers(), json={})
    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

    assert install.status_code == 200
    assert install.json()["plugin"]["package_metadata"]["activation_state"] == "blocked_pending_approval"
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert started.status_code == 200
    assert started.json()["status"] == "running"
```

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest tests/api/test_admin_resources.py -q -k "runtime_registered_adapter_package"`
Expected: FAIL because `runtime_registered` is not yet accepted.

- [ ] **Step 4: Implement minimal backend changes**

```python
PluginPackageInstallMode = Literal["scan_only", "runtime_registered"]
SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS = frozenset(("1.0",))
SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS = frozenset(("in_process",))
```

Update validation so `runtime_registered` requires `sdk_api_version in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS`, `isolation in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS`, at least one capability, and every capability `adapter == package.adapter_id` and `sandbox_profile == package.isolation`.

- [ ] **Step 5: Run backend target tests**

Run: `pytest tests/api/test_admin_resources.py tests/unit/plugins/test_runtime.py -q -k "runtime_registered_adapter_package or scan_only_adapter_package"`
Expected: PASS.

### Task 2: Frontend Schema Compatibility

**Files:**
- Modify: `web/src/api/client.ts`
- Test: `web/src/api/client.test.ts`
- Test: `web/src/pages/OperationalPages.test.tsx`

**Interfaces:**
- Consumes: backend may return `install_mode: "runtime_registered"`.
- Produces: frontend API parsing preserves the new install mode.

- [ ] **Step 1: Write failing client schema test**

```typescript
expect(result.package_metadata?.install_mode).toBe("runtime_registered");
expect(result.package_metadata?.activation_state).toBe("eligible");
```

- [ ] **Step 2: Run test to verify RED**

Run: `npm test -- --run web/src/api/client.test.ts -t "runtime registered"`
Expected: FAIL because Zod only accepts `scan_only`.

- [ ] **Step 3: Update Zod schema**

```typescript
install_mode: z.enum(["scan_only", "runtime_registered"]).default("scan_only")
```

- [ ] **Step 4: Run frontend target tests**

Run: `npm test -- --run web/src/api/client.test.ts web/src/pages/OperationalPages.test.tsx`
Expected: PASS.
