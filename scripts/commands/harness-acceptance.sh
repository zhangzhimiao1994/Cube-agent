#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${AGENT_HUB_ACCEPTANCE_BASE_URL:-http://127.0.0.1:8000}"
profile="all"
stress=0
strict_interaction_recovery="${AGENT_HUB_ACCEPTANCE_STRICT_INTERACTION_RECOVERY:-0}"
read_only=0
concurrency="${AGENT_HUB_ACCEPTANCE_CONCURRENCY:-4}"
iterations="${AGENT_HUB_ACCEPTANCE_ITERATIONS:-10}"
connect_timeout="${AGENT_HUB_ACCEPTANCE_CONNECT_TIMEOUT_SECONDS:-5}"
max_time="${AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS:-20}"
retries="${AGENT_HUB_ACCEPTANCE_RETRIES:-3}"
retry_delay="${AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS:-2}"
bearer_token="${AGENT_HUB_ACCEPTANCE_BEARER_TOKEN:-}"
run_message="${AGENT_HUB_ACCEPTANCE_RUN_MESSAGE:-Agent Hub harness acceptance run lifecycle probe}"
verify_release=0
install_root="${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"
expect_revision="${AGENT_HUB_ACCEPTANCE_EXPECT_REVISION:-}"
failures=0

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub harness-acceptance [options]

Run non-destructive real-machine acceptance checks for Codex-style harness
stability and DeepSeek-style pluggable harness goals.

Set AGENT_HUB_ACCEPTANCE_BEARER_TOKEN to also run an authenticated
create/read/events lifecycle probe against /api/v1/runs.

Options:
  --base-url URL                 Base URL to test.
  --profile codex|deepseek|all|production-safe
                                 Acceptance profile to run. production-safe runs all profiles in read-only mode.
  --read-only                    Skip runtime write probes; keep GET probes, OpenAPI contracts, and stress.
  --strict-interaction-recovery  Run authenticated, non-mutating interaction recovery probes; requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN.
  --stress                       Run bounded HTTP stress checks.
  --concurrency N                Stress workers. Defaults to AGENT_HUB_ACCEPTANCE_CONCURRENCY or 4.
  --iterations N                 Requests per worker. Defaults to AGENT_HUB_ACCEPTANCE_ITERATIONS or 10.
  --connect-timeout SECONDS      Curl connect timeout. Defaults to 5.
  --max-time SECONDS             Curl total request timeout. Defaults to 20.
  --retries N                    Attempts per smoke URL. Defaults to AGENT_HUB_ACCEPTANCE_RETRIES or 3.
  --retry-delay SECONDS          Delay between URL attempts. Defaults to AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS or 2.
  --verify-release               Also verify native current release pointer, REVISION, and service state.
  --install-root DIR             Native install root for --verify-release. Defaults to AGENT_HUB_INSTALL_ROOT or /opt/agent-hub.
  --expect-revision SHA          Require current release REVISION to match SHA. Also enables --verify-release.
  --help                         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      base_url="${2:?missing value for --base-url}"
      shift 2
      ;;
    --profile)
      profile="${2:?missing value for --profile}"
      shift 2
      ;;
    --stress)
      stress=1
      shift
      ;;
    --strict-interaction-recovery)
      strict_interaction_recovery=1
      shift
      ;;
    --read-only)
      read_only=1
      shift
      ;;
    --concurrency)
      concurrency="${2:?missing value for --concurrency}"
      shift 2
      ;;
    --iterations)
      iterations="${2:?missing value for --iterations}"
      shift 2
      ;;
    --connect-timeout)
      connect_timeout="${2:?missing value for --connect-timeout}"
      shift 2
      ;;
    --max-time)
      max_time="${2:?missing value for --max-time}"
      shift 2
      ;;
    --retries)
      retries="${2:?missing value for --retries}"
      shift 2
      ;;
    --retry-delay)
      retry_delay="${2:?missing value for --retry-delay}"
      shift 2
      ;;
    --verify-release)
      verify_release=1
      shift
      ;;
    --install-root)
      install_root="${2:?missing value for --install-root}"
      shift 2
      ;;
    --expect-revision)
      expect_revision="${2:?missing revision}"
      verify_release=1
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$profile" in
  codex|deepseek|all|production-safe) ;;
  *)
    printf 'invalid --profile: %s\n' "$profile" >&2
    exit 2
    ;;
esac

case "$profile" in
  production-safe) profile="all"; read_only=1 ;;
esac

positive_int() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

if ! positive_int "$concurrency" || ! positive_int "$iterations" || ! positive_int "$retries"; then
  printf 'concurrency, iterations, and retries must be positive integers\n' >&2
  exit 2
fi

case "$strict_interaction_recovery" in
  0|1) ;;
  *)
    printf 'AGENT_HUB_ACCEPTANCE_STRICT_INTERACTION_RECOVERY must be 0 or 1\n' >&2
    exit 2
    ;;
esac

require_curl() {
  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required for harness acceptance checks\n' >&2
    exit 2
  fi
}

detect_python() {
  if [[ -n "${AGENT_HUB_ACCEPTANCE_PYTHON:-}" ]]; then
    printf '%s\n' "$AGENT_HUB_ACCEPTANCE_PYTHON"
    return 0
  fi
  local script_dir
  local source_dir
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if [[ -x "$source_dir/.venv/bin/python" ]]; then
    printf '%s\n' "$source_dir/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

check_url() {
  local name="$1"
  local path="$2"
  local expected="${3:-200}"
  local attempt
  local status
  for ((attempt = 1; attempt <= retries; attempt += 1)); do
    if status="$(curl --noproxy '*' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -fsS -o /dev/null -w '%{http_code}' \
      "$base_url$path" 2>/dev/null)" && [[ "$status" == "$expected" ]]; then
      printf 'ok: %s %s -> %s\n' "$name" "$path" "$status"
      return 0
    fi
    if [[ "$attempt" -lt "$retries" ]]; then
      sleep "$retry_delay"
    fi
  done
  printf 'fail: %s %s -> %s\n' "$name" "$path" "${status:-curl-error}"
  failures=$((failures + 1))
  return 1
}

check_health_json() {
  local name="$1"
  local path="$2"
  local python_bin
  local response
  if ! python_bin="$(detect_python)"; then
    printf 'fail: %s %s requires python for JSON handling\n' "$name" "$path" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    "$base_url$path" 2>/dev/null)"; then
    printf 'fail: %s %s -> curl-error\n' "$name" "$path" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$response" "$python_bin" -c 'import json, os, sys; sys.exit(0 if json.loads(os.environ["ACCEPTANCE_RESPONSE"]).get("status") == "ok" else 1)' 2>/dev/null; then
    printf 'ok: %s %s JSON status=ok\n' "$name" "$path"
    return 0
  fi
  printf 'fail: %s %s JSON status!=ok\n' "$name" "$path" >&2
  failures=$((failures + 1))
  return 1
}

check_prometheus_metrics() {
  local response_headers
  local metrics_path="/metrics"
  local temp_file
  temp_file="$(mktemp)"
  if ! response_headers="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS -D - -o "$temp_file" \
    "$base_url$metrics_path" 2>/dev/null)"; then
    rm -f -- "$temp_file"
    printf 'fail: prometheus metrics %s -> curl-error\n' "$metrics_path" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! printf '%s\n' "$response_headers" | grep -qi '^content-type: text/plain'; then
    rm -f -- "$temp_file"
    printf 'fail: prometheus metrics %s content-type is not text/plain\n' "$metrics_path" >&2
    failures=$((failures + 1))
    return 1
  fi
  if grep -q '^# TYPE agent_hub_runs_total counter' "$temp_file" \
    && grep -Eq '^agent_hub_runs_total( |[{])' "$temp_file" \
    && grep -q '^# TYPE agent_hub_model_429_total counter' "$temp_file" \
    && grep -Eq '^agent_hub_model_429_total( |[{])' "$temp_file" \
    && grep -q '^# TYPE agent_hub_queue_depth gauge' "$temp_file" \
    && grep -Eq '^agent_hub_queue_depth( |[{])' "$temp_file" \
    && grep -q '^# TYPE agent_hub_scheduler_lag_seconds gauge' "$temp_file" \
    && grep -Eq '^agent_hub_scheduler_lag_seconds( |[{])' "$temp_file" \
    && grep -q '^# TYPE agent_hub_model_capacity_wait_seconds gauge' "$temp_file" \
    && grep -Eq '^agent_hub_model_capacity_wait_seconds( |[{])' "$temp_file"; then
    rm -f -- "$temp_file"
    printf 'ok: prometheus metrics %s\n' "$metrics_path"
    return 0
  fi
  rm -f -- "$temp_file"
  printf 'fail: prometheus metrics %s missing core agent_hub metrics\n' "$metrics_path" >&2
  failures=$((failures + 1))
  return 1
}

