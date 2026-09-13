import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")



def test_openclaw_local_adapter_has_cross_platform_and_installed_cli_entrypoints() -> None:
    pyproject = tomllib.loads(read("pyproject.toml"))
    launcher = read("scripts/agent-hub")
    command = read("scripts/commands/openclaw-adapter.sh")

    assert (
        pyproject["project"]["scripts"]["agent-hub-openclaw-adapter"]
        == "agent_hub.openclaw.local_adapter:main"
    )
    assert "openclaw-adapter     Start a local OpenClaw Adapter" in launcher
    assert "openclaw-adapter" in launcher
    assert "OPENCLAW_ADAPTER_TOKEN" in command
    assert "OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON" in command
    assert "OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON" in command
    assert "OPENCLAW_ADAPTER_SCREEN_READ_COMMAND_JSON" in command
    assert "OPENCLAW_ADAPTER_DESKTOP_ACTION_COMMAND_JSON" in command
    assert 'export PYTHONPATH="$SOURCE_DIR/src:$PYTHONPATH"' in command
    assert "-m agent_hub.openclaw.local_adapter" in command


def test_release_packager_includes_built_web_dist() -> None:
    launcher = read("scripts/agent-hub")
    command = read("scripts/commands/package-release.sh")

    assert "package-release     Build a deployable source archive including web/dist." in launcher
    assert "package-release" in launcher
    assert "npm --prefix \"$SOURCE_DIR/web\" run build" in command
    assert '[[ -f "$SOURCE_DIR/web/dist/index.html" ]]' in command
    assert 'git -C "$SOURCE_DIR" archive --format=tar HEAD' in command
    assert 'tar -xf - -C "$staging_dir"' in command
    assert 'cp -a "$SOURCE_DIR/web/dist/." "$staging_dir/web/dist/"' in command
    assert 'tar -cf "$output" -C "$staging_dir" .' in command


def test_release_pruner_is_registered_and_protects_current_release() -> None:
    launcher = read("scripts/agent-hub")
    command = read("scripts/commands/prune-releases.sh")

    assert "prune-releases      Preview or remove old native release directories." in launcher
    assert "doctor|status|logs|backup|restore|upgrade|prune-releases|verify-release" in launcher
    assert 'keep="${AGENT_HUB_RELEASES_TO_KEEP:-2}"' in command
    assert "Usage: scripts/agent-hub prune-releases" in command
    assert "--install-root" in command
    assert "--execute" in command
    assert "--yes" in command
    assert '"$release_dir_real"/*)' in command
    assert 'die "current must point inside release directory' in command
    assert 'protected["$(basename -- "$current_real")"]="current"' in command
    assert 'find "$release_dir_real" -mindepth 1 -maxdepth 1 -type d' in command
    assert 'rm -rf -- "$release_path"' in command


def test_release_verifier_is_registered_and_checks_current_revision() -> None:
    launcher = read("scripts/agent-hub")
    command = read("scripts/commands/verify-release.sh")

    assert "verify-release      Verify native current release pointer and service state." in launcher
    assert "verify-release" in launcher
    assert "Usage: scripts/agent-hub verify-release" in command
    assert "--expect-revision" in command
    assert "--skip-services" in command
    assert 'current_link="$install_root/current"' in command
    assert 'current_real="$(readlink -f -- "$current_link")"' in command
    assert 'die "current must point inside release directory' in command
    assert 'die "current release REVISION file is missing' in command
    assert 'die "current release revision mismatch' in command
    assert "agent-hub-api.service agent-hub-worker.service agent-hub-litellm.service" in command


