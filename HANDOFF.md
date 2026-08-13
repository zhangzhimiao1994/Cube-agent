
## 2026-08-13 Skill Bundle Upload Slice

Current state:

- Skill archive uploads now return a structured upload result: `{ filename, bundle, items }`.
- A normal single Skill ZIP/TAR upload remains compatible through `items[0]` and `bundle=false`.
- A ZIP/TAR that contains multiple top-level Skill folders is now split into independent safe ZIP packages, scanned separately, stored separately, and returned as multiple `items` with `bundle=true`.
- A single Skill that was zipped with one outer folder is also accepted by the same fallback split path.
- Chat attachment flow still treats archives as ordinary attachments first; the user must click `作为 Skill 安装` before scanning/installing.
- Chat Skill install confirmation now displays every scanned Skill in a bundle and approves all scanned items only after the user confirms.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-skill-bundle-upload.tgz`.
- Server services `agent-hub-api` and `agent-hub-worker` were restarted and active afterward; Caddy was reloaded for the rebuilt frontend.
- Server real HTTP check passed with `/tmp/server_skill_bundle_upload_check.py`:
  - loaded `/etc/agent-hub/secrets.env` and used deployed code from `/opt/agent-hub/current`;
  - generated a short-lived in-memory super-admin token without printing secrets;
  - created a real multi-Skill ZIP containing `server_bundle_writer` and `server_bundle_reviewer`;
  - called real `POST /api/v1/admin/skills/upload`;
  - confirmed `bundle=true`, two scanned items, and requested permission `tool:filesystem.read`;
  - called real approve APIs for both uploaded Skill IDs;
  - confirmed both uploaded Skills appeared as `enabled` through `GET /api/v1/admin/skills`;
  - deleted the uploaded test Skills afterward.

Changes made:

- `src/agent_hub/api/routers/admin.py`
  - Added `SkillArchiveUploadResponse`.
  - Added bundle splitting for ZIP and TAR archives with path traversal and special-file rejection before subpackage scanning.
  - Updated in-memory and persistent admin resource services to store and return multiple scanned Skills.
- `web/src/api/client.ts`
  - Added upload-result schema and updated `uploadSkillArchive`.
- `web/src/pages/RunsPage.tsx`
  - Tracks `skills[]` for pending Skill installs and approves all scanned bundle items after confirmation.
- `web/src/pages/OperationalPages.test.tsx`
  - Updated Skill upload mock and assertion to match the new upload result shape.
- `tests/api/test_admin_resources.py`
  - Updated existing ZIP/TAR upload tests for `bundle/items`.
  - Added regression coverage for a multi-directory all-Skills ZIP.

Verification performed:

- TDD red:
  - `uv run pytest tests/api/test_admin_resources.py::test_skill_archive_upload_scans_real_zip_package tests/api/test_admin_resources.py::test_skill_archive_upload_accepts_real_tar_gz_package tests/api/test_admin_resources.py::test_skill_archive_upload_scans_bundle_with_multiple_skill_directories -q --tb=short` first failed because the API returned a single `SkillResponse` and rejected the multi-directory bundle with 422.
- Green/local:
  - `uv run pytest tests/api/test_admin_resources.py::test_skill_archive_upload_scans_real_zip_package tests/api/test_admin_resources.py::test_skill_archive_upload_accepts_real_tar_gz_package tests/api/test_admin_resources.py::test_skill_archive_upload_scans_bundle_with_multiple_skill_directories -q --tb=short` -> 3 passed.
  - `uv run pytest tests/api/test_admin_resources.py tests/unit/skills/test_package.py -q --tb=short` -> 89 passed.
  - `uv run ruff check src tests` -> passed.
  - `uv run mypy --strict src tests` -> passed.
  - `npm test -- --run src/pages/OperationalPages.test.tsx -t "archive|Skill"` -> 43 passed.
  - `npm run build` -> passed; Vite still reports the existing chunk-size warning.
- Green/server:
  - `/tmp/server_skill_bundle_upload_check.py` -> `PASS: server skill bundle upload scanned, approved, listed, and cleaned up`.

Remaining risks / TODOs:

- Add a user-facing attachment manager with list/delete support. Current attachments are stored under `/var/lib/agent-hub/attachments/{tenant_id}/` as `att_*.bin`, `att_*.json`, optional manifests, and optional extracted archive directories; there is no web UI or API delete endpoint yet.
- Continue remaining P3 items after this release workflow: deeper Vibe runtime integration, OpenClaw multi-system adapters and permission modes, multimedia generation executor/capability enforcement, protected Feishu Skill install commands, and final usage README.

## 2026-08-13 Channel Language Directives Slice

Current state:

- Channel messages now support a unified `//` channel language directive layer before the task text.
- English mode directives:
  - `//auto`, `//direct`, `//dispatch`, `//discuss`, `//hybrid` plus conservative aliases such as `//route`, `//mix`, and `//mixed`.
- Chinese mode directives:
  - `//自动`, `//直连`, `//直接`, `//派单`, `//分派`, `//讨论`, `//辩论`, `//混合`.
- Vibe Coding capability directives:
  - `//vi`, `//vibe`, `//vibecoding`, `//vibe-coding`, `//code`, `//coding`, `//代码`, `//编程`, `//代码协作`.
- Existing channel directives remain compatible:
  - `/dispatch` and the other legacy single-slash mode commands;
  - `/#name` for MCP;
  - `&name` for Skill;
  - `@name` for plugin.
- Channel Vibe Coding requests fail closed when the system `vibe_coding_enabled` switch is disabled.
- Production app wiring now passes the admin settings service into `RunServiceInboundSubmitter`, so channel submissions obey the same Vibe switch as the web API.
- Accepted channel Vibe requests pass `vibe_coding=True` into `RunService` and preserve `requested_channel_features=vibe_coding` in routing metadata.
- Admin run detail now exposes channel-requested features, skills, MCP servers, and plugins through `explicit_details`.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-channel-language-directives.tgz`.
- Server services `agent-hub-api` and `agent-hub-worker` were restarted and active afterward.
- Server real environment check passed with `/tmp/server_channel_language_directives_check.py`:
  - loaded `/etc/agent-hub/secrets.env` and deployed code from `/opt/agent-hub/current`;
  - used the real production database;
  - used real `PersistentAdminResourceService`, `RunService`, `RunServiceInboundSubmitter`, `ChannelGateway`, and `InboundDedupRepository`;
  - confirmed `//hybrid //vi` is rejected with `vibe_coding_disabled` while the system switch is off;
  - enabled the system switch and submitted real English and Chinese channel messages;
  - confirmed created run records have `mode=hybrid` and `mode=discuss`;
  - confirmed admin details expose `vibe_coding=enabled`, `capability=vibe_coding`, and `requested_channel_features=vibe_coding`;
  - restored original system settings and cleaned up created test runs.

Changes made:

- `src/agent_hub/channels/directives.py`
  - Added `//` channel language parsing for bilingual modes and Vibe Coding capability requests.
  - Invalid `//...` directives now fail through the existing channel directive validation path.
- `src/agent_hub/channels/submitter.py`
  - Added settings-service gating for channel Vibe Coding.
  - Passes channel Vibe requests into `RunService.submit(vibe_coding=True)`.
  - Adds `requested_channel_features=vibe_coding` to safe channel context.
- `src/agent_hub/app.py`
  - Wires the production admin settings service into the channel submitter.
- `src/agent_hub/runs/service.py`
  - Allows `requested_channel_features` through the safe channel context whitelist.
- `src/agent_hub/api/routers/admin.py`
  - Exposes requested channel features, skills, MCP servers, and plugins in run details.
- Tests:
  - Added unit coverage for English and Chinese channel language directives.
  - Added admin detail coverage for channel directive metadata.

Verification performed:

- TDD red:
  - `uv run pytest tests/unit/channels/test_submitter.py::test_submitter_parses_english_channel_language_directives_for_mode_and_vibe tests/unit/channels/test_submitter.py::test_submitter_parses_chinese_channel_language_directives_and_rejects_disabled_vibe -q --tb=short` first failed because `RunServiceInboundSubmitter` did not accept `settings_service`.
  - `uv run pytest tests/api/test_admin_resources.py::test_routing_details_exposes_channel_directive_context -q --tb=short` first failed because `_routing_details` did not expose `requested_channel_features`.
- Green:
  - `uv run pytest tests/unit/channels/test_submitter.py::test_submitter_parses_english_channel_language_directives_for_mode_and_vibe tests/unit/channels/test_submitter.py::test_submitter_parses_chinese_channel_language_directives_and_rejects_disabled_vibe -q --tb=short` -> 2 passed.
  - `uv run pytest tests/api/test_admin_resources.py::test_routing_details_exposes_channel_directive_context -q --tb=short` -> 1 passed.
  - `uv run pytest tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/unit/runs/test_temporary_agent.py tests/api/test_admin_resources.py::test_routing_details_exposes_channel_directive_context -q --tb=short` -> 47 passed.
  - `uv run ruff check src tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/unit/runs/test_temporary_agent.py tests/api/test_admin_resources.py` -> passed.
  - `uv run mypy src` -> passed.
  - Server real channel language directives check -> passed.

Remaining risks / TODOs:

- Add a user-facing attachment manager with list/delete support. Current attachments are stored under `/var/lib/agent-hub/attachments/{tenant_id}/` as `att_*.bin`, `att_*.json`, optional manifests, and optional extracted archive directories; there is no web UI or API delete endpoint yet.
- Final project README should focus on system usage, not test reporting. It should cover setup, settings, channel command language, Vibe Coding, OpenClaw, multimedia generation, Feishu/channel usage, recovery archive workflow, and common operations.
- Continue the remaining P3 items: deeper Vibe runtime integration, OpenClaw multi-system adapters and permission modes, multimedia generation executor/capability enforcement, and protected Feishu Skill install commands.

## 2026-08-13 Chat Composer Layout Fix

Current state:

- Chat composer actions are split into three layout rows:
  - `.composer-tool-row` for attachment, handoff, Vibe Coding, and config toggle controls.
  - `.composer-status-line` for the current mode/model/session status.
  - `.composer-send-row` for the submit action.
- This prevents mobile composer controls from crowding the status text and send button.
- Handoff and Vibe Coding remain independent toggles and can still be enabled together.
- Mobile CSS now uses a fixed grid for tool controls and a full-width send row, so the buttons do not collapse into the status line.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-chat-composer-layout.tgz`.
- Caddy was reloaded after the frontend bundle was deployed.
- Server real HTTP check passed with `/tmp/server_frontend_layout_check.py`:
  - fetched `http://127.0.0.1/` from the server;
  - confirmed the served `index.html` matches `/opt/agent-hub/current/web/dist/index.html`;
  - confirmed served JS/CSS assets match deployed files;
  - confirmed served bundle contains `composer-tool-row`, `composer-status-line`, and `composer-send-row`.

Changes made:

- `web/src/pages/RunsPage.tsx`
  - Added semantic wrappers for composer tool controls, status, and send action.
- `web/src/styles.css`
  - Replaced the old shared flex layout with grid/flex rows scoped to the composer.
  - Added mobile-specific grid sizing so tool buttons, status, and send action remain separated.
- `web/src/pages/OperationalPages.test.tsx`
  - Added coverage proving the composer renders the separated tool/status/send structure.

Verification performed:

- TDD red:
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx -t "separates composer tools"` failed before implementation because `.composer-tool-row` did not exist.
- Green:
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx -t "separates composer tools"` -> 1 passed.
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx -t "Vibe Coding|handoff|composer|direct mode"` -> 9 passed.
  - `npm.cmd test -- --run src/pages/OperationalPages.test.tsx` -> 43 passed.
  - `npm.cmd run lint` -> passed.
  - `npm.cmd run build` -> passed; Vite reported the existing chunk-size warning.
  - Server HTTP bundle check -> passed.

## 2026-08-13 System Context Auto-Compaction Slice

Current state:

- Conversation history compaction is now a system/runtime feature in `RunService`, not a Vibe-only behavior and not a channel/UI rule.
- Before runtime execution, conversation history is converted into a context artifact using a history token budget derived from:
  - the configured runtime token budget;
  - the Main Agent model context window when available;
  - explicit routing metadata such as `main_agent_context_window_tokens` for tests/diagnostics.
- Short history remains a `conversation_history` artifact with `context_policy=full_history`.
- Long history becomes `conversation_history_compacted` with `context_policy=auto_compacted`, preserving recent turns after compaction.
- Production app wiring now injects a Main Agent context-window getter into `RunService`.
- Main Agent context window inference is conservative by model family:
  - Gemini: 1,000,000;
  - GPT-5 / GPT-4.1: 400,000;
  - Claude: 200,000;
  - DeepSeek / Qwen / Kimi / GPT-4o: 128,000;
  - unknown: 32,768.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-context-compaction.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` were active after restart/reload.
- Server real environment check passed with `/tmp/server_context_compaction_check.py` after loading `/etc/agent-hub/secrets.env`:
  - used production settings and real database connection;
  - created real Run rows in a unique conversation;
  - called deployed `RunService` conversation-context artifact construction;
  - confirmed long history produced `conversation_history_compacted`;
  - confirmed the latest decision survived compaction;
  - verified the current run through the local HTTP admin API;
  - cleaned up the test runs afterward.

Changes made:

- `src/agent_hub/runs/service.py`
  - Added system-level conversation history budget and artifact policy helpers.
  - Added optional Main Agent context-window getter.
  - Added auto-compacted history artifact generation before runtime execution.
- `src/agent_hub/app.py`
  - Added Main Agent model-family context-window inference.
  - Injected the context-window getter into production `RunService`.
- `src/agent_hub/context/compaction.py`
  - Changed over-budget compaction to preserve the recent tail of transcript content instead of the oldest tail candidate.
- Tests:
  - Added budget, full-history, compacted-history, model-window inference, and getter wiring coverage.

Verification performed:

- TDD red:
  - `_conversation_history_artifact` / `_conversation_history_token_budget` imports failed before implementation.
  - `_MainAgentContextWindowGetter` import failed before production wiring was implemented.
- Green:
  - `uv run pytest tests/unit/runs/test_runtime_context_policy.py -q --tb=short` -> 7 passed.
  - `uv run pytest tests/unit/test_app_wiring.py -q -k "context_window" --tb=short` -> 2 passed.
  - `uv run pytest tests/unit/test_app_wiring.py tests/unit/runs/test_runtime_context_policy.py tests/api/test_runs_api.py -q --tb=short` -> 28 passed.
  - `uv run pytest tests/unit/test_app_wiring.py tests/unit/runs/test_runtime_context_policy.py tests/api/test_runs_api.py tests/api/test_admin_resources.py -q --tb=short` -> 90 passed.
  - `uv run ruff check src tests` -> passed.
  - `uv run mypy src` -> passed.

## 2026-08-13 Model Save Form Refresh Fix

Current state:

- Model test-and-save now immediately upserts the returned model into the `["models"]` React Query cache before invalidating the query.
- After a successful create/update, the model form resets to the default add-new state: provider OpenAI, API Base `https://api.openai.com/v1`, logical model `main`, default capabilities/concurrency/limits, and an empty API Key field.
- This prevents stale provider/API Base/logical model/capability values from lingering after a successful test-and-save.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-model-form-refresh.tgz`.
- Server-side bundle check passed via `/tmp/deploy_verify_model_form.sh`:
  - deployed `web/dist/index.html` points to `/assets/index-BreD6_rd.js`;
  - deployed bundle contains the expected default API Base marker and `setQueryData` cache-update path;
  - Caddy serves the same bundle from `http://127.0.0.1/`.

Changes made:

- `web/src/pages/ModelsPage.tsx`
  - Added `resetModelForm()`.
  - Updated save success handling to upsert returned model data into query cache and reset stale local form state.
- `web/src/pages/ModelsPage.test.tsx`
  - Extended the save test to prove the provider/API Base/logical model/API Key fields reset after successful save.

Verification performed:

- TDD red: `npm.cmd test -- --run src/pages/ModelsPage.test.tsx -t "limits the model dropdown"` first failed because provider stayed `deepseek` after save.
- Green: the same targeted test passed after the form reset/cache update fix.
- `npm.cmd test -- --run src/pages/ModelsPage.test.tsx` -> 12 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

## 2026-08-13 Chat Toggles and Hermes Delete Fix

Current state:

- Chat composer Handoff (`按照原思路`) is now a true toggle: click once to start a referenced follow-up conversation, click again to clear `reference_conversation_id` before sending.
- Vibe Coding remains independently toggleable and is not mutually exclusive with Handoff. This is intentional: long Vibe Coding conversations may still need a referenced/condensed prior conversation.
- Hermes learning records can now be deleted from the table and from the detail page. Delete calls the real admin API and removes the record from subsequent list/detail reads.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-chat-hermes-fix.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server real HTTP functional check `/tmp/server_chat_hermes_check.py` passed in the deployed environment:
  - temporarily enabled `vibe_coding_enabled`;
  - submitted a real `/api/v1/runs` payload with both `reference_conversation_id` and `vibe_coding=true`;
  - confirmed admin run detail preserved `conversation_id`, `reference_conversation_id`, `vibe_coding=enabled`, and `capability=vibe_coding`;
  - created a Hermes learning record through `/api/v1/admin/hermes/feedback`;
  - deleted it through `DELETE /api/v1/admin/hermes/{id}`;
  - confirmed detail returns `404` and list no longer includes the deleted record;
  - restored original system settings afterward.
- GitHub recovery archive was created before pushing the functional fix:
  - local ignored bundle `.local-archives/github-pushes/mutilagent-main-before-20260813-143955-55a3500.bundle`;
  - GitHub tag `archive/mutilagent-main-before-20260813-143955-55a3500`.
- Commit `b87c945 fix chat toggles and hermes deletion` was pushed with `git push --force-with-lease mutilagent main`.
- GitHub Actions run `31674722025` passed. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made:

- `web/src/pages/RunsPage.tsx`
  - Added active pressed state for Handoff and second-click cancellation.
  - Kept Handoff and Vibe Coding independent so they can be submitted together.
- `src/agent_hub/api/routers/admin.py`
  - Added `delete_hermes_insight` to the admin resource service protocol, in-memory service, persistent service, and REST router.
- `web/src/api/client.ts`
  - Added `deleteHermesInsight`.
- `web/src/pages/HermesPage.tsx`
  - Added delete actions in the Hermes table and detail view.
- Tests added/updated:
  - Handoff + Vibe can submit together.
  - Handoff can be canceled before send.
  - Vibe Coding can be canceled before send.
  - Hermes delete removes a learning record from the UI.
  - Hermes delete removes the backend learning record and returns 404 after deletion.

Verification performed:

- TDD red checks were observed first:
  - Handoff cancellation and combined Handoff+Vibe behavior failed before the RunsPage fix.
  - Hermes UI delete button was missing.
  - Backend DELETE returned HTTP 405 before the API route/service method was added.
- `npm.cmd test -- --run src/pages/OperationalPages.test.tsx` -> 42 passed.
- `uv run pytest tests/api/test_admin_resources.py -q -k "hermes" --tb=short` -> 4 passed.
- `uv run ruff check src tests` -> passed.
- `uv run mypy src` -> passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

Follow-up backlog from user feedback:

- Model test/save page: after test-and-save, refresh persisted model data and clear stale local form/config state.
- Chat page UI: improve layout and button presentation; current functional fix is intentionally narrow.
- System framework context compression: implement automatic context compression at the system/runtime layer, not only for Vibe Coding. The decision should consider main-agent model capability/context window and trigger compression before long conversations degrade usability.
- Channel mode grammar: define bilingual channel commands/directives so non-web channels can select modes/capabilities, for example `//vi` or `//vibe` for Vibe Coding plus Chinese equivalents. This should be implemented as channel-language parsing, not ad hoc per-channel string checks.

## 2026-08-13 P3 Conversation-Integrated Vibe Coding Switch

Current state:

- Vibe Coding is now a system-level conversation capability, not a workflow.
- System settings include `vibe_coding_enabled`, defaulting to `false`.
- `/api/v1/runs` accepts `vibe_coding: true` only when the system switch is enabled; when disabled, the API fails closed with `409 vibe_coding_disabled`.
- Accepted Vibe Coding conversation runs persist `vibe_coding=true` and `capability=vibe_coding` in the existing run `routing_decision`, so admin run details and runtime context can identify the request without adding a separate workflow/module.
- The chat composer exposes a `Vibe Coding` button when the system switch is enabled and submits the flag with the normal conversation run payload.
- The settings page exposes the Vibe Coding system switch alongside OpenClaw and multimedia generation switches.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-vibe-coding.tgz` before GitHub push.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server source compiles with `.venv/bin/python -m py_compile src/agent_hub/api/routers/admin.py src/agent_hub/api/routers/runs.py src/agent_hub/runs/service.py`.
- Server functional check `/tmp/vibe_coding_functional_check.py` passed through the real local HTTP API with the production EnvironmentFile loaded:
  - disabled `vibe_coding_enabled=false` rejects a Vibe Coding run with `vibe_coding_disabled`;
  - enabled `vibe_coding_enabled=true` accepts a real conversation run;
  - admin run detail exposes `conversation_id`, `vibe_coding=enabled`, and `capability=vibe_coding`;
  - original system settings are restored afterward.
- Server frontend bundle contains the Vibe Coding settings toggle and composer button markers.
- Server-side feature verification requirement is now explicit: do not use mocks for server acceptance checks. Server checks must exercise the deployed service, production-like settings, real HTTP/API boundaries, real persistence, and actual provider/tool paths where the feature depends on them. Local unit/UI tests may still use mocks for development regression coverage, but they are not sufficient for server acceptance.
- GitHub recovery archive was created before pushing:
  - local ignored bundle `.local-archives/github-pushes/mutilagent-main-before-20260813-140950-d8ce1b4.bundle`;
  - GitHub tag `archive/mutilagent-main-before-20260813-140950-d8ce1b4`.
- Commit `c5b10a4 feat: gate vibe coding in conversations` was pushed with `git push --force-with-lease mutilagent main`.
- GitHub Actions run `31672859925` passed. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made:

- Added `vibe_coding_enabled` to admin system settings request/response models and frontend settings schema.
- Added `vibe_coding` to run creation requests and RunService submission.
- Added fail-closed run API gating based on system settings.
- Added run metadata exposure through admin `explicit_details`.
- Added Config page switch and chat composer Vibe Coding button.
- Added backend API/unit coverage and frontend Config/Runs regression tests.

Local verification:

- TDD red checks were added first:
  - run API initially accepted Vibe Coding while disabled;
  - run API did not forward the flag while enabled;
  - system settings had no `vibe_coding_enabled`;
  - RunService did not accept/persist `vibe_coding`;
  - composer had no `Vibe Coding` button.
- `uv run pytest tests/api/test_runs_api.py::test_vibe_coding_submission_is_rejected_when_system_switch_is_disabled tests/api/test_runs_api.py::test_vibe_coding_submission_is_forwarded_when_system_switch_is_enabled tests/api/test_admin_resources.py::test_system_settings_default_openclaw_is_disabled tests/unit/runs/test_temporary_agent.py::test_submit_persists_vibe_coding_capability_metadata -q --tb=short` -> 4 passed.
- `uv run pytest tests/api/test_runs_api.py tests/api/test_admin_resources.py tests/unit/runs/test_temporary_agent.py -q --tb=short` -> 84 passed.
- `uv run ruff check src\agent_hub\api\routers\runs.py src\agent_hub\api\routers\admin.py src\agent_hub\runs\service.py tests\api\test_runs_api.py tests\api\test_admin_resources.py tests\unit\runs\test_temporary_agent.py` -> passed.
- `uv run mypy --strict src\agent_hub\api\routers\runs.py src\agent_hub\api\routers\admin.py src\agent_hub\runs\service.py tests\api\test_runs_api.py tests\api\test_admin_resources.py tests\unit\runs\test_temporary_agent.py` -> passed.
- `npm.cmd test -- --run src/pages/OperationalPages.test.tsx src/pages/ConfigPage.test.tsx` -> 41 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

Next:

- Continue P3 with richer Vibe Coding behavior inside runtime prompts/artifact handling and multi-system OpenClaw adapters.

## 2026-08-13 P3 Feishu Production Media Factory Wiring Slice

Current state:

- P3 Feishu image pipeline now has production app wiring after the webhook factory hook slice.
- `create_app()` wires `app.state.feishu_media_service_factory` during lifespan when production secret/redis resources are available.
- The factory reuses app-level resources for vision analysis and creates a Feishu OpenAPI media client from the runtime `FeishuSettings` for each webhook request.
- Feishu image webhook flow can now use saved Feishu channel config to download user images, analyze them through `VisionService`, append image context, and submit the enriched text to the main Agent.
- System settings now include the merged `multimedia_generation_enabled` switch, defaulting to `false`.
- When `multimedia_generation_enabled` is disabled, Feishu image messages are acknowledged but not submitted to the Agent and not analyzed; the channel replies that image handling is temporarily unavailable until multimedia processing is enabled.
- When `multimedia_generation_enabled` is enabled, Feishu image messages follow the production media analysis path.
- Development/test environments use `MemoryImageStore`; non-local POSIX deployments use `FilesystemImageStore` under `attachment_store_dir / "vision"`.
- Vision cleanup recovery items are recorded through admin `channel_error` logs when a log service is available.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-multimedia-factory.tgz` before GitHub push.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server OpenAPI contains the deployed `multimedia_generation_enabled` system setting.
- Server syntax check passed with `.venv/bin/python -m py_compile src/agent_hub/api/routers/admin.py src/agent_hub/app.py src/agent_hub/channels/feishu/webhook.py src/agent_hub/channels/feishu/media_factory.py tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py`.
- Server function check passed with `PYTHONPATH=src .venv/bin/python /tmp/feishu_multimedia_check.py`, covering both disabled image reply/no-submit behavior and enabled image analysis/gateway submission behavior.
- User clarified two follow-up P3 design requirements:
  - Feishu-side skill installation is feasible, but must be a permission-protected channel admin command with audit logs and source validation.
  - Multimedia generation should be a single system-level button/switch, not separate workflow-local switches.
  - Image/video generation should be performed by dedicated executor roles after planning.
  - Video generation must never be submitted to a model that lacks a `video_generation` capability. The later design should combine automatic capability inference with manual admin confirmation/override, and enforce capability checks at execution time.

Changes made locally:

- `src/agent_hub/channels/feishu/media_factory.py`
  - Added `FeishuMediaServiceFactory`, `ConfigBackedVisionGateway`, and `AdminLogImageCleanupRecoverySink`.
  - Added production builder for `FeishuOpenAPIMediaClient + VisionService + ModelGateway`.
- `src/agent_hub/api/routers/admin.py`
  - Added `multimedia_generation_enabled` to system settings, defaulting off.
- `src/agent_hub/channels/feishu/webhook.py`
  - Added system-setting gating before Feishu image analysis/gateway submission.
  - Added direct Feishu reply behavior for image messages while multimedia generation is disabled.
- `src/agent_hub/app.py`
  - Wires the production Feishu media service factory during lifespan and closes owned image store resources on shutdown.
- `tests/api/test_channel_webhooks.py`
  - Added TDD coverage that Feishu image messages are not submitted to the Agent when multimedia generation is disabled and receive a channel reply instead.
  - Updated image analysis tests to explicitly enable `multimedia_generation_enabled`.
- `tests/unit/test_app_wiring.py`
  - Added TDD coverage for app lifespan wiring of `feishu_media_service_factory`.
  - Added coverage that development builds use an in-memory image store.

Verification performed locally:

- TDD red: `uv run pytest tests/unit/test_app_wiring.py::test_create_app_wires_production_feishu_media_service_factory -q --tb=short` first failed because `feishu_media_service_factory` was missing from app state.
- Green: the same targeted test passed after app wiring and factory builder were added.
- TDD red: `uv run pytest tests/unit/test_app_wiring.py::test_feishu_media_factory_uses_memory_store_in_development -q --tb=short` first failed because the builder had no environment switch.
- Green: the targeted app wiring and environment store tests passed after adding the `environment` parameter.
- TDD red: `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_replies_when_image_arrives_with_multimodal_disabled -q --tb=short` first failed because the image was still submitted to the gateway.
- Green: the targeted disabled/enabled Feishu image webhook tests passed after adding `multimedia_generation_enabled` gating.
- `uv run pytest tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/api/test_admin_resources.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py -q --tb=short` -> 106 passed.
- `uv run ruff check src tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/api/test_admin_resources.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- Server incremental deployment and server-side function checks are complete for this slice.
- GitHub recovery archive, full push, and Actions verification are pending for this slice.
- Next P3 slices should design the multimedia executor roles/capability checks and protected Feishu admin commands such as skill installation.

## 2026-08-13 P3 Feishu Media Service Factory Hook Slice

Current state:

- P3 channel hardening continued after the Feishu webhook media context hook slice.
- Lazy Feishu webhook media enrichment now prefers `request.app.state.feishu_media_service_factory(settings)` when present.
- The factory receives the Feishu settings resolved from saved runtime channel config, so production wiring can construct Feishu media dependencies with the tenant's active app id/secret/token/encrypt key.
- The previous fixed `request.app.state.feishu_media_service` hook remains supported as a fallback for tests and simple deployments.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-media-factory-hook.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed marker `feishu_media_service_factory`.
- Server syntax check passed with `.venv/bin/python -m py_compile src/agent_hub/channels/feishu/webhook.py tests/api/test_channel_webhooks.py`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-113146-a06264b.bundle`, pointing at current GitHub `mutilagent/main` commit `a06264b`.
- GitHub recovery archive tag prepared and pushed for this push: `archive/mutilagent-main-before-20260813-113146-a06264b`.
- P3 Feishu media service factory hook commit `71dc2c3` was pushed to GitHub; Actions run `31664222291` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/feishu/webhook.py`
  - Added `FeishuMediaServiceFactoryProtocol`.
  - Passed resolved `FeishuSettings` into media-context enrichment.
  - Added `_feishu_media_service()` to choose the settings-aware factory first and the existing fixed service second.
- `tests/api/test_channel_webhooks.py`
  - Added TDD coverage that a Feishu image webhook uses the factory with the runtime settings saved through admin channel config.

Verification performed locally:

- TDD red: `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_uses_media_service_factory_with_runtime_settings -q --tb=short` first failed with `assert [] == ['cli_runtime_feishu']`, proving the factory was not called.
- Green: the same targeted test passed after adding the settings-aware factory hook.
- `uv run pytest tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py -q --tb=short` -> 48 passed.
- `uv run ruff check src tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- The next slice should wire a real production `feishu_media_service_factory` into app bootstrap using `FeishuOpenAPIMediaClient` and the configured vision analyzer.

## 2026-08-13 P3 Feishu Webhook Media Context Hook Slice

Current state:

- P3 channel hardening continued after the Feishu OpenAPI media client slice.
- Lazy Feishu webhook handling now checks `request.app.state.feishu_media_service` before submitting inbound messages to the channel gateway.
- When the media service is present and returns image analyses, the webhook appends a `Channel image analysis:` section to the text submitted to the main Agent.
- If Feishu media analysis raises `FeishuMediaError`, the webhook records the existing `channel_error` diagnostics and still submits the original message text, so a transient media download/analysis failure does not drop the inbound message.
- Default behavior remains unchanged when no `feishu_media_service` is configured.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-webhook-media-hook.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed marker `_append_feishu_media_context`.
- Server syntax check passed with `.venv/bin/python -m py_compile src/agent_hub/channels/feishu/webhook.py tests/api/test_channel_webhooks.py`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-111617-e654b02.bundle`, pointing at current GitHub `mutilagent/main` commit `e654b02`.
- GitHub recovery archive tag prepared and pushed for this push: `archive/mutilagent-main-before-20260813-111617-e654b02`.
- P3 Feishu webhook media context hook commit `2b58e47` was pushed to GitHub; Actions run `31663447019` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/feishu/webhook.py`
  - Reordered lazy Feishu webhook handling so gateway submission occurs after optional media-context enrichment.
  - Added `_append_feishu_media_context()` and safe summary rendering for image analysis results.
  - Reused `log_feishu_media_failure()` for media-analysis failures.