check_protected_boundary() {
  local name="$1"
  local path="$2"
  local method="${3:-GET}"
  local body="${4:-{}}"
  local status
  local temp_headers
  temp_headers="$(mktemp)"
  local curl_args=(
    --noproxy '*'
    --connect-timeout "$connect_timeout"
    --max-time "$max_time"
    -sS
    -X "$method"
    -D "$temp_headers"
    -o /dev/null
    -w '%{http_code}'
  )
  if [[ "$method" != "GET" ]]; then
    curl_args+=(-H 'content-type: application/json' --data "$body")
  fi
  if status="$(curl "${curl_args[@]}" "$base_url$path" 2>/dev/null)" && [[ "$status" == "401" ]]; then
    if grep -qi '^www-authenticate: Bearer' "$temp_headers"; then
      rm -f -- "$temp_headers"
      printf 'ok: %s %s %s -> %s\n' "$name" "$method" "$path" "$status"
      return 0
    fi
    rm -f -- "$temp_headers"
    printf 'fail: %s %s %s expected WWW-Authenticate: Bearer\n' "$name" "$method" "$path" >&2
    failures=$((failures + 1))
    return 1
  fi
  rm -f -- "$temp_headers"
  printf 'fail: %s %s %s expected 401 -> %s\n' "$name" "$method" "$path" "${status:-curl-error}" >&2
  failures=$((failures + 1))
  return 1
}

check_write_protected_boundary() {
  local name="$1"
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: %s disabled by read-only mode\n' "$name"
    return 0
  fi
  check_protected_boundary "$@"
}

check_error_envelope() {
  local name="$1"
  local path="$2"
  local method="$3"
  local expected_status="$4"
  local expected_code="$5"
  local python_bin
  local response
  local status
  local temp_body
  temp_body="$(mktemp)"
  if ! python_bin="$(detect_python)"; then
    rm -f -- "$temp_body"
    printf 'fail: %s %s requires python for JSON handling\n' "$name" "$path" >&2
    failures=$((failures + 1))
    return 1
  fi
  status="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -sS -X "$method" -o "$temp_body" -w '%{http_code}' \
    "$base_url$path" 2>/dev/null || true)"
  response="$(cat "$temp_body")"
  rm -f -- "$temp_body"
  if [[ "$status" != "$expected_status" ]]; then
    printf 'fail: %s %s expected %s -> %s\n' "$name" "$path" "$expected_status" "${status:-curl-error}" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$response" ACCEPTANCE_ERROR_CODE="$expected_code" "$python_bin" - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
error = payload.get("error", {})
if error.get("code") != os.environ["ACCEPTANCE_ERROR_CODE"]:
    raise SystemExit(1)
message = error.get("message")
if not isinstance(message, str) or not message:
    raise SystemExit(1)
PY
  then
    printf 'ok: %s %s -> %s\n' "$name" "$path" "$status"
    return 0
  fi
  printf 'fail: %s %s invalid error envelope\n' "$name" "$path" >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_model_capability_schema() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
schemas = document.get("components", {}).get("schemas", {})
capability_schema = schemas.get("ModelCapability", {})
expected = [
    "text",
    "vision",
    "audio",
    "tool_calling",
    "structured_output",
    "image_generation",
    "video_generation",
    "audio_generation",
]
if capability_schema.get("enum") != expected:
    raise SystemExit(1)
request_schema = schemas.get("ModelDeploymentRequest", {})
items = request_schema.get("properties", {}).get("capabilities", {}).get("items")
if items != {"$ref": "#/components/schemas/ModelCapability"}:
    raise SystemExit(1)
PY
  then
    printf 'ok: model capability enum schema\n'
    return 0
  fi
  printf 'fail: model capability enum schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_model_deployment_response_schema() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
schemas = document.get("components", {}).get("schemas", {})
schema = schemas.get("ModelDeploymentResponse", {})
properties = schema.get("properties", {})
expected = {
    "id": ("string", None),
    "logical_model": ("string", None),
    "provider": ("string", None),
    "upstream_model": ("string", None),
    "credential_ref": ("string", None),
    "capabilities": ("array", None),
    "max_concurrency": ("integer", 1),
    "target_utilization": ("number", 0.1),
    "reserved_capacity": ("integer", 0),
    "effective_slots": ("integer", None),
    "queue_timeout_seconds": ("integer", 1),
    "saturation_policy": ("string", None),
}
for name, (expected_type, minimum) in expected.items():
    prop = properties.get(name)
    if not isinstance(prop, dict):
        raise SystemExit(1)
    if prop.get("type") != expected_type:
        raise SystemExit(1)
    if minimum is not None and prop.get("minimum") != minimum:
        raise SystemExit(1)
capabilities = properties.get("capabilities", {})
items = capabilities.get("items")
if items != {"$ref": "#/components/schemas/ModelCapability"}:
    raise SystemExit(1)
fallback = properties.get("fallback")
if not isinstance(fallback, dict):
    raise SystemExit(1)
if fallback.get("anyOf") != [{"type": "string", "maxLength": 128}, {"type": "null"}]:
    raise SystemExit(1)
PY
  then
    printf 'ok: model deployment response schema\n'
    return 0
  fi
  printf 'fail: model deployment response schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_model_probe_response_schema() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
schemas = document.get("components", {}).get("schemas", {})
schema = schemas.get("ProbeResponse", {})
properties = schema.get("properties", {})
expected = {
    "recommended_concurrency": ("integer", None),
    "warning": ("string", None),
}
for name, (expected_type, minimum) in expected.items():
    prop = properties.get(name)
    if not isinstance(prop, dict):
        raise SystemExit(1)
    if prop.get("type") != expected_type:
        raise SystemExit(1)
    if minimum is not None and prop.get("minimum") != minimum:
        raise SystemExit(1)
PY
  then
    printf 'ok: model probe response schema\n'
    return 0
  fi
  printf 'fail: model probe response schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_task_mode_schema() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

expected_modes = ("auto", "direct", "dispatch", "discuss", "hybrid")
with open(sys.argv[1], encoding="utf-8") as handle:
    spec = json.load(handle)
schemas = spec.get("components", {}).get("schemas", {})
mode_schema = schemas.get("TaskMode", {})
if tuple(mode_schema.get("enum", ())) != expected_modes:
    raise SystemExit(1)

create_mode = (
    schemas.get("CreateRunRequest", {})
    .get("properties", {})
    .get("mode", {})
)
if create_mode.get("default") != "auto":
    raise SystemExit(1)
if create_mode.get("$ref") != "#/components/schemas/TaskMode":
    all_of = create_mode.get("allOf")
    if all_of != [{"$ref": "#/components/schemas/TaskMode"}]:
        raise SystemExit(1)

choose_mode = (
    schemas.get("ChooseModeRequest", {})
    .get("properties", {})
    .get("mode", {})
)
if choose_mode.get("$ref") != "#/components/schemas/TaskMode":
    all_of = choose_mode.get("allOf")
    if all_of != [{"$ref": "#/components/schemas/TaskMode"}]:
        raise SystemExit(1)
PY
  then
    printf 'ok: task mode schema\n'
    return 0
  fi
  printf 'fail: task mode schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_run_create_idempotency_header() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
operation = document.get("paths", {}).get("/api/v1/runs", {}).get("post", {})
parameters = operation.get("parameters", [])
header = None
for item in parameters:
    if (
        isinstance(item, dict)
        and str(item.get("name", "")).lower() == "idempotency-key"
        and item.get("in") == "header"
    ):
        header = item
        break
if not isinstance(header, dict):
    raise SystemExit(1)
if header.get("required") is True:
    raise SystemExit(1)
schema = header.get("schema", {})
if not isinstance(schema, dict):
    raise SystemExit(1)
string_schema = schema if schema.get("type") == "string" else None
any_of = schema.get("anyOf")
if string_schema is None and isinstance(any_of, list):
    for item in any_of:
        if isinstance(item, dict) and item.get("type") == "string":
            string_schema = item
            break
if not isinstance(string_schema, dict):
    raise SystemExit(1)
if string_schema.get("maxLength") != 90:
    raise SystemExit(1)
if string_schema.get("pattern") != "^[A-Za-z0-9._:-]+$":
    raise SystemExit(1)
PY
  then
    printf 'ok: run create idempotency header schema\n'
    return 0
  fi
  printf 'fail: run create idempotency header schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_capability_manifest_failure_codes_schema() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
schemas = document.get("components", {}).get("schemas", {})
item_schema = schemas.get("CapabilityManifestItemResponse", {})
failure_codes = item_schema.get("properties", {}).get("failure_codes")
if not isinstance(failure_codes, dict):
    raise SystemExit(1)
if failure_codes.get("type") != "array":
    raise SystemExit(1)
if failure_codes.get("maxItems") != 32:
    raise SystemExit(1)
items = failure_codes.get("items")
if not isinstance(items, dict) or items.get("type") != "string":
    raise SystemExit(1)
PY
  then
    printf 'ok: capability manifest failure codes schema\n'
    return 0
  fi
  printf 'fail: capability manifest failure codes schema\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_safe_projection() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
serialized = json.dumps(document, ensure_ascii=False).lower()
for sensitive in (
    "password_hash",
    "code_hash",
    "ciphertext",
    "nonce",
    "private_key",
    "secret_key_hash",
    "encrypted_secret",
    "chain_of_thought",
    "hidden_reasoning",
):
    if sensitive in serialized:
        raise SystemExit(1)
PY
  then
    printf 'ok: openapi safe projection\n'
    return 0
  fi
  printf 'fail: openapi safe projection\n' >&2
  failures=$((failures + 1))
  return 1
}

