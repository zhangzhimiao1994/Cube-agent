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

## 2026-08-12 Runtime Root-Cause Fix: Capability Exposure and Final Synthesis Timeout

Current state:

- The server at `103.236.98.133` was updated incrementally in `/opt/agent-hub/current`.
- `agent-hub-api` and `agent-hub-worker` are active.
- `/health` and `/health/ready` return `{"status":"ok"}`.
- Production smoke runs on the server completed for direct, auto, dispatch, discuss, and hybrid modes.

Root causes found:

- Dispatch role plans exposed unavailable skills/tools as callable capabilities. Models then called tools such as role-named pseudo-tools, and the runtime failed with `CapabilityOutcomeUncertain`.
- OpenAI-compatible providers can return tool-call-only messages with empty assistant text. The runtime treated those as invalid text responses even when valid tool calls were present.
- The final dispatch synthesizer received every upstream role output as full text. For larger multi-role tasks this produced very large prompts, and the final synthesizer timed out even though reviewer and prior role outputs had already persisted successfully.
- AutoGen discussion streams could remain stuck after the wall-time cancellation signal if the framework stream did not yield again. The runtime needed a hard polling boundary around `run_stream()`.

Changes made:

- Added production runtime capability availability gateway:
  - replay-safe tools stay available;
  - installed skill packages are available;
  - unavailable skill/tool names are filtered before reaching model prompts.
- Updated default dispatch/discussion/hybrid planning to expose only available capabilities to child agents.
- Allowed tool-call-only model responses with empty text while still rejecting empty text-only responses.
- Persisted model responses before later validation paths so failures keep the output produced before the break point.
- Added bounded final-synthesis input payloads. Full child-agent artifacts are still stored, but the final synthesizer receives concise bounded summaries/references instead of all role outputs in full.
- Added a hard polling/cleanup boundary for AutoGen `run_stream()` so wall-time expiry can terminate the run path instead of leaving runs stuck as `running`.

Verification performed:

- Local backend:
  - `uv run pytest tests\unit tests\contracts -q --tb=short`
  - Result: 1127 passed, 13 skipped, 2 warnings.
  - `uv run mypy src tests`
  - Result: success, no issues in 241 source files.
- Local frontend:
  - `npm test -- --run`
  - Result: 68 passed.
  - `npm.cmd run build`
  - Result: passed; only the existing Vite chunk-size warning.
- Server deployment:
  - Uploaded `.tmp/agent-hub-incremental.tar` to `/tmp/agent-hub-incremental.tar`.
  - Extracted into `/opt/agent-hub/current`.
  - Restarted `agent-hub-api` and `agent-hub-worker`.
  - `curl -fsS http://127.0.0.1:8000/health` returned `{"status":"ok"}`.
  - `curl -fsS http://127.0.0.1:8000/health/ready` returned `{"status":"ok"}`.
- Server real mode smoke evidence:
  - direct run `1c04dd17-cd27-440a-9be1-da0073a27002`: completed.
  - auto run `2ae8dfca-2f25-4a7d-a59b-8511c7a55da9`: completed as dispatch.
  - dispatch run `e6fb8327-6e2c-44fb-b987-2a897cf04204`: completed; final synthesizer produced result after input bounding.
  - discuss run `9fd5f1c0-72fd-4908-bba9-806f8eff453b`: completed with discussion and decision output.
  - hybrid run `44c20a5f-b9d9-4828-86c3-aac71795d5fc`: completed with dispatch outputs and discussion completion.

Remaining risks / TODOs:

- The real hybrid smoke can take close to the diagnostic script timeout because it performs full dispatch plus discussion. The application completes, but future smoke scripts should use a longer timeout or per-stage polling output.
- Server logs still contain older pre-fix errors from earlier runs. Latest post-deploy health is clean; do not interpret old journal tail entries as new failures without checking timestamps.
- Local integration tests that require PostgreSQL were blocked by the local Windows database not being available. Production-like behavior was verified on the Linux server using its real PostgreSQL/Redis/services.

