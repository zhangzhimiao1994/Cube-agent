# Agent Hub Phase 3 Capabilities and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single audited capability boundary for safe tools, Skills, and MCP, plus tenant-isolated memory, knowledge retrieval, context compaction, and scheduled work.

**Architecture:** Runtimes submit typed capability requests to `CapabilityGateway`; policy returns allow, approval, or deny before an executor is selected. Skills are immutable approved packages executed by a sandbox backend. Memory and knowledge are separate stores assembled by `ContextBuilder`, while scheduler jobs submit ordinary TaskRequests.

**Tech Stack:** Pydantic, PostgreSQL, Redis, Celery, MCP Python SDK, zipfile, YARA-compatible/static rules, systemd-run/Docker sandbox adapters, pgvector, APScheduler.

---

## File map

- `src/agent_hub/capabilities/`: request types, policy engine, approval flow, gateway, and safe tools.
- `src/agent_hub/skills/`: package validation, quarantine, approval, registry, and sandbox executors.
- `src/agent_hub/mcp/`: server definitions, clients, tool discovery, and permission projection.
- `src/agent_hub/memory/`: working, episodic, and core memory services.
- `src/agent_hub/knowledge/`: document ingestion, hybrid retrieval, and citations.
- `src/agent_hub/context/`: bounded prompt assembly and compaction.
- `src/agent_hub/scheduler/`: one-time and recurring task submission.

### Task 1: Implement capability policy and approvals

**Files:**
- Create: `src/agent_hub/capabilities/types.py`
- Create: `src/agent_hub/capabilities/policy.py`
- Create: `src/agent_hub/capabilities/gateway.py`
- Create: `src/agent_hub/capabilities/approvals.py`
- Create: `tests/unit/capabilities/test_policy.py`
- Create: `tests/integration/capabilities/test_approval.py`

- [ ] **Step 1: Write allow, approval, deny, and scope tests**

```python
def test_read_only_workspace_file_is_safe(policy) -> None:
    decision = policy.evaluate(request("file.read", resource="workspace/docs/a.md"))
    assert decision.effect == "allow"


def test_host_shell_is_always_denied(policy) -> None:
    decision = policy.evaluate(request("shell.exec", resource="host"))
    assert decision.effect == "deny"


async def test_restricted_call_pauses_run(gateway, run_repository) -> None:
    result = await gateway.invoke(request("message.send", resource="external"))
    assert result.status == "waiting_approval"
    assert (await run_repository.get(result.run_id)).status.value == "waiting_approval"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/unit/capabilities tests/integration/capabilities -q`

Expected: FAIL because policy types are absent.

- [ ] **Step 3: Implement exact-match policy evaluation**

```python
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class CapabilityRequest(BaseModel):
    tenant_id: UUID
    user_id: UUID
    agent_id: str
    capability: str
    operation: str
    resource: str
    arguments: dict[str, JsonValue]
    idempotency_key: str
```

Rules match tenant, RBAC role, agent, capability, operation, and normalized resource prefix. Deny wins, approval wins over allow, and absence of a matching rule denies by default.

- [ ] **Step 4: Implement approval creation and resume**

Store a hash of normalized arguments with the approval. Approval is valid only for the exact request, actor, resource, and expiry. Approving transitions the run back to `RUNNING`; rejection or timeout cancels the capability call without pretending it executed.

- [ ] **Step 5: Test and commit**

Run: `uv run pytest tests/unit/capabilities tests/integration/capabilities -q`

Expected: precedence, path normalization, tenant isolation, exact approval, expiry, reject, and resume tests pass.

```bash
git add src/agent_hub/capabilities tests/unit/capabilities tests/integration/capabilities
git commit -m "feat: add audited capability policy gateway"
```

### Task 2: Add safe built-in tools

**Files:**
- Create: `src/agent_hub/capabilities/tools/registry.py`
- Create: `src/agent_hub/capabilities/tools/http_read.py`
- Create: `src/agent_hub/capabilities/tools/workspace_read.py`
- Create: `src/agent_hub/capabilities/tools/calculator.py`
- Create: `tests/unit/capabilities/tools/test_http_read.py`
- Create: `tests/unit/capabilities/tools/test_workspace_read.py`

- [ ] **Step 1: Write SSRF and path-escape tests**