check_openapi_schema_safe_projection() {
  if "$acceptance_python_bin" - "$acceptance_openapi_file" <<'PY'
import json
import sys

openapi_file = sys.argv[1]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
schemas = document.get("components", {}).get("schemas", {})
forbidden = (
    "password_hash",
    "code_hash",
    "ciphertext",
    "nonce",
    "private_key",
    "secret_key_hash",
    "encrypted_secret",
    "chain_of_thought",
    "hidden_reasoning",
    "checkpoint_state",
    "state_sha256",
    "lease_id",
    "quota_scope_id",
    "outbox",
    "provider_metadata",
    "traceback",
    "api_base",
)


def resolve_ref(ref):
    prefix = "#/components/schemas/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    return ref.removeprefix(prefix)


def collect_schema(name, seen):
    if name in seen:
        return {}
    seen.add(name)
    schema = schemas.get(name)
    if not isinstance(schema, dict):
        raise SystemExit(1)
    collected = {name: schema}
    stack = [schema]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref_name = resolve_ref(current.get("$ref"))
            if ref_name and ref_name not in seen:
                seen.add(ref_name)
                ref_schema = schemas.get(ref_name)
                if not isinstance(ref_schema, dict):
                    raise SystemExit(1)
                collected[ref_name] = ref_schema
                stack.append(ref_schema)
            for value in current.values():
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return collected


target_schemas = {}
for schema_name in (
    "RunSummaryResponse",
    "RunDetailResponse",
    "RunEventResponse",
    "RunArtifactResponse",
):
    target_schemas.update(collect_schema(schema_name, set()))
serialized = json.dumps(target_schemas, ensure_ascii=False).lower()
for sensitive in forbidden:
    if sensitive in serialized:
        raise SystemExit(1)
details_responses = (
    document.get("paths", {})
    .get("/api/v1/runs/{run_id}/details", {})
    .get("get", {})
    .get("responses", {})
)
details_schema = (
    details_responses.get("200", {})
    .get("content", {})
    .get("application/json", {})
    .get("schema", {})
)
if details_schema != {"$ref": "#/components/schemas/RunSummaryResponse"}:
    raise SystemExit(1)
PY
  then
    printf 'ok: run detail schema safe projection\n'
    return 0
  fi
  printf 'fail: run detail schema safe projection\n' >&2
  failures=$((failures + 1))
  return 1
}

check_runtime_failure_diagnostics() {
  local python_bin
  local script_dir
  local source_dir
  if ! python_bin="$(detect_python)"; then
    printf 'fail: runtime failure diagnostics requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from agent_hub.runtime.defaults import _capability_inventory_payload
from agent_hub.capabilities.tools.registry import (
    PluginConfigCapabilityManifestSource,
    create_builtin_tool_registry,
)
from agent_hub.project_preflight import build_project_preflight_files
from types import SimpleNamespace

cases = [
    ("Plugin tool timed out", "plugin_runtime", "timeout", "plugin.timeout", True),
    (
        "Plugin credential unavailable",
        "plugin_runtime",
        "credential_unavailable",
        "plugin.credential_unavailable",
        False,
    ),
    (
        "Plugin arguments do not match input schema: invalid type",
        "plugin_runtime",
        "invalid_arguments",
        "plugin.invalid_arguments",
        False,
    ),
    (
        "Plugin result does not match output schema: invalid type",
        "plugin_runtime",
        "invalid_result",
        "plugin.invalid_result",
        False,
    ),
    ("Plugin backend unavailable", "plugin_runtime", "backend_unavailable", "plugin.backend_unavailable", True),
    ("Plugin sandbox profile unsupported", "plugin_runtime", "sandbox_unsupported", "plugin.sandbox_unsupported", False),
    ("MCP tool unavailable", "mcp_runtime", "tool_unavailable", "mcp.tool_unavailable", False),
    ("MCP tool timed out", "mcp_runtime", "timeout", "mcp.timeout", True),
    ("mcp_server_not_discovered", "mcp_runtime", "server_not_discovered", "mcp.server_not_discovered", False),
    ("mcp_server_timeout", "mcp_runtime", "server_timeout", "mcp.server_timeout", True),
    ("mcp_server_failed", "mcp_runtime", "server_failed", "mcp.server_failed", True),
]

for reason, stage, category, code, retryable in cases:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    if diagnostic.get("error_stage") != stage:
        raise SystemExit(1)
    if diagnostic.get("error_category") != category:
        raise SystemExit(1)
    if diagnostic.get("error_code") != code:
        raise SystemExit(1)
    if diagnostic.get("retryable") is not retryable:
        raise SystemExit(1)


class CapabilityGateway:
    def capability_manifest(self, tenant_id):
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "calendar.create_event",
                    "kind": "plugin",
                    "adapter": "plugin_registry",
                    "permission_class": "plugin.use",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "inherit",
                    "available": True,
                    "availability_reason": None,
                    "failure_codes": (
                        "plugin.timeout",
                        "bad code",
                        "plugin.secret_token",
                        *(f"plugin.failure_{index}" for index in range(40)),
                    ),
                    "replay_safe": False,
                    "aliases": (),
                },
            ),
        }


inventory = _capability_inventory_payload(None, capability_gateway=CapabilityGateway())
if inventory is not None:
    raise SystemExit(1)
inventory = _capability_inventory_payload("tenant-probe", capability_gateway=CapabilityGateway())
items = inventory.get("items") if isinstance(inventory, dict) else None
if not isinstance(items, tuple) or len(items) != 1:
    raise SystemExit(1)
failure_codes = items[0].get("failure_codes")
expected_codes = ("plugin.timeout", *(f"plugin.failure_{index}" for index in range(31)))
if failure_codes != expected_codes:
    raise SystemExit(1)

preflight_files = build_project_preflight_files(
    title="超大型 Agent 项目",
    request="添加超大型项目架构和构建能力，读取约束和技能规则，生成计划 MD 文件和浏览器链接图谱。",
)
if set(preflight_files) != {"PROJECT_ARCHITECTURE_PLAN.md", "architecture-map.html"}:
    raise SystemExit(1)
preflight_plan = preflight_files["PROJECT_ARCHITECTURE_PLAN.md"].decode()
preflight_graph = preflight_files["architecture-map.html"].decode()
if "约束和技能规则读取" not in preflight_plan:
    raise SystemExit(1)
if "约束读取" not in preflight_graph:
    raise SystemExit(1)
builtin_registry = create_builtin_tool_registry()
builtin_capabilities = builtin_registry.manifests().get("capabilities")
if not isinstance(builtin_capabilities, tuple):
    raise SystemExit(1)
preflight_capability = [
    item
    for item in builtin_capabilities
    if isinstance(item, dict) and item.get("id") == "project.preflight_architecture"
]
if len(preflight_capability) != 1:
    raise SystemExit(1)
if preflight_capability[0].get("replay_safe") is not True:
    raise SystemExit(1)

activation_reason_cases = (
    (
        "runtime-registered adapter package requires a registered adapter",
        "plugin_package_adapter_unavailable",
    ),
    (
        "runtime-registered adapter package capabilities must use package isolation",
        "plugin_package_capability_isolation_mismatch",
    ),
)
for raw_reason, expected_reason in activation_reason_cases:
    source = PluginConfigCapabilityManifestSource((
        SimpleNamespace(
            id="calendar",
            enabled=True,
            status="running",
            health="healthy",
            package_metadata=SimpleNamespace(
                kind="adapter_package",
                activation_state="blocked_unsupported_runtime",
                activation_reason=raw_reason,
            ),
            capabilities=(
                SimpleNamespace(
                    id="calendar.create_event",
                    adapter="calendar_python",
                    permission_class="calendar.write",
                    sandbox_profile="local_process",
                    replay_safe=False,
                    aliases=(),
                ),
            ),
        ),
    ))
    capabilities = source.manifests()["capabilities"]
    capability = capabilities[0]
    if capability.get("available") is not False:
        raise SystemExit(1)
    if capability.get("availability_reason") != expected_reason:
        raise SystemExit(1)
    if raw_reason in str(capability):
        raise SystemExit(1)
PY
  then
    printf 'ok: runtime failure diagnostics\n'
    return 0
  fi
  printf 'fail: runtime failure diagnostics\n' >&2
  failures=$((failures + 1))
  return 1
}

check_interaction_prevention_and_recovery() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: interaction prevention and last-resort recovery\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: interaction prevention and recovery requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from uuid import uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import ModelGatewayError, _fallback_reason, _retryable_model_failure
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.runs.observer import RunMonitor
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    repair_context_from_proposal,
)
from agent_hub.runs.service import _looks_like_schedule_intent
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason


def require(value, label):
    if not value:
        raise SystemExit(label)


run_id = uuid4()

for message in (
    "每天9点提醒我填写日报",
    "设置提醒：每天9点提醒我填写日报",
    "创建提醒：每天9点提醒我填写日报",
    "设置闹钟：每天9点填写日报",
    "create reminder every day at 9am to fill daily report",
):
    require(
        _looks_like_schedule_intent(message, message.lower()) is False,
        "ordinary schedule-like reminders must stay chat",
    )

for message in (
    "帮我设计计划任务创建规则：每天9点提醒我填写日报",
    "讨论一下怎么创建计划任务，每天9点提醒我填写日报",
    "review scheduled task design: create reminder every day at 9am",
):
    require(
        _looks_like_schedule_intent(message, message.lower()) is False,
        "schedule feature discussion must stay chat",
    )

for message in (
    "创建计划任务：每天9点提醒我填写日报",
    "创建计划任务：9月3号给我生成一个方案",
    "create scheduled task every day at 9am to fill daily report",
):
    require(
        _looks_like_schedule_intent(message, message.lower()) is True,
        "explicit schedule task creation must still propose",
    )

