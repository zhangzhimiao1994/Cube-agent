# Agent Hub Handoff

## 2026-08-12 Feishu 204 Ack Fix

Current state:

- Feishu event log showed `success=fail` with `httpCode=204` for `im.chat.access_event.bot_p2p_chat_entered_v1`.
- Root cause: Agent Hub treated valid-but-unsupported Feishu platform events as ignored and returned HTTP 204. Feishu event subscriptions expect HTTP 200 within the timeout window, so the platform marked delivery as failed even though the request reached the server.
- Debug server `103.236.98.133` has been updated incrementally with the backend fix and `agent-hub-api` was restarted.

Changes made:

- `src/agent_hub/channels/feishu/webhook.py`: valid Feishu events that do not normalize into user messages now return `200 {"accepted": true, "ignored": true}` instead of HTTP 204.
- Added API regression coverage for `im.chat.access_event.bot_p2p_chat_entered_v1` so this behavior does not regress.

Verification performed:

- TDD red: new test first failed with `assert 204 == 200`.
- Local: `uv run pytest tests/api/test_channel_webhooks.py -q` → 18 passed.
- Local: `uv run ruff check src tests/api/test_channel_webhooks.py` → all checks passed.
- Local: `uv run mypy --strict src tests` → success, no issues in 239 source files.
- Server: `systemctl restart agent-hub-api`, then `/health/ready` returned `{"status":"ok"}`.
- Server local Feishu probe using the saved Feishu channel config returned `status=200 body={"accepted":true,"ignored":true}` for the same event class.

Remaining risks / TODOs:

- `bot_p2p_chat_entered` is only an entry/access event; it is expected to be acknowledged but not answered by the Agent.
- To get a bot reply, Feishu must deliver `im.message.receive_v1` events to the same callback URL, and the app must have the matching message receive permissions/events enabled and published.

## 2026-08-12 CI Static Check Follow-up

Current state:

- `main` has been fast-forwarded and pushed to GitHub at commit `89d3e37` (`Fix CI static checks`).
- The latest GitHub Actions run for `main` is green: `quality` run `31584869891` completed successfully.
- Local working tree still has untracked `.tmp/` smoke/debug files from prior server probes; they were intentionally not committed.

Changes made:

- Fixed ruff import formatting in `src/agent_hub/app.py`, `tests/unit/runtime/test_configured_runtime.py`, and `tests/unit/test_app_wiring.py`.
- Changed two invalid-type routing policy validation errors from `ValueError` to `TypeError` in `src/agent_hub/routing/service.py`, matching the repository ruff rule.
- Made the sequential route-classifier path validate classifier results before returning them, matching the existing parallel path and satisfying mypy without weakening defensive checks.
- Tightened test typing for the main-agent capacity helper and JSON routing fixture.

Verification performed:

- Local: `uv run ruff check src tests` → all checks passed.
- Local: `uv run mypy --strict src tests` → success, no issues in 239 source files.
- Local: `uv run pytest tests/unit tests/api -q` → 1143 passed, 13 skipped.
- Local full `uv run pytest -q` was attempted but integration tests timed out because the Windows machine did not have the test PostgreSQL fixture reachable at `127.0.0.1:54329`; this was environmental and CI later ran the compose-backed integration suite successfully.
- GitHub Actions `quality` run `31584869891` passed all stages: ruff, mypy, docker compose test DB startup, full pytest, frontend lint/test/build, install script syntax/shellcheck/bats, and compose config.

Remaining risks / TODOs:

- Continue feature/server validation work from the Feishu/channel and conversation-process backlog below.
- Feishu real platform callback still needs a real event from Feishu after callback URL and event subscription are confirmed in the Feishu console; synthetic route probes only prove Agent Hub and Caddy accept the path.

## 2026-08-12

Current state:

- Local code includes conversation UI, runtime routing, native systemd, and timeout fixes.
- Debug server `103.236.98.133` has been synced with the current `src/`, `web/dist`, and native systemd template changes.
- Server systemd now sets `PYTHONPATH=/opt/agent-hub/current/src` for `agent-hub-api` and `agent-hub-worker`; this fixes the previous issue where services imported stale `.venv/site-packages` code instead of the current release source.