```python
@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://169.254.169.254/latest", "file:///etc/passwd"])
async def test_http_reader_rejects_private_targets(http_reader, url) -> None:
    with pytest.raises(UnsafeTarget):
        await http_reader.fetch(url)


def test_workspace_reader_rejects_parent_escape(workspace_reader) -> None:
    with pytest.raises(UnsafePath):
        workspace_reader.read("../../etc/passwd")
```

- [ ] **Step 2: Implement safe tools with bounded outputs**

Resolve DNS before and after redirects; block loopback, link-local, private, multicast, Unix sockets, non-HTTP schemes, credentials in URLs, and more than three redirects. Workspace reads use resolved paths beneath the tenant root, reject symlinks escaping the root, and enforce byte limits. Calculator parses an AST allowlist and never calls `eval`.

- [ ] **Step 3: Register tools only through CapabilityGateway**

```python
registry = ToolRegistry()
registry.register("http.read", http_reader.fetch)
registry.register("workspace.read", workspace_reader.read)
registry.register("calculator.evaluate", calculator.evaluate)
```

No runtime receives executor callables directly; it receives only tool schemas whose invocation returns to the gateway.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/unit/capabilities/tools -q`

Expected: private-network, redirect rebinding, oversized response, traversal, symlink escape, and unsafe expression tests pass.

```bash
git add src/agent_hub/capabilities/tools tests/unit/capabilities/tools
git commit -m "feat: add safe built in tools"
```

### Task 3: Validate, quarantine, and approve Skill packages

**Files:**
- Create: `src/agent_hub/skills/manifest.py`
- Create: `src/agent_hub/skills/package.py`
- Create: `src/agent_hub/skills/scanner.py`
- Create: `src/agent_hub/skills/service.py`
- Create: `tests/unit/skills/test_package.py`
- Create: `tests/integration/skills/test_lifecycle.py`

- [ ] **Step 1: Write malicious archive and lifecycle tests**

```python
@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape"])
def test_zip_path_traversal_is_rejected(skill_package, name) -> None:
    with pytest.raises(InvalidSkillPackage):
        skill_package.inspect(zip_with_member(name))


async def test_uploaded_skill_cannot_run_before_approval(skill_service) -> None:
    skill = await skill_service.upload(valid_skill_zip())
    assert skill.status == "quarantined"
    with pytest.raises(SkillNotApproved):
        await skill_service.activate(skill.id)
```

- [ ] **Step 2: Implement the manifest and archive limits**

Manifest fields are name, semantic version, entry point, compatible runtime, declared tools, network policy, writable paths, environment secret references, and dependency lock hash. Reject duplicate paths, links, devices, nested archives, forbidden extensions, excessive count/size/ratio, unpinned dependencies, or undeclared executables.

- [ ] **Step 3: Implement immutable lifecycle states**

Use `quarantined → scanned → approved → active → disabled/revoked`. Approval records content SHA-256, scanner version, reviewer, Diff summary, requested capabilities, and dependency lock. Any byte change creates a new version requiring approval.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/skills tests/integration/skills -q`

Expected: path traversal, zip bomb, symlink, dependency, content mutation, approval, activation, disable, and revoke tests pass.

```bash
git add src/agent_hub/skills tests/unit/skills tests/integration/skills
git commit -m "feat: add secure skill package lifecycle"
```

### Task 4: Execute Skills through interchangeable sandboxes

**Files:**
- Create: `src/agent_hub/skills/sandbox/base.py`
- Create: `src/agent_hub/skills/sandbox/docker.py`
- Create: `src/agent_hub/skills/sandbox/systemd.py`
- Create: `tests/contracts/test_skill_sandbox.py`

- [ ] **Step 1: Write backend contract tests**

```python
@pytest.mark.parametrize("backend_name", ["docker", "systemd"])
async def test_sandbox_enforces_timeout_and_bounded_output(sandbox_factory, backend_name) -> None:
    backend = sandbox_factory(backend_name)
    result = await backend.run(invocation("while true; do echo x; done", timeout=1, output_limit=1024))
    assert result.timed_out is True
    assert len(result.stdout.encode()) <= 1024
```

- [ ] **Step 2: Define the sandbox protocol**