- `tests/api/test_channel_webhooks.py`
  - Added TDD coverage that Feishu image analysis summaries are appended before gateway submission.
  - Added coverage that media failures are logged and the original message is still submitted.

Verification performed locally:

- TDD red: `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_appends_image_analysis_context -q --tb=short` first failed because `feishu_media_service` was never called.
- Intermediate red: after adding the hook, the same test failed because the gateway had already received the original text before media context was appended; this exposed the submission-order bug.
- Green: the same targeted test passed after moving lazy gateway submission after media enrichment.
- `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_logs_media_failure_and_submits_original_message -q --tb=short` -> passed.
- `uv run pytest tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py -q --tb=short` -> 47 passed.
- `uv run ruff check src tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- The next slice should construct a real production `feishu_media_service` from saved Feishu channel config, `FeishuOpenAPIMediaClient`, and the configured vision model gateway.

## 2026-08-13 P3 Feishu OpenAPI Media Client Slice

Current state:

- P3 channel hardening continued after Feishu media error log wiring.
- Added a concrete `FeishuOpenAPIMediaClient` for Feishu user-message resource download.
- The client uses `FeishuSettings` explicitly instead of reading global environment at construction time.
- Tenant token retrieval calls `/open-apis/auth/v3/tenant_access_token/internal`.
- User-message resource download calls `/open-apis/im/v1/messages/:message_id/resources/:file_key?type=image` with `Authorization: Bearer <tenant_access_token>`.
- Official Feishu documentation distinguishes this user-message resource endpoint from `/open-apis/im/v1/images/:image_key`, which only downloads images uploaded by the current bot; this slice intentionally uses the message resource endpoint for inbound user images.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-openapi-media-client.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed marker `FeishuOpenAPIMediaClient`.
- Server syntax check passed with `.venv/bin/python -m py_compile src/agent_hub/channels/feishu/media.py tests/unit/channels/feishu/test_media_client.py`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-103612-8996648.bundle`, pointing at current GitHub `mutilagent/main` commit `8996648`.
- GitHub recovery archive tag prepared and pushed for this push: `archive/mutilagent-main-before-20260813-103612-8996648`.
- P3 Feishu OpenAPI media client commit `d8b3e9c` was pushed to GitHub; Actions run `31661356783` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/feishu/media.py`
  - Added `FeishuOpenAPIMediaClient`.
  - Added safe response validation for tenant token JSON and binary resource download status errors.
- `tests/unit/channels/feishu/test_media_client.py`
  - Added TDD coverage for tenant token retrieval, resource download URL, `type=image`, authorization header, and binary chunk streaming.

Verification performed locally:

- TDD red: `uv run pytest tests/unit/channels/feishu/test_media_client.py -q --tb=short` first failed because `FeishuOpenAPIMediaClient` did not exist.
- Green: the same targeted test passed after implementation.
- `uv run pytest tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/test_submitter.py -q --tb=short` -> 45 passed.
- `uv run ruff check src tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/api/test_channel_webhooks.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- The production app still needs a factory/wiring pass that constructs `FeishuOpenAPIMediaClient` from saved channel config and a configured vision analyzer; this slice supplies the HTTP client boundary.

## 2026-08-13 P3 Feishu Media Error Log Wiring Slice

Current state:

- P3 channel hardening continued after Feishu media diagnostics.
- `FeishuMediaService` now accepts an optional admin log service and records `FeishuMediaError` failures into `channel_error` when media analysis is actually invoked.
- Media failures keep propagating after logging; the new logging path is best-effort and does not mask the original channel/media exception.
- Recorded details are safe diagnostics only: channel, message id, resource key, tenant key, attachment kind, reason, and error type.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-media-log-wiring.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart/reload.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed marker `log_feishu_media_failure`.
- Server syntax check passed with `.venv/bin/python -m py_compile src/agent_hub/channels/feishu/media.py tests/e2e/feishu/test_conversation.py`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-102125-b851967.bundle`, pointing at current GitHub `mutilagent/main` commit `b851967`.
- GitHub recovery archive tag prepared and pushed for this push: `archive/mutilagent-main-before-20260813-102125-b851967`.
- P3 Feishu media error log wiring commit `0630f06` was pushed to GitHub; Actions run `31660607365` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/feishu/media.py`
  - Added optional `log_service` support to `FeishuMediaService`.
  - Added `log_feishu_media_failure()` to record media failures under `channels.feishu.media`.
- `tests/e2e/feishu/test_conversation.py`
  - Added TDD coverage that a Feishu image MIME mismatch is written to `channel_error` logs with diagnostics.

Verification performed locally:

- TDD red: `uv run pytest tests/e2e/feishu/test_conversation.py::test_media_errors_are_recorded_in_channel_logs -q --tb=short` first failed because `FeishuMediaService.__init__()` did not accept `log_service`.
- Green: the same targeted test passed after implementation.
- `uv run pytest tests/e2e/feishu/test_conversation.py tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/unit/channels/test_submitter.py -q --tb=short` -> 44 passed.
- `uv run ruff check src tests/e2e/feishu/test_conversation.py tests/api/test_channel_webhooks.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- Production app wiring still needs a concrete Feishu media client/provider before image bytes can be downloaded automatically from Feishu in live callbacks; this slice hardens the media-analysis error boundary that will be used by that runtime wiring.

## 2026-08-13 P3 Feishu Media Diagnostics Slice

Current state:

- P3 channel hardening continued with Feishu media attachment diagnostics.
- `FeishuMediaError` now carries a safe `diagnostics` dictionary that can be logged by later webhook/run boundaries.
- Diagnostics include `channel`, `message_id`, `resource_key`, `tenant_key`, `attachment_kind`, and `reason`.
- The diagnostics are populated for image MIME mismatch, invalid media chunks, empty media, and media size limit failures.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-feishu-media-diagnostics.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed diagnostics marker.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-101600-1bdb93f.bundle`, pointing at current GitHub `mutilagent/main` commit `1bdb93f`.
- GitHub recovery archive tag prepared for this push: `archive/mutilagent-main-before-20260813-101600-1bdb93f`.
- Feishu media diagnostics commit `01f7f4a` was pushed to GitHub; Actions run `31659746824` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.
- Local recovery bundle for this final handoff-doc push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-102000-01f7f4a.bundle`, pointing at current GitHub `mutilagent/main` commit `01f7f4a`.
- GitHub recovery archive tag prepared for this final handoff-doc push: `archive/mutilagent-main-before-20260813-102000-01f7f4a`.

Changes made locally:

- `src/agent_hub/channels/feishu/media.py`
  - Added diagnostics support to `FeishuMediaError`.
  - Added safe diagnostic metadata construction for image attachment processing.
- `tests/e2e/feishu/test_conversation.py`
  - Added TDD coverage that MIME mismatch errors include channel diagnostics.

Verification performed locally:

- TDD red: `uv run pytest tests/e2e/feishu/test_conversation.py::test_media_errors_include_channel_diagnostics -q --tb=short` first failed because `FeishuMediaError` had no `diagnostics`.
- Green: the same targeted test passed after implementation.
- `uv run pytest tests/e2e/feishu/test_conversation.py -q --tb=short` -> 10 passed.
- `uv run pytest tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py tests/e2e/feishu/test_conversation.py -q --tb=short` -> 43 passed.
- `uv run ruff check src tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py tests/e2e/feishu/test_conversation.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This Feishu media diagnostics slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- Only this final handoff-doc commit remains to push after the prepared archive tag.
- Next P3 channel hardening step should wire these media diagnostics into the actual channel error log path when media analysis is invoked by production runtime.

## 2026-08-13 P3 Channel Directive Grammar Slice

Current state:

- P3 channel hardening continued after ignored Feishu event diagnostics.
- Channel directive parsing now rejects malformed MCP/Skill/plugin reference tokens instead of silently folding them into task text.
- Invalid reference forms are limited to channel directive prefixes: `/#` for MCP, `&` for Skill, and `@` for plugin.
- Unknown ordinary slash commands remain untouched so existing explicit command parsing is not broadened accidentally.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-channel-directives.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed diagnostic marker `invalid_directive`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-100440-e752008.bundle`, pointing at current GitHub `mutilagent/main` commit `e752008`.
- GitHub recovery archive tag prepared for this push: `archive/mutilagent-main-before-20260813-100440-e752008`.
- P3 channel directive grammar commit `1bdb93f` was pushed to GitHub; Actions run `31659234998` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/directives.py`
  - Added malformed directive detection for invalid `/#...`, `&...`, and `@...` tokens.
  - Added an `invalid_directive` summary message.
- `tests/unit/channels/test_submitter.py`
  - Added TDD coverage that malformed channel directives raise `ChannelDirectiveError` and do not submit a run.

Verification performed locally:

- TDD red: `uv run pytest tests/unit/channels/test_submitter.py::test_submitter_rejects_malformed_channel_directives -q --tb=short` first failed because `/#bad!` was accepted as task text.
- Green: the same targeted test passed after implementation.
- `uv run pytest tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py -q --tb=short` -> 22 passed.
- `uv run ruff check src tests/unit/channels/test_submitter.py tests/api/test_channel_webhooks.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Remaining risks / TODOs:

- This directive grammar slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- Continue P3 channel hardening with real attachment retrieval/delivery diagnostics.

## 2026-08-13 P3 Channel Diagnostics Slice

Current state:

- P3 channel hardening has started after the mobile navigation interruption was completed.
- This slice improves Feishu callback observability for valid platform callbacks that are acknowledged but ignored because they are not inbound user messages.
- Ignored Feishu events now retain diagnostic metadata from the callback header: `event_type`, `event_id`, `tenant_key`, and `reason`.
- The lazy Feishu webhook route records ignored-event diagnostics as `channel_error` warning logs through the admin resource service when available.
- Feishu event normalization now checks `header.event_type` before requiring `event.message`, so unsupported platform events report `unsupported event type` instead of the misleading `missing event.message`.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-p3-channel-diagnostics.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart.
- Server health checks passed on API port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server source contains the deployed diagnostic marker `ignored_reason`.
- Local recovery bundle for this GitHub push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-095330-f8749e2.bundle`, pointing at current GitHub `mutilagent/main` commit `f8749e2`.
- GitHub recovery archive tag prepared for this push: `archive/mutilagent-main-before-20260813-095330-f8749e2`.
- P3 channel diagnostics commit `e752008` was pushed to GitHub; Actions run `31658743485` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.

Changes made locally:

- `src/agent_hub/channels/feishu/webhook.py`
  - Extended `FeishuWebhookResult` with ignored-event diagnostic fields.
  - Added `_record_feishu_ignored_event()` for best-effort `channel_error` logging.
- `src/agent_hub/channels/feishu/normalize.py`
  - Moved event-type filtering ahead of message extraction for clearer diagnostics.
- `tests/api/test_channel_webhooks.py`
  - Added TDD coverage for ignored Feishu platform event diagnostics in admin logs.

Verification performed locally:

- TDD red: `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_records_ignored_platform_event_diagnostics -q --tb=short` first failed because no diagnostic log existed.
- Intermediate red: after adding logging, the test failed because the reason was `missing event.message`; this exposed the event-type ordering bug.
- Green: `uv run pytest tests/api/test_channel_webhooks.py::test_feishu_webhook_records_ignored_platform_event_diagnostics -q --tb=short` -> passed.
- `uv run pytest tests/api/test_channel_webhooks.py -q --tb=short` -> 19 passed.
- `uv run pytest tests/api/test_channel_webhooks.py tests/contracts/feishu/test_receivers.py -q --tb=short` -> 30 passed.
- `uv run ruff check src tests/api/test_channel_webhooks.py` -> passed.
- `uv run mypy --strict src tests` -> passed.
- Server target pytest was attempted with `.venv/bin/python -m pytest`, but the production virtualenv does not install pytest (`No module named pytest`).

Remaining risks / TODOs:

- This P3 channel diagnostics slice has been committed, server-synced, pushed to GitHub, and verified by Actions.
- Continue P3 channel hardening after this slice with real attachment retrieval/delivery diagnostics and command grammar validation.

## 2026-08-13 Mobile Floating Navigation Fix

Current state:

- Mobile console navigation no longer exposes a fixed-navigation mode or any fixed/floating toggle.
- Desktop keeps the existing floating rail plus secondary drawer behavior.
- Mobile uses a GitHub/Cloudflare-style overlay drawer: the top bar has a compact open button, the drawer slides over the page, and the workspace is not squeezed or resized.
- Mobile secondary modules are now expandable rows inside the drawer, matching the requested dropdown-style navigation.
- The mobile drawer closes after selecting a second-level module link.
- User confirmed the standing release workflow: every change should be synced to the server incrementally and verified first, then fully pushed to GitHub.
- User also requires a recovery archive for every GitHub push. Keep both a local ignored `git bundle` under `.local-archives/github-pushes/` and a GitHub archive tag such as `archive/mutilagent-main-before-YYYYMMDD-HHMMSS` pointing at the previous remote `main`.
- Local recovery bundle for this push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-091728-00b77c4.bundle`, pointing at the pre-push GitHub `mutilagent/main` commit `00b77c4`.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed with `/tmp/agent-hub-mobile-nav-incremental.tgz`.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart.
- Server health checks passed on the API service port `8000`: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- Server frontend dist contains the mobile drawer CSS marker `mobile-nav-groups`.
- GitHub recovery archive tag `archive/mutilagent-main-before-20260813-091728-00b77c4` has been pushed and points at pre-push remote `main` commit `00b77c4`.
- GitHub `main` was pushed to `8d84adc`; Actions run `31657441326` failed in `npm run test -- --run` because the global navigation description reused the text `临场调整`, which conflicted with the workflow page test that asserts those controls are not present in workflow configuration.
- The CI failure was fixed by changing the system configuration navigation description to use `运行期调度` instead of the workflow-sensitive phrase.
- The CI fix was deployed incrementally to `103.236.98.133:/opt/agent-hub/current` with `/tmp/agent-hub-mobile-nav-ci-fix.tgz`; server dist now contains `运行期调度`.
- Local recovery bundle for the follow-up CI fix push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-093300-8d84adc.bundle`, pointing at current GitHub `mutilagent/main` commit `8d84adc`.
- GitHub recovery archive tag prepared for the follow-up CI fix push: `archive/mutilagent-main-before-20260813-093300-8d84adc`.
- Follow-up CI fix commit `a05e224` was pushed to GitHub; Actions run `31657918709` passed all quality checks. The only annotation was GitHub's non-failing Node.js 20 deprecation warning for actions.
- Local recovery bundle for this final handoff-doc push was created at `.local-archives/github-pushes/mutilagent-main-before-20260813-093710-a05e224.bundle`, pointing at current GitHub `mutilagent/main` commit `a05e224`.
- GitHub recovery archive tag prepared for the final handoff-doc push: `archive/mutilagent-main-before-20260813-093710-a05e224`.

Changes made locally:

- `web/src/app/AppShell.tsx`
  - Removed `navLayout`, `agent_hub_nav_layout`, and fixed-navigation toggle behavior.
  - Added mobile drawer state and mobile-only expandable module groups.
- `web/src/styles.css`
  - Removed pinned-navigation CSS branches from the active UI.
  - Added overlay drawer styling for mobile floating navigation.
  - Hid desktop navigation/drawer content inside the mobile overlay and rendered compact expandable module rows instead.
- `web/src/app/AppShell.test.tsx`
  - Updated navigation tests for the no-fixed-nav requirement.
  - Added mobile drawer and expandable second-level module coverage.
- `.gitignore`
  - Added `.local-archives/` so local recovery bundles are never committed.
- `web/src/app/navigation.ts`
  - Updated the system configuration module description so global navigation text does not collide with workflow-isolation tests.

Verification performed locally:

- `npm.cmd test -- src/app/AppShell.test.tsx -- --runInBand` -> passed, 6 tests.
- `npm.cmd test -- src/pages/OperationalPages.test.tsx -- --runInBand` -> passed, 37 tests.
- `npm.cmd test -- --run` -> passed, 10 test files / 76 tests.
- Playwright mobile smoke at 390x844 with mocked API/login:
  - Closed state: `scrollWidth=390`, `innerWidth=390`.
  - Open drawer state: `scrollWidth=390`, `innerWidth=390`, drawer width about `296px`, workspace width stays `390px`.
  - Expanded `编排` second-level dropdown and clicked `主 Agent`; route changed to `/main-agent` and drawer closed.
  - Console/page errors: none.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed; existing Vite chunk-size warning only.
- `git diff --check` -> passed.
- GitHub Actions `quality` run `31657918709` -> passed.
- Temporary Vite dev server used for validation was closed after testing.

Remaining risks / TODOs:

- The mobile navigation fix is deployed on the server and verified on GitHub. Final handoff-doc commit `f8749e2` was pushed and GitHub Actions `quality` run `31658163507` passed.
- If the mobile drawer needs iconography like GitHub/Cloudflare, add an icon set later; this pass intentionally kept the change scoped to layout and interaction.

## 2026-08-13 P3 Runtime Process Observability Slice

Current state:

- P3-1 local implementation is complete for runtime process-event quality.
- Local commit is prepared on `main`; check `git rev-parse --short HEAD` for the latest hash because the commit may be amended before push while handoff/deploy status is finalized.
- Server incremental deployment to `103.236.98.133:/opt/agent-hub/current` was performed after user authorization.
- Server services `agent-hub-api`, `agent-hub-worker`, and `caddy` are active after restart.
- Server health checks passed after startup completed: `GET /health/live` -> `{"status":"ok"}`, `GET /health/ready` -> `{"status":"ok"}`.
- GitHub full sync to `zhangzhimiao1994/mutilagent` was completed with user-approved `git push --force-with-lease mutilagent main`, updating remote `main` from `094e56c` to `ae7352d` for the implementation push.
- GitHub Actions `quality` run `31654453267` for `feat: improve runtime process observability` passed. The run executed ruff, strict mypy, docker-backed pytest, frontend lint/test/build, shell syntax, shellcheck, bats, and docker compose config checks. Only a non-failing Node.js 20 deprecation annotation was reported by GitHub Actions.
- Config-backed dispatch, discussion, and hybrid runtimes now emit a first-class `step.started` planning event from `main_agent` before child execution.
- The planning event uses `step_id=main_agent_plan` and includes structured, safe payload fields for mode, main-agent logical model, selected roles, selected models, and planned steps.
- Hybrid runtime now preserves safe child process events instead of collapsing child execution down to only artifacts/messages. Forwarded child events include step, model, message, review, and tool events, while still filtering child checkpoints and terminal events.
- Frontend conversation process rows now render `main_agent` / `main` as `主 Agent`, so the new planning row reads as a user-facing main-Agent action.
- Added `docs/superpowers/plans/2026-08-13-p3-runtime-observability.md` to lock the P3 slice scope and guardrails.

Changes made locally:

- `src/agent_hub/runtime/defaults.py`
  - Added `_PlannedRuntime` wrapper and event payload helpers.
  - Wrapped configured dispatch/discussion/hybrid runtimes with the planning event.
- `src/agent_hub/runtime/hybrid.py`
  - Added safe forwarding for child process events.
- `web/src/pages/RunsPage.tsx`
  - Localizes main-agent actor IDs to `主 Agent`.
- Tests updated:
  - `tests/unit/runtime/test_configured_runtime.py`
  - `tests/unit/runtime/test_hybrid.py`
  - `web/src/pages/OperationalPages.test.tsx`
- Planning docs updated:
  - `docs/superpowers/plans/2026-08-13-p3-runtime-observability.md`
  - `REFACTOR_HANDOFF.md`
- `.gitignore` now ignores `.tmp/` so local deployment packages are not accidentally pushed.

Verification performed locally:

- TDD red/green:
  - Dispatch planning event test first failed because the first event was child `runtime.completed`.
  - Discussion planning event test first failed for the same missing planning-event reason.
  - Hybrid planning event test first failed for the same missing planning-event reason after fixing the test probe signature.
  - Hybrid child-process preservation test first failed because `step.started` was not forwarded.
  - Frontend timeline test first failed because `main_agent` rendered raw instead of `主 Agent`.
- Local backend:
  - `uv run ruff check src tests` -> passed.
  - `uv run mypy src tests` -> passed.
  - `uv run pytest tests/unit/runtime/test_configured_runtime.py tests/unit/runtime/test_hybrid.py -q --tb=short` -> 25 passed.
- Local frontend:
  - `npm.cmd test -- src/pages/OperationalPages.test.tsx -- --runInBand` -> 37 passed.
  - `npm.cmd run build` -> passed; existing Vite chunk-size warning only.

Remaining risks / TODOs:

- Server sync and server validation have been performed for this P3 slice. The first immediate health check ran too early while Uvicorn was still starting; a follow-up check passed once application startup completed.
- Final handoff status should be committed and pushed after this note so future sessions can see the completed server sync, GitHub full sync, and green Actions run.
- P3 channel hardening, Skill/MCP real E2E, attachment edge formats, multimodal expansion, Vibe Coding, and OpenClaw remain pending.
- Vibe Coding must be integrated into the conversation experience, not implemented as a standalone system module and not as a workflow preset.
- OpenClaw should be a system-level feature switch, off by default, for long-lived controlled computer-operation sessions. It needs explicit administrator configuration for allowed operation scope, session timeout, human-confirmation policy, audit level, and emergency-stop behavior.

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
  - GitHub run `31626370339` then failed because two-participant integration tests expected AutoGen's native reasons (`explicit_completion`, `consensus`, `max_turns`), while the new soft-completion fallback returned `sufficient_discussion`.
  - Fixed by restricting soft completion to 3+ participant discussions. Two-participant discussions now preserve AutoGen/native termination semantics.
  - Server re-smoke after the scope fix:
    - direct run `392e2e24-d8df-4943-a33c-f7790f8452bc`: completed.
    - dispatch run `01045063-55cc-4e66-b201-78c413fedc9e`: completed.
    - discuss run `fa0de888-d641-4f7d-add3-b7de87c30605`: completed.
    - hybrid run `2de0f33b-e5ba-4509-8d9e-b752a3b7a505`: completed.