empty_error = ModelGatewayError("model response text is empty")
require(_retryable_model_failure(empty_error) is True, "empty response must be retryable")
require(_fallback_reason(empty_error) == "empty_response", "empty response must trigger fallback")

model_prevention = runtime_failure_diagnostic_from_reason(
    "model gateway failed: model response text is empty"
)
require(model_prevention.get("retryable") is True, "empty response diagnostic retryable")
require(model_prevention.get("error_code") == "model.empty_response", "empty response code")
require("fallback" in str(model_prevention.get("suggested_action")), "empty response action")

provider_rate_limit = ModelTransportError("safe provider rate limit", status_code=429)
require(_retryable_model_failure(provider_rate_limit) is True, "provider 429 must be retryable")
provider_rate_limit_diagnostic = runtime_failure_diagnostic_from_reason(
    "model gateway failed: model response failed (status=429)"
)
require(
    provider_rate_limit_diagnostic.get("error_code") == "model.provider_rate_limited",
    "provider 429 diagnostic code",
)
require(provider_rate_limit_diagnostic.get("retryable") is True, "provider 429 diagnostic retryable")

for reason, code, retryable in (
    ("Plugin backend unavailable", "plugin.backend_unavailable", True),
    ("Plugin credential unavailable", "plugin.credential_unavailable", False),
    ("mcp_server_timeout", "mcp.server_timeout", True),
    ("mcp_server_failed", "mcp.server_failed", True),
):
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    require(diagnostic.get("error_code") == code, f"{reason} code")
    require(diagnostic.get("retryable") is retryable, f"{reason} retryable")
    require(diagnostic.get("suggested_action"), f"{reason} suggested action")
    require("runtime.failed" not in str(diagnostic), f"{reason} must not be generic")

capacity_failure = RunEvent(
    kind=EventKind.RUNTIME_FAILED,
    sequence=1,
    run_id=run_id,
    reason="model gateway failed: model capacity queue timeout",
)
capacity_decision = RunMonitor().observe(capacity_failure)
require(capacity_decision is not None, "capacity decision")
require(capacity_decision.trigger == "model_capacity_pressure", "model_capacity_pressure")
require(capacity_decision.action == "reschedule_or_reassign_model", "reschedule_or_reassign_model")
require(
    capacity_decision.recommendation == "switch_to_available_model_and_retry",
    "switch_to_available_model_and_retry",
)

provider_rate_limit_failure = RunEvent(
    kind=EventKind.RUNTIME_FAILED,
    sequence=4,
    run_id=run_id,
    reason="model gateway failed: model response failed (status=429)",
)
provider_rate_limit_monitor = RunMonitor()
provider_rate_limit_decision = provider_rate_limit_monitor.observe(provider_rate_limit_failure)
require(provider_rate_limit_decision is not None, "provider 429 decision")
require(
    provider_rate_limit_decision.trigger == "model_capacity_pressure",
    "provider 429 must trigger model switch",
)
require(
    provider_rate_limit_decision.recommendation == "switch_to_available_model_and_retry",
    "provider 429 recommendation",
)
provider_rate_limit_repair = classify_terminal_run(
    status=RunStatus.FAILED,
    mode=TaskMode.AUTO,
    routing_decision={"source": "manual"},
    events=(
        provider_rate_limit_failure,
        provider_rate_limit_decision.to_event(run_id=run_id, sequence=5),
    ),
    policy=SelfRepairPolicy(requires_approval=True),
)
require(provider_rate_limit_repair is not None, "provider 429 repair decision")
require(provider_rate_limit_repair.failure_category == "capacity_pressure", "provider 429 repair category")
require(
    provider_rate_limit_repair.recovery_strategy == "switch_to_available_model_and_retry",
    "provider 429 repair strategy",
)

empty_failure = RunEvent(
    kind=EventKind.RUNTIME_FAILED,
    sequence=2,
    run_id=run_id,
    reason="model gateway failed: model response text is empty",
)
empty_monitor = RunMonitor()
empty_decision = empty_monitor.observe(empty_failure)
require(empty_decision is not None, "empty response decision")
require(empty_decision.trigger == "empty_model_response", "empty_model_response")
require(empty_decision.action == "retry_fallback_or_reassign_model", "retry_fallback_or_reassign_model")
require(
    empty_decision.recommendation == "retry_with_fallback_or_reassign_model",
    "retry_with_fallback_or_reassign_model",
)
observer_event = empty_decision.to_event(run_id=run_id, sequence=3)
repair = classify_terminal_run(
    status=RunStatus.FAILED,
    mode=TaskMode.AUTO,
    routing_decision={"source": "manual"},
    events=(empty_failure, observer_event),
    policy=SelfRepairPolicy(requires_approval=True),
)
require(repair is not None, "repair decision")
require(repair.kind == "repair.classified", "repair.classified")
require(repair.failure_category == "empty_model_response", "repair failure category")
require(repair.recovery_strategy == "retry_with_fallback_or_reassign_model", "repair strategy")
require(repair.requires_approval is True, "requires_approval")
require(repair.automatic_execution is False, "automatic_execution")

proposal = repair.to_proposal(run_id=run_id)
require(proposal is not None, "self_repair proposal")
require(proposal.get("kind") == "self_repair", "self_repair")
require(proposal.get("failure_kind") == "empty_model_response", "proposal failure kind")
require(proposal.get("recovery_strategy") == "retry_with_fallback_or_reassign_model", "proposal strategy")
repair_context = repair_context_from_proposal(proposal)
require(repair_context.get("source") == "self_repair", "repair context source")
require(repair_context.get("failure_kind") == "empty_model_response", "repair context failure kind")
require(
    repair_context.get("recovery_strategy") == "retry_with_fallback_or_reassign_model",
    "repair context strategy",
)
require(repair_context.get("requires_approval") is True, "repair context requires approval")
require(repair_context.get("automatic_execution") is False, "repair context automatic execution")
PY
  then
    printf 'ok: interaction prevention and recovery\n'
    return 0
  fi
  printf 'fail: interaction prevention and recovery\n' >&2
  failures=$((failures + 1))
  return 1
}

check_multimode_interaction_matrix() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: multi-mode interaction matrix\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: multi-mode interaction matrix requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from uuid import uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.observer import RunMonitor
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runs.service import (
    _harness_task_requirements,
    _local_main_agent_auto_mode,
    _main_agent_adjusted_ready_mode,
)
from agent_hub.runtime.contracts import EventKind, RunEvent


def require(value, label):
    if not value:
        raise SystemExit(label)


expected_modes = ("auto", "direct", "dispatch", "discuss", "hybrid")
require(tuple(mode.value for mode in TaskMode) == expected_modes, "TaskMode enum drift")

require(
    _local_main_agent_auto_mode("请直接回答这个问题", ()) is TaskMode.DIRECT,
    "auto direct routing",
)
require(
    _local_main_agent_auto_mode("请调度执行并生成报告", ()) is TaskMode.DISPATCH,
    "auto dispatch routing",
)
require(
    _local_main_agent_auto_mode("先讨论优缺点再给结论", ()) is TaskMode.HYBRID,
    "auto hybrid routing",
)
mega_project_message = (
    "添加超大型项目架构和构建能力，从需求拆解、架构搭建、分阶段实现、"
    "严格验收测试到最终生产结果都要稳定完成"
)
require(
    _local_main_agent_auto_mode(mega_project_message, ()) is TaskMode.HYBRID,
    "ultra-large project auto routing",
)
require(
    _local_main_agent_auto_mode("请复核并争论观点", ()) is TaskMode.DISCUSS,
    "auto discuss routing",
)
require(
    _main_agent_adjusted_ready_mode(
        TaskMode.DIRECT,
        message="请调度执行并生成报告",
        attachment_ids=(),
    )
    is TaskMode.DISPATCH,
    "router direct adjusted to dispatch",
)
require(
    _main_agent_adjusted_ready_mode(
        TaskMode.DISPATCH,
        message="请讨论这个执行方案",
        attachment_ids=(),
    )
    is TaskMode.HYBRID,
    "router dispatch plus discuss adjusted to hybrid",
)

requirements_by_mode = {}
messages_by_mode = {
    TaskMode.DIRECT: "请直接回答这个问题",
    TaskMode.DISPATCH: "请调度执行并生成报告",
    TaskMode.DISCUSS: "请复核并争论观点",
    TaskMode.HYBRID: "先讨论方案风险再执行检查",
}
for mode in (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID):
    requirements = _harness_task_requirements(
        message=messages_by_mode[mode],
        mode=mode,
        routing_decision={},
    )
    requirements_by_mode[mode] = requirements
    require("text" in requirements.required_capabilities, f"{mode.value} text capability")
    require(requirements.estimated_input_tokens > 0, f"{mode.value} token estimate")

require(
    "tool_calling" not in requirements_by_mode[TaskMode.DIRECT].required_capabilities,
    "direct should stay simple unless tool use is requested",
)
require(
    requirements_by_mode[TaskMode.DISPATCH].needs_reasoning is True,
    "dispatch reasoning",
)
require(
    requirements_by_mode[TaskMode.DISPATCH].needs_parallel_tool_calls is True,
    "dispatch parallel tools",
)
require(
    requirements_by_mode[TaskMode.DISCUSS].needs_reasoning is True,
    "discuss reasoning",
)
require(
    requirements_by_mode[TaskMode.HYBRID].needs_parallel_tool_calls is True,
    "hybrid parallel tools",
)
mega_project_requirements = _harness_task_requirements(
    message=mega_project_message,
    mode=TaskMode.HYBRID,
    routing_decision={},
)
require(
    mega_project_requirements.needs_long_running is True,
    "ultra-large project long running",
)
require(
    mega_project_requirements.needs_parallel_tool_calls is True,
    "ultra-large project parallel tools",
)
require(
    mega_project_requirements.requires_sandbox is True,
    "ultra-large project sandbox",
)
require(
    mega_project_requirements.prefers_prefix_cache is True,
    "ultra-large project prefix cache",
)