def test_harness_acceptance_command_is_registered_for_real_machine_and_stress_checks() -> None:
    launcher = read("scripts/agent-hub")
    command = read("scripts/commands/harness-acceptance.sh")

    assert "harness-acceptance  Run Codex/DeepSeek harness acceptance smoke and stress checks." in launcher
    assert "harness-acceptance" in launcher
    assert "Usage: scripts/agent-hub harness-acceptance" in command
    assert "--profile codex|deepseek|all|production-safe" in command
    assert "--stress" in command
    assert "--strict-interaction-recovery" in command
    assert "--read-only" in command
    assert 'production-safe) profile="all"; read_only=1 ;;' in command
    assert 'codex|deepseek|all|production-safe) ;;' in command
    assert "mode: read-only" in command
    assert "mode: write-probes-enabled" in command
    assert "run_codex_profile" in command
    assert "run_deepseek_profile" in command
    assert "check_health_json" in command
    assert "check_prometheus_metrics" in command
    assert "check_protected_boundary" in command
    assert "check_write_protected_boundary" in command
    assert "check_error_envelope" in command
    assert "check_runtime_failure_diagnostics" in command
    assert "check_interaction_prevention_and_recovery" in command
    assert "check_multimode_interaction_matrix" in command
    assert "check_openapi_safe_projection" in command
    assert "check_openapi_task_mode_schema" in command
    assert "check_openapi_model_capability_schema" in command
    assert "run_lifecycle_profile" in command
    assert "skip: run lifecycle probe is disabled in read-only mode" in command
    assert "run_strict_interaction_recovery_profile" in command
    assert "AGENT_HUB_ACCEPTANCE_STRICT_INTERACTION_RECOVERY" in command
    assert "profile: strict interaction recovery" in command
    assert "fail: strict interaction recovery requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN" in command
    assert '"/api/v1/admin/models/probe"' in command
    assert '"desired_concurrency": 32' in command
    assert "ok: strict model probe interaction control" in command
    assert "run_openapi_capability_profile" in command
    assert "run_stress_profile" in command
    assert "for path in /health /health/live /health/ready /metrics /openapi.json /login; do" in command
    assert "/health/live" in command
    assert "/health/ready" in command
    assert 'check_url "api health alias" "/health"' in command
    assert 'check_health_json "api health alias" "/health"' in command
    assert "/metrics" in command
    assert "/openapi.json" in command
    assert "/login" in command
    assert "AGENT_HUB_ACCEPTANCE_BASE_URL" in command
    assert "AGENT_HUB_ACCEPTANCE_CONCURRENCY" in command
    assert "AGENT_HUB_ACCEPTANCE_ITERATIONS" in command
    assert "AGENT_HUB_ACCEPTANCE_RETRIES" in command
    assert "AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS" in command
    assert "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN" in command
    assert "AGENT_HUB_ACCEPTANCE_RUN_MESSAGE" in command
    assert "--retries N" in command
    assert "--retry-delay SECONDS" in command
    assert "--verify-release" in command
    assert "--install-root DIR" in command
    assert "--expect-revision SHA" in command
    assert 'expect_revision="${AGENT_HUB_ACCEPTANCE_EXPECT_REVISION:-}"' in command
    assert 'verify_release=1' in command
    assert "run_release_verification_profile" in command
    assert 'args=(verify-release --install-root "$install_root")' in command
    assert 'args+=(--expect-revision "$expect_revision")' in command
    assert "for ((attempt = 1; attempt <= retries; attempt += 1))" in command
    assert "Authorization: Bearer" in command
    assert "^agent_hub_runs_total( |[{])" in command
    assert "^agent_hub_model_429_total( |[{])" in command
    assert "^agent_hub_queue_depth( |[{])" in command
    assert "^agent_hub_scheduler_lag_seconds( |[{])" in command
    assert "^agent_hub_model_capacity_wait_seconds( |[{])" in command
    assert "^www-authenticate: Bearer" in command
    assert '"/api/v1/runs"' in command
    assert '"/api/v1/runs/$run_id"' in command
    assert '"/api/v1/runs/$run_id/events"' in command
    assert 'check_openapi_path "run pause control" "/api/v1/runs/{run_id}/pause" "post"' in command
    assert 'check_openapi_path "run capability approval" "/api/v1/runs/{run_id}/approve-capability" "post"' in command
    assert 'check_openapi_path "plugin package install" "/api/v1/admin/plugins/install" "post"' in command
    assert 'securitySchemes", {}).get("BearerAuth")' in command
    assert '"$acceptance_python_bin" - "$acceptance_openapi_file" "$path" "$method"' in command
    assert 'if "401" not in responses or "403" not in responses:' in command
    assert '"password_hash"' in command
    assert '"chain_of_thought"' in command
    assert "fail: openapi safe projection" in command
    assert 'check_openapi_schema_safe_projection' in command
    assert '"RunSummaryResponse"' in command
    assert '"RunDetailResponse"' in command
    assert '"checkpoint_state"' in command
    assert '"provider_metadata"' in command
    assert "fail: run detail schema safe projection" in command
    assert "fail: runtime failure diagnostics" in command
    assert '"plugin.timeout"' in command
    assert '"plugin.credential_unavailable"' in command
    assert '"mcp.timeout"' in command
    assert '"mcp.server_failed"' in command
    assert "profile: interaction prevention and last-resort recovery" in command
    assert "fail: interaction prevention and recovery" in command
    assert "ok: interaction prevention and recovery" in command
    assert "RunMonitor" in command
    assert "SelfRepairPolicy" in command
    assert "repair_context_from_proposal" in command
    assert '"model_capacity_pressure"' in command
    assert '"reschedule_or_reassign_model"' in command
    assert '"switch_to_available_model_and_retry"' in command
    assert '"empty_model_response"' in command
    assert '"retry_fallback_or_reassign_model"' in command
    assert '"retry_with_fallback_or_reassign_model"' in command
    assert '"repair.classified"' in command
    assert '"self_repair"' in command
    assert '"requires_approval"' in command
    assert '"automatic_execution"' in command
    assert '"plugin.backend_unavailable"' in command
    assert '"mcp.server_timeout"' in command
    assert "profile: multi-mode interaction matrix" in command
    assert "ok: multi-mode interaction matrix" in command
    assert "fail: multi-mode interaction matrix" in command
    assert 'expected_modes = ("auto", "direct", "dispatch", "discuss", "hybrid")' in command
    assert "_local_main_agent_auto_mode" in command
    assert "_main_agent_adjusted_ready_mode" in command
    assert "_harness_task_requirements" in command
    assert "mode in (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)" in command
    assert "TaskMode.DISPATCH" in command
    assert "TaskMode.DISCUSS" in command
    assert "TaskMode.HYBRID" in command
    assert "TaskMode.DIRECT" in command
    assert "ok: task mode schema" in command
    assert "fail: task mode schema" in command
    assert 'schemas.get("TaskMode", {})' in command
    assert 'check_error_envelope "missing api route envelope" "/api/missing-acceptance-probe" "GET" "404" "not_found"' in command
    assert 'check_error_envelope "method not allowed envelope" "/health/live" "POST" "405" "method_not_allowed"' in command
    assert 'check_openapi_path "run reject capability" "/api/v1/runs/{run_id}/reject-capability" "post"' in command
    assert 'check_openapi_path "run cancel control" "/api/v1/runs/{run_id}/cancel" "post"' in command
    assert 'check_openapi_path "run detail projection" "/api/v1/runs/{run_id}/details" "get"' in command
    assert 'check_openapi_path "config validate" "/api/v1/config/validate" "post"' in command
    assert 'check_openapi_path "config draft create" "/api/v1/config/drafts" "post"' in command
    assert 'check_openapi_path "config draft publish" "/api/v1/config/drafts/{revision_id}/publish" "post"' in command
    assert 'check_openapi_path "config current" "/api/v1/config/current" "get"' in command
    assert 'check_openapi_path "config history" "/api/v1/config/history" "get"' in command
    assert 'check_openapi_path "config version" "/api/v1/config/history/{version}" "get"' in command
    assert 'check_openapi_path "config diff" "/api/v1/config/diff" "get"' in command
    assert 'check_openapi_path "config rollback" "/api/v1/config/history/{version}/rollback" "post"' in command
    assert 'check_openapi_path "user list" "/api/v1/users" "get"' in command
    assert 'check_openapi_path "user create" "/api/v1/users" "post"' in command
    assert 'check_openapi_path "user update" "/api/v1/users/{user_id}" "patch"' in command
    assert 'check_openapi_path "user role" "/api/v1/users/{user_id}/role" "patch"' in command
    assert 'check_openapi_path "user disabled" "/api/v1/users/{user_id}/disabled" "patch"' in command
    assert 'check_openapi_path "user password" "/api/v1/users/{user_id}/password" "patch"' in command
    assert 'check_openapi_path "user delete" "/api/v1/users/{user_id}" "delete"' in command
    assert 'check_openapi_path "workspace file list" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/files" "get"' in command
    assert 'check_openapi_path "workspace file download" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/files/download" "get"' in command
    assert 'check_openapi_path "workspace bundle download" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/bundle/download" "get"' in command
    assert 'check_openapi_path "model routing registry" "/api/v1/admin/models" "get"' in command
    assert 'check_openapi_path "model routing create" "/api/v1/admin/models" "post"' in command
    assert 'check_openapi_path "model routing probe" "/api/v1/admin/models/probe" "post"' in command
    assert 'items != {"$ref": "#/components/schemas/ModelCapability"}' in command
    assert "check_openapi_model_deployment_response_schema" in command
    assert 'schemas.get("ModelDeploymentResponse", {})' in command
    assert '"target_utilization": ("number", 0.1)' in command
    assert '"effective_slots": ("integer", None)' in command
    assert '"saturation_policy": ("string", None)' in command
    assert '"queue_timeout_seconds": ("integer", 1)' in command
    assert 'fallback.get("anyOf")' in command
    assert "check_openapi_model_probe_response_schema" in command
    assert 'schemas.get("ProbeResponse", {})' in command
    assert '"recommended_concurrency": ("integer", None)' in command
    assert '"warning": ("string", None)' in command
    assert "check_openapi_capability_manifest_failure_codes_schema" in command
    assert 'schemas.get("CapabilityManifestItemResponse", {})' in command
    assert 'failure_codes.get("maxItems") != 32' in command
    assert "from agent_hub.runtime.defaults import _capability_inventory_payload" in command
    assert 'expected_codes = ("plugin.timeout", *(f"plugin.failure_{index}" for index in range(31)))' in command
    assert 'check_openapi_path "admin secret create" "/api/v1/admin/secrets" "post"' in command
    assert 'check_openapi_path "admin secret read" "/api/v1/admin/secrets/{ref}" "get"' in command
    assert 'check_openapi_path "admin config draft save" "/api/v1/admin/config/draft" "put"' in command
    assert 'check_openapi_path "admin config draft diff" "/api/v1/admin/config/diff" "post"' in command
    assert 'check_openapi_path "admin config publish" "/api/v1/admin/config/publish" "post"' in command
    assert 'check_openapi_path "admin config rollback" "/api/v1/admin/config/rollback/{version}" "post"' in command
    assert 'check_openapi_path "admin agents list" "/api/v1/admin/agents" "get"' in command
    assert 'check_openapi_path "admin agent upsert" "/api/v1/admin/agents" "post"' in command
    assert 'check_openapi_path "admin agent delete" "/api/v1/admin/agents/{agent_id}" "delete"' in command
    assert 'check_openapi_path "admin workflows list" "/api/v1/admin/workflows" "get"' in command
    assert 'check_openapi_path "admin workflow upsert" "/api/v1/admin/workflows" "post"' in command
    assert 'check_openapi_path "admin workflow delete" "/api/v1/admin/workflows/{workflow_id}" "delete"' in command
    assert 'check_openapi_path "admin settings get" "/api/v1/admin/settings" "get"' in command
    assert 'check_openapi_path "admin settings update" "/api/v1/admin/settings" "put"' in command
    assert 'check_openapi_path "admin main agent get" "/api/v1/admin/main-agent" "get"' in command
    assert 'check_openapi_path "admin main agent update" "/api/v1/admin/main-agent" "put"' in command
    assert 'check_openapi_path "admin runs list" "/api/v1/admin/runs" "get"' in command
    assert 'check_openapi_path "admin run detail" "/api/v1/admin/runs/{run_id}" "get"' in command
    assert 'check_openapi_path "admin run artifact download" "/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download" "get"' in command
    assert 'check_openapi_path "admin run debug" "/api/v1/admin/runs/{run_id}/debug" "get"' in command
    assert 'check_openapi_path "admin run pause" "/api/v1/admin/runs/{run_id}/pause" "post"' in command
    assert 'check_openapi_path "admin run resume" "/api/v1/admin/runs/{run_id}/resume" "post"' in command
    assert 'check_openapi_path "admin run cancel" "/api/v1/admin/runs/{run_id}/cancel" "post"' in command
    assert 'check_openapi_path "admin run delete" "/api/v1/admin/runs/{run_id}" "delete"' in command
    assert 'check_openapi_path "admin skills list" "/api/v1/admin/skills" "get"' in command
    assert 'check_openapi_path "admin skill upload" "/api/v1/admin/skills" "post"' in command
    assert 'check_openapi_path "admin skill archive upload" "/api/v1/admin/skills/upload" "post"' in command
    assert 'check_openapi_path "admin skill version activate" "/api/v1/admin/skills/{skill_id}/versions/{version_id}/activate" "post"' in command
    assert 'check_openapi_path "admin skill approve" "/api/v1/admin/skills/{skill_id}/approve" "post"' in command
    assert 'check_openapi_path "admin skill delete" "/api/v1/admin/skills/{skill_id}" "delete"' in command
    assert 'check_openapi_path "plugin capability manifest" "/api/v1/admin/capabilities/manifest" "get"' in command
    assert 'check_openapi_path "plugin registry list" "/api/v1/admin/plugins" "get"' in command
    assert 'check_openapi_path "plugin registry upsert" "/api/v1/admin/plugins" "post"' in command
    assert 'check_openapi_path "plugin adapters" "/api/v1/admin/plugins/adapters" "get"' in command
    assert 'check_openapi_path "plugin policy summary" "/api/v1/admin/plugins/policy-summary" "get"' in command
    assert 'check_openapi_path "plugin policy review" "/api/v1/admin/plugins/policy-review" "post"' in command
    assert 'check_openapi_path "plugin signing key list" "/api/v1/admin/plugins/signing-keys" "get"' in command
    assert 'check_openapi_path "plugin signing key upsert" "/api/v1/admin/plugins/signing-keys" "post"' in command
    assert 'check_openapi_path "plugin signing key delete" "/api/v1/admin/plugins/signing-keys/{key_id}" "delete"' in command
    assert 'check_openapi_path "plugin package rejection" "/api/v1/admin/plugins/{plugin_id}/package/reject" "post"' in command
    assert 'check_openapi_path "plugin lifecycle start" "/api/v1/admin/plugins/{plugin_id}/start" "post"' in command
    assert 'check_openapi_path "plugin lifecycle enable" "/api/v1/admin/plugins/{plugin_id}/enable" "post"' in command
    assert 'check_openapi_path "plugin lifecycle disable" "/api/v1/admin/plugins/{plugin_id}/disable" "post"' in command
    assert 'check_openapi_path "plugin lifecycle stop" "/api/v1/admin/plugins/{plugin_id}/stop" "post"' in command
    assert 'check_openapi_path "plugin lifecycle reload" "/api/v1/admin/plugins/{plugin_id}/reload" "post"' in command
    assert 'check_openapi_path "plugin uninstall" "/api/v1/admin/plugins/{plugin_id}/uninstall" "post"' in command
    assert 'check_openapi_path "plugin delete" "/api/v1/admin/plugins/{plugin_id}" "delete"' in command
    assert 'check_openapi_path "mcp server registry" "/api/v1/admin/mcp" "get"' in command
    assert 'check_openapi_path "mcp server upsert" "/api/v1/admin/mcp" "post"' in command
    assert 'check_protected_boundary "run read requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000"' in command
    assert 'check_protected_boundary "run events requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/events"' in command
    assert 'check_protected_boundary "run details requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/details"' in command
    assert 'check_protected_boundary "run artifact download requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/artifacts/00000000-0000-0000-0000-000000000000/download"' in command
    assert 'check_protected_boundary "workspace file list requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/files"' in command
    assert 'check_protected_boundary "workspace file download requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/files/download?path=artifact.txt"' in command
    assert 'check_protected_boundary "workspace bundle download requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/bundle/download"' in command
    assert 'check_protected_boundary "model registry requires bearer" "/api/v1/admin/models"' in command
    assert 'check_write_protected_boundary "model create requires bearer" "/api/v1/admin/models" "POST"' in command
    assert 'check_write_protected_boundary "model probe requires bearer" "/api/v1/admin/models/probe" "POST"' in command
    assert 'check_protected_boundary "admin secret read requires bearer" "/api/v1/admin/secrets/probe"' in command
    assert 'check_protected_boundary "admin agents list requires bearer" "/api/v1/admin/agents"' in command
    assert 'check_protected_boundary "admin workflows list requires bearer" "/api/v1/admin/workflows"' in command
    assert 'check_protected_boundary "admin settings get requires bearer" "/api/v1/admin/settings"' in command
    assert 'check_protected_boundary "admin main agent get requires bearer" "/api/v1/admin/main-agent"' in command
    assert 'check_protected_boundary "admin runs list requires bearer" "/api/v1/admin/runs"' in command
    assert 'check_protected_boundary "admin run detail requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000"' in command
    assert 'check_protected_boundary "admin run artifact download requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000/artifacts/00000000-0000-0000-0000-000000000000/download"' in command
    assert 'check_protected_boundary "admin run debug requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000/debug"' in command
    assert 'check_protected_boundary "admin skills list requires bearer" "/api/v1/admin/skills"' in command
    assert 'check_protected_boundary "plugin adapters require bearer" "/api/v1/admin/plugins/adapters"' in command
    assert 'check_protected_boundary "plugin registry list requires bearer" "/api/v1/admin/plugins"' in command
    assert 'check_write_protected_boundary "plugin registry upsert requires bearer" "/api/v1/admin/plugins" "POST"' in command
    assert 'check_protected_boundary "plugin policy summary requires bearer" "/api/v1/admin/plugins/policy-summary"' in command
    assert 'check_write_protected_boundary "plugin policy review requires bearer" "/api/v1/admin/plugins/policy-review" "POST"' in command
    assert 'check_protected_boundary "plugin signing key list requires bearer" "/api/v1/admin/plugins/signing-keys"' in command
    assert 'check_write_protected_boundary "plugin signing key upsert requires bearer" "/api/v1/admin/plugins/signing-keys" "POST"' in command
    assert 'check_write_protected_boundary "plugin signing key delete requires bearer" "/api/v1/admin/plugins/signing-keys/probe-key" "DELETE"' in command
    assert 'check_write_protected_boundary "plugin install requires bearer" "/api/v1/admin/plugins/install" "POST"' in command
    assert 'check_write_protected_boundary "plugin package approval requires bearer" "/api/v1/admin/plugins/probe/package/approve" "POST"' in command
    assert 'check_write_protected_boundary "plugin package rejection requires bearer" "/api/v1/admin/plugins/probe/package/reject" "POST"' in command
    assert 'check_write_protected_boundary "plugin lifecycle start requires bearer" "/api/v1/admin/plugins/probe/start" "POST"' in command
    assert 'check_write_protected_boundary "plugin lifecycle enable requires bearer" "/api/v1/admin/plugins/probe/enable" "POST"' in command
    assert 'check_write_protected_boundary "plugin lifecycle disable requires bearer" "/api/v1/admin/plugins/probe/disable" "POST"' in command
    assert 'check_write_protected_boundary "plugin lifecycle stop requires bearer" "/api/v1/admin/plugins/probe/stop" "POST"' in command
    assert 'check_write_protected_boundary "plugin lifecycle reload requires bearer" "/api/v1/admin/plugins/probe/reload" "POST"' in command
    assert 'check_write_protected_boundary "plugin uninstall requires bearer" "/api/v1/admin/plugins/probe/uninstall" "POST"' in command
    assert 'check_write_protected_boundary "plugin delete requires bearer" "/api/v1/admin/plugins/probe" "DELETE"' in command
    assert 'check_protected_boundary "plugin capability manifest requires bearer" "/api/v1/admin/capabilities/manifest"' in command
    assert 'check_protected_boundary "mcp registry requires bearer" "/api/v1/admin/mcp"' in command
    assert 'check_write_protected_boundary "mcp upsert requires bearer" "/api/v1/admin/mcp" "POST"' in command
    assert '-X "$method"' in command
    assert 'check_write_protected_boundary "run create requires bearer" "/api/v1/runs" "POST"' in command
    assert 'check_write_protected_boundary "run pause requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/pause" "POST"' in command
    assert 'check_write_protected_boundary "run resume requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/resume" "POST"' in command
    assert 'check_write_protected_boundary "run cancel requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/cancel" "POST"' in command
    assert 'check_write_protected_boundary "run capability approve requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/approve-capability" "POST"' in command
    assert 'check_write_protected_boundary "run capability reject requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/reject-capability" "POST"' in command
    assert 'check_protected_boundary "auth me requires bearer" "/api/v1/auth/me"' in command
    assert 'check_protected_boundary "config current requires bearer" "/api/v1/config/current"' in command
    assert 'check_protected_boundary "config history requires bearer" "/api/v1/config/history"' in command
    assert 'check_protected_boundary "config version requires bearer" "/api/v1/config/history/1"' in command
    assert 'check_protected_boundary "config diff requires bearer" "/api/v1/config/diff?from_version=1&to_version=2"' in command
    assert 'check_write_protected_boundary "config publish requires bearer" "/api/v1/config/drafts/00000000-0000-0000-0000-000000000000/publish" "POST"' in command
    assert 'check_write_protected_boundary "config rollback requires bearer" "/api/v1/config/history/1/rollback" "POST"' in command
    assert 'check_protected_boundary "user list requires bearer" "/api/v1/users"' in command
    assert 'check_write_protected_boundary "user delete requires bearer" "/api/v1/users/00000000-0000-0000-0000-000000000000" "DELETE"' in command
    assert "skip: run lifecycle probe requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN" in command