```python
class SkillInvocation(BaseModel):
    execution_id: str
    package_path: Path
    package_sha256: str
    input: dict[str, JsonValue]
    timeout_seconds: int
    output_limit_bytes: int
    memory_limit_bytes: int
    cpu_quota_percent: int
    network_allowlist: list[str] = Field(default_factory=list)


class SkillResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class SkillSandbox(Protocol):
    async def run(self, invocation: SkillInvocation) -> SkillResult:
        raise NotImplementedError

    async def terminate(self, execution_id: str) -> None:
        raise NotImplementedError
```

`SkillInvocation` contains immutable package path/hash, JSON input, time/CPU/memory/output limits, network allowlist, read-only inputs, temporary writable directory, and selected secret references.

- [ ] **Step 3: Implement Docker and systemd command builders**

Docker must use non-root UID, read-only root, dropped capabilities, no-new-privileges, pids/memory/CPU limits, no Docker socket, and `--network none` unless policy grants an isolated network. systemd-run must use `DynamicUser=yes`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `PrivateTmp=yes`, `PrivateDevices=yes`, `RestrictSUIDSGID=yes`, `MemoryMax`, `CPUQuota`, and `RuntimeMaxSec`.

- [ ] **Step 4: Run contract tests and commit**

Run: `uv run pytest tests/contracts/test_skill_sandbox.py -q`

Expected: both backends enforce timeout, output, filesystem, process, and network restrictions in privileged CI jobs; command-builder unit tests run everywhere.

```bash
git add src/agent_hub/skills/sandbox tests/contracts
git commit -m "feat: add docker and systemd skill sandboxes"
```

### Task 5: Add MCP discovery and invocation

**Files:**
- Create: `src/agent_hub/mcp/types.py`
- Create: `src/agent_hub/mcp/client.py`
- Create: `src/agent_hub/mcp/service.py`
- Create: `tests/integration/mcp/test_mcp.py`

- [ ] **Step 1: Write allowlist and hot-reload tests**

```python
async def test_unapproved_mcp_tool_is_not_projected(mcp_service) -> None:
    tools = await mcp_service.tools_for_agent("researcher")
    assert "dangerous.delete_all" not in {tool.name for tool in tools}


async def test_config_generation_reloads_remote_server(mcp_service, publish_config) -> None:
    before = await mcp_service.generation_id()
    await publish_config(with_new_mcp_server())
    assert await mcp_service.generation_id() != before
```

- [ ] **Step 2: Implement stdio, SSE, and Streamable HTTP clients**

Validate executable allowlists for stdio and HTTPS/domain allowlists for remote transports. OAuth tokens and headers are SecretRefs. Discover tools into a generation snapshot, project only policy-approved schemas, and route every call back through CapabilityGateway.

- [ ] **Step 3: Add health, timeout, cancellation, and audit events**

Run: `uv run pytest tests/integration/mcp/test_mcp.py -q`

Expected: transport, allowlist, authentication reference, hot reload, timeout, cancellation, and audit tests pass.

- [ ] **Step 4: Commit MCP support**

```bash
git add src/agent_hub/mcp tests/integration/mcp
git commit -m "feat: add policy controlled mcp support"
```

### Task 6: Implement layered memory with user controls

**Files:**
- Create: `src/agent_hub/memory/types.py`
- Create: `src/agent_hub/memory/repository.py`
- Create: `src/agent_hub/memory/service.py`
- Create: `tests/integration/memory/test_memory.py`

- [ ] **Step 1: Write isolation, filtering, and forget tests**

```python
async def test_memory_is_tenant_and_user_isolated(memory_service) -> None:
    await memory_service.add_candidate(tenant="a", user="u1", text="prefers concise answers")
    assert await memory_service.search(tenant="b", user="u1", query="answers") == []


async def test_secret_like_candidate_is_rejected(memory_service) -> None:
    result = await memory_service.add_candidate(tenant="a", user="u1", text="API key sk-secret-value")
    assert result.status == "rejected_sensitive"
```

- [ ] **Step 2: Implement working, episodic, and core memory records**

Every record carries tenant, user, source run/event, category, confidence, created/updated timestamps, expiry, and deletion tombstone. Core memory promotion requires stable facts or explicit user confirmation; prompt-like instructions from retrieved external content are never promoted.

- [ ] **Step 3: Implement inspect, edit, forget, retention, and audit**