## 2026-08-13 CI Root-Cause Follow-up: Reviewer Event Contract and Tool-Call Limit Ordering

User requirement:

- Do not adjust model/role weights as a workaround.
- Find the root cause and fix the runtime behavior.

Root causes found:

- Reviewer skip handling emitted `review.completed` with `payload.verdict="skipped"`, but the runtime event contract only allows reviewer verdicts `approve`, `revise`, or `reject`. This made the event invalid before dispatch completion.
- Step model responses were persisted into artifacts before `_valid_response()` checked the per-response tool-call count. With very large tool-call batches, artifact lineage validation could fail first, hiding the real root cause (`model response exceeds tool call limit`).

Changes made:

- Reviewer skip is now encoded as a contract-valid `verdict="approve"` plus `review_status="skipped"` and the existing warning payload.
- Step gateway responses are now validated immediately after the model gateway returns and tool-call names are mapped, before model artifacts, usage, checkpoints, or tool placeholder artifacts are written. This preserves the true failure reason and avoids polluting run state with invalid model responses.
- Updated the integration test assertion to match the stable reviewer event contract.

Verification performed:

- Local static/backend checks:
  - `uv run ruff check src tests` -> passed.
  - `uv run mypy src tests` -> passed, no issues in 241 source files.
  - `uv run pytest tests\unit tests\contracts -q --tb=short` -> 1127 passed, 13 skipped.
- Local targeted integration tests could not run because the Windows local PostgreSQL integration database was unavailable (`Database did not become ready within 30 seconds`). The targeted tests are expected to run in GitHub Actions' Linux/PostgreSQL environment.
- Server deployment:
  - Copied `src/agent_hub/runtime/crew/adapter.py` to `/opt/agent-hub/current/src/agent_hub/runtime/crew/adapter.py`.
  - Restarted `agent-hub-api` and `agent-hub-worker`.
  - `/health` and `/health/ready` returned `{"status":"ok"}`.
- Server real smoke:
  - dispatch run `9ef5de3f-23c0-41d8-a0d1-bd10ab6ff720`: completed.
  - hybrid run `007b4f08-3fb4-4ea6-b4a6-47cef893c91d`: completed. The SSH smoke process timed out before printing completion, but querying `/api/v1/admin/runs` showed the run completed.

Remaining risks / TODOs:

- The GitHub Actions integration suite must be checked after push because local Windows does not have the integration PostgreSQL service.
- Temporary server validation scripts/tokens under `/tmp` should be deleted after CI confirmation.

## 2026-08-13 P1/P1.5 Stabilization: Temporary Agent Model Selection and AutoGen Cleanup Degradation

User directive:

- Continue by plan priority even if new feature requests arrive.
- Finish P1/P1.5 first, verify on the Linux server, then push and check CI.
- Do not work around reviewer/model failures by lowering weights; fix the root cause.

Current state:

- Local branch: `main`.
- Server: `103.236.98.133`, deploy path `/opt/agent-hub/current`.
- `agent-hub-api` and `agent-hub-worker` are active.
- `/health/ready` returns `{"status":"ok"}`.
- Caddy serves the current frontend build:
  - `/` returns HTTP 200.
  - `/assets/index-4KGtXJcp.js` returns HTTP 200.

Root causes found:

- Temporary Agent persistence could fall back to the proposal `id` when no explicit model was supplied. This was wrong: the main Agent must select an actual registered model according to task capability, not use an Agent identifier as a model.
- The temporary Agent policy did not recommend a model from the published model configuration, so dispatch/hybrid approval flows could create proposals without a safe executable model.
- AutoGen discussion could produce usable participant output, then fail during `team.reset()` / runtime stop / runtime close cleanup. That cleanup failure overrode the successful discussion output and marked the run failed.
- Generic artifact strings such as `text: reviewer` / `model_response: reviewer` could leak into UI summaries instead of concrete artifact titles or user-readable content.