def test_native_installer_deploys_release_before_starting_services() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "deploy_native_release" in script
    assert "normalize_native_release_line_endings" in script
    assert "ln -sfn" in script
    assert '"$INSTALL_ROOT/current"' in script
    assert "uv sync --frozen --no-dev" in script

    deploy = script.index("deploy_native_release")
    start = script.index("systemctl enable --now agent-hub.target")
    assert deploy < start


def test_auto_mode_prefers_native_on_supported_systemd_hosts() -> None:
    detect = read("scripts/lib/detect.sh")
    install = read("install.sh")
    readme = read("README.md")
    installation = read("docs/installation.md")

    assert (
        'if [[ "$HAS_SYSTEMD" -eq 1 && "$HOST_MANAGER" != "unknown" ]]; then\n'
        '      MODE="native"'
    ) in detect
    assert 'elif [[ "$HAS_DOCKER" -eq 1 || "$HOST_MANAGER" == "unknown" ]]; then' in detect
    assert "chooses native on supported systemd apt/dnf hosts" in install
    assert "prefers native mode" in readme
    assert "Native mode when systemd plus apt/dnf support are detected" in installation


def test_native_installer_prunes_old_releases_after_successful_deploy() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "prune_native_releases" in script
    assert "AGENT_HUB_RELEASES_TO_KEEP" in script
    assert "readlink -f \"$INSTALL_ROOT/current\"" in script
    assert 'resolved_release="$(readlink -f "$release"' in script
    assert 'case "$resolved_release" in' in script
    assert '"$INSTALL_ROOT/releases/"*)' in script
    assert 'rm -rf -- "$resolved_release"' in script

    link_current = script.index('ln -sfn "$release" "$INSTALL_ROOT/current"')
    prune = script.index("prune_native_releases")
    assert link_current < prune