for mode in (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID):
    run_id = uuid4()
    failure = RunEvent(
        kind=EventKind.RUNTIME_FAILED,
        sequence=1,
        run_id=run_id,
        reason="model gateway failed: model response text is empty",
    )
    decision = RunMonitor().observe(failure)
    require(decision is not None, f"{mode.value} observer decision")
    require(decision.trigger == "empty_model_response", f"{mode.value} empty response")
    repair = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=mode,
        routing_decision={"source": "manual"},
        events=(failure, decision.to_event(run_id=run_id, sequence=2)),
        policy=SelfRepairPolicy(requires_approval=True),
    )
    require(repair is not None, f"{mode.value} repair decision")
    require(repair.kind == "repair.classified", f"{mode.value} repair classified")
    require(repair.failure_category == "empty_model_response", f"{mode.value} repair category")
    require(
        repair.recovery_strategy == "retry_with_fallback_or_reassign_model",
        f"{mode.value} recovery strategy",
    )
    require(repair.automatic_execution is False, f"{mode.value} automatic execution")
PY
  then
    printf 'ok: multi-mode interaction matrix\n'
    return 0
  fi
  printf 'fail: multi-mode interaction matrix\n' >&2
  failures=$((failures + 1))
  return 1
}

run_codex_profile() {
  printf 'profile: codex harness stability\n'
  check_url "api health alias" "/health" || true
  check_url "api live health" "/health/live" || true
  check_url "api readiness" "/health/ready" || true
  check_health_json "api health alias" "/health" || true
  check_health_json "api live health" "/health/live" || true
  check_health_json "api readiness" "/health/ready" || true
  check_prometheus_metrics || true
  check_protected_boundary "run read requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000" || true
  check_protected_boundary "run events requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/events" || true
  check_protected_boundary "run details requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/details" || true
  check_protected_boundary "run artifact download requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/artifacts/00000000-0000-0000-0000-000000000000/download" || true
  check_protected_boundary "workspace file list requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/files" || true
  check_protected_boundary "workspace file download requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/files/download?path=artifact.txt" || true
  check_protected_boundary "workspace bundle download requires bearer" "/api/v1/workspaces/projects/probe-project/sessions/probe-session/bundle/download" || true
  check_write_protected_boundary "run create requires bearer" "/api/v1/runs" "POST" || true
  check_write_protected_boundary "run pause requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/pause" "POST" || true
  check_write_protected_boundary "run resume requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/resume" "POST" || true
  check_write_protected_boundary "run cancel requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/cancel" "POST" || true
  check_write_protected_boundary "run project preflight approval requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/approve-project-preflight" "POST" || true
  check_write_protected_boundary "run capability approve requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/approve-capability" "POST" || true
  check_write_protected_boundary "run capability reject requires bearer" "/api/v1/runs/00000000-0000-0000-0000-000000000000/reject-capability" "POST" || true
  check_protected_boundary "auth me requires bearer" "/api/v1/auth/me" || true
  check_protected_boundary "config current requires bearer" "/api/v1/config/current" || true
  check_protected_boundary "config history requires bearer" "/api/v1/config/history" || true
  check_protected_boundary "config version requires bearer" "/api/v1/config/history/1" || true
  check_protected_boundary "config diff requires bearer" "/api/v1/config/diff?from_version=1&to_version=2" || true
  check_write_protected_boundary "config publish requires bearer" "/api/v1/config/drafts/00000000-0000-0000-0000-000000000000/publish" "POST" || true
  check_write_protected_boundary "config rollback requires bearer" "/api/v1/config/history/1/rollback" "POST" || true
  check_protected_boundary "user list requires bearer" "/api/v1/users" || true
  check_write_protected_boundary "user delete requires bearer" "/api/v1/users/00000000-0000-0000-0000-000000000000" "DELETE" || true
  check_protected_boundary "model registry requires bearer" "/api/v1/admin/models" || true
  check_write_protected_boundary "model create requires bearer" "/api/v1/admin/models" "POST" || true
  check_write_protected_boundary "model probe requires bearer" "/api/v1/admin/models/probe" "POST" || true
  check_protected_boundary "admin secret read requires bearer" "/api/v1/admin/secrets/probe" || true
  check_protected_boundary "admin agents list requires bearer" "/api/v1/admin/agents" || true
  check_protected_boundary "admin workflows list requires bearer" "/api/v1/admin/workflows" || true
  check_protected_boundary "admin settings get requires bearer" "/api/v1/admin/settings" || true
  check_protected_boundary "admin main agent get requires bearer" "/api/v1/admin/main-agent" || true
  check_protected_boundary "admin runs list requires bearer" "/api/v1/admin/runs" || true
  check_protected_boundary "admin run detail requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000" || true
  check_protected_boundary "admin run artifact download requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000/artifacts/00000000-0000-0000-0000-000000000000/download" || true
  check_protected_boundary "admin run debug requires bearer" "/api/v1/admin/runs/00000000-0000-0000-0000-000000000000/debug" || true
  check_protected_boundary "admin skills list requires bearer" "/api/v1/admin/skills" || true
  check_protected_boundary "admin schedule list requires bearer" "/api/v1/admin/schedules" || true
  check_write_protected_boundary "admin schedule create requires bearer" "/api/v1/admin/schedules" "POST" || true
  check_write_protected_boundary "admin schedule tick requires bearer" "/api/v1/admin/schedules/tick" "POST" || true
  check_write_protected_boundary "admin schedule delete requires bearer" "/api/v1/admin/schedules/00000000-0000-0000-0000-000000000000" "DELETE" || true
  check_error_envelope "missing api route envelope" "/api/missing-acceptance-probe" "GET" "404" "not_found" || true
  check_error_envelope "method not allowed envelope" "/health/live" "POST" "405" "method_not_allowed" || true
  check_url "openapi contract" "/openapi.json" || true
  check_url "management ui entry" "/login" || true
}

run_deepseek_profile() {
  printf 'profile: deepseek pluggable harness goals\n'
  check_url "plugin-safe openapi projection" "/openapi.json" || true
  check_url "operator ui for plugin orchestration" "/login" || true
  check_url "runtime readiness boundary" "/health/ready" || true
  check_prometheus_metrics || true
  check_runtime_failure_diagnostics || true
  check_interaction_prevention_and_recovery || true
  check_multimode_interaction_matrix || true
  check_protected_boundary "plugin adapters require bearer" "/api/v1/admin/plugins/adapters" || true
  check_protected_boundary "plugin registry list requires bearer" "/api/v1/admin/plugins" || true
  check_write_protected_boundary "plugin registry upsert requires bearer" "/api/v1/admin/plugins" "POST" || true
  check_protected_boundary "plugin policy summary requires bearer" "/api/v1/admin/plugins/policy-summary" || true
  check_write_protected_boundary "plugin policy review requires bearer" "/api/v1/admin/plugins/policy-review" "POST" || true
  check_protected_boundary "plugin signing key list requires bearer" "/api/v1/admin/plugins/signing-keys" || true
  check_write_protected_boundary "plugin signing key upsert requires bearer" "/api/v1/admin/plugins/signing-keys" "POST" || true
  check_write_protected_boundary "plugin signing key delete requires bearer" "/api/v1/admin/plugins/signing-keys/probe-key" "DELETE" || true
  check_write_protected_boundary "plugin install requires bearer" "/api/v1/admin/plugins/install" "POST" || true
  check_write_protected_boundary "plugin package approval requires bearer" "/api/v1/admin/plugins/probe/package/approve" "POST" || true
  check_write_protected_boundary "plugin package rejection requires bearer" "/api/v1/admin/plugins/probe/package/reject" "POST" || true
  check_write_protected_boundary "plugin lifecycle start requires bearer" "/api/v1/admin/plugins/probe/start" "POST" || true
  check_write_protected_boundary "plugin lifecycle enable requires bearer" "/api/v1/admin/plugins/probe/enable" "POST" || true
  check_write_protected_boundary "plugin lifecycle disable requires bearer" "/api/v1/admin/plugins/probe/disable" "POST" || true
  check_write_protected_boundary "plugin lifecycle stop requires bearer" "/api/v1/admin/plugins/probe/stop" "POST" || true
  check_write_protected_boundary "plugin lifecycle reload requires bearer" "/api/v1/admin/plugins/probe/reload" "POST" || true
  check_write_protected_boundary "plugin uninstall requires bearer" "/api/v1/admin/plugins/probe/uninstall" "POST" || true
  check_write_protected_boundary "plugin delete requires bearer" "/api/v1/admin/plugins/probe" "DELETE" || true
  check_protected_boundary "plugin capability manifest requires bearer" "/api/v1/admin/capabilities/manifest" || true
  check_protected_boundary "mcp registry requires bearer" "/api/v1/admin/mcp" || true
  check_write_protected_boundary "mcp upsert requires bearer" "/api/v1/admin/mcp" "POST" || true
}