Changes made:

- Added `recommended_model` to `TemporaryAgentProposal`.
- `AdminResourceTemporaryAgentPolicy` now selects a recommended text model from the published platform config using task capability hints and provider/model characteristics.
- Temporary Agent approval no longer falls back to proposal IDs as model names. Missing safe model now raises a clear conflict.
- AutoGen cleanup failure now fails the run only when there is no usable discussion output. If usable discussion output exists, the runtime records `runtime.cleanup_degraded` and continues to `runtime.completed`.
- Added filtering so generic artifact wrapper text is not shown as a meaningful UI result.
- Channel configuration UI keeps editable input fields for channel secrets/parameters.

Verification performed:

- Local backend:
  - `uv run ruff check src tests` -> passed.
  - `uv run pytest tests\unit\runtime\test_autogen_artifact_rollback.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\test_hybrid.py -q --tb=short` -> 26 passed.
  - `uv run pytest tests\api\test_admin_resources.py tests\unit\runs\test_temporary_agent.py tests\unit\runs\test_temporary_agent_policy.py tests\unit\runtime\test_autogen_artifact_rollback.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\test_hybrid.py -q --tb=short` -> 88 passed, 1 warning.
- Local frontend:
  - `npm.cmd test -- src/pages/OperationalPages.test.tsx src/pages/ChannelsPage.test.tsx -- --runInBand` -> 39 passed.
  - `npm.cmd run build` -> passed; Vite emitted only the existing chunk-size warning.
- Server deployment:
  - Uploaded incremental package `.tmp/deploy-p1p15-current.tgz` to `/tmp/agent-hub-p1p15-current.tgz`.
  - Extracted into `/opt/agent-hub/current`.
  - Fixed ownership and static file permissions.
  - Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Server real smoke after final sync:
  - direct run `d1400dbe-ef7e-491c-902a-5bb80009f9f2`: completed, 4 events, 1 artifact.
  - dispatch run `22a3a79c-56d7-449c-89e1-52e1bb2bc0f0`: completed, 65 events, 14 artifacts.
  - discuss run `f92070ae-6132-4e89-af4d-740fcfb213ec`: completed, 7 events, 1 artifact.
  - hybrid run `900f458f-7206-4a53-8239-288783d8efd3`: completed, 22 events, 15 artifacts.
- CI follow-up:
  - GitHub run `31625489844` failed because `runtime.cleanup_degraded` carried `message`, violating the RunEvent contract that only `message.created` may carry message text.
  - Fixed by moving the cleanup summary into `payload.summary`.
  - Local `ruff` and AutoGen unit regression passed after the fix.
  - Server re-smoke after this fix:
    - direct run `2ddfeeb1-fc8f-4ffe-861d-30c114ec5032`: completed.
    - dispatch run `4961bba5-4a69-43ad-abe2-1985093ed42e`: completed.
    - discuss run `395086ee-2224-4ee6-a5b7-bd0e9b18e72f`: completed.
    - hybrid run `129436e1-1134-4dd2-b3f2-8841ba6bd998`: completed.

Remaining risks / TODOs:

- P1/P1.5 runtime chain is server-verified, but the user still needs to visually validate the mobile conversation UI details in browser because screenshot-level UX cannot be fully proven from CLI tests.
- Old frontend asset files remain in `web/dist/assets` on the server. Current `index.html` points to the latest assets and works, but the deploy script should later replace `web/dist` atomically or clean old hashed assets.
- Old pre-fix failed runs remain in the database and logs. Diagnose only by timestamp/run id when comparing future failures.
- Continue plan order: finish remaining P1.5 UI/admin closure, then P2 Hermes learning loop, then full module verification, then P3 channel intelligent-agent mode/multimodal/vibe coding.