Changes made:

- Conversation process rows now show concise one-line action summaries; details stay in the process drawer.
- Bare/generic `artifact.created` rows without useful payload are hidden from the main process stream.
- Process detail drawer includes actor/model-related metadata when available.
- Conversation page no longer exposes the old inline role-pool/direct-answerer guide block that made the chat feel like a task dispatch form.
- Direct mode remains model-based, not child-agent-based.
- Auto mode no longer silently uses the stale local fallback path on the server after PYTHONPATH fix.
- Native systemd templates add `PYTHONPATH=/opt/agent-hub/current/src`.
- Dispatch role step timeout is relaxed from a 45s minimum to a 120s minimum / 300s cap so slower compatible model APIs do not fail trivial dispatch runs too aggressively.
- Hybrid discussion handoff now removes raw `model_response` artifacts that are already wrapped by user-readable `text` artifacts. This prevents the dispatch stage from doubling the discussion prompt with duplicate content.
- AutoGen-style discussion plans now bound participant output to 1536 tokens and selector output to 512 tokens. The server failure showed the hybrid discussion phase hitting a 60s provider transport timeout after receiving a large duplicated handoff.

Verification performed:

- Local frontend: `npm.cmd test -- --run src/pages/OperationalPages.test.tsx` → 31 passed.
- Local frontend build: `npm.cmd run build` → success, Vite chunk-size warning only.
- Local backend tests: `uv run pytest tests/unit/runtime/test_hybrid.py tests/unit/runtime/test_configured_runtime.py tests/api/test_runs_api.py tests/unit/runs/test_temporary_agent.py tests/unit/install/test_native_install_scripts.py -q` → 55 passed.
- Local lint: `uv run ruff check src tests/api/test_runs_api.py tests/unit/runs/test_temporary_agent.py tests/unit/runtime/test_configured_runtime.py tests/unit/runtime/test_hybrid.py tests/unit/install/test_native_install_scripts.py tests/integration/runtime/test_hybrid_runtime.py` → all checks passed.
- Server health: `curl http://127.0.0.1:8000/health/live`, `curl http://127.0.0.1:8000/health/ready`, and Caddy `/` all returned OK/200.
- Server direct smoke: authenticated API, model list, run submit, worker execution, and conversation read completed.
- Server conversation smoke: two direct turns in the same `conversation_id` completed and the conversation retained 2 runs.
- Server auto smoke: ambiguous `auto` input now returns `waiting_user_mode` with `router_unavailable`, not silent direct fallback.
- Server mode smoke after latest sync:
  - dispatch run `9ae78681-9b3b-4bd7-be87-55c769a142d1` → `completed`.
  - discuss run `a298fb64-f545-44ff-9b3c-93f822ff7eb9` → `completed`.
  - hybrid run `e6acfc61-b923-4ff2-88f2-788a43957d30` → `completed`.
- Server hybrid discussion input audit for `e6acfc61-b923-4ff2-88f2-788a43957d30`: `discussion_input_count=6`, all input types were `text`, confirming duplicate raw `model_response` handoff was removed.

Remaining risks / TODOs:

- Server currently reports `main_agent_model=None` and `agents=0`; full production-quality multi-agent behavior still depends on configuring the main Agent model and persistent role pool in the UI.
- Local integration runtime tests that require the project’s local test PostgreSQL fixture were not used as the final local gate on this Windows machine because the fixture connection timed out on `127.0.0.1:54329`. Equivalent runtime paths were verified against the deployed server with the real PostgreSQL/Redis/model configuration.
- The frontend bundle remains above Vite’s 500 kB warning threshold; not a functional blocker, but code-splitting should be considered later.
- PowerShell output may show Chinese text as mojibake; source files themselves are UTF-8 and were verified by Python reads.

## 2026-08-12 Process Timeline Follow-up