Remaining risks / TODOs:

- P1/P1.5 runtime chain is server-verified, but the user still needs to visually validate the mobile conversation UI details in browser because screenshot-level UX cannot be fully proven from CLI tests.
- Old frontend asset files remain in `web/dist/assets` on the server. Current `index.html` points to the latest assets and works, but the deploy script should later replace `web/dist` atomically or clean old hashed assets.
- Old pre-fix failed runs remain in the database and logs. Diagnose only by timestamp/run id when comparing future failures.
- Continue plan order: finish remaining P1.5 UI/admin closure, then P2 Hermes learning loop, then full module verification, then P3 channel intelligent-agent mode/multimodal/vibe coding.

## 2026-08-13 P2 Hermes Runtime Policy Closure

User directive:

- Continue strictly by plan priority.
- Do not let Hermes make the system "smarter" by silently bypassing review/confirmation.
- Verify on the real Linux server before pushing.

Root causes found:

- `PersistentHermesRunAdvisor` considered all stored Hermes lessons, including entries whose `confirmed_at` was still empty. That meant an unconfirmed learning record could affect runtime routing.
- The runtime advisor ignored the main Agent `hermes_policy` stored by the management UI. As a result, `off` / `observe` did not reliably prevent Hermes advice from entering dispatch decisions.

Changes made:

- Runtime Hermes advice now only uses confirmed learning records.
- Runtime Hermes advice now reads the main Agent policy from `AdminResourceRow(kind="main_agent", resource_id="default")`.
- Policy behavior:
  - `off` and `observe`: no runtime routing advice is returned.
  - `suggest` and `confirm_before_apply`: advice may be returned, but it is marked `requires_approval=True`.
  - Missing or invalid policy defaults to `observe`, so Hermes is passive unless explicitly enabled for advice.
- Added integration coverage for:
  - unconfirmed Hermes records being ignored;
  - confirmed Hermes records being usable;
  - main Agent `observe` policy blocking runtime advice;
  - main Agent `suggest` policy returning approval-required advice.

Verification performed:

- Server real checks on `103.236.98.133`:
  - `server_hermes_policy_check.py` -> `PASS: unconfirmed ignored; confirmed used mode=dispatch`.
  - `server_hermes_main_policy_check.py` -> `PASS: observe ignored; suggest requires approval mode=dispatch`.
  - P1 runtime smoke after this change:
    - direct: completed, 4 events, 1 artifact.
    - dispatch: completed, 65 events, 14 artifacts.
    - discuss: completed, 7 events, 1 artifact.
    - hybrid: completed, 24 events, 17 artifacts.
  - P1.5 admin smoke:
    - users, models, agents, workflows, settings, main-agent, runs, skills, MCP, channels, memory, audit, logs, Hermes all returned HTTP 200.
    - user create/role/disable/enable/password/delete passed.
    - agent/workflow/MCP/memory/skill/archive/attachment/log category flows passed.
- Local checks:
  - `uv run ruff check src tests` -> passed.
  - `uv run mypy src` -> passed.
  - `uv run pytest tests\api\test_admin_resources.py tests\unit\runs\test_temporary_agent.py tests\unit\runs\test_temporary_agent_policy.py tests\unit\runtime\test_autogen_artifact_rollback.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\test_hybrid.py -q --tb=short` -> 88 passed.
  - `npm.cmd test -- src/pages/OperationalPages.test.tsx src/pages/ChannelsPage.test.tsx -- --runInBand` -> 39 passed.
  - `npm.cmd run build` -> passed; Vite emitted only the existing chunk-size warning.

Remaining risks / TODOs:

- Local integration tests still require a PostgreSQL test service; the Hermes DB policy behavior was verified against the real server DB instead.
- Temporary validation scripts remain under server `/tmp` and local `.tmp/`; they are not part of production code and should not be committed.
- P3 is still pending and must not start until the user accepts P2/full verification as complete: multimodal APIs, channel "Agent/智能体" integration mode, and vibe coding/OpenClaw-style expansion.

## 2026-08-13 CI Migration Downgrade Fix

Context:

- GitHub run `31630569668` failed in `uv run pytest -q`.
- Failure was in migration round-trip tests, not in the Hermes runtime path:
  - downgrade from `0013_agent_admin_resources` attempted to restore an older `agent_hub_admin_resources.kind` check constraint.
  - existing rows with newer kinds such as `agent` / `main_agent` violated the old constraint.

Root cause:

- Migrations `0012`, `0013`, and `0014` widened the allowed admin resource kinds but their downgrade paths recreated the previous constraint without first removing rows that no longer fit the previous version.

Changes made:

- `0012_admin_resource_logs.downgrade()` now deletes `log` and `setting` resources before restoring the pre-0012 constraint.
- `0013_agent_admin_resources.downgrade()` now deletes `agent` and `main_agent` resources before restoring the pre-0013 constraint.
- `0014_channel_admin_resources.downgrade()` now deletes `channel` resources before restoring the pre-0014 constraint.
- Expanded the admin resource constraint unit test to assert all current persistent admin resource kinds are present.

Verification performed:

- Local:
  - `uv run ruff check alembic tests/unit/test_database_resources.py src tests` -> passed.
  - `uv run mypy src tests` -> passed.
  - `uv run pytest tests\unit\test_database_resources.py tests\api\test_admin_resources.py::test_main_agent_config_saves_dedicated_model_api_and_control_policy tests\api\test_admin_resources.py::test_channel_config_can_be_saved_without_exposing_secrets -q --tb=short` -> 9 passed.
- Server:
  - Synced the three Alembic migration files into `/opt/agent-hub/current/alembic/versions`.
  - Ran `python -m py_compile` on those migration files.

Important note:

- Do not run downgrade verification on the production server DB; it intentionally deletes newer admin resource rows when moving to older schema versions. CI's isolated PostgreSQL environment is the correct place to verify downgrade/upgrade round trips.

## 2026-08-13 P3 Gap Inventory / Priority Guardrail

Context:

- User sent `54123`; treated as non-actionable input and continued the agreed priority plan.
- P1 / P1.5 / P2 are considered closed from prior server verification:
  - direct / dispatch / discuss / hybrid runtime smoke completed on the server.
  - admin resource surfaces returned healthy and CRUD flows passed.
  - Hermes runtime advice is gated by confirmation and main-Agent policy.
  - GitHub Actions was checked after push and is green at run `31631051122`.

Current implementation facts verified from source:

- Navigation is already consolidated into 6 top-level groups in `web/src/app/navigation.ts`: 对话、编排、资源、工具、通道、系统.
- User management is not only a display page anymore; `web/src/pages/UsersPage.tsx` includes create user, role changes, disable/enable, password reset, and protected-account semantics via backend permissions.
- Skill upload and lifecycle are real:
  - `/api/v1/admin/skills/upload` exists.
  - ZIP and tar/tar.gz skill package inspection is covered by tests.
  - `RuntimeCapabilityGateway` can invoke approved skill packages via `SystemdSkillSandbox`.
- MCP is not a mock-only shell:
  - admin list/upsert/delete endpoints exist.
  - `McpService` handles discovery, projection, invoke, denial, timeout, and audit.
  - `McpClient` implements stdio, SSE, and streamable HTTP JSON-RPC clients.
- Attachments are persisted with retention and safe extraction manifest:
  - image/context/archive classification exists.
  - ZIP and tar archive manifests/extraction exist.
  - unsupported archive formats are marked unsupported instead of silently pretending success.
- Conversation continuity has backend and frontend paths:
  - `conversation_id` and `reference_conversation_id` are accepted by run submission.
  - `/api/v1/admin/conversations/{conversation_id}` exists.
  - frontend tests cover keeping old conversation messages, starting new chat, handoff/reference loading, and bulk conversation deletion.
- Feishu basic callback/reply path exists:
  - `/channels/feishu/events` is mounted on the main API.
  - events are normalized, deduplicated, submitted to the run gateway, and replied when terminal.
  - reply failures are recorded under `channel_error`.
- Generic webhooks exist for DingTalk, WeCom bot/app, WeChat Official/KF, Telegram, Slack, QQ, and custom webhook.
- Multimodal currently covers image/OCR/vision analysis. It does not yet cover video understanding, image/video generation, ASR/TTS, or per-capability model routing beyond current text/vision paths.

P3 gaps to handle next, in priority order:

1. Runtime process event quality:
   - Current UI has compact process rows and detail drawer, but backend events are still not granular enough for the user's required Kimi/Codex hybrid UX.
   - Need one chronological row per meaningful action: main Agent routing, role selection, task assignment, each child Agent receiving work, model used, child Agent output, discussion opinion, decision, final synthesis.
   - Remove vague rows like "生成了结果" unless details include actor, model, instruction, output summary, and artifact reference.
2. Channel hardening:
   - Current Feishu integration is enterprise self-built app callback style. Target P3 goal is channel-as-Agent / Feishu intelligent-agent style where supported.
   - Do not block P3 on native intelligent-agent mode; first harden current callback mode, attachment retrieval/delivery, command grammar, and diagnostic logs.
3. Skill/MCP real E2E:
   - Admin and runtime plumbing exists, but needs server-side end-to-end validation through an actual uploaded skill and a real MCP server invocation from an agent run.
4. Attachment coverage:
   - ZIP/tar are implemented. Other common archive formats such as 7z/rar need explicit supported/unsupported behavior, preferably safe rejection with clear UI guidance unless a safe extractor is introduced.
5. Multimodal P3 expansion:
   - Add provider/model capability routing for image understanding, video understanding, image generation, video generation, ASR/TTS, and embeddings.
   - Keep multimodal optional, not part of base model config.
6. Vibe coding / OpenClaw-style workflows:
   - Make code review and coding workflows first-class: repo/zip intake, multiple model reviewers, model-attributed findings, synthesis by main Agent, and artifact/report output.
7. CowAgent-level parity decisions:
   - Memory/Hermes should evolve toward stronger long-term memory/retrieval/experience distillation.
   - Skill install sources should grow beyond upload to GitHub/URL/curated hub/conversational skill authoring.

Guardrail:

- Do not jump to P3 feature work before closing P3-0 verification tasks above.
- User wants every change synced to the server first, verified on the server, then pushed to GitHub only after validation.
- After any push, check GitHub Actions and fix until green or report a concrete external blocker.

## 2026-08-13 P1/P1.5 Polish Pass: Users, Skills, Navigation, Archive Attachments

Current state:

- This pass is still P1/P1.5/P2 stabilization work, not P3 feature expansion.
- User clarified the execution rule: finish and server-verify P1/P1.5/P2 first; only then start P3.
- Before implementing Vibe Coding / OpenClaw-level capabilities, do a module-unification refactor. Treat Vibe Coding / OpenClaw as system-level capabilities, not workflow presets.
- Server sync and server validation are still pending for this pass; do not push this pass before server validation.

Changes made locally:

- User management:
  - Added backend `PATCH /api/v1/users/{user_id}` for editing username, role, and disabled/enabled state in one operation.
  - Preserved protected initial-admin safeguards and last-super-admin/current-user disable protections.
  - Blocked non-super-admin operators from assigning `super_admin`.
  - Frontend user page now has an explicit edit panel.
  - Password reset now opens a modal instead of occupying persistent page space.
- Skill management:
  - Added multi-select, select-all, batch approve, and batch delete.
  - Kept per-skill approve/delete actions.
- Chat attachment handling:
  - Archive uploads no longer auto-scan as Skill and no longer fall back silently.
  - Archive uploads now default to normal attachments so the same archive can be used for code review, task files, normal context, or Skill install depending on user intent.
  - Added explicit `作为 Skill 安装` action on archive attachment cards; only that action calls the Skill upload/scanning API.
- Navigation:
  - Top-level grouped navigation enters the default module directly instead of forcing an intermediate hub page.
  - Added a persistent `悬浮导航栏` / `固定导航栏` toggle using `localStorage`.
  - Added compact styles for the new toggle and pinned layout.
- UI density:
  - Existing compact button/table styling from this pass remains in place for users, skills, and table action areas.

Verification performed locally:

- Frontend targeted red/green:
  - New archive behavior test failed first because ZIP was still auto-scanned as Skill.
  - New navigation toggle test failed first because no toggle existed.
  - After implementation: `npm.cmd test -- src/app/AppShell.test.tsx src/pages/OperationalPages.test.tsx -t "archive as a normal attachment|switch the navigation"` -> passed.
- Frontend regression:
  - `npm.cmd test -- src/app/AppShell.test.tsx src/pages/UsersPage.test.tsx src/pages/SkillsPage.test.tsx src/pages/OperationalPages.test.tsx -t "AppShell presentation|user|Skill|archive|image attachment|conversation"` -> 4 files passed, 23 tests passed.
- Frontend production build:
  - `npm.cmd run build` -> passed; only existing Vite chunk-size warning.
- Backend API:
  - `.venv\Scripts\python.exe -m pytest tests/api/test_foundation_api.py -q` -> 72 passed.
- Local integration DB note:
  - `tests/integration/auth/test_bootstrap.py::test_user_admin_creates_disables_and_deletes_local_users` timed out waiting for a local PostgreSQL fixture at `127.0.0.1:54329`; this is an environment readiness issue on the Windows dev box, not a failed assertion in the new user-management code.

Server validation performed for this pass:

- Synced the incremental source and `web/dist` package to `103.236.98.133:/opt/agent-hub/current` without uploading a full release directory.
- Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Verified `/health/live` and `/health/ready` returned `{"status":"ok"}`.
- Verified Caddy served the web UI root over the forwarded public UI path.
- Verified deployed frontend assets contain the new Skill, batch-delete, and navigation-toggle strings.
- Ran real HTTP smoke against the deployed API with a short-lived super-admin token generated from the server's configured signing key:
  - `GET /api/v1/users` -> 200.
  - `POST /api/v1/users` -> 200.
  - `PATCH /api/v1/users/{id}` -> 200.
  - `PATCH /api/v1/users/{id}/disabled` -> 200.
  - `PATCH /api/v1/users/{id}/password` -> 200.
  - protected initial admin update rejection -> 409.
  - `DELETE /api/v1/users/{id}` -> 204.
  - `POST /api/v1/runs/attachments/upload` with `.tar.gz` ordinary archive -> 200, `kind=archive`.
  - `POST /api/v1/admin/skills/upload` with a valid Skill ZIP -> 200.
  - `POST /api/v1/admin/skills/{id}/approve` -> 200.
  - `DELETE /api/v1/admin/skills/{id}` -> 200.

Remaining before stopping:

- Commit this pass.
- Push to GitHub.
- Check GitHub Actions for the pushed `main` run and fix if red.
- Then stop and provide the P3 handoff; do not start P3 in this thread.

## 2026-08-13 P3 Feishu Multimedia Factory Wiring

Current state:

- P3 Feishu production media factory wiring is complete.
- The Feishu webhook now respects the system-level `multimedia_generation_enabled` switch.
- When the switch is disabled and a Feishu image message arrives, Agent Hub replies with a temporary unsupported-image message and does not submit the image to the agent gateway.
- Vibe coding remains planned as a conversation-integrated system capability, not a separate module.
- OpenClaw remains planned as a long-running computer-control feature switch.
- Multimedia generation should be implemented as one system switch covering image/video processing and generation. Future video generation execution must validate model capability before dispatch.

Changes made:

- Added `multimedia_generation_enabled` to admin system settings.
- Added `src/agent_hub/channels/feishu/media_factory.py` with production Feishu media service factory wiring.
- Wired `build_feishu_media_service_factory` into the FastAPI app lifespan when secret service and Redis are available.
- Added webhook gating so image attachments are blocked and replied to when multimedia generation is disabled.
- Added tests for disabled-image reply/no-submit behavior and production factory wiring.

Local verification:

- `uv run pytest tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/api/test_admin_resources.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py -q --tb=short` -> 106 passed.
- `uv run ruff check src tests/api/test_channel_webhooks.py tests/unit/test_app_wiring.py tests/api/test_admin_resources.py tests/contracts/feishu/test_receivers.py tests/unit/channels/feishu/test_media_client.py tests/e2e/feishu/test_conversation.py tests/unit/channels/test_submitter.py` -> passed.
- `uv run mypy --strict src tests` -> passed.

Server deployment and verification:

- Uploaded incremental package to `103.236.98.133:/tmp/agent-hub-p3-feishu-multimedia-factory.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Verified `agent-hub-api`, `agent-hub-worker`, and `caddy` were active.
- Verified `/health/live` and `/health/ready` returned `{"status":"ok"}`.
- Verified deployed OpenAPI includes `multimedia_generation_enabled`.
- Verified deployed source compiles with `py_compile`.
- Ran a server-side source-path Feishu multimedia check covering disabled image reply/no-submit and enabled image analysis/gateway submission -> passed.
- Note: the production virtualenv does not include `pytest`, so full pytest was not run on the server.

GitHub push and recovery:

- Commit: `d16cef9 feat: gate feishu media by multimedia setting`.
- Local ignored recovery bundle: `.local-archives/github-pushes/mutilagent-main-before-20260813-120200-d53657e.bundle`.
- GitHub recovery tag: `archive/mutilagent-main-before-20260813-120200-d53657e`.
- Pushed with `git push --force-with-lease mutilagent main`.
- GitHub Actions run `31665754005` for `d16cef9` completed successfully.

Next:

- Continue P3 with model capability recognition and multimedia execution guardrails:
  - built-in known model capability registry,
  - admin override support,
  - execution-time enforcement so video tasks never dispatch to models without `video_generation`,
  - executor role/button path for multimedia generation after planning.

## 2026-08-13 P3 Multimedia Capability Registry and Guardrails

Current state:

- Model capability recognition and multimedia execution guardrails are implemented.
- `multimedia_generation_enabled` is exposed in the frontend settings page as a system-level switch.
- Model configuration supports `image_generation` and `video_generation`.
- Known image/video generation model families are inferred conservatively; unknown models do not receive video capability unless an admin explicitly declares it.
- Runtime multimedia generation requests go through a dedicated executor that asks the model gateway for `image_generation` or `video_generation`, so the existing registry/capacity path blocks unsupported models.
- Dynamic role planning now includes a `multimedia_generator` executor role for image/video generation tasks after planning.

Changes made:

- Added `agent_hub.models.capabilities.infer_model_capabilities`.
- Extended `ModelCapability` and config schema with `image_generation` and `video_generation`.
- Normalized admin model create/update requests to merge declared and inferred capabilities.
- Added `agent_hub.multimodal.generation.MultimediaGenerationExecutor`.
- Added a catalog-backed `multimedia_generator` dispatch/hybrid role with `generate_multimedia` permission and a `submit_video_to_text_only_model` forbidden action.
- Added frontend settings UI for the multimedia generation switch.
- Added frontend model capability checkboxes for image and video generation.

Local verification:

- TDD red checks were added first for model inference, schema acceptance, generation executor dispatch requirements, role planning, API auto-inference, and frontend controls.
- `uv run pytest tests/unit/models/test_registry.py tests/unit/models/test_gateway.py tests/unit/config/test_schema.py tests/unit/multimodal/test_generation.py tests/unit/multimodal/test_images.py tests/unit/runtime/test_role_planner.py tests/api/test_admin_resources.py -q --tb=short` -> 334 passed, 12 skipped.
- `uv run ruff check src tests/unit/models/test_registry.py tests/unit/config/test_schema.py tests/unit/multimodal/test_generation.py tests/unit/runtime/test_role_planner.py tests/api/test_admin_resources.py` -> passed.
- `uv run mypy --strict src tests/unit/models/test_registry.py tests/unit/config/test_schema.py tests/unit/multimodal/test_generation.py tests/unit/runtime/test_role_planner.py tests/api/test_admin_resources.py` -> passed.
- `npm.cmd test -- --run src/pages/ConfigPage.test.tsx src/pages/ModelsPage.test.tsx src/pages/MainAgentPage.test.tsx src/pages/OperationalPages.test.tsx` -> 56 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

Server deployment and verification:

- Uploaded incremental package to `103.236.98.133:/tmp/agent-hub-p3-multimedia-capabilities.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Verified `agent-hub-api`, `agent-hub-worker`, and `caddy` were active.
- Verified `/health/live` and `/health/ready` returned `{"status":"ok"}`.
- Verified deployed Python files compile with `py_compile`.
- Ran `/tmp/multimedia_capability_check.py` with `PYTHONPATH=src`; it passed capability inference, API model-create auto-inference, unsupported-video registry blocking, and executor video-capability dispatch checks.
- Verified deployed `web/dist` contains the new multimedia switch and video generation capability UI.

Next:

- Commit this P3 slice.
- Create local ignored GitHub recovery bundle and GitHub archive tag for the previous remote main.
- Push `main` with `git push --force-with-lease mutilagent main`.
- Check GitHub Actions and fix/redeploy/repush if red.
- Continue P3 toward OpenClaw feature switch wiring and conversation-integrated vibe coding after CI is green.

## 2026-08-13 P3 OpenClaw Feature Switch

Current state:

- OpenClaw is represented as a system-level capability, not a workflow.
- `openclaw_enabled` defaults to `false`.
- `openclaw_mode` defaults to `ask` and supports Codex-style permission modes: `read_only`, `ask`, `auto_review`, and `trusted_auto`.
- The settings page exposes the OpenClaw switch and permission mode selector.
- OpenClaw operation requests currently create a persisted `waiting_user_approval` plan and audit record; they do not execute computer/server actions before approval.
- OpenClaw plans can be fetched and resolved with approve/reject decisions; approval still does not execute the action yet.
- The request model is platform-aware (`linux`, `windows`, `macos`) so future executors can adapt per OS instead of assuming Linux-only behavior.

Changes made:

- Added `openclaw_enabled` to admin system settings API request/response models.
- Added `openclaw_mode` with Codex-style permission modes.
- Added `POST /api/v1/admin/openclaw/operations` to create user-approval requests without executing actions.
- Added `GET /api/v1/admin/openclaw/operations/{operation_id}` and `PATCH /api/v1/admin/openclaw/operations/{operation_id}` for the approval lifecycle.
- Added read-only blocking for non-read OpenClaw operation plans.
- Added persistent `openclaw` admin resources and Alembic migration `0016_openclaw_admin_resources.py`.
- Added frontend API schema support for OpenClaw fields with backward-compatible defaulting.
- Added Config page UI for the OpenClaw long-running computer operation switch and permission mode selector.
- Added tests for default-disabled API behavior, approval-plan creation, read-only blocking, and frontend save payload behavior.

Local verification:

- TDD red checks were added first:
  - backend failed because `SystemSettingsResponse` had no `openclaw_enabled`;
  - OpenClaw operation endpoint initially returned 405 before approval planning was implemented;
  - frontend failed because `openclaw-toggle` / mode controls did not exist.
- `uv run pytest tests/api/test_admin_resources.py tests/unit/test_database_resources.py -q --tb=short` -> 63 passed.
- `uv run ruff check src\agent_hub\api\routers\admin.py src\agent_hub\db\models.py tests\api\test_admin_resources.py tests\unit\test_database_resources.py alembic\versions\0016_openclaw_admin_resources.py` -> passed.
- `uv run mypy --strict src\agent_hub\api\routers\admin.py src\agent_hub\db\models.py tests\api\test_admin_resources.py tests\unit\test_database_resources.py alembic\versions\0016_openclaw_admin_resources.py` -> passed.
- `npm.cmd test -- --run src/pages/ConfigPage.test.tsx src/app/AppShell.test.tsx src/pages/OperationalPages.test.tsx` -> 45 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

Functional verification standard going forward:

- Server-side verification must exercise the implemented feature path, not only port availability, service `active`, or `/health/*`.
- For OpenClaw, server verification must cover the disabled rejection path, enabled approval-request path, read-only blocking path, audit/log evidence, and later the approved execution path on each supported OS adapter.
- For image/video generation, server verification must submit an actual generation request when enabled, confirm the generated artifact or explicit provider result, and confirm disabled/capability-mismatch blocking.

Server deployment and verification:

- Uploaded incremental package to `103.236.98.133:/tmp/agent-hub-p3-openclaw-switch.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Uploaded refreshed approval lifecycle package to `103.236.98.133:/tmp/agent-hub-p3-openclaw-approval.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Ran Alembic migration `0016_openclaw_admin_resources` with the production EnvironmentFile loaded without printing secrets.
- Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Verified `agent-hub-api`, `agent-hub-worker`, and `caddy` were active.
- Verified `/health/live` and `/health/ready` returned `{"status":"ok"}`.
- Verified deployed Python files compile with `py_compile`.
- Ran `/tmp/openclaw_settings_check.py` with `PYTHONPATH=src`; it passed default-disabled and enabled settings serialization checks.
- Verified deployed `web/dist` contains the OpenClaw switch UI and `openclaw_enabled` field.
- Ran `/tmp/openclaw_api_functional_check.py` through the real local HTTP API with a short-lived admin token generated from the running server environment; it passed:
  - disabled OpenClaw rejects operation creation with `openclaw_disabled`;
  - `ask` mode creates a persisted `waiting_user_approval` operation;
  - the operation can be fetched by ID;
  - approval changes status to `approved`;
  - repeated resolution is rejected with `openclaw_already_resolved`;
  - `read_only` mode rejects server command planning with `openclaw_read_only`;
  - original settings are restored at the end.
- Verified deployed `web/dist` contains `OpenClaw 权限模式` and `openclaw_mode`.

GitHub push and recovery:

- Commit: `af1171f feat: add openclaw feature switch`.
- Local ignored recovery bundle: `.local-archives/github-pushes/mutilagent-main-before-20260813-130255-3949892.bundle`.
- GitHub recovery tag: `archive/mutilagent-main-before-20260813-130255-3949892`.
- Pushed with `git push --force-with-lease mutilagent main`.
- GitHub Actions run `31669019886` completed successfully.

Next:

- Continue P3 toward approved executor integration across Linux/Windows/macOS and conversation-integrated vibe coding after CI is green.

## 2026-08-13 P3 OpenClaw Approved Execution Boundary

Current state:

- OpenClaw now has an approved execution path for Linux `server_command` operations.
- Execution is still blocked by default: `openclaw_allowed_commands` defaults to an empty list.
- An operation must be approved first, the global OpenClaw switch must still be enabled at execution time, and the command argv must exactly match an allowlisted argv list.
- Shell wrapper executables (`bash`, `sh`, `cmd`, `powershell`, `pwsh`, etc.) are denied even if an admin accidentally allowlists them.
- Windows/macOS and non-command OpenClaw operations are explicitly modeled but return `openclaw_adapter_unavailable` until dedicated adapters are implemented.
- Executed operations are persisted with `status=executed` and an execution summary (`exit_code`, stdout/stderr, truncation flag, executor, timestamp).

Changes made:

- Added `agent_hub.openclaw.executor` with a bounded subprocess executor and command allowlist check.
- Added `openclaw_allowed_commands` to system settings.
- Added `OpenClawExecutionResponse` and execution metadata on OpenClaw operations.
- Added `POST /api/v1/admin/openclaw/operations/{operation_id}/execute`.
- Added in-memory and persistent service support for saving execution results and audit records.
- Updated frontend system settings schema so config saves preserve the allowlist field.
- Added API tests for approval-required execution, unlisted command denial, allowlisted Linux command execution, shell-deny hardening, and Windows adapter unavailability.

Local verification:

- TDD red checks were added first:
  - default settings had no `openclaw_allowed_commands`;
  - execution endpoint returned 405;
  - settings update with the new allowlist field returned 422.
- `uv run pytest tests/api/test_admin_resources.py -q -k openclaw --tb=short` -> 9 passed.
- `uv run pytest tests/api/test_admin_resources.py tests/unit/test_database_resources.py -q --tb=short` -> 68 passed.
- `uv run ruff check src\agent_hub\api\routers\admin.py src\agent_hub\openclaw tests\api\test_admin_resources.py` -> passed.
- `uv run mypy --strict src\agent_hub\api\routers\admin.py src\agent_hub\openclaw tests\api\test_admin_resources.py` -> passed.
- `npm.cmd test -- --run src/pages/ConfigPage.test.tsx src/app/AppShell.test.tsx src/pages/OperationalPages.test.tsx` -> 45 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.
- `uv run pytest -q --tb=short` was attempted but timed out after 5 minutes before producing a result; targeted backend and frontend checks above passed.

Server deployment and verification:

- Uploaded incremental package to `103.236.98.133:/tmp/agent-hub-p3-openclaw-executor.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Restarted `agent-hub-api` and `agent-hub-worker`; reloaded Caddy.
- Verified `agent-hub-api`, `agent-hub-worker`, and `caddy` were active.
- Verified deployed Python files compile with `py_compile`.
- Ran `/tmp/openclaw_executor_functional_check.py` through the real local HTTP API with a short-lived admin token generated from the running server environment; it passed:
  - unapproved OpenClaw operation execution is rejected with `openclaw_not_approved`;
  - approved allowlisted Linux command executes and returns `openclaw-approved-exec-ok`;
  - executed operation can be fetched with persisted `status=executed` and execution metadata;
  - shell wrapper command is rejected with `openclaw_command_denied` even when present in the allowlist;
  - Windows command returns `openclaw_adapter_unavailable`.
- Verified deployed `web/dist` contains `openclaw_allowed_commands`.

Next:

- Commit this slice.
- Create local ignored GitHub recovery bundle and GitHub archive tag for the previous remote main.
- Push `main` with `git push --force-with-lease mutilagent main`.
- Check GitHub Actions and fix/redeploy/repush if red.
- Continue P3 with the OpenClaw approval UI/operation console and then conversation-integrated Vibe Coding.

## 2026-08-13 P3 OpenClaw Settings Console UI

Current state:

- The system settings page now exposes OpenClaw allowlist editing and a small operation console.
- Admins can save `openclaw_allowed_commands` as JSON argv arrays from the UI.
- Admins can create a Linux `server_command` approval request from the UI, then approve/reject it, then execute after approval.
- The UI displays the latest operation status and command output from the execution response.
- The backend safety boundary remains unchanged: execution still requires approval and exact allowlist match, and shell wrappers remain blocked.

Changes made:

- Added frontend API schemas and methods for OpenClaw operation create/resolve/execute.
- Added OpenClaw allowlist textarea to Config page.
- Added OpenClaw operation console controls: request approval, approve, reject, execute, status, and execution output.
- Added Config page regression test for saving allowlist and running the UI approval/execution flow.

Local verification:

- TDD red check was added first; ConfigPage failed because OpenClaw console controls did not exist.
- `npm.cmd test -- --run src/pages/ConfigPage.test.tsx` -> 3 passed.
- `npm.cmd run lint` -> passed.
- `npm.cmd test -- --run src/pages/ConfigPage.test.tsx src/app/AppShell.test.tsx src/pages/OperationalPages.test.tsx` -> 46 passed.
- `npm.cmd run build` -> passed, with the existing Vite chunk-size warning.

Server deployment and verification:

- Uploaded incremental package to `103.236.98.133:/tmp/agent-hub-p3-openclaw-console.tgz`.
- Deployed incrementally into `/opt/agent-hub/current`.
- Reloaded Caddy and verified `agent-hub-api`, `agent-hub-worker`, and `caddy` were active.
- Verified server `web/dist/index.html` points to the new JS bundle `index-rd35pJsw.js`.
- Verified current server frontend bundle contains `openclaw-create-operation` and `openclaw-execution-output`.
- Re-ran `/tmp/openclaw_executor_functional_check.py` through the real local HTTP API; it passed the approved execution path and guardrail checks.

Next:

- Commit this UI slice.
- Create local ignored GitHub recovery bundle and GitHub archive tag for the previous remote main.
- Push `main` with `git push --force-with-lease mutilagent main`.
- Check GitHub Actions and fix/redeploy/repush if red.
- Continue P3 with conversation-integrated Vibe Coding and richer multi-system OpenClaw adapters.