def test_installer_failure_output_includes_context_and_hints() -> None:
    common = read("scripts/lib/common.sh")
    install = read("install.sh")

    assert 'installer_failed "$LINENO" "$?" "$BASH_COMMAND"' in install
    assert "Failed stage:" in common
    assert "Failed command:" in common
    assert "Common causes and checks:" in common
    assert "journalctl -u agent-hub-api" in common
    assert "systemctl status caddy" in common
    assert "curl -v http://127.0.0.1:8000/health/ready" in common


def test_installer_preflights_required_support_files_before_sourcing() -> None:
    install = read("install.sh")
    common = read("scripts/lib/common.sh")
    docker = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_SOURCE_DIR" in install
    assert "AGENT_HUB_SOURCE_DIR" in common
    assert "AGENT_HUB_SOURCE_DIR" in docker
    assert "normalize_installer_tree" in install
    assert "sed -i 's/\\r$//'" in install
    assert "chmod 0755" in install
    assert "require_installer_files" in install
    assert "installation package is incomplete" in install
    assert install.index("require_installer_files") < install.index(
        'source "$SCRIPT_DIR/scripts/lib/common.sh"'
    )
    assert '"$SCRIPT_DIR/scripts/lib/install_docker.sh"' in install
    assert '"$SCRIPT_DIR/scripts/lib/install_native.sh"' in install
    assert 'bash "$AGENT_HUB_SOURCE_DIR/scripts/agent-hub" doctor' in common
    assert 'cp -R "$AGENT_HUB_SOURCE_DIR/deploy/compose"' in docker