check_openapi_path() {
  local name="$1"
  local path="$2"
  local method="$3"
  if "$acceptance_python_bin" - "$acceptance_openapi_file" "$path" "$method" <<'PY'
import json
import sys

openapi_file, path, method = sys.argv[1:4]
with open(openapi_file, encoding="utf-8") as handle:
    document = json.load(handle)
security_scheme = document.get("components", {}).get("securitySchemes", {}).get("BearerAuth")
if security_scheme != {"type": "http", "scheme": "bearer"}:
    raise SystemExit(1)
operation = document.get("paths", {}).get(path, {})
method_key = method.lower()
if method_key not in {str(key).lower() for key in operation}:
    raise SystemExit(1)
operation_doc = operation.get(method_key, {})
security = operation_doc.get("security", [])
if {"BearerAuth": []} not in security:
    raise SystemExit(1)
responses = operation_doc.get("responses", {})
if "401" not in responses or "403" not in responses:
    raise SystemExit(1)
PY
  then
    printf 'ok: %s %s %s\n' "$name" "$method" "$path"
    return 0
  fi
  printf 'fail: %s %s %s\n' "$name" "$method" "$path"
  failures=$((failures + 1))
  return 1
}

run_openapi_capability_profile() {
  local openapi_file
  printf 'profile: openapi capability surface\n'
  if ! acceptance_python_bin="$(detect_python)"; then
    printf 'fail: openapi capability probe requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  openapi_file="$(mktemp)"
  acceptance_openapi_file="$openapi_file"
  if ! curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS -o "$openapi_file" \
    "$base_url/openapi.json" 2>/dev/null; then
    rm -f -- "$openapi_file"
    printf 'fail: openapi capability surface /openapi.json\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  check_openapi_safe_projection || true
  check_openapi_schema_safe_projection || true
  check_openapi_path "run create" "/api/v1/runs" "post" || true
  check_openapi_run_create_idempotency_header || true
  check_openapi_path "run pause control" "/api/v1/runs/{run_id}/pause" "post" || true
  check_openapi_path "run resume control" "/api/v1/runs/{run_id}/resume" "post" || true
  check_openapi_path "run cancel control" "/api/v1/runs/{run_id}/cancel" "post" || true
  check_openapi_path "run project preflight approval" "/api/v1/runs/{run_id}/approve-project-preflight" "post" || true
  check_openapi_path "run capability approval" "/api/v1/runs/{run_id}/approve-capability" "post" || true
  check_openapi_path "run reject capability" "/api/v1/runs/{run_id}/reject-capability" "post" || true
  check_openapi_path "run detail projection" "/api/v1/runs/{run_id}/details" "get" || true
  check_openapi_path "config validate" "/api/v1/config/validate" "post" || true
  check_openapi_path "config draft create" "/api/v1/config/drafts" "post" || true
  check_openapi_path "config draft publish" "/api/v1/config/drafts/{revision_id}/publish" "post" || true
  check_openapi_path "config current" "/api/v1/config/current" "get" || true
  check_openapi_path "config history" "/api/v1/config/history" "get" || true
  check_openapi_path "config version" "/api/v1/config/history/{version}" "get" || true
  check_openapi_path "config diff" "/api/v1/config/diff" "get" || true
  check_openapi_path "config rollback" "/api/v1/config/history/{version}/rollback" "post" || true
  check_openapi_path "user list" "/api/v1/users" "get" || true
  check_openapi_path "user create" "/api/v1/users" "post" || true
  check_openapi_path "user update" "/api/v1/users/{user_id}" "patch" || true
  check_openapi_path "user role" "/api/v1/users/{user_id}/role" "patch" || true
  check_openapi_path "user disabled" "/api/v1/users/{user_id}/disabled" "patch" || true
  check_openapi_path "user password" "/api/v1/users/{user_id}/password" "patch" || true
  check_openapi_path "user delete" "/api/v1/users/{user_id}" "delete" || true
  check_openapi_path "workspace file list" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/files" "get" || true
  check_openapi_path "workspace file download" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/files/download" "get" || true
  check_openapi_path "workspace bundle download" "/api/v1/workspaces/projects/{project_id}/sessions/{session_id}/bundle/download" "get" || true
  check_openapi_path "model routing registry" "/api/v1/admin/models" "get" || true
  check_openapi_path "model routing create" "/api/v1/admin/models" "post" || true
  check_openapi_path "model routing probe" "/api/v1/admin/models/probe" "post" || true
  check_openapi_task_mode_schema || true
  check_openapi_model_capability_schema || true
  check_openapi_model_deployment_response_schema || true
  check_openapi_model_probe_response_schema || true
  check_openapi_capability_manifest_failure_codes_schema || true
  check_openapi_path "admin secret create" "/api/v1/admin/secrets" "post" || true
  check_openapi_path "admin secret read" "/api/v1/admin/secrets/{ref}" "get" || true
  check_openapi_path "admin config draft save" "/api/v1/admin/config/draft" "put" || true
  check_openapi_path "admin config draft diff" "/api/v1/admin/config/diff" "post" || true
  check_openapi_path "admin config publish" "/api/v1/admin/config/publish" "post" || true
  check_openapi_path "admin config rollback" "/api/v1/admin/config/rollback/{version}" "post" || true
  check_openapi_path "admin agents list" "/api/v1/admin/agents" "get" || true
  check_openapi_path "admin agent upsert" "/api/v1/admin/agents" "post" || true
  check_openapi_path "admin agent delete" "/api/v1/admin/agents/{agent_id}" "delete" || true
  check_openapi_path "admin workflows list" "/api/v1/admin/workflows" "get" || true
  check_openapi_path "admin workflow upsert" "/api/v1/admin/workflows" "post" || true
  check_openapi_path "admin workflow delete" "/api/v1/admin/workflows/{workflow_id}" "delete" || true
  check_openapi_path "admin settings get" "/api/v1/admin/settings" "get" || true
  check_openapi_path "admin settings update" "/api/v1/admin/settings" "put" || true
  check_openapi_path "admin main agent get" "/api/v1/admin/main-agent" "get" || true
  check_openapi_path "admin main agent update" "/api/v1/admin/main-agent" "put" || true
  check_openapi_path "admin runs list" "/api/v1/admin/runs" "get" || true
  check_openapi_path "admin run detail" "/api/v1/admin/runs/{run_id}" "get" || true
  check_openapi_path "admin run artifact download" "/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download" "get" || true
  check_openapi_path "admin run debug" "/api/v1/admin/runs/{run_id}/debug" "get" || true
  check_openapi_path "admin run pause" "/api/v1/admin/runs/{run_id}/pause" "post" || true
  check_openapi_path "admin run resume" "/api/v1/admin/runs/{run_id}/resume" "post" || true
  check_openapi_path "admin run cancel" "/api/v1/admin/runs/{run_id}/cancel" "post" || true
  check_openapi_path "admin run delete" "/api/v1/admin/runs/{run_id}" "delete" || true
  check_openapi_path "admin schedule list" "/api/v1/admin/schedules" "get" || true
  check_openapi_path "admin schedule create" "/api/v1/admin/schedules" "post" || true
  check_openapi_path "admin schedule tick" "/api/v1/admin/schedules/tick" "post" || true
  check_openapi_path "admin schedule delete" "/api/v1/admin/schedules/{schedule_id}" "delete" || true
  check_openapi_path "admin skills list" "/api/v1/admin/skills" "get" || true
  check_openapi_path "admin skill upload" "/api/v1/admin/skills" "post" || true
  check_openapi_path "admin skill archive upload" "/api/v1/admin/skills/upload" "post" || true
  check_openapi_path "admin skill version activate" "/api/v1/admin/skills/{skill_id}/versions/{version_id}/activate" "post" || true
  check_openapi_path "admin skill approve" "/api/v1/admin/skills/{skill_id}/approve" "post" || true
  check_openapi_path "admin skill delete" "/api/v1/admin/skills/{skill_id}" "delete" || true
  check_openapi_path "plugin adapters" "/api/v1/admin/plugins/adapters" "get" || true
  check_openapi_path "plugin registry list" "/api/v1/admin/plugins" "get" || true
  check_openapi_path "plugin registry upsert" "/api/v1/admin/plugins" "post" || true
  check_openapi_path "plugin policy summary" "/api/v1/admin/plugins/policy-summary" "get" || true
  check_openapi_path "plugin policy review" "/api/v1/admin/plugins/policy-review" "post" || true
  check_openapi_path "plugin signing key list" "/api/v1/admin/plugins/signing-keys" "get" || true
  check_openapi_path "plugin signing key upsert" "/api/v1/admin/plugins/signing-keys" "post" || true
  check_openapi_path "plugin signing key delete" "/api/v1/admin/plugins/signing-keys/{key_id}" "delete" || true
  check_openapi_path "plugin package install" "/api/v1/admin/plugins/install" "post" || true
  check_openapi_path "plugin package approval" "/api/v1/admin/plugins/{plugin_id}/package/approve" "post" || true
  check_openapi_path "plugin package rejection" "/api/v1/admin/plugins/{plugin_id}/package/reject" "post" || true
  check_openapi_path "plugin lifecycle start" "/api/v1/admin/plugins/{plugin_id}/start" "post" || true
  check_openapi_path "plugin lifecycle enable" "/api/v1/admin/plugins/{plugin_id}/enable" "post" || true
  check_openapi_path "plugin lifecycle disable" "/api/v1/admin/plugins/{plugin_id}/disable" "post" || true
  check_openapi_path "plugin lifecycle stop" "/api/v1/admin/plugins/{plugin_id}/stop" "post" || true
  check_openapi_path "plugin lifecycle reload" "/api/v1/admin/plugins/{plugin_id}/reload" "post" || true
  check_openapi_path "plugin uninstall" "/api/v1/admin/plugins/{plugin_id}/uninstall" "post" || true
  check_openapi_path "plugin delete" "/api/v1/admin/plugins/{plugin_id}" "delete" || true
  check_openapi_path "plugin capability manifest" "/api/v1/admin/capabilities/manifest" "get" || true
  check_openapi_path "mcp server registry" "/api/v1/admin/mcp" "get" || true
  check_openapi_path "mcp server upsert" "/api/v1/admin/mcp" "post" || true
  rm -f -- "$openapi_file"
}

