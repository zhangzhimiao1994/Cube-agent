# Agent Hub Phase 4 Feishu and Web Interfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users operate Agent Hub through Feishu private/group conversations and a secure Web management console, including configuration, approvals, Skills/MCP, model capacity, image input, and run visibility.

**Architecture:** `ChannelGateway` normalizes Feishu WebSocket and Webhook events into channel-independent messages and sends them to application services. Feishu rendering consumes normalized outbound events. The React console calls versioned FastAPI endpoints and never talks to framework adapters or databases directly.

**Tech Stack:** Feishu/Lark Python SDK, FastAPI, WebSocket/Webhook, React 19, TypeScript, Vite, React Router, TanStack Query, Zod, Vitest, Testing Library, Playwright.

---

## File map

- `src/agent_hub/channels/base.py`, `gateway.py`: channel contracts and deduplication.
- `src/agent_hub/channels/feishu/`: SDK clients, WebSocket, Webhook, verification, commands, cards, images, and outbound delivery.
- `src/agent_hub/auth/feishu_oauth.py`: OAuth callback and account binding.
- `src/agent_hub/api/routers/`: REST endpoints for every console resource.
- `web/src/`: login/setup, application shell, configuration, runs, models, Skills, MCP, memory, users, and audit.
- `tests/e2e/feishu/`, `web/e2e/`: interface-level behavior.

### Task 1: Define channel contracts and durable inbound deduplication

**Files:**
- Create: `src/agent_hub/channels/base.py`
- Create: `src/agent_hub/channels/gateway.py`
- Create: `src/agent_hub/channels/dedup.py`
- Create: `tests/unit/channels/test_gateway.py`
- Create: `tests/integration/channels/test_dedup.py`

- [ ] **Step 1: Write normalization and duplicate tests**

```python
async def test_duplicate_message_submits_one_task(channel_gateway, same_message_twice) -> None:
    first, second = await same_message_twice(channel_gateway)
    assert first.accepted is True
    assert second.duplicate is True
    assert first.run_id == second.run_id


def test_group_message_requires_mention(normalizer) -> None:
    assert normalizer.normalize(group_text(mentioned=False)) is None
```

- [ ] **Step 2: Define normalized channel types**

```python
class InboundAttachment(BaseModel):
    kind: Literal["image", "file"]
    external_key: str
    filename: str | None = None
    declared_mime: str | None = None


class InboundMessage(BaseModel):
    channel: Literal["feishu", "web"]
    tenant_external_id: str
    sender_external_id: str
    conversation_external_id: str
    message_id: str
    event_id: str
    conversation_type: Literal["private", "group"]
    text: str
    mentions_bot: bool
    attachments: list[InboundAttachment]
    received_at: datetime
```

Outbound types are progress, clarification, approval, final answer, error, and file/image response. No channel-specific card JSON enters application services.

- [ ] **Step 3: Implement PostgreSQL idempotency reservation**

Reserve `(channel, tenant_external_id, event_id)` and `(channel, message_id)` before submission with unique constraints. A retry reads and returns the existing run ID. Expiry cleanup may remove only old completed records, never active references.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/unit/channels tests/integration/channels -q`

Expected: private/group normalization, mention filtering, duplicate event, duplicate message, and concurrent arrival tests pass.

```bash
git add src/agent_hub/channels tests/unit/channels tests/integration/channels alembic
git commit -m "feat: add normalized channel gateway"
```

### Task 2: Add Feishu WebSocket and Webhook receivers

**Files:**
- Create: `src/agent_hub/channels/feishu/settings.py`
- Create: `src/agent_hub/channels/feishu/verify.py`
- Create: `src/agent_hub/channels/feishu/webhook.py`
- Create: `src/agent_hub/channels/feishu/websocket.py`
- Create: `src/agent_hub/channels/feishu/normalize.py`
- Create: `tests/contracts/feishu/test_receivers.py`

- [ ] **Step 1: Write receiver parity and verification tests**

```python
@pytest.mark.parametrize("transport", ["webhook", "websocket"])
async def test_receivers_produce_same_inbound_message(receiver_factory, transport) -> None:
    message = await receiver_factory(transport).receive(feishu_private_text_fixture())
    assert message.message_id == "om_123"
    assert message.text == "hello"