Run: `uv run pytest tests/integration/memory/test_memory.py -q`

Expected: isolation, deduplication, expiry, sensitive filtering, inspect, edit, forget, and tombstone tests pass.

- [ ] **Step 4: Commit memory service**

```bash
git add src/agent_hub/memory tests/integration/memory
git commit -m "feat: add tenant isolated layered memory"
```

### Task 7: Add knowledge retrieval and bounded context building

**Files:**
- Create: `src/agent_hub/knowledge/ingest.py`
- Create: `src/agent_hub/knowledge/retrieval.py`
- Create: `src/agent_hub/context/builder.py`
- Create: `src/agent_hub/context/compaction.py`
- Create: `tests/integration/knowledge/test_retrieval.py`
- Create: `tests/unit/context/test_builder.py`

- [ ] **Step 1: Write citation and token-budget tests**

```python
async def test_hybrid_retrieval_returns_source_citations(retriever) -> None:
    hits = await retriever.search("refund policy", limit=5)
    assert hits
    assert all(hit.source_uri and hit.version for hit in hits)


def test_context_builder_never_exceeds_budget(context_builder) -> None:
    context = context_builder.build(huge_session(), token_budget=4000)
    assert context.estimated_tokens <= 4000
    assert context.sections[-1].name == "current_user_request"
```

- [ ] **Step 2: Implement versioned document ingestion and hybrid retrieval**

Chunk by headings and semantic boundaries, store source/version/hash, generate embeddings through a configured embedding logical model, and combine normalized keyword rank with vector rank. Apply tenant ACL before ranking and return citations with each hit.

- [ ] **Step 3: Implement deterministic context priority and compaction**

Order: system/policy, current request, approvals, active plan/checkpoint, relevant core memory, episodic memory, knowledge hits, recent transcript, older compacted summary. Compaction creates a versioned summary Artifact and never drops unresolved approvals or current constraints.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/integration/knowledge tests/unit/context -q`

Expected: ACL, versioning, hybrid ranking, citations, prompt-injection labeling, token budget, and compaction preservation tests pass.

```bash
git add src/agent_hub/knowledge src/agent_hub/context tests/integration/knowledge tests/unit/context
git commit -m "feat: add cited knowledge and context compaction"
```

### Task 8: Add schedules, follow-ups, and reviewed improvement drafts

**Files:**
- Create: `src/agent_hub/scheduler/types.py`
- Create: `src/agent_hub/scheduler/service.py`
- Create: `src/agent_hub/improvements/service.py`
- Create: `tests/integration/scheduler/test_scheduler.py`
- Create: `tests/integration/improvements/test_improvements.py`

- [ ] **Step 1: Write deduplication and no-auto-publish tests**

```python
async def test_repeated_scheduler_tick_submits_once(scheduler, tick_twice) -> None:
    await tick_twice("daily-report", at="2026-08-06T09:00:00+08:00")
    assert await scheduler.submission_count("daily-report", "2026-08-06T09:00:00+08:00") == 1


async def test_improvement_is_only_a_draft(improvement_service) -> None:
    proposal = await improvement_service.propose_prompt_change(sample_run())
    assert proposal.status == "draft"
    assert proposal.published_config_version is None
```

- [ ] **Step 2: Implement schedules as ordinary TaskRequest producers**

Support one-time and cron schedules with IANA timezone, owner, mode, workflow, budget, next fire time, misfire policy, and idempotency key. Scheduler cannot bypass routing, model capacity, capability policy, or approval.

- [ ] **Step 3: Implement follow-up and improvement drafts**

Unfinished tasks may schedule a user-visible follow-up. Improvement analysis may create a draft Prompt/workflow/Skill diff plus evaluation cases, but only ConfigService/SkillService administrator approval can publish it.

- [ ] **Step 4: Verify the phase and commit**

Run: `uv run ruff check . && uv run mypy src && uv run pytest tests/unit/capabilities tests/unit/skills tests/unit/context tests/contracts/test_skill_sandbox.py tests/integration/capabilities tests/integration/skills tests/integration/mcp tests/integration/memory tests/integration/knowledge tests/integration/scheduler tests/integration/improvements -q`

Expected: all checks pass and there is no direct executor path outside CapabilityGateway.

```bash
git add src tests alembic
git commit -m "feat: complete capabilities memory and scheduling"
```