def test_readme_archive_install_extracts_into_isolated_source_directory() -> None:
    readme = read("README.md")

    assert "mktemp -d /tmp/agent-hub-install" in readme
    assert 'mkdir -p "$tmp/source"' in readme
    assert 'tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp/source"' in readme
    assert 'cd "$tmp/source"' in readme
    assert "Do not extract the archive directly into `/root`" in readme


def test_install_verification_uses_public_url_for_docker_mode() -> None:
    verify = read("scripts/lib/verify.sh")

    assert "installation_health_base_url" in verify
    assert '[[ "${MODE:-}" == "docker" ]]' in verify
    assert "AGENT_HUB_PUBLIC_URL" in verify
    assert "verify_native_service agent-hub-api.service" in verify
    assert "verify_native_service agent-hub-worker.service" in verify
    assert "verify_native_service agent-hub-litellm.service" in verify
    assert "verify_native_litellm_proxy" in verify
    assert ".litellm-venv/bin/litellm" in verify
    assert "litellm.proxy.proxy_server" in verify
    assert 'verify_url "$base_url/health/live"' in verify
    assert 'verify_url "$base_url/health/ready"' in verify


def test_native_installer_creates_runtime_dirs_and_migrates_before_services() -> None:
    script = read("scripts/lib/install_native.sh")

    tmpfiles = script.index("systemd-tmpfiles --create")
    database = script.index("configure_native_database")
    migrations = script.index("alembic upgrade head")
    start = script.index("systemctl enable --now agent-hub.target")

    assert tmpfiles < start
    assert database < migrations
    assert migrations < start