async def test_webhook_rejects_invalid_signature(webhook_client) -> None:
    response = await webhook_client.post("/channels/feishu/events", content=b"{}", headers={"X-Lark-Signature": "bad"})
    assert response.status_code == 401
```

- [ ] **Step 2: Implement encrypted event verification and URL challenge**

Use official SDK verification where available; validate timestamp freshness, signature/token, encrypt key, app/tenant identity, and challenge response. Return quickly after durable idempotency reservation and queue submission.

- [ ] **Step 3: Implement the long-running WebSocket connector**

Build a reconnect loop with jitter, readiness state, credential refresh, graceful shutdown, duplicate handling, and structured connection metrics. `FEISHU_TRANSPORT=websocket|webhook|both` selects active receivers.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/contracts/feishu/test_receivers.py -q`

Expected: WS/Webhook parity, challenge, signature, encrypted event, reconnect, shutdown, duplicate, private message, and group mention tests pass.

```bash
git add src/agent_hub/channels/feishu tests/contracts/feishu pyproject.toml uv.lock
git commit -m "feat: add feishu websocket and webhook receivers"
```

### Task 3: Add commands, cards, approvals, and text fallback

**Files:**
- Create: `src/agent_hub/channels/feishu/commands.py`
- Create: `src/agent_hub/channels/feishu/cards.py`
- Create: `src/agent_hub/channels/feishu/delivery.py`
- Create: `tests/unit/channels/feishu/test_commands.py`
- Create: `tests/integration/channels/feishu/test_fallback.py`

- [ ] **Step 1: Write command authorization and fallback tests**

```python
def test_non_admin_cannot_publish_config(command_service, ordinary_user) -> None:
    result = command_service.parse("/config publish 12", ordinary_user)
    assert result.error_code == "permission_denied"


async def test_clarification_card_failure_sends_text(delivery, feishu_api) -> None:
    feishu_api.fail_cards_with(403)
    await delivery.send_clarification(sample_choice())
    assert feishu_api.last_text.startswith("请选择执行模式")
```

- [ ] **Step 2: Implement strict command grammar**

Support `/auto`, `/direct`, `/dispatch`, `/discuss`, `/hybrid`, `/status`, `/details`, `/pause`, `/resume`, `/cancel`, structured `/config`, `/skill`, and `/mcp`. Parse with explicit subcommand models; reject unknown flags and never pass free-form command text to a shell or YAML loader.

- [ ] **Step 3: Implement interactive cards with signed actions**

Mode choice and approval cards carry an opaque action ID tied to tenant, user, run, normalized argument hash, and expiry. Validate the actor and consume actions once. If card send/update fails, send a numbered text prompt; replies `1`–`4` bind to the pending clarification for that conversation.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/unit/channels/feishu tests/integration/channels/feishu/test_fallback.py -q`

Expected: all commands, RBAC, card signature, replay, wrong user, expiry, card failure, and text-choice tests pass.

```bash
git add src/agent_hub/channels/feishu tests/unit/channels/feishu tests/integration/channels/feishu
git commit -m "feat: add feishu commands cards and fallback"
```

### Task 4: Handle images, progress, details, and final replies

**Files:**
- Create: `src/agent_hub/channels/feishu/media.py`
- Create: `src/agent_hub/channels/feishu/render.py`
- Create: `tests/e2e/feishu/test_conversation.py`

- [ ] **Step 1: Write image and progress E2E tests**

```python
async def test_image_message_routes_to_vision(feishu_fixture, vision_gateway) -> None:
    await feishu_fixture.send_private_image("cat.png")
    assert vision_gateway.last_request.required_capabilities == {"vision", "structured_output"}
    assert "图片" in feishu_fixture.last_reply_text


async def test_default_reply_hides_internal_discussion(feishu_fixture) -> None:
    await feishu_fixture.send("/discuss evaluate proposal")
    assert "最终结论" in feishu_fixture.last_reply_text
    assert "internal_turn" not in feishu_fixture.last_reply_text