Current state:

- Local source has been updated for Kimi/Codex-style process timeline rendering.
- Debug server `103.236.98.133` has been synced with the latest changed source files and rebuilt `web/dist`.
- Server UI is reachable from the external forwarded address `http://113.142.217.42:21015/` and the served `index.html` points to `/assets/index-CyNxpLNi.js`.
- No GitHub push has been performed for this follow-up yet because authenticated production-flow verification is still blocked by lack of normal login credentials/session.

Changes made:

- Admin run event responses now include nested `artifact` data when an event carries it, so the web UI can show the concrete output tied to that exact event instead of guessing from the global artifact list.
- Frontend API schema accepts `event.artifact`.
- Conversation process rows now preserve event order and no longer deduplicate repeated-looking action messages. This keeps `main dispatch -> subagent task -> model call -> output -> discussion -> decision` as separate chronological rows.
- `discussion.completed` is split into a discussion summary row, per-role `*_opinion` rows, and a separate `主 Agent 裁决` row when judgement data exists.
- Process row details now include available model/provider, executor, participants, tool/step, instruction/payload, and concrete artifact output.
- Artifact fallback is stricter: it only uses explicit artifact IDs or actor-title matches, avoiding accidental use of the final assistant reply as a process-row output.
- Added UI regression coverage that verifies old conversation messages are restored after starting a new chat and then reopening the old conversation.
- Fixed the local sync packaging mistake: `web/dist` must be copied with wildcard expansion (`Copy-Item -Path web\dist\*`) or the server will get an empty dist directory.

Verification performed:

- Local targeted UI tests:
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx -t "restores historical conversation messages"` → passed.
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx -t "ordered timeline"` → passed.
- Local full UI tests: `npm.cmd test -- --run` → 64 passed.
- Local frontend build: `npm.cmd run build` → success; Vite chunk-size warning only.
- Local backend API tests with project-local temp/cache dirs: `uv run pytest tests/api -q` → 148 passed.
- Local lint/type checks:
  - `uv run ruff check src tests` → passed.
  - `uv run mypy --strict src tests` → passed.
- Server sync:
  - Uploaded minimal package containing changed source files and `web/dist`.
  - Cleared server `web/dist` before extraction to avoid stale assets.
  - Restarted `agent-hub-api` and `agent-hub-worker`, reloaded Caddy.
  - `systemctl is-active agent-hub-api agent-hub-worker caddy` → all active.
  - Internal GET `/` returned the new index after startup wait.
  - External GET `http://113.142.217.42:21015/` returned HTTP 200 and references `/assets/index-CyNxpLNi.js`.
  - Server bundle check: `main_decision=True`, `opinion_line=True`, `vague_result=False`, `asset_count=1`.

Remaining risks / TODOs:

- Authenticated admin/run API verification was not completed in this follow-up. Attempting to mint a super-admin token from `AGENT_HUB_JWT_SIGNING_KEY` was rejected by safety review, and should not be bypassed. Use a normal username/password login flow or have the user trigger a run in the UI, then inspect logs/results.
- Need user-side validation of the actual chat screen after login: the server has the new bundle, but browser cache may need refresh if old JS is cached.
- Do not push until the user confirms authenticated server behavior or provides normal login credentials/session for verification.

## 2026-08-12 Feishu Channel Debugging

Current state:

- User reported that the Feishu channel was configured but still unusable.
- Server being debugged: `103.236.98.133`; public forwarded UI/base URL: `http://113.142.217.42:21015`.
- No code changes were kept for this investigation; temporary local `.tmp` probe files were created and removed.

Evidence gathered:

- FastAPI route exists and is registered: `POST /channels/feishu/events`.
- Server-local route probe returned `401 {"error":"invalid_feishu_event"}` for an empty JSON body, which confirms the route is reached and Feishu verification runs.
- Caddy route probe on server-local port 80 returned the same `401`, confirming Caddy proxies `/channels/*` to API.
- External forwarded probe to `http://113.142.217.42:21015/channels/feishu/events` returned the same `401`, confirming the public callback path is reachable from outside.
- Database channel config for `feishu` exists with keys:
  - `AGENT_HUB_PUBLIC_URL`
  - `FEISHU_APP_ID`
  - `FEISHU_APP_SECRET`
  - `FEISHU_VERIFICATION_TOKEN`
  - `FEISHU_ENCRYPT_KEY`
  - `FEISHU_TRANSPORT`
- Saved `AGENT_HUB_PUBLIC_URL` is `http://113.142.217.42:21015`.
- Field lengths were non-zero for all Feishu fields checked.
- Feishu tenant access token probe using saved App ID/App Secret returned `http_status=200 feishu_code=0 msg=ok` and `has_tenant_access_token=True`.
- Internal simulated Feishu message event using saved App ID/Verification Token returned `202 {"accepted":true}`, proving Agent Hub can accept and submit a Feishu inbound message when the event reaches the API.
- The simulated event then logged `feishu_reply_failed ... reply request failed status=400`, which is expected because the probe used a fake Feishu `message_id`; it does not indicate real-user reply failure.
- Recent API/Caddy logs did not show real Feishu platform message events hitting `/channels/feishu/events`; only local/external probes were visible.

Working hypothesis:

- Agent Hub server-side webhook route, Caddy proxy, public forwarding, saved channel config, and App Secret are functional.
- The remaining likely break is Feishu Open Platform configuration: callback URL, event subscription (`im.message.receive_v1`), app publish/version activation, or bot installation/chat trigger. The server has not received a real Feishu message callback during the inspected window.

Next verification needed:

- In Feishu Open Platform, configure request URL exactly as:
  `http://113.142.217.42:21015/channels/feishu/events`
- Add/enable the message receive event: `im.message.receive_v1`.
- Save/verify the request URL, then publish/release the app configuration if Feishu requires publishing for the app type.
- Send a private message to the bot, or in a group chat mention the bot.
- Immediately check:
  `journalctl -u agent-hub-api --since=-5min --no-pager | grep -i feishu`
  Expected result after a real callback is either a `202 Accepted` route log or a specific `feishu_verification_failed` reason.

Follow-up note:

- User provided a Feishu OpenAPI log for:
  `/open-apis/im/v1/messages/om_probe_41f87395f5d8458aa6463cd20bf53774/reply`
- That `om_probe_*` ID was generated by the internal server-side probe, not by a real Feishu user message.
- Feishu returned `errCode=99992354` / invalid `open_message_id`, which is expected for the fake probe message ID and should not be treated as the production failure.
- Rechecked service logs after `2026-08-12 04:40:00`; only the Feishu callback URL verification request is present:
  `POST /channels/feishu/events HTTP/1.1" 200 OK`.
- No real `im.message.receive_v1` callback has reached Agent Hub yet. The next required evidence is Feishu Open Platform **事件日志检索** for a real user message, not the OpenAPI reply-call log.

## 2026-08-12 Refactor Handoff Index

Created a dedicated refactor handoff document:

- `REFACTOR_HANDOFF.md`

Purpose:

- Keep long-term architecture/refactor direction separate from short-term hotfix handoffs.
- Track the recommended module boundaries before adding larger capabilities such as vibe coding and OpenClaw.
- Record the CowAgent-level usability target: not UI-only prototypes, but deployable, channel-capable, observable, multi-agent usable behavior.
- Future refactor-related decisions should update `REFACTOR_HANDOFF.md` promptly, not only this general handoff file.

## 2026-08-12 Runtime / Main Agent Routing Stabilization

Current state:

- Local code and server `/opt/agent-hub/current` were updated incrementally.
- Server services after deployment:
  - `agent-hub-api`: active
  - `agent-hub-worker`: active
  - Caddy reloaded after latest `web/dist` upload
- Server public UI returned HTML from `http://113.142.217.42:21015/`.

Changes made:

- Main Agent auto routing now uses the separately configured Main Agent model instead of an unavailable/injected-only router path.
- Main Agent routing merges matching registered-model capabilities and quota/capacity metadata when the Main Agent reuses an already registered provider/base/model/key. This avoids capacity fingerprint quota conflicts such as one key being registered under both `main-agent` and `deepseek-account`.
- Main Agent routing now uses provider-compatible plain JSON classification for the Main Agent path. This avoids repeated failures from providers/intermediaries that reject `response_format=json_schema`, while still strictly parsing the returned JSON.
- Main Agent routing runs as a single low-cost classifier call for automatic mode selection. It no longer launches classifier/verifier concurrently against a one-slot model key.
- Generic routing still supports the existing stricter dual-classifier path. Added policy flags:
  - `parallel_classifiers`
  - `allow_single_classifier_decision`
  - `conflict_decision_margin`
- Low-risk classifier/verifier disagreement can now resolve to the clearly higher-confidence assessment instead of always forcing user mode selection.
- AutoGen discussion now completes with `partial_discussion_after_model_failure` when a late model-gateway failure happens after a usable discussion text artifact was already produced.
- Hybrid mode now completes with `partial_hybrid_after_discussion_failure` when dispatch already produced artifacts and the later discussion stage fails due to model gateway failure. Dispatch-stage failures still fail the run.
- Selected configured agents are resolved from `selected_agent_ids` for dispatch/discuss/hybrid without expanding to unrelated planner/reviewer roles.
- AutoGen participant IDs are normalized for framework compatibility so configured IDs with `-`/`.` do not crash AutoGen.

Server verification performed:

- Real server five-mode smoke passed with configured model:
  - direct: completed
  - auto: completed
  - dispatch: completed
  - discuss: completed
  - hybrid: completed
- Server capability smoke passed:
  - health
  - login
  - user create/reset/delete
  - agent/workflow create/delete
  - archive upload/extract
  - skill upload/approve/delete
  - MCP create/delete
  - memory create/update/delete
  - Hermes feedback/confirm/recommend
  - channel list
  - direct model run completed
- Feishu status in capability smoke is `configured`; real Feishu user-message callback still needs platform-side event-log verification as described in the Feishu section above.

Local verification performed:

- Backend/runtime/routing focused tests:
  - `uv run pytest tests/unit/runtime/test_hybrid.py tests/unit/routing/test_router.py tests/unit/test_app_wiring.py tests/unit/runtime/test_autogen_artifact_rollback.py tests/unit/runtime/test_configured_runtime.py -q`
  - Result: 154 passed
- Backend API/Skill/Run tests:
  - `uv run pytest tests/api/test_admin_resources.py tests/api/test_foundation_api.py tests/api/test_runs_api.py tests/unit/skills/test_package.py -q`
  - Result: 158 passed
- Frontend tests:
  - `npm test -- --run src/pages/OperationalPages.test.tsx src/pages/UsersPage.test.tsx` from `web/`
  - Result: 39 passed
- Frontend production build:
  - `npm.cmd run build` from `web/`
  - Result: passed; Vite emitted only a chunk-size warning.
- `git diff --check`:
  - No whitespace errors; only CRLF normalization warnings in the Windows working tree.

Remaining risks / TODOs:

- Feishu real reply still needs a real Feishu event callback to reach `/channels/feishu/events`; synthetic `om_probe_*` reply failures are expected and not proof of production reply failure.
- Server Main Agent was switched from MiniMax to the already working DeepSeek registered model during verification because MiniMax pure text and structured-output calls failed through the current server gateway. The code now handles such provider incompatibility better, but a bad Main Agent key/model will still correctly ask for user choice instead of silently falling back.
- UI behavior still needs user-side browser validation for the exact mobile interaction details: process-line layout, handoff button placement, and no history overwrite.

## 2026-08-12 Navigation Consolidation

Current state:

- Local code and server `/opt/agent-hub/current` were updated incrementally.
- Public UI entry `http://113.142.217.42:21015/` returned HTTP 200.
- Current frontend assets returned HTTP 200:
  - `/assets/index-BKz7GAiS.js`
  - `/assets/index-DWQw7Ted.css`