def test_native_installer_fails_fast_when_core_services_do_not_become_active() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "require_native_service_active" in script
    assert "systemctl status \"$unit\" --no-pager -l" in script
    assert "journalctl -u \"$unit\" -n 120 --no-pager" in script
    assert "did not become active after install" in script
    assert "require_native_service_active caddy.service" in script
    assert "require_native_service_active agent-hub-api.service" in script
    assert "require_native_service_active agent-hub-worker.service" in script
    assert "require_native_service_active agent-hub-litellm.service" in script
    start = script.index("systemctl enable --now agent-hub.target")
    litellm_check = script.index("require_native_service_active agent-hub-litellm.service")
    mark = script.index('mark_stage "native-up"')
    assert start < litellm_check < mark


def test_native_installer_starts_local_dependencies_and_writes_runtime_urls() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_DATABASE_URL=" in secrets
    assert "AGENT_HUB_REDIS_URL=" in secrets
    assert "\nDATABASE_URL=" not in f"\n{secrets}"
    assert "\nREDIS_URL=" not in f"\n{secrets}"
    assert "sanitize_legacy_secrets" in secrets
    assert "DATABASE_URL|REDIS_URL|JWT_SIGNING_KEY|AGENT_HUB_SECRET_KEY" in secrets
    assert 'database_url="$(native_secret_value AGENT_HUB_DATABASE_URL)"' in script
    assert "systemctl enable --now postgresql" in script
    assert "systemctl enable --now redis" in script
    assert "createdb" in script