```

- [ ] **Step 2: Implement authorized media download**

Download by Feishu message/resource key using tenant token, enforce streaming byte limit, pass declared and detected MIME to image intake, and never log binary data or signed URLs. Store channel metadata on the resulting Artifact.

- [ ] **Step 3: Implement rate-aware rendering**

Coalesce rapid progress into phase updates, split long answers on semantic boundaries, include citations and warnings, and expose explicit discussion/tool events only through `/details` after RBAC filtering.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/e2e/feishu/test_conversation.py -q`

Expected: text, group mention, multiple images, invalid image, progress coalescing, details, final split, cancellation, and vision failure tests pass.

```bash
git add src/agent_hub/channels/feishu tests/e2e/feishu
git commit -m "feat: complete feishu task conversations"
```

### Task 5: Scaffold the Web console and local authentication

**Files:**
- Create: `web/package.json`
- Create: `web/vite.config.ts`
- Create: `web/src/main.tsx`
- Create: `web/src/app/router.tsx`
- Create: `web/src/api/client.ts`
- Create: `web/src/auth/AuthProvider.tsx`
- Create: `web/src/pages/SetupPage.tsx`
- Create: `web/src/pages/LoginPage.tsx`
- Create: `web/src/app/AppShell.tsx`
- Create: `web/src/pages/LoginPage.test.tsx`

- [ ] **Step 1: Scaffold and write the failing login test**

Run: `npm create vite@latest web -- --template react-ts && npm --prefix web install`

```tsx
it("logs in and opens the dashboard", async () => {
  render(<TestApp initialPath="/login" />)
  await userEvent.type(screen.getByLabelText("用户名"), "owner")
  await userEvent.type(screen.getByLabelText("密码"), "correct horse battery staple")
  await userEvent.click(screen.getByRole("button", { name: "登录" }))
  expect(await screen.findByRole("heading", { name: "运行概览" })).toBeVisible()
})
```

- [ ] **Step 2: Install exact console dependencies**

Run: `npm --prefix web install react-router-dom @tanstack/react-query zod react-hook-form @hookform/resolvers && npm --prefix web install -D vitest jsdom @testing-library/react @testing-library/user-event @playwright/test eslint`

- [ ] **Step 3: Implement setup, login, protected routing, and session handling**

Use an HttpOnly, Secure, SameSite=Lax session cookie returned by the API; do not store access tokens in localStorage. Setup form accepts one-time code, username, and password. AppShell shows only navigation allowed by `/api/v1/me` permissions.

- [ ] **Step 4: Run tests and commit**

Run: `npm --prefix web run test -- --run && npm --prefix web run build`

Expected: setup, login, invalid credentials, expiry, logout, protected route, and permission navigation tests pass; production build succeeds.

```bash
git add web
git commit -m "feat: add web setup and local login"
```

### Task 6: Add Feishu OAuth and administrator management

**Files:**
- Create: `src/agent_hub/auth/feishu_oauth.py`
- Create: `src/agent_hub/api/routers/users.py`
- Create: `web/src/pages/UsersPage.tsx`
- Create: `tests/integration/auth/test_feishu_oauth.py`
- Create: `web/src/pages/UsersPage.test.tsx`

- [ ] **Step 1: Write OAuth state and last-super-admin tests**

```python
async def test_oauth_rejects_replayed_state(oauth_service) -> None:
    state = await oauth_service.issue_state()
    await oauth_service.consume_state(state, code="first")
    with pytest.raises(OAuthStateError):
        await oauth_service.consume_state(state, code="second")


async def test_cannot_demote_last_super_admin(user_service, owner) -> None:
    with pytest.raises(LastSuperAdminError):
        await user_service.change_role(owner.id, "admin")
```

- [ ] **Step 2: Implement PKCE/state OAuth and account binding**

Bind returned Feishu `open_id` only after state, tenant, redirect URI, and code exchange validation. Existing logged-in admins may bind; unknown users require an administrator invitation. Protect OAuth and local login with rate limits and audit events.

- [ ] **Step 3: Add user/RBAC Web management and tests**

