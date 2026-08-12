# Agent Hub Handoff

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