def test_native_database_bootstrap_avoids_psql_variable_identifier_interpolation() -> None:
    script = read("scripts/lib/install_native.sh")

    assert ':"role"' not in script
    assert ":'password'" not in script
    assert "sql_literal" in script
    assert 'CREATE ROLE \\"${postgres_user}\\" LOGIN PASSWORD' in script
    assert 'ALTER ROLE \\"${postgres_user}\\" WITH LOGIN PASSWORD' in script


def test_native_installer_normalizes_release_and_systemd_line_endings() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "normalize_native_release_line_endings" in script
    assert "normalize_native_systemd_units" in script
    assert "sed -i 's/\\r$//'" in script
    assert "chmod 0755" in script
    assert "-name '*.sh'" in script
    assert "-name '*.service'" in script


def test_installer_defaults_management_url_to_external_address() -> None:
    secrets = read("scripts/lib/secrets.sh")
    installer = read("scripts/lib/install_native.sh")

    assert "detect_public_url" in secrets
    assert "api.ipify.org" in secrets
    assert "hostname -I" in secrets
    assert "is_private_ipv4" in secrets
    assert "private or loopback address" in secrets
    assert "prompt_public_url" in secrets
    assert "Enter Agent Hub external access URL, including forwarded public port when needed" in secrets
    assert "unable to detect a public Agent Hub URL" in secrets
    assert "ensure_public_url_secret" in secrets
    assert "AGENT_HUB_PUBLIC_URL must be externally reachable" in secrets
    assert "AGENT_HUB_PUBLIC_URL must use a public address" in secrets
    assert "printf 'http://127.0.0.1\\n'" not in secrets
    assert '${AGENT_HUB_PUBLIC_URL:-http://127.0.0.1}' not in installer
    assert "detect_public_url" in installer


def test_native_api_stays_private_and_caddy_exposes_management_ui() -> None:
    api_unit = read("deploy/native/systemd/agent-hub-api.service")
    caddyfile = read("deploy/native/Caddyfile")
    installer = read("scripts/lib/install_native.sh")

    assert "--host ${AGENT_HUB_API_BIND_HOST:-127.0.0.1}" in api_unit
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "AGENT_HUB_WEB_DIR=/opt/agent-hub/current/web/dist" in api_unit
    assert "http://*)\n      printf ':80" in installer
    assert "hostport=\"${public_url#http://}\"" not in installer
    assert "handle /setup*" in caddyfile
    assert "handle /setup*" in installer
    assert "handle /health {" in caddyfile
    assert "handle /health {" in installer
    assert "handle /openapi.json" in caddyfile
    assert "handle /openapi.json" in installer
    assert "fix_native_web_permissions" in installer
    assert 'chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"' in installer
    assert 'chmod 0755 "$release"' in installer
    assert 'chmod 0755 "$release/web"' in installer
    assert 'chmod 0755 "$release/web/dist"' in installer
    assert 'chmod -R a+rX "$release/web/dist"' in installer
    assert 'chmod -R u+rwX,g+rX,o-rwx "$release/.venv"' in installer
    assert 'chmod -R u+rwX,g+rX,o-rwx "$release/.litellm-venv"' in installer
    assert "fix_native_uv_permissions" in installer
    assert 'chmod -R a+rX "$python_dir"' in installer
    assert "chown -R agent-hub:agent-hub" in installer
    assert "systemctl reload-or-restart caddy" in installer