Run: `uv run pytest tests/integration/auth/test_feishu_oauth.py -q && npm --prefix web run test -- --run UsersPage`

Expected: login, bind, invitation, replay, wrong tenant, role change, disable, and last-super-admin protections pass.

- [ ] **Step 4: Commit OAuth and users**

```bash
git add src/agent_hub/auth src/agent_hub/api/routers/users.py tests/integration/auth web/src
git commit -m "feat: add feishu oauth and user administration"
```

### Task 7: Build configuration, models, concurrency, and workflow pages

**Files:**
- Create: `web/src/pages/ConfigPage.tsx`
- Create: `web/src/pages/ModelsPage.tsx`
- Create: `web/src/pages/AgentsPage.tsx`
- Create: `web/src/pages/WorkflowsPage.tsx`
- Create: `web/src/components/ConfigDiff.tsx`
- Create: `web/src/pages/ModelsPage.test.tsx`
- Create: `tests/api/test_admin_resources.py`

- [ ] **Step 1: Write model-pool and publish tests**

```tsx
it("shows that a serial key contributes one slot", async () => {
  render(<ModelsPage />)
  expect(await screen.findByText("最大并发：1"))
  expect(screen.getByText("满载策略：先排队，超时后降级"))
})
```

- [ ] **Step 2: Expose typed CRUD/test APIs**

Add models/deployments, credential create/rotate, concurrency probe, agents, workflows, YAML import/export, drafts, schema validation, Diff, publish, and rollback. Secret create responses return a reference and last four identifier only; GET never returns secret values or fingerprints.

- [ ] **Step 3: Implement Web forms and immutable publish flow**

Model form edits provider, base URL, logical model, capabilities, max concurrency, RPM/TPM, queue timeout, fallback, weight, and secret reference. Show probe output as a recommendation requiring explicit save. Configuration pages edit a draft and require Diff confirmation before publish.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/api/test_admin_resources.py -q && npm --prefix web run test -- --run && npm --prefix web run build`

Expected: CRUD, validation, secret redaction, duplicate fingerprint, probe, draft, Diff, publish conflict, and rollback tests pass.

```bash
git add src/agent_hub/api web/src tests/api
git commit -m "feat: add web model and configuration management"
```

### Task 8: Complete operational management pages and interface E2E

**Files:**
- Create: `web/src/pages/RunsPage.tsx`
- Create: `web/src/pages/RunDetailPage.tsx`
- Create: `web/src/pages/SkillsPage.tsx`
- Create: `web/src/pages/McpPage.tsx`
- Create: `web/src/pages/MemoryPage.tsx`
- Create: `web/src/pages/AuditPage.tsx`
- Create: `web/e2e/admin.spec.ts`
- Create: `web/e2e/task.spec.ts`

- [ ] **Step 1: Write browser journeys**

```ts
test("administrator uploads and approves a skill", async ({ page }) => {
  await loginAsAdmin(page)
  await page.goto("/skills")
  await page.getByLabel("Skill ZIP").setInputFiles("web/e2e/fixtures/safe-skill.zip")
  await page.getByRole("button", { name: "上传" }).click()
  await expect(page.getByText("隔离待审")).toBeVisible()
  await page.getByRole("button", { name: "批准并启用" }).click()
  await expect(page.getByText("已启用")) .toBeVisible()
})
```

- [ ] **Step 2: Implement run and governance pages**

Runs show status, mode, queue/capacity wait, cost, events, Artifacts, pause/resume/cancel, and explicit details. Skills show scan Diff and requested permissions. MCP shows health and allowed tools. Memory supports inspect/edit/forget. Audit supports filters and export without secrets.

- [ ] **Step 3: Run full interface verification**

Run: `uv run pytest tests/contracts/feishu tests/e2e/feishu tests/api -q && npm --prefix web run lint && npm --prefix web run test -- --run && npm --prefix web run build && npm --prefix web exec playwright test`

Expected: all backend and browser tests pass, including card-to-text fallback, image task, config publish, model pool display, approval, and audit.

- [ ] **Step 4: Commit the interface checkpoint**

```bash
git add src tests web
git commit -m "feat: complete feishu and web management"
```