Changes made:

- Consolidated the left navigation into 6 top-level categories:
  - 对话
  - 编排
  - 资源
  - 工具
  - 通道
  - 系统
- Kept all existing functional pages reachable through module hub cards instead of deleting routes.
- Updated module hub labels/descriptions so users enter specific pages through colored module cards.
- Fixed AppShell, module hub, login page, first-run setup page, and related tests to use stable UTF-8 Chinese copy.
- Kept permission filtering in the navigation unchanged: modules still only appear when the current user has the required permission.

Verification performed:

- Frontend focused test:
  - `npm.cmd test -- --run src/app/AppShell.test.tsx`
  - Result: 2 passed
- Frontend type check:
  - `npm.cmd run lint`
  - Result: passed
- Full frontend test suite:
  - `npm.cmd test -- --run`
  - Result: 68 passed
- Frontend production build:
  - `npm.cmd run build`
  - Result: passed; Vite emitted only the existing chunk-size warning.
- Server deployment:
  - Uploaded incremental package `.tmp/nav-consolidation-web.tar.gz` to `/tmp/agent-hub-nav-consolidation.tar.gz`.
  - Extracted in `/opt/agent-hub/current`.
  - Reloaded Caddy.
  - `curl -fsS http://127.0.0.1:8000/health/ready` returned `{"status":"ok"}`.
  - External HTML and asset HEAD checks returned HTTP 200.

Remaining risks / TODOs:

- Browser-side visual acceptance still depends on user checking the exact mobile sidebar/header feel.
- This change intentionally did not refactor the underlying route tree; old direct routes remain available so bookmarks and internal links keep working.

## 2026-08-12 Floating Navigation Drawer

Current state:

- The console still uses 6 top-level navigation groups, but the visual frame was changed from a wide left sidebar to a compact floating left rail with a second-level drawer.
- Server `/opt/agent-hub/current` was updated incrementally, not through a full release upload.
- Public UI entry `http://113.142.217.42:21015/` returned HTTP 200 after deployment.
- Current frontend assets returned HTTP 200:
  - `/assets/index-DDQyvCTw.js`
  - `/assets/index-CXTG4MI5.css`

Changes made:

- `AppShell` now renders:
  - compact floating navigation rail;
  - 6 top-level groups;
  - second-level drawer for the active or hovered group;
  - drawer links filtered by the current user's permissions.
- Drawer title no longer uses a page-level heading. This avoids confusing tests and assistive navigation with actual page headings such as the chat page title.
- Desktop layout uses a narrow left rail and floating drawer.
- Mobile layout keeps the top-level groups horizontally scrollable and places the drawer below it.
- Tests now cover that the drawer exposes second-level module links.
- Login-page navigation test was adjusted for duplicated module links intentionally appearing both in drawer and hub content.

Verification performed:

- Focused drawer test:
  - `npm.cmd test -- --run src/app/AppShell.test.tsx`
  - Result: 2 passed
- Frontend type check:
  - `npm.cmd run lint`
  - Result: passed
- Full frontend test suite:
  - `npm.cmd test -- --run`
  - Result: 68 passed
- Frontend production build:
  - `npm.cmd run build`
  - Result: passed; Vite emitted only the existing chunk-size warning.
- Server deployment:
  - Uploaded `.tmp/nav-floating-drawer-web.tar.gz` to `/tmp/agent-hub-nav-floating-drawer.tar.gz`.
  - Extracted in `/opt/agent-hub/current`.
  - Reloaded Caddy.
  - `curl -fsS http://127.0.0.1:8000/health/ready` returned `{"status":"ok"}`.
  - External HTML and asset HEAD checks returned HTTP 200.

Backlog / deferred:

- Feishu currently supports enterprise self-built app style channel integration. The user wants future support for configuring Feishu as a native Feishu intelligent agent. This is intentionally deferred until after the current UI/Agent flow work stabilizes.