def test_native_systemd_units_do_not_use_stale_console_script_shebangs() -> None:
    api_unit = read("deploy/native/systemd/agent-hub-api.service")
    litellm_unit = read("deploy/native/systemd/agent-hub-litellm.service")

    assert "/opt/agent-hub/current/.venv/bin/python -m uvicorn" in api_unit
    assert "/opt/agent-hub/current/.venv/bin/uvicorn" not in api_unit
    assert "/opt/agent-hub/current/.litellm-venv/bin/python" in litellm_unit
    assert "from litellm import run_server" in litellm_unit
    assert "/opt/agent-hub/current/.litellm-venv/bin/litellm" not in litellm_unit


def test_doctor_diagnoses_web_asset_permission_failures() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "web ui assets readable by Caddy" in doctor
    assert "Caddy cannot read Web UI asset" in doctor
    assert "namei -l" in doctor
    assert "chmod -R a+rX" in doctor


def test_doctor_accepts_caddy_owned_public_ports_after_native_install() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "port_available_or_expected_proxy" in doctor
    assert "systemd_unit_active caddy.service" in doctor
    assert "port 80 free or served by Caddy" in doctor
    assert "port 443 free or served by Caddy" in doctor


def test_doctor_diagnoses_runtime_services_and_litellm_proxy_environment() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "systemd_unit_active_if_present" in doctor
    assert "api systemd service active when installed" in doctor
    assert "worker systemd service active when installed" in doctor
    assert "litellm systemd service active when installed" in doctor
    assert "native LiteLLM proxy environment" in doctor
    assert ".litellm-venv/bin/litellm" in doctor
    assert "litellm.proxy.proxy_server" in doctor
    assert "journalctl -u agent-hub-litellm" in doctor
    assert "model gateway failed" in doctor


def test_doctor_does_not_require_docker_when_native_stack_is_installed() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "docker_available_or_native_installed" in doctor
    assert "native_stack_installed" in doctor
    assert "docker available for docker install path" in doctor


def test_native_caddy_supports_user_supplied_tls_certificate() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_TLS_CERT_FILE" in secrets
    assert "AGENT_HUB_TLS_KEY_FILE" in secrets
    assert "tls $cert_file $key_file" in script


def test_native_install_packages_installs_uv_runtime_dependencies() -> None:
    packages = read("deploy/native/install-packages.sh")
    installer = read("scripts/lib/install_native.sh")

    assert "AGENT_HUB_SOURCE_DIR" in installer
    assert 'bash "$AGENT_HUB_SOURCE_DIR/deploy/native/install-packages.sh"' in installer
    assert '"$SCRIPT_DIR/deploy/native/install-packages.sh"' not in installer
    assert "python3-venv" in packages
    assert "nodejs" in packages
    assert "npm" in packages
    assert "uv python install 3.12" in installer
    assert "uv venv --python" in installer
    assert "UV_PYTHON_INSTALL_DIR" in installer
    assert "${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" in installer
    assert "native_uv_env uv python install 3.12" in installer
    assert "native_uv_env uv python find 3.12" in installer


def test_native_installer_falls_back_to_china_mirrors_when_official_sources_fail() -> None:
    packages = read("deploy/native/install-packages.sh")
    installer = read("scripts/lib/install_native.sh")
    docker = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_MIRROR_MODE" in packages
    assert "configure_china_package_mirror" in packages
    assert "install_with_mirror_fallback" in packages
    assert "pypi.tuna.tsinghua.edu.cn" in installer
    assert "UV_DEFAULT_INDEX" in installer
    assert "registry.npmmirror.com" in installer
    assert "docker.io" in docker
    assert "registry.cn-hangzhou.aliyuncs.com" in docker


def test_native_installer_uses_mirror_install_without_locked_wheel_urls_in_china_mode() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "sync_python_project_with_lock_or_mirror" in script
    assert "install_litellm_proxy_venv" in script
    assert 'if [[ "$mode" == "china" ]]; then\n    install_python_project_from_mirror "$mirror"\n    return\n  fi' in script
    assert "locked uv sync is skipped in China mirror mode" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "uv pip install \\\n    --python .litellm-venv/bin/python" in script
    assert "--index-url" in script


def test_native_installer_falls_back_from_locked_uv_sync_to_mirror_pip_install() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "sync_python_project_with_lock_or_mirror" in script
    assert "run_with_timeout" in script
    assert "${AGENT_HUB_UV_SYNC_TIMEOUT_SECONDS:-900}" in script
    assert "uv sync --frozen --no-dev" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "verify_litellm_proxy_venv" in script
    assert "litellm.proxy.proxy_server" in script
    assert "--index-url" in script
    assert "official locked uv sync failed" in script