run_lifecycle_profile() {
  local python_bin
  local idempotency_key
  local request_body
  local response
  local runs_path="/api/v1/runs"
  local run_id
  local run_path
  local events_path

  printf 'profile: authenticated run lifecycle\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: run lifecycle probe is disabled in read-only mode\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: run lifecycle probe requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: run lifecycle probe requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! request_body="$(ACCEPTANCE_RUN_MESSAGE="$run_message" "$python_bin" -c 'import json, os; print(json.dumps({"message": os.environ["ACCEPTANCE_RUN_MESSAGE"], "mode": "direct", "sandbox_profile": "none", "requested_permissions": [], "skip_evolution_proposal": True}, ensure_ascii=False))')"; then
    printf 'fail: could not build run lifecycle request body\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  idempotency_key="harness-acceptance-$(date +%s)-$$"
  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $idempotency_key" \
    -d "$request_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: run lifecycle create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! run_id="$(ACCEPTANCE_RESPONSE="$response" "$python_bin" -c 'import json, os; print(json.loads(os.environ["ACCEPTANCE_RESPONSE"])["id"])')"; then
    printf 'fail: run lifecycle create response did not include id\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  run_path="/api/v1/runs/$run_id"
  events_path="/api/v1/runs/$run_id/events"
  if ! curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS -o /dev/null \
    -H "Authorization: Bearer $bearer_token" \
    "$base_url$run_path" 2>/dev/null; then
    printf 'fail: run lifecycle read /api/v1/runs/%s\n' "$run_id" >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS -o /dev/null \
    -H "Authorization: Bearer $bearer_token" \
    "$base_url$events_path" 2>/dev/null; then
    printf 'fail: run lifecycle events /api/v1/runs/%s/events\n' "$run_id" >&2
    failures=$((failures + 1))
    return 1
  fi

  post_run_control_expect_status \
    "$python_bin" \
    "$run_id" \
    "cancel" \
    "cancelled" \
    "run lifecycle cleanup cancel reaches cancelled" || return 1

  printf 'ok: run lifecycle create/read/events/cleanup run_id=%s\n' "$run_id"
}

run_create_idempotency_replay_guard_profile() {
  local python_bin
  local request_body
  local first_response
  local replay_response
  local first_run_id
  local replay_run_id
  local idempotency_key
  local runs_path="/api/v1/runs"

  printf 'profile: authenticated run create idempotency replay guard\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: run create idempotency replay guard is disabled in read-only mode\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: run create idempotency replay guard requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: run create idempotency replay guard requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! request_body="$("$python_bin" - <<'PY'
import json

print(json.dumps({
    "message": "Agent Hub run create idempotency replay acceptance probe",
    "mode": "direct",
    "sandbox_profile": "none",
    "requested_permissions": [],
    "skip_evolution_proposal": True,
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build run create idempotency replay request body\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  idempotency_key="create-replay-$(date +%s)-$$"
  if ! first_response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $idempotency_key" \
    -d "$request_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: run create idempotency first create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! replay_response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $idempotency_key" \
    -d "$request_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: run create idempotency replay create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! first_run_id="$(ACCEPTANCE_RESPONSE="$first_response" "$python_bin" -c 'import json, os; print(json.loads(os.environ["ACCEPTANCE_RESPONSE"])["id"])')"; then
    printf 'fail: first create response did not include id\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! replay_run_id="$(ACCEPTANCE_RESPONSE="$replay_response" "$python_bin" -c 'import json, os; print(json.loads(os.environ["ACCEPTANCE_RESPONSE"])["id"])')"; then
    printf 'fail: replayed create response did not include id\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$first_run_id" != "$replay_run_id" ]]; then
    printf 'fail: replayed create returned different run id first=%s replay=%s\n' \
      "$first_run_id" "$replay_run_id" >&2
    failures=$((failures + 1))
    return 1
  fi

  printf 'ok: replayed create returned original run id run_id=%s\n' "$first_run_id"
}

post_run_control_expect_status() {
  local python_bin="$1"
  local run_id="$2"
  local action="$3"
  local expected_status="$4"
  local label="$5"
  local response

  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -X POST \
    -H "Authorization: Bearer $bearer_token" \
    "$base_url/api/v1/runs/$run_id/$action" 2>/dev/null)"; then
    printf 'fail: %s /api/v1/runs/%s/%s\n' "$label" "$run_id" "$action" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$response" ACCEPTANCE_EXPECTED_STATUS="$expected_status" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
if payload.get("status") != os.environ["ACCEPTANCE_EXPECTED_STATUS"]:
    raise SystemExit(1)
PY
  then
    printf 'ok: %s\n' "$label"
    return 0
  fi
  printf 'fail: %s expected status=%s\n' "$label" "$expected_status" >&2
  failures=$((failures + 1))
  return 1
}

create_control_idempotency_probe_run() {
  local python_bin="$1"
  local probe_name="$2"
  local request_body
  local response
  local idempotency_key

  if ! request_body="$(ACCEPTANCE_CONTROL_PROBE_NAME="$probe_name" "$python_bin" - <<'PY'
import json
import os

probe_name = os.environ["ACCEPTANCE_CONTROL_PROBE_NAME"]
print(json.dumps({
    "message": f"Agent Hub run control idempotency acceptance probe: {probe_name}",
    "mode": "direct",
    "sandbox_profile": "none",
    "requested_permissions": [],
    "skip_evolution_proposal": True,
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build run control idempotency request body\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  idempotency_key="control-idempotency-$probe_name-$(date +%s)-$$"
  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $idempotency_key" \
    -d "$request_body" \
    "$base_url/api/v1/runs" 2>/dev/null)"; then
    printf 'fail: run control idempotency create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! ACCEPTANCE_RESPONSE="$response" "$python_bin" -c 'import json, os; print(json.loads(os.environ["ACCEPTANCE_RESPONSE"])["id"])'; then
    printf 'fail: run control idempotency create response did not include id\n' >&2
    failures=$((failures + 1))
    return 1
  fi
}

run_control_idempotency_guard_profile() {
  local python_bin
  local run_id
  local cancel_probe_run_id

  printf 'profile: authenticated run control idempotency guard\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: run control idempotency guard is disabled in read-only mode\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: run control idempotency guard requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: run control idempotency guard requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  run_id="$(create_control_idempotency_probe_run "$python_bin" "pause-resume")" || return 1

  post_run_control_expect_status "$python_bin" "$run_id" "pause" "paused" "initial pause reaches paused" || return 1
  post_run_control_expect_status "$python_bin" "$run_id" "pause" "paused" "repeated pause stays paused" || return 1
  post_run_control_expect_status "$python_bin" "$run_id" "resume" "queued" "initial resume reaches queued" || return 1
  post_run_control_expect_status "$python_bin" "$run_id" "resume" "queued" "repeated resume stays queued" || return 1

  cancel_probe_run_id="$(create_control_idempotency_probe_run "$python_bin" "cancel")" || return 1
  post_run_control_expect_status "$python_bin" "$cancel_probe_run_id" "pause" "paused" "cancel probe pause reaches paused" || return 1
  post_run_control_expect_status "$python_bin" "$cancel_probe_run_id" "cancel" "cancelled" "initial cancel reaches cancelled" || return 1
  post_run_control_expect_status "$python_bin" "$cancel_probe_run_id" "cancel" "cancelled" "repeated cancel stays cancelled" || return 1
}

run_schedule_interaction_guard_profile() {
  local python_bin
  local ordinary_body
  local explicit_body
  local ordinary_response
  local explicit_response
  local runs_path="/api/v1/runs"

  printf 'profile: authenticated schedule interaction guard\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: schedule interaction guard is disabled in read-only mode\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: schedule interaction guard requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: schedule interaction guard requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! ordinary_body="$("$python_bin" - <<'PY'
import json

print(json.dumps({
    "message": "设置提醒：每天9点提醒我填写日报",
    "mode": "auto",
    "sandbox_profile": "none",
    "requested_permissions": [],
    "skip_evolution_proposal": True,
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build ordinary schedule-like run body\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! ordinary_response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: schedule-guard-ordinary-$(date +%s)-$$" \
    -d "$ordinary_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: schedule interaction ordinary run create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$ordinary_response" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
if payload.get("schedule_proposal") is not None:
    raise SystemExit("ordinary reminder must not return schedule proposal")
if payload.get("clarification_reason") == "schedule_requires_user_confirmation":
    raise SystemExit("ordinary reminder must not request schedule confirmation")
PY
  then
    printf 'ok: ordinary reminder must not return schedule proposal\n'
  else
    printf 'fail: ordinary reminder must not return schedule proposal\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! explicit_body="$("$python_bin" - <<'PY'
import json

print(json.dumps({
    "message": "创建计划任务：每天9点提醒我填写日报",
    "mode": "auto",
    "sandbox_profile": "none",
    "requested_permissions": [],
    "skip_evolution_proposal": True,
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build explicit schedule run body\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! explicit_response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: schedule-guard-explicit-$(date +%s)-$$" \
    -d "$explicit_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: schedule interaction explicit run create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$explicit_response" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
proposal = payload.get("schedule_proposal")
if payload.get("status") != "waiting_approval":
    raise SystemExit("explicit schedule task must wait for approval")
if payload.get("clarification_reason") != "schedule_requires_user_confirmation":
    raise SystemExit("explicit schedule task must request schedule confirmation")
if not isinstance(proposal, dict):
    raise SystemExit("explicit schedule task must return schedule proposal")
if proposal.get("kind") != "cron" or proposal.get("cron") != "0 9 * * *":
    raise SystemExit("explicit schedule task proposal must preserve daily cron")
PY
  then
    printf 'ok: explicit schedule task must return schedule proposal\n'
    return 0
  fi
  printf 'fail: explicit schedule task must return schedule proposal\n' >&2
  failures=$((failures + 1))
  return 1
}

run_project_preflight_approval_guard_profile() {
  local python_bin
  local request_body
  local response
  local parsed
  local run_id
  local decision_token
  local version
  local approval_body
  local approval_response
  local runs_path="/api/v1/runs"

  printf 'profile: authenticated project preflight approval guard\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: project preflight approval guard is disabled in read-only mode\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: project preflight approval guard requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: project preflight approval guard requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! request_body="$("$python_bin" - <<'PY'
import json

print(json.dumps({
    "message": "需要构建一个大型项目，从需求拆解、架构设计、分阶段实现到生产结果全部完成",
    "mode": "auto",
    "sandbox_profile": "none",
    "requested_permissions": [],
    "skip_evolution_proposal": True,
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build project preflight run body\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: project-preflight-guard-$(date +%s)-$$" \
    -d "$request_body" \
    "$base_url$runs_path" 2>/dev/null)"; then
    printf 'fail: project preflight run create /api/v1/runs\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  if ! parsed="$(ACCEPTANCE_RESPONSE="$response" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
proposal = payload.get("project_preflight_proposal")
if payload.get("status") != "waiting_approval":
    raise SystemExit("project preflight run must wait for approval")
if payload.get("clarification_reason") != "project_preflight_requires_user_approval":
    raise SystemExit("project preflight run must expose approval reason")
if payload.get("mode") != "hybrid":
    raise SystemExit("project preflight run must use hybrid mode")
if not isinstance(proposal, dict):
    raise SystemExit("project preflight run must return proposal")
if proposal.get("kind") != "project_architecture_preflight":
    raise SystemExit("project preflight proposal kind mismatch")
if proposal.get("capability") != "project.preflight_architecture":
    raise SystemExit("project preflight proposal capability mismatch")
if proposal.get("requires_constraints_and_skills_reading") is not True:
    raise SystemExit("project preflight proposal must require constraints reading")
if proposal.get("plan_path") != "PROJECT_ARCHITECTURE_PLAN.md":
    raise SystemExit("project preflight proposal plan path mismatch")
if proposal.get("graph_path") != "architecture-map.html":
    raise SystemExit("project preflight proposal graph path mismatch")
print("\t".join([payload["id"], payload["decision_token"], str(payload["version"])]))
PY
  )"; then
    printf 'fail: project preflight run must wait for approval\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  IFS=$'\t' read -r run_id decision_token version <<< "$parsed"
  printf 'ok: project preflight proposal must require constraints reading\n'

  if ! approval_body="$(ACCEPTANCE_DECISION_TOKEN="$decision_token" ACCEPTANCE_VERSION="$version" "$python_bin" - <<'PY'
import json
import os

print(json.dumps({
    "decision_token": os.environ["ACCEPTANCE_DECISION_TOKEN"],
    "version": int(os.environ["ACCEPTANCE_VERSION"]),
}, ensure_ascii=False))
PY
  )"; then
    printf 'fail: could not build project preflight approval body\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! approval_response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -X POST \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -d "$approval_body" \
    "$base_url/api/v1/runs/$run_id/approve-project-preflight" 2>/dev/null)"; then
    printf 'fail: project preflight approval /api/v1/runs/%s/approve-project-preflight\n' "$run_id" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$approval_response" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
if payload.get("status") != "queued":
    raise SystemExit("project preflight approval enqueues planned run")
if payload.get("mode") != "hybrid":
    raise SystemExit("project preflight approval must preserve hybrid mode")
PY
  then
    printf 'ok: project preflight approval enqueues planned run\n'
  else
    printf 'fail: project preflight approval enqueues planned run\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  post_run_control_expect_status \
    "$python_bin" \
    "$run_id" \
    "cancel" \
    "cancelled" \
    "project preflight guard cleanup cancel reaches cancelled" || return 1
}

run_strict_interaction_recovery_profile() {
  local python_bin
  local request_body
  local response

  printf 'profile: strict interaction recovery\n'
  if [[ -z "$bearer_token" ]]; then
    printf 'fail: strict interaction recovery requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: strict interaction recovery requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! request_body="$("$python_bin" - <<'PY'
import json

print(json.dumps({"quota_scope": "harness_acceptance_strict", "desired_concurrency": 32}))
PY
  )"; then
    printf 'fail: could not build strict model probe request body\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! response="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -fsS \
    -H "Authorization: Bearer $bearer_token" \
    -H "Content-Type: application/json" \
    -d "$request_body" \
    "$base_url/api/v1/admin/models/probe" 2>/dev/null)"; then
    printf 'fail: strict model probe interaction control /api/v1/admin/models/probe\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ACCEPTANCE_RESPONSE="$response" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["ACCEPTANCE_RESPONSE"])
recommended = payload.get("recommended_concurrency")
warning = payload.get("warning")
if not isinstance(recommended, int) or recommended < 1 or recommended > 32:
    raise SystemExit(1)
if not isinstance(warning, str) or not warning.strip():
    raise SystemExit(1)
PY
  then
    printf 'ok: strict model probe interaction control\n'
    return 0
  fi
  printf 'fail: strict model probe interaction control invalid response\n' >&2
  failures=$((failures + 1))
  return 1
}

stress_worker() {
  local worker="$1"
  local path
  local index
  for ((index = 1; index <= iterations; index += 1)); do
    for path in /health /health/live /health/ready /metrics /openapi.json /login; do
      curl --noproxy '*' \
        --connect-timeout "$connect_timeout" \
        --max-time "$max_time" \
        -fsS -o /dev/null \
        "$base_url$path" 2>/dev/null || {
          printf 'stress-fail: worker=%s iteration=%s path=%s\n' "$worker" "$index" "$path" >&2
          return 1
        }
    done
  done
}

run_stress_profile() {
  local pids=()
  local worker
  local failed=0
  printf 'profile: bounded stress concurrency=%s iterations=%s\n' "$concurrency" "$iterations"
  for ((worker = 1; worker <= concurrency; worker += 1)); do
    stress_worker "$worker" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      failed=1
    fi
  done
  if [[ "$failed" -eq 0 ]]; then
    printf 'ok: stress profile completed\n'
    return 0
  fi
  printf 'fail: stress profile completed with failures\n'
  failures=$((failures + 1))
  return 1
}

run_release_verification_profile() {
  local script_dir
  local launcher
  local args=(verify-release --install-root "$install_root")
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  launcher="$script_dir/../agent-hub"
  printf 'profile: native release verification\n'
  if [[ -n "$expect_revision" ]]; then
    args+=(--expect-revision "$expect_revision")
  fi
  if "$launcher" "${args[@]}"; then
    return 0
  fi
  printf 'fail: native release verification\n' >&2
  failures=$((failures + 1))
  return 1
}

require_curl
if [[ "$read_only" -eq 1 ]]; then
  printf 'mode: read-only\n'
else
  printf 'mode: write-probes-enabled\n'
fi

case "$profile" in
  codex)
    run_codex_profile
    run_openapi_capability_profile || true
    run_lifecycle_profile || true
    run_create_idempotency_replay_guard_profile || true
    run_control_idempotency_guard_profile || true
    run_schedule_interaction_guard_profile || true
    run_project_preflight_approval_guard_profile || true
    ;;
  deepseek)
    run_deepseek_profile
    run_openapi_capability_profile || true
    ;;
  all)
    run_codex_profile
    run_deepseek_profile
    run_openapi_capability_profile || true
    run_lifecycle_profile || true
    run_create_idempotency_replay_guard_profile || true
    run_control_idempotency_guard_profile || true
    run_schedule_interaction_guard_profile || true
    run_project_preflight_approval_guard_profile || true
    ;;
esac

if [[ "$stress" -eq 1 ]]; then
  run_stress_profile || true
fi

if [[ "$strict_interaction_recovery" -eq 1 ]]; then
  run_strict_interaction_recovery_profile || true
fi

if [[ "$verify_release" -eq 1 ]]; then
  run_release_verification_profile || true
fi

if [[ "$failures" -gt 0 ]]; then
  printf 'harness acceptance failed: %s check(s) failed\n' "$failures" >&2
  exit 1
fi

printf 'harness acceptance passed profile=%s stress=%s base_url=%s\n' "$profile" "$stress" "$base_url"
