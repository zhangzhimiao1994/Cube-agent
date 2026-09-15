#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${AGENT_HUB_ACCEPTANCE_BASE_URL:-http://127.0.0.1:8000}"
public_url="${AGENT_HUB_ACCEPTANCE_PUBLIC_URL:-}"
profile="all"
stress=0
strict_interaction_recovery="${AGENT_HUB_ACCEPTANCE_STRICT_INTERACTION_RECOVERY:-0}"
project_scale_execute_profile="${AGENT_HUB_PROJECT_SCALE_EXECUTE_PROFILE:-0}"
project_scale_scales="${AGENT_HUB_PROJECT_SCALE_PROFILE_SCALES:-${AGENT_HUB_PROJECT_SCALE_PROFILE_SCALE:-small}}"
project_scale_flows="${AGENT_HUB_PROJECT_SCALE_PROFILE_FLOWS:-${AGENT_HUB_PROJECT_SCALE_PROFILE_FLOW:-direct}}"
project_scale_wait_seconds="${AGENT_HUB_PROJECT_SCALE_WAIT_SECONDS:-120}"
project_scale_poll_interval="${AGENT_HUB_PROJECT_SCALE_POLL_INTERVAL_SECONDS:-2}"
project_scale_report_path="${AGENT_HUB_PROJECT_SCALE_REPORT_PATH:-}"
read_only=0
stress_profile="${AGENT_HUB_ACCEPTANCE_STRESS_PROFILE:-custom}"
concurrency="${AGENT_HUB_ACCEPTANCE_CONCURRENCY:-4}"
iterations="${AGENT_HUB_ACCEPTANCE_ITERATIONS:-10}"
concurrency_explicit=0
iterations_explicit=0
if [[ -n "${AGENT_HUB_ACCEPTANCE_CONCURRENCY:-}" ]]; then
  concurrency_explicit=1
fi
if [[ -n "${AGENT_HUB_ACCEPTANCE_ITERATIONS:-}" ]]; then
  iterations_explicit=1
fi
connect_timeout="${AGENT_HUB_ACCEPTANCE_CONNECT_TIMEOUT_SECONDS:-5}"
max_time="${AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS:-20}"
retries="${AGENT_HUB_ACCEPTANCE_RETRIES:-3}"
retry_delay="${AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS:-2}"
ready_timeout="${AGENT_HUB_ACCEPTANCE_READY_TIMEOUT_SECONDS:-45}"
ready_poll_interval="${AGENT_HUB_ACCEPTANCE_READY_POLL_INTERVAL_SECONDS:-2}"
bearer_token="${AGENT_HUB_ACCEPTANCE_BEARER_TOKEN:-}"
configured_bearer_token="$bearer_token"
acceptance_login_username="${AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME:-}"
acceptance_login_password="${AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD:-}"
acceptance_login_tenant_id="${AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID:-}"
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
Alternatively set AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME and
AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD to acquire a short-lived bearer token
through /api/v1/auth/login for authenticated acceptance probes. Set
AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID when the target tenant is not the
configured bootstrap tenant.
Set AGENT_HUB_PROJECT_SCALE_EXECUTE_PROFILE=1 with a bearer token to run
the authenticated bounded project-scale execution runner.

Options:
  --base-url URL                 Base URL to test.
  --public-url URL               Optional public/Caddy URL to test for /login and /health/ready.
  --profile codex|deepseek|all|production-safe
                                 Acceptance profile to run. production-safe runs all profiles in read-only mode.
  --read-only                    Skip runtime write probes; keep GET probes, OpenAPI contracts, and stress.
  --strict-interaction-recovery  Run authenticated, non-mutating interaction recovery probes; requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN.
  --stress                       Run bounded HTTP stress checks.
  --stress-profile smoke|standard|heavy|endurance|custom
                                 Run a named stress scale. Defaults to AGENT_HUB_ACCEPTANCE_STRESS_PROFILE or custom.
                                 smoke=4x5, standard=8x10, heavy=16x20, endurance=32x50.
                                 Named profiles enable --stress. Explicit concurrency/iterations override profile defaults.
  --concurrency N                Stress workers. Defaults to AGENT_HUB_ACCEPTANCE_CONCURRENCY or 4.
  --iterations N                 Requests per worker. Defaults to AGENT_HUB_ACCEPTANCE_ITERATIONS or 10.
  --connect-timeout SECONDS      Curl connect timeout. Defaults to 5.
  --max-time SECONDS             Curl total request timeout. Defaults to 20.
  --retries N                    Attempts per smoke URL. Defaults to AGENT_HUB_ACCEPTANCE_RETRIES or 3.
  --retry-delay SECONDS          Delay between URL attempts. Defaults to AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS or 2.
  Set AGENT_HUB_ACCEPTANCE_READY_TIMEOUT_SECONDS and
  AGENT_HUB_ACCEPTANCE_READY_POLL_INTERVAL_SECONDS to tune startup readiness warmup.
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
    --public-url)
      public_url="${2:?missing value for --public-url}"
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
    --stress-profile)
      stress_profile="${2:?missing value for --stress-profile}"
      shift 2
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
      concurrency_explicit=1
      shift 2
      ;;
    --iterations)
      iterations="${2:?missing value for --iterations}"
      iterations_explicit=1
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

if [[ -n "$public_url" ]]; then
  case "$public_url" in
    http://*|https://*) public_url="${public_url%/}" ;;
    *)
      printf 'invalid --public-url: %s\n' "$public_url" >&2
      exit 2
      ;;
  esac
fi

positive_int() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

set_stress_defaults() {
  local default_concurrency="$1"
  local default_iterations="$2"
  if [[ "$concurrency_explicit" -eq 0 ]]; then
    concurrency="$default_concurrency"
  fi
  if [[ "$iterations_explicit" -eq 0 ]]; then
    iterations="$default_iterations"
  fi
}

case "$stress_profile" in
  smoke) stress=1; set_stress_defaults 4 5 ;;
  standard) stress=1; set_stress_defaults 8 10 ;;
  heavy) stress=1; set_stress_defaults 16 20 ;;
  endurance) stress=1; set_stress_defaults 32 50 ;;
  custom) ;;
  *)
    printf 'invalid --stress-profile: %s\n' "$stress_profile" >&2
    exit 2
    ;;
esac

if ! positive_int "$concurrency" \
  || ! positive_int "$iterations" \
  || ! positive_int "$retries" \
  || ! positive_int "$ready_timeout" \
  || ! positive_int "$ready_poll_interval"; then
  printf 'concurrency, iterations, retries, and readiness warmup values must be positive integers\n' >&2
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

resolve_acceptance_bearer_token() {
  local python_bin
  local login_body_file
  local login_response_file
  local login_path="/api/v1/auth/login"
  local login_status
  local acquired_token

  if [[ -n "$bearer_token" ]]; then
    return 0
  fi
  if [[ -z "$acceptance_login_username" && -z "$acceptance_login_password" && -z "$acceptance_login_tenant_id" ]]; then
    return 0
  fi
  if [[ -z "$acceptance_login_username" || -z "$acceptance_login_password" ]]; then
    printf 'fail: acceptance login requires AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME and AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: acceptance login requires python for JSON handling\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  login_body_file="$(mktemp)"
  login_response_file="$(mktemp)"
  if ! AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME="$acceptance_login_username" \
    AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD="$acceptance_login_password" \
    AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID="$acceptance_login_tenant_id" \
    "$python_bin" - "$login_body_file" <<'PY'
import json
import os
import sys

body = {
    "username": os.environ["AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME"],
    "password": os.environ["AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD"],
}
tenant_id = os.environ.get("AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID", "")
if tenant_id:
    body["tenant_id"] = tenant_id
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(body, handle, ensure_ascii=False)
PY
  then
    rm -f -- "$login_body_file" "$login_response_file"
    printf 'fail: could not build acceptance login request body\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  login_status="$(curl --noproxy '*' \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    -sS \
    -H "Content-Type: application/json" \
    --data-binary "@$login_body_file" \
    -o "$login_response_file" \
    -w '%{http_code}' \
    "$base_url$login_path" 2>/dev/null || true)"
  rm -f -- "$login_body_file"
  if [[ "$login_status" != "200" ]]; then
    rm -f -- "$login_response_file"
    printf 'fail: acceptance login /api/v1/auth/login -> %s\n' "${login_status:-curl-error}" >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! acquired_token="$(ACCEPTANCE_LOGIN_RESPONSE_FILE="$login_response_file" "$python_bin" - <<'PY'
import json
import os

with open(os.environ["ACCEPTANCE_LOGIN_RESPONSE_FILE"], encoding="utf-8") as handle:
    payload = json.load(handle)
token = payload.get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit(1)
print(token)
PY
  )"; then
    rm -f -- "$login_response_file"
    printf 'fail: acceptance login response missing access_token\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  rm -f -- "$login_response_file"
  bearer_token="$acquired_token"
  acquired_token=""
  printf 'ok: acceptance login token acquired\n'
}

check_acceptance_credential_readiness() {
  printf 'profile: acceptance credential readiness\n'
  if [[ -n "$configured_bearer_token" ]]; then
    printf 'ok: acceptance credential readiness bearer_token=set\n'
    return 0
  fi
  if [[ -n "$acceptance_login_username" && -n "$acceptance_login_password" && -n "$bearer_token" ]]; then
    printf 'ok: acceptance credential readiness login_credentials=set bearer_token=acquired\n'
    return 0
  fi
  if [[ -n "$acceptance_login_username" || -n "$acceptance_login_password" || -n "$acceptance_login_tenant_id" ]]; then
    printf 'skip: acceptance credential readiness login credentials configured but bearer unavailable\n'
    return 0
  fi
  printf 'skip: acceptance credential readiness no bearer or login credentials configured\n'
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

check_public_url() {
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
      "$public_url$path" 2>/dev/null)" && [[ "$status" == "$expected" ]]; then
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

run_public_entrypoint_profile() {
  if [[ -z "$public_url" ]]; then
    return 0
  fi
  printf 'profile: public entrypoint\n'
  check_public_url "public management ui entry" "/login"
  check_public_url "public readiness boundary" "/health/ready"
}

check_health_json() {
  local name="$1"
  local path="$2"
  local python_bin
  local response
  local attempt
  local last_status="curl-error"
  if ! python_bin="$(detect_python)"; then
    printf 'fail: %s %s requires python for JSON handling\n' "$name" "$path" >&2
    failures=$((failures + 1))
    return 1
  fi
  for ((attempt = 1; attempt <= retries; attempt += 1)); do
    if response="$(curl --noproxy '*' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -fsS \
      "$base_url$path" 2>/dev/null)"; then
      if ACCEPTANCE_RESPONSE="$response" "$python_bin" -c 'import json, os, sys; sys.exit(0 if json.loads(os.environ["ACCEPTANCE_RESPONSE"]).get("status") == "ok" else 1)' 2>/dev/null; then
        printf 'ok: %s %s JSON status=ok\n' "$name" "$path"
        return 0
      fi
      last_status="JSON status!=ok"
    else
      last_status="curl-error"
    fi
    if [[ "$attempt" -lt "$retries" ]]; then
      sleep "$retry_delay"
    fi
  done
  printf 'fail: %s %s %s\n' "$name" "$path" "$last_status" >&2
  failures=$((failures + 1))
  return 1
}

wait_for_readiness() {
  local status=""
  local started="$SECONDS"
  printf 'profile: readiness warmup\n'
  while true; do
    status="$(curl --noproxy '*' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -sS -o /dev/null -w '%{http_code}' \
      "$base_url/health/ready" 2>/dev/null || true)"
    if [[ "$status" == "200" ]]; then
      printf 'ok: readiness warmup /health/ready -> %s\n' "$status"
      return 0
    fi
    if ((SECONDS - started >= ready_timeout)); then
      printf 'fail: readiness warmup /health/ready -> %s\n' "${status:-curl-error}" >&2
      failures=$((failures + 1))
      return 1
    fi
    sleep "$ready_poll_interval"
  done
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

def property_type_matches(prop, expected_type):
    if prop.get("type") == expected_type:
        return True
    any_of = prop.get("anyOf")
    if not isinstance(any_of, list):
        return False
    return any(
        isinstance(branch, dict) and branch.get("type") == expected_type
        for branch in any_of
    )

def property_minimum_matches(prop, expected_type, minimum):
    if minimum is None:
        return True
    if prop.get("type") == expected_type:
        return prop.get("minimum") == minimum
    any_of = prop.get("anyOf")
    if not isinstance(any_of, list):
        return False
    for branch in any_of:
        if isinstance(branch, dict) and branch.get("type") == expected_type:
            return branch.get("minimum") == minimum
    return False

request_schema = schemas.get("ProbeRequest", {})
request_properties = request_schema.get("properties", {})
request_expected = {
    "desired_concurrency": ("integer", 1),
    "target_utilization": ("number", 0.1),
    "reserved_capacity": ("integer", 0),
}
for name, (expected_type, minimum) in request_expected.items():
    prop = request_properties.get(name)
    if not isinstance(prop, dict):
        raise SystemExit(1)
    if not property_type_matches(prop, expected_type):
        raise SystemExit(1)
    if not property_minimum_matches(prop, expected_type, minimum):
        raise SystemExit(1)

response_schema = schemas.get("ProbeResponse", {})
response_properties = response_schema.get("properties", {})
response_expected = {
    "recommended_concurrency": ("integer", None),
    "warning": ("string", None),
}
for name, (expected_type, minimum) in response_expected.items():
    prop = response_properties.get(name)
    if not isinstance(prop, dict):
        raise SystemExit(1)
    if not property_type_matches(prop, expected_type):
        raise SystemExit(1)
    if not property_minimum_matches(prop, expected_type, minimum):
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
    (
        "Plugin endpoint unavailable",
        "plugin_runtime",
        "endpoint_unavailable",
        "plugin.endpoint_unavailable",
        True,
    ),
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
if "实现阶段执行契约" not in preflight_plan:
    raise SystemExit(1)
if "阶段自修复闭环" not in preflight_plan or "`stage_repair_actions`" not in preflight_plan:
    raise SystemExit(1)
if "阶段验收和风险回收" not in preflight_plan:
    raise SystemExit(1)
if "约束读取" not in preflight_graph:
    raise SystemExit(1)
if "阶段契约" not in preflight_graph:
    raise SystemExit(1)
if "自修复闭环" not in preflight_graph:
    raise SystemExit(1)
if "风险回收" not in preflight_graph:
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
    "项目每完成一个较大的功能，罗列任务计划和完成情况。",
    "以后罗列清单之后，不要停止任务，一边开展任务，一边罗列",
    "不打断任务，给我罗列一下当前计划完成情况，然后罗列之后继续任务",
    "完成之后按流程继续就行了，不用再问我",
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
    ("Plugin endpoint unavailable", "plugin.endpoint_unavailable", True),
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

check_self_repair_failure_injection_matrix() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: self-repair failure injection matrix\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: self-repair failure injection matrix requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from uuid import uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    repair_context_from_proposal,
)
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import RECOVERY_BLOCKED_FAILURE_REASON


def require(value, label):
    if not value:
        raise SystemExit(label)


def classify(events, expected_category, expected_strategy):
    run_id = uuid4()
    normalized_events = tuple(
        event if event.run_id == run_id else event.model_copy(update={"run_id": run_id})
        for event in events
    )
    decision = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=TaskMode.HYBRID,
        routing_decision={"source": "manual"},
        events=normalized_events,
        policy=SelfRepairPolicy(requires_approval=False),
    )
    require(decision is not None, f"{expected_category} decision")
    require(decision.kind == "repair.classified", f"{expected_category} classified")
    require(decision.failure_category == expected_category, f"{expected_category} category")
    require(decision.recovery_strategy == expected_strategy, f"{expected_category} strategy")
    require(decision.requires_approval is False, f"{expected_category} approval")
    require(decision.automatic_execution is True, f"{expected_category} automatic")
    proposal = decision.to_proposal(run_id=run_id)
    require(proposal is not None, f"{expected_category} proposal")
    require(proposal.get("failure_kind") == expected_category, f"{expected_category} proposal kind")
    require(proposal.get("recovery_strategy") == expected_strategy, f"{expected_category} proposal strategy")
    context = repair_context_from_proposal(proposal)
    require(context.get("failure_kind") == expected_category, f"{expected_category} context kind")
    require(context.get("recovery_strategy") == expected_strategy, f"{expected_category} context strategy")
    serialized = repr({"proposal": proposal, "context": context})
    require("secret://token" not in serialized, f"{expected_category} secret redaction")
    require("Authorization: Bearer" not in serialized, f"{expected_category} auth redaction")


base_run_id = uuid4()
cases = (
    (
        (
            RunEvent(
                kind=EventKind.TOOL_FAILED,
                sequence=1,
                run_id=base_run_id,
                actor="tool_runner",
                tool_call_id="call_read_file",
                tool_name="filesystem.read_file",
                reason="tool failed after timeout secret://token",
            ),
        ),
        "tool_failure", "repair_tool_invocation_after_permission_check",
    ),
    (
        (
            RunEvent(
                kind=EventKind.STEP_FAILED,
                sequence=1,
                run_id=base_run_id,
                step_id="builder_step",
                actor="builder",
                reason="step crashed after context compaction Authorization: Bearer token",
            ),
        ),
        "step_failure", "retry_failed_step_after_context_compaction",
    ),
    (
        (),
        "missing_failure_event", "manual_review_missing_failure_event",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason=RECOVERY_BLOCKED_FAILURE_REASON,
            ),
        ),
        "runtime_recovery_blocked", "manual_review_recovery_checkpoint",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="model.provider_rate_limited",
            ),
        ),
        "capacity_pressure", "switch_to_available_model_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="model.provider_unavailable",
            ),
        ),
        "capacity_pressure", "switch_to_available_model_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="model.provider_transient_failed",
            ),
        ),
        "capacity_pressure", "switch_to_available_model_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="Plugin endpoint unavailable",
            ),
        ),
        "plugin_runtime_unavailable", "repair_plugin_endpoint_or_adapter_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="Plugin tool timed out",
            ),
        ),
        "plugin_runtime_unavailable", "repair_plugin_endpoint_or_adapter_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="mcp_server_timeout",
            ),
        ),
        "mcp_runtime_unavailable", "repair_mcp_server_or_adapter_and_retry",
    ),
    (
        (
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=base_run_id,
                reason="MCP tool timed out",
            ),
        ),
        "mcp_runtime_unavailable", "repair_mcp_server_or_adapter_and_retry",
    ),
)

for events, category, strategy in cases:
    classify(events, category, strategy)

manual_cases = (
    (
        "model.provider_auth_failed secret://model-token",
        "model_credential_unavailable",
        "manual_review_model_credentials",
    ),
    (
        "model credential resolution failed secret://model-token",
        "model_credential_unavailable",
        "manual_review_model_credentials",
    ),
    (
        "model.provider_quota_or_billing_failed secret://model-token",
        "model_quota_or_billing_unavailable",
        "manual_review_model_quota_or_billing",
    ),
    (
        "model.provider_model_not_found secret://model-token",
        "model_deployment_unavailable",
        "manual_review_model_deployment",
    ),
    (
        "model.provider_bad_request secret://model-token",
        "model_request_contract_invalid",
        "manual_review_model_request_contract",
    ),
    (
        "Plugin credential unavailable secret://plugin-token",
        "plugin_credential_unavailable",
        "manual_review_plugin_credentials",
    ),
    (
        "Plugin arguments do not match input schema: invalid type secret://plugin-token",
        "plugin_invalid_arguments",
        "manual_review_plugin_arguments",
    ),
    (
        "Plugin result does not match output schema: invalid type secret://plugin-token",
        "plugin_invalid_result",
        "manual_review_plugin_result_contract",
    ),
    (
        "Plugin sandbox profile unsupported secret://plugin-token",
        "plugin_sandbox_unsupported",
        "manual_review_plugin_sandbox",
    ),
    (
        "MCP tool unavailable secret://plugin-token",
        "mcp_tool_unavailable",
        "manual_review_mcp_configuration",
    ),
    (
        "mcp_server_not_discovered secret://plugin-token",
        "mcp_server_not_discovered",
        "manual_review_mcp_configuration",
    ),
)

for reason, category, strategy in manual_cases:
    manual_run_id = uuid4()
    manual_repair = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=TaskMode.HYBRID,
        routing_decision={"source": "manual"},
        events=(
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=manual_run_id,
                reason=reason,
            ),
        ),
        policy=SelfRepairPolicy(requires_approval=False),
    )
    require(manual_repair is not None, f"{category} decision")
    require(manual_repair.failure_category == category, f"{category} category")
    require(manual_repair.recovery_strategy == strategy, f"{category} strategy")
    require(manual_repair.requires_approval is True, f"{category} approval")
    require(manual_repair.automatic_execution is False, f"{category} must not auto execute")
    manual_proposal = manual_repair.to_proposal(run_id=manual_run_id)
    require(manual_proposal is not None, f"{category} proposal")
    require(manual_proposal.get("requires_approval") is True, f"{category} proposal approval")
    require(manual_proposal.get("automatic_execution") is False, f"{category} proposal automatic")
    require("secret://plugin-token" not in repr(manual_proposal), f"{category} secret redaction")
    require("secret://model-token" not in repr(manual_proposal), f"{category} model secret redaction")
PY
  then
    printf 'ok: self-repair failure injection matrix\n'
    return 0
  fi
  printf 'fail: self-repair failure injection matrix\n' >&2
  failures=$((failures + 1))
  return 1
}

check_running_recovery_contract() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: running recovery guard\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: running recovery guard requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService, SubmittedRun
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry

logging.getLogger("agent_hub.runs.service").setLevel(logging.CRITICAL)


def require(value, label):
    if not value:
        raise SystemExit(label)


class RecoverableRunRepository:
    def __init__(self, candidates: tuple[UUID, ...]) -> None:
        self._candidates = candidates

    async def running_for_recovery(self, limit: int, *, now: datetime) -> tuple[UUID, ...]:
        del now
        return self._candidates[:limit]


class RecoverRunningService(RunService):
    def __init__(
        self,
        *,
        recoverable_run_repository: RecoverableRunRepository,
        recovered: list[UUID],
        failing_run_id: UUID,
        active_run_id: UUID | None = None,
    ) -> None:
        super().__init__(
            cast(RunRepository, recoverable_run_repository),
            runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
            router=None,
            task_queue=UnusedQueue(),
        )
        self._recovered = recovered
        self._failing_run_id = failing_run_id
        self._active_run_id = active_run_id

    async def recover(self, run_id: UUID) -> SubmittedRun:
        if run_id == self._failing_run_id:
            raise RuntimeError("synthetic recovery failure")
        if run_id == self._active_run_id:
            return SubmittedRun(
                id=run_id,
                tenant_id=uuid4(),
                status=RunStatus.RUNNING,
                mode=TaskMode.DISPATCH,
                decision_token=None,
                version=1,
            )
        self._recovered.append(run_id)
        return SubmittedRun(
            id=run_id,
            tenant_id=uuid4(),
            status=RunStatus.COMPLETED,
            mode=TaskMode.DISPATCH,
            decision_token=None,
            version=1,
        )


class UnusedRuntime:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        raise AssertionError("recover_running should call the patched recover method")
        yield  # pragma: no cover

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class UnusedQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del run_id, idempotency_key
        raise AssertionError("not used")


async def main() -> None:
    recovered: list[UUID] = []
    first = uuid4()
    second = uuid4()
    third = uuid4()
    service = RecoverRunningService(
        recoverable_run_repository=RecoverableRunRepository((first, second, third)),
        recovered=recovered,
        failing_run_id=second,
    )
    count = await service.recover_running(limit=10)
    require(count == 2, "recover_running must continue after one candidate fails")
    require(recovered == [first, third], "failed candidate must not stop later recovery")

    recovered = []
    active = uuid4()
    stale = uuid4()
    service = RecoverRunningService(
        recoverable_run_repository=RecoverableRunRepository((active, stale)),
        recovered=recovered,
        failing_run_id=uuid4(),
        active_run_id=active,
    )
    count = await service.recover_running(limit=10)
    require(count == 1, "active race must not count as recovered")
    require(recovered == [stale], "stale candidate must still recover")


asyncio.run(main())
PY
  then
    printf 'ok: running recovery guard\n'
    return 0
  fi
  printf 'fail: running recovery guard\n' >&2
  failures=$((failures + 1))
  return 1
}

check_model_capability_recovery_contract() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: model capability recovery\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: model capability recovery requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_hub.api.routers.admin import RunDetailResponse, _admin_run_event
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from agent_hub.runs.observer import RunMonitor
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    repair_context_from_proposal,
)
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.self_repair_context import (
    self_repair_context_text,
    self_repair_recovery_plan_payload,
    self_repair_role_capability_requirements,
)


def require(value, label):
    if not value:
        raise SystemExit(label)


class CapacityStub:
    def __init__(self):
        self.configured = ()
        self.acquire_calls = []
        self.records = []

    def validate_configuration(self, deployments):
        self.configured = tuple(deployments)

    async def initialize(self):
        return None

    async def acquire(self, candidates, wait_timeout, *, estimated_tokens):
        self.acquire_calls.append(
            (tuple(candidate.id for candidate in candidates), wait_timeout, estimated_tokens)
        )
        selected = candidates[0]
        return CapacityLease(
            id=str(uuid4()),
            deployment_id=selected.id,
            quota_scope_id=selected.quota_scope_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            renew_after_seconds=1,
        )

    async def release(self, lease):
        return True

    async def renew(self, lease):
        return lease

    async def record_outcome(self, quota_scope_id, *, status_code, latency_seconds, succeeded):
        self.records.append((quota_scope_id, status_code, succeeded))


class SecretStub:
    async def resolve(self, secret_ref):
        return "safe-key"


class TransportStub:
    def __init__(self):
        self.calls = []

    async def complete(self, deployment, request, api_key):
        del request, api_key
        self.calls.append(deployment.id)
        return ModelResponse(text="ok")


async def verify_gateway_capability_fallback():
    primary = Deployment(
        id="primary_model",
        logical_model="primary",
        provider_model="deepseek/deepseek-chat",
        secret_ref="secret://primary",
        quota_scope_id="scope_primary",
        capabilities=frozenset({ModelCapability.TEXT}),
    )
    backup = Deployment(
        id="backup_model",
        logical_model="backup",
        provider_model="openai/gpt-5",
        secret_ref="secret://backup",
        quota_scope_id="scope_backup",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
    )
    capacity = CapacityStub()
    transport = TransportStub()
    gateway = ModelGateway(
        ModelRegistry((primary, backup)),
        capacity,
        SecretStub(),
        transport,
        fallbacks={"primary": "backup"},
    )
    completion = await gateway.complete_with_context(
        ModelRequest(
            logical_model="primary",
            messages=(ModelMessage(role="user", content="needs a tool"),),
            required_capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        )
    )
    require(completion.deployment_id == "backup_model", "capable fallback deployment")
    require(completion.logical_model == "backup", "capable fallback logical model")
    require(completion.fallback_used is True, "capability fallback used")
    require(completion.fallback_from_logical_model == "primary", "capability fallback source")
    require(completion.fallback_reason == "capability_unavailable", "capability fallback reason")
    require(completion.attempted_logical_models == ("primary", "backup"), "capability attempts")
    require(transport.calls == ["backup_model"], "primary must not be invoked without capability")
    require(capacity.acquire_calls[0][0] == ("backup_model",), "capacity only sees capable fallback")


asyncio.run(verify_gateway_capability_fallback())

run_id = uuid4()
failure = RunEvent(
    kind=EventKind.STEP_FAILED,
    sequence=2,
    run_id=run_id,
    actor="scheduler",
    step_id="scheduler_step",
    reason="planned capability is unavailable",
    payload={"error_code": "capability.planned_unavailable"},
)
observer = RunMonitor().observe(failure)
require(observer is not None, "model capability observer decision")
require(observer.trigger == "model_capability_routing_unavailable", "observer trigger")
require(observer.action == "reassign_tool_role_to_capable_model", "observer action")
require(
    observer.recommendation == "reassign_tool_role_to_capable_model_and_retry",
    "observer recommendation",
)
observer_event = observer.to_event(run_id=run_id, sequence=3)

negotiation = RunEvent(
    kind=EventKind.STEP_STARTED,
    sequence=1,
    run_id=run_id,
    actor="main_agent",
    step_id="main_agent_plan",
    payload={
        "model_execution_plan": {
            "model_capability_negotiation": {
                "schema_version": 1,
                "items": (
                    {
                        "role_id": "scheduler",
                        "logical_model": "main",
                        "required_capabilities": (
                            "text",
                            "structured_output",
                            "tool_calling",
                        ),
                        "missing_capabilities": ("tool_calling",),
                        "status": "missing_capability",
                    },
                    {
                        "role_id": "../unsafe",
                        "logical_model": "secret://model",
                        "required_capabilities": ("tool_calling",),
                        "missing_capabilities": ("tool_calling",),
                        "status": "missing_capability",
                    },
                ),
            }
        }
    },
)
repair = classify_terminal_run(
    status=RunStatus.FAILED,
    mode=TaskMode.DISPATCH,
    routing_decision={"source": "manual"},
    events=(negotiation, failure, observer_event),
    policy=SelfRepairPolicy(requires_approval=False),
)
require(repair is not None, "model capability repair")
require(repair.failure_category == "model_capability_routing_unavailable", "repair category")
require(
    repair.recovery_strategy == "reassign_tool_role_to_capable_model_and_retry",
    "repair strategy",
)
require(repair.requires_approval is False, "repair approval policy")
require(repair.automatic_execution is True, "repair automatic execution")

proposal = repair.to_proposal(run_id=run_id)
require(proposal is not None, "repair proposal")
require(proposal.get("failure_kind") == "model_capability_routing_unavailable", "proposal failure kind")
require(
    proposal.get("recovery_strategy") == "reassign_tool_role_to_capable_model_and_retry",
    "proposal recovery strategy",
)
require(
    proposal.get("role_capability_requirements")
    == (
        {
            "role_id": "scheduler",
            "required_capabilities": (
                "text",
                "structured_output",
                "tool_calling",
            ),
        },
    ),
    "bounded role requirements",
)
serialized_proposal = repr(proposal)
require("../unsafe" not in serialized_proposal, "unsafe role must be filtered")
require("secret://model" not in serialized_proposal, "unsafe model must be filtered")
require("planned capability is unavailable" not in serialized_proposal, "raw failure must be filtered")

routing_decision = {
    "source": "self_repair",
    "self_repair_context": repair_context_from_proposal(proposal),
}
context = self_repair_context_text(routing_decision)
require("reassign_tool_role_to_capable_model_and_retry" in context, "context strategy")
require("../unsafe" not in context and "secret://model" not in context, "context redaction")

plan = self_repair_recovery_plan_payload(routing_decision)
require(plan is not None, "recovery plan")
require(plan["replan_scope"] == "model_capability_roles", "replan scope")
require(plan["reuse_completed_artifacts"] is True, "reuse completed artifacts")
require(plan["automatic_execution"] is True, "automatic execution")
require(
    plan["role_capability_requirements"] == proposal["role_capability_requirements"],
    "plan role requirements",
)
requirements = self_repair_role_capability_requirements(routing_decision)
require("scheduler" in requirements, "parsed role requirements")
require("tool_calling" in {capability.value for capability in requirements["scheduler"]}, "tool capability")

admin_event = _admin_run_event(
    {
        "sequence": 4,
        "kind": "step.started",
        "message": "model capability self-repair summary",
        "created_at": datetime.now(UTC),
        "actor": "main_agent",
        "step_id": "self_repair_plan",
        "payload": {
            "model_execution_plan": {
                "self_repair_recovery": {
                    **plan,
                    "credential_ref": "credential-private",
                    "api_base": "https://internal.example.invalid",
                    "raw_plan": {"token": "secret://repair-token"},
                },
            },
        },
    }
)
detail = RunDetailResponse(
    id=run_id,
    status="running",
    mode="dispatch",
    request="verify model capability self-repair summary",
    created_at=datetime.now(UTC),
    queue_wait_ms=0,
    capacity_wait_ms=0,
    cost_usd="0",
    events=[admin_event],
    artifacts=[],
    explicit_details={},
)
projection = detail.model_dump(mode="json")
summary = projection.get("self_repair_recovery_summary")
require(isinstance(summary, dict), "model capability self-repair summary")
require(
    summary.get("recovery_strategy") == "reassign_tool_role_to_capable_model_and_retry",
    "summary recovery strategy",
)
require(summary.get("replan_scope") == "model_capability_roles", "summary replan scope")
require(summary.get("automatic_execution") is True, "summary automatic execution")
require(summary.get("role_capability_requirement_count") == 1, "summary role count")
require(summary.get("required_capability_count") == 3, "summary capability count")
require(
    projection["events"][0]["payload"]["model_execution_plan"]["self_repair_recovery"]
    == summary,
    "event summary projection",
)
serialized_projection = repr(projection)
require("credential-private" not in serialized_projection, "unsafe repair internals must be filtered")
require("internal.example.invalid" not in serialized_projection, "unsafe repair internals must be filtered")
require("secret://repair-token" not in serialized_projection, "unsafe repair internals must be filtered")
require("raw_plan" not in serialized_projection, "unsafe repair internals must be filtered")
PY
  then
    printf 'ok: model capability recovery\n'
    return 0
  fi
  printf 'fail: model capability recovery\n' >&2
  failures=$((failures + 1))
  return 1
}

check_model_selection_policy_contract() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: model selection policy\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: model selection policy requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from decimal import Decimal

from agent_hub.models.registry import ModelRegistry
from agent_hub.models.routing_policy import (
    ModelSelectionPolicyError,
    model_selection_policy_from_decision,
    rank_deployments_for_selection,
)
from agent_hub.models.types import Deployment


def require(value, label):
    if not value:
        raise SystemExit(label)


cheap = Deployment(
    id="z_cheap",
    logical_model="main",
    provider_model="deepseek/deepseek-chat",
    input_per_million_usd=Decimal("0.2"),
    output_per_million_usd=Decimal("0.8"),
)
expensive = Deployment(
    id="m_expensive",
    logical_model="main",
    provider_model="openai/gpt-5.6-sol",
    input_per_million_usd=Decimal("2.0"),
    output_per_million_usd=Decimal("8.0"),
)
unpriced = Deployment(id="a_unpriced", logical_model="main")
standard = Deployment(id="main_standard", logical_model="main", weight=100)
preferred = Deployment(id="main_preferred", logical_model="main", weight=250)

configured = (expensive, cheap, unpriced)
require(model_selection_policy_from_decision({}) == "configured", "default model selection")
require(
    rank_deployments_for_selection(configured, "configured") == configured,
    "configured policy must preserve configured deployment order",
)

low_cost_policy = model_selection_policy_from_decision(
    {"harness_policy": {"model_selection": "low_cost"}}
)
low_cost_ranked = rank_deployments_for_selection(configured, low_cost_policy)
require(
    tuple(deployment.id for deployment in low_cost_ranked)
    == ("z_cheap", "m_expensive", "a_unpriced"),
    "low_cost policy must prefer cheaper priced deployment",
)

high_quality_policy = model_selection_policy_from_decision(
    {"harness_policy": {"model_selection": "high_quality"}}
)
high_quality_ranked = rank_deployments_for_selection(
    (standard, preferred),
    high_quality_policy,
)
require(
    tuple(deployment.id for deployment in high_quality_ranked)
    == ("main_preferred", "main_standard"),
    "high_quality policy must prefer higher weight deployment",
)

try:
    model_selection_policy_from_decision({"harness_policy": {"model_selection": "fastest"}})
except ModelSelectionPolicyError:
    pass
else:
    raise SystemExit("invalid model selection policy must fail closed")

default_registry = ModelRegistry(low_cost_ranked)
require(
    tuple(deployment.id for deployment in default_registry.deployments)
    == ("a_unpriced", "m_expensive", "z_cheap"),
    "registry default id order must remain deterministic",
)
policy_registry = ModelRegistry(low_cost_ranked, preserve_order=True)
require(
    tuple(deployment.id for deployment in policy_registry.deployments)
    == ("z_cheap", "m_expensive", "a_unpriced"),
    "preserve_order=True must retain policy-ranked deployments",
)
require(
    tuple(deployment.id for deployment in policy_registry.candidates("main"))[0]
    == "z_cheap",
    "policy-ranked candidate must remain first",
)
PY
  then
    printf 'ok: model selection policy\n'
    return 0
  fi
  printf 'fail: model selection policy\n' >&2
  failures=$((failures + 1))
  return 1
}

check_model_fallback_capacity_pressure_contract() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: model fallback capacity pressure\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: model fallback capacity pressure requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelMessage, ModelRequest, ModelResponse

logging.getLogger("agent_hub.models.gateway").setLevel(logging.CRITICAL)


def require(value, label):
    if not value:
        raise SystemExit(label)


class CapacityStub:
    def __init__(self):
        self.acquire_calls = []
        self.records = []

    def validate_configuration(self, deployments):
        self.configured = tuple(deployments)

    async def initialize(self):
        return None

    async def acquire(self, candidates, wait_timeout, *, estimated_tokens):
        del wait_timeout, estimated_tokens
        self.acquire_calls.append(tuple(candidate.id for candidate in candidates))
        selected = candidates[0]
        return CapacityLease(
            id=str(uuid4()),
            deployment_id=selected.id,
            quota_scope_id=selected.quota_scope_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            renew_after_seconds=1,
        )

    async def release(self, lease):
        return True

    async def renew(self, lease):
        return lease

    async def record_outcome(self, quota_scope_id, *, status_code, latency_seconds, succeeded):
        del latency_seconds
        self.records.append((quota_scope_id, status_code, succeeded))


class SecretStub:
    async def resolve(self, secret_ref):
        del secret_ref
        return "safe-key"


class AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        chunk = self._chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    async def aclose(self):
        self.closed = True


class FallbackTransport:
    def __init__(self):
        self.complete_calls = []
        self.stream_calls = []
        self.streams = []

    async def complete(self, deployment, request, api_key):
        del request, api_key
        self.complete_calls.append(deployment.id)
        if deployment.id == "primary_model":
            raise ModelTransportError("provider rate limited", status_code=429)
        return ModelResponse(text="backup ok")

    def stream_openai_compatible_chunks(self, deployment, request, api_key):
        del request, api_key
        self.stream_calls.append(deployment.id)
        if deployment.id == "primary_model":
            stream = AsyncChunkStream(
                [ModelTransportError("provider rate limited", status_code=429)]
            )
        else:
            stream = AsyncChunkStream(
                [{"choices": [{"delta": {"content": "backup answer"}}]}]
            )
        self.streams.append(stream)
        return stream


async def main():
    primary = Deployment(
        id="primary_model",
        logical_model="primary",
        provider_model="deepseek/deepseek-chat",
        secret_ref="secret://primary",
        quota_scope_id="scope_primary",
    )
    backup = Deployment(
        id="backup_model",
        logical_model="backup",
        provider_model="openai/gpt-5",
        secret_ref="secret://backup",
        quota_scope_id="scope_backup",
    )
    request = ModelRequest(
        logical_model="primary",
        messages=(ModelMessage(role="user", content="hello"),),
    )

    completion_capacity = CapacityStub()
    completion_transport = FallbackTransport()
    completion_gateway = ModelGateway(
        ModelRegistry((primary, backup)),
        completion_capacity,
        SecretStub(),
        completion_transport,
        fallbacks={"primary": "backup"},
    )
    completion = await completion_gateway.complete_with_context(request)
    require(completion.deployment_id == "backup_model", "completion fallback deployment")
    require(completion.fallback_used is True, "completion fallback used")
    require(
        completion.fallback_reason == "capacity_pressure",
        "completion fallback must expose capacity pressure",
    )
    require(
        completion.attempted_logical_models == ("primary", "backup"),
        "completion fallback attempts",
    )

    streaming_capacity = CapacityStub()
    streaming_transport = FallbackTransport()
    streaming_gateway = ModelGateway(
        ModelRegistry((primary, backup)),
        streaming_capacity,
        SecretStub(),
        streaming_transport,
        fallbacks={"primary": "backup"},
    )
    events = [event async for event in streaming_gateway.stream_openai_compatible_events(request)]
    require(events[0].kind == "model.fallback", "streaming fallback event")
    require(
        events[0].payload["reason"] == "capacity_pressure",
        "streaming fallback must expose capacity pressure",
    )
    require(events[1].kind == "model.text_delta", "streaming fallback text")
    require(streaming_transport.stream_calls == ["primary_model", "backup_model"], "stream calls")


asyncio.run(main())
PY
  then
    printf 'ok: model fallback capacity pressure\n'
    return 0
  fi
  printf 'fail: model fallback capacity pressure\n' >&2
  failures=$((failures + 1))
  return 1
}

check_project_scale_matrix_contract() {
  local python_bin
  local script_dir
  local source_dir
  printf 'profile: project scale acceptance matrix\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: project scale acceptance matrix requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY'
from agent_hub.harness.project_scale import (
    PROJECT_SCALE_FLOW_KINDS,
    PROJECT_SCALE_TIERS,
    ProjectScaleMatrix,
    describe_project_scale_matrix,
)


def require(value, label):
    if not value:
        raise SystemExit(label)


matrix = ProjectScaleMatrix.default()
matrix.validate()
summary = describe_project_scale_matrix(matrix)
require("small,medium,large,ultra" in summary, "project scale tiers")
require(
    "direct,dispatch,hybrid,multi_agent,plugin,model_failure,self_repair,artifact_production,capability_validation"
    in summary,
    "project scale flows",
)
require("workspace_bundle" in summary, "project scale evidence")
require("cancel_or_archive_probe_runs" in summary, "project scale cleanup")
require(matrix.case_count == len(PROJECT_SCALE_TIERS) * len(PROJECT_SCALE_FLOW_KINDS), "case count")
require(matrix.requires_isolated_workspace is True, "isolated workspace")
for case in matrix.cases:
    require(case.requires_bearer_token is True, f"{case.id} bearer token")
    if case.scale in {"large", "ultra"}:
        require(case.requires_explicit_server_profile is True, f"{case.id} explicit server profile")
        require(case.expected_preflight is True, f"{case.id} preflight")
require(
    any(case.flow == "self_repair" and "self_repair" in case.validation_focus for case in matrix.cases),
    "self repair focus",
)
print(summary)
PY
  then
    printf 'ok: project scale acceptance matrix\n'
    return 0
  fi
  printf 'fail: project scale acceptance matrix\n' >&2
  failures=$((failures + 1))
  return 1
}

check_project_scale_runner_contract() {
  local python_bin
  local script_dir
  local source_dir
  local help_output
  local dry_run_output
  local dry_run_text_output
  local report_file
  local report_output
  local text_line_output
  local focus_output
  local execute_output
  local execute_status

  printf 'profile: project scale execution runner\n'
  if ! python_bin="$(detect_python)"; then
    printf 'fail: project scale execution runner requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if ! help_output="$(PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" -m agent_hub.harness.project_scale_runner --help 2>&1)"; then
    printf 'fail: project scale execution runner help\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$help_output" != *"--execute"* || "$help_output" != *"--wait-seconds"* || "$help_output" != *"--poll-interval"* || "$help_output" != *"--output"* ]]; then
    printf 'fail: project scale execution runner help missing execute/wait options\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! dry_run_output="$(PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" -m agent_hub.harness.project_scale_runner --scale small --flow capability_validation --json 2>&1)"; then
    printf 'fail: project scale execution runner dry-run\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$dry_run_output" != *'"dry_run": true'* \
    || "$dry_run_output" != *'"case_id": "small:capability_validation"'* \
    || "$dry_run_output" != *'"validation_focus": ["interaction_stability", "final_result", "capability_matrix", "mode_control", "no_silent_downgrade"]'* ]]; then
    printf 'fail: project scale execution runner dry-run payload\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! dry_run_text_output="$(PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" -m agent_hub.harness.project_scale_runner --scale small --flow capability_validation 2>&1)"; then
    printf 'fail: project scale execution runner dry-run text\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$dry_run_text_output" != *"small:capability_validation focus=interaction_stability,final_result,capability_matrix,mode_control,no_silent_downgrade"* ]]; then
    printf 'fail: project scale execution runner dry-run text focus\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! report_file="$(mktemp /tmp/project-scale-runner.XXXXXX)"; then
    printf 'fail: project scale execution runner report file setup\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" \
    "$python_bin" -m agent_hub.harness.project_scale_runner \
    --scale small --flow direct --json --output "$report_file" >/dev/null 2>&1; then
    rm -f -- "$report_file"
    printf 'fail: project scale execution runner report output\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  report_output="$(cat -- "$report_file" 2>/dev/null || true)"
  rm -f -- "$report_file"
  if [[ "$report_output" != *'"dry_run": true'* || "$report_output" != *'"case_id": "small:direct"'* ]]; then
    printf 'fail: project scale execution runner report payload\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! text_line_output="$(PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY' 2>&1
from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    format_project_scale_result_line,
)

result = ProjectScaleCaseResult(
    case_id="ultra:self_repair",
    run_id="run-ultra-self-repair",
    status="failed",
    evidence={
        "run_details": True,
        "run_events": True,
        "terminal_status": True,
        "project_preflight_approval": True,
        "workspace_bundle": True,
        "cleanup_cancel": True,
    },
    errors=("terminal_status: failed",),
)
print(format_project_scale_result_line(result))
PY
  )"; then
    printf 'fail: project scale execution runner text diagnostics\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$text_line_output" != "ultra:self_repair run_id=run-ultra-self-repair ok=false missing=final_artifacts,self_repair_trace errors=1" ]]; then
    printf 'fail: project scale execution runner text diagnostic payload\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if ! focus_output="$(PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" "$python_bin" - <<'PY' 2>&1
from agent_hub.harness.project_scale_runner import ProjectScaleCaseResult

payload = ProjectScaleCaseResult(
    case_id="small:capability_validation",
    run_id="run-small-capability-validation",
    status="completed",
    evidence={
        "run_details": True,
        "run_events": True,
        "terminal_status": True,
        "final_artifacts": True,
        "workspace_bundle": True,
        "cleanup_cancel": True,
    },
    validation_focus=(
        "interaction_stability",
        "final_result",
        "capability_matrix",
        "mode_control",
        "no_silent_downgrade",
    ),
).to_payload()
print(",".join(payload["validation_focus"]))
PY
  )"; then
    printf 'fail: project scale execution result validation focus\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  if [[ "$focus_output" != "interaction_stability,final_result,capability_matrix,mode_control,no_silent_downgrade" ]]; then
    printf 'fail: project scale execution result validation focus payload\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  set +e
  execute_output="$(
    env -u AGENT_HUB_ACCEPTANCE_BEARER_TOKEN \
      PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" \
      "$python_bin" -m agent_hub.harness.project_scale_runner \
      --execute --scale small --flow direct --wait-seconds 1 --poll-interval 0 --json 2>&1
  )"
  execute_status=$?
  set -e
  if [[ "$execute_status" -ne 2 || "$execute_output" != *"AGENT_HUB_ACCEPTANCE_BEARER_TOKEN is required for --execute"* ]]; then
    printf 'fail: project scale execution runner execute token gate\n' >&2
    failures=$((failures + 1))
    return 1
  fi
  printf 'ok: project scale execution runner\n'
}

run_authenticated_project_scale_execution_profile() {
  local python_bin
  local script_dir
  local source_dir
  local output
  local args
  local scale_values
  local flow_values
  local value

  printf 'profile: authenticated project scale execution runner\n'
  if [[ "$read_only" -eq 1 ]]; then
    printf 'skip: authenticated project scale execution runner is disabled in read-only mode\n'
    return 0
  fi
  if [[ "$project_scale_execute_profile" != "1" ]]; then
    printf 'skip: authenticated project scale execution runner requires AGENT_HUB_PROJECT_SCALE_EXECUTE_PROFILE=1\n'
    return 0
  fi
  if [[ -z "$bearer_token" ]]; then
    printf 'skip: authenticated project scale execution runner requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or acceptance login credentials\n'
    return 0
  fi
  if ! python_bin="$(detect_python)"; then
    printf 'fail: authenticated project scale execution runner requires python\n' >&2
    failures=$((failures + 1))
    return 1
  fi

  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  source_dir="$(cd -- "$script_dir/../.." && pwd -P)"
  if [[ -z "$project_scale_report_path" ]]; then
    if ! project_scale_report_path="$(mktemp /tmp/agent-hub-project-scale-report.XXXXXX)"; then
      printf 'fail: authenticated project scale execution runner could not create report path\n' >&2
      failures=$((failures + 1))
      return 1
    fi
  fi
  printf 'project-scale execution report: %s\n' "$project_scale_report_path"
  args=(
    -m agent_hub.harness.project_scale_runner
    --execute
    --base-url "$base_url"
  )
  IFS=',' read -r -a scale_values <<< "$project_scale_scales"
  for value in "${scale_values[@]}"; do
    if [[ -n "$value" ]]; then
      args+=(--scale "$value")
    fi
  done
  IFS=',' read -r -a flow_values <<< "$project_scale_flows"
  for value in "${flow_values[@]}"; do
    if [[ -n "$value" ]]; then
      args+=(--flow "$value")
    fi
  done
  args+=(--wait-seconds "$project_scale_wait_seconds")
  args+=(--poll-interval "$project_scale_poll_interval")
  if [[ -n "$project_scale_report_path" ]]; then
    args+=(--output "$project_scale_report_path")
  fi
  args+=(--json)
  if ! output="$(
    PYTHONPATH="$source_dir/src:${PYTHONPATH:-}" \
      AGENT_HUB_ACCEPTANCE_BEARER_TOKEN="$bearer_token" \
      "$python_bin" "${args[@]}" 2>&1
  )"; then
    printf 'fail: authenticated project scale execution runner scales=%s flows=%s\n' \
      "$project_scale_scales" "$project_scale_flows" >&2
    printf '%s\n' "$output" >&2
    failures=$((failures + 1))
    return 1
  fi
  printf '%s\n' "$output"
  printf 'ok: authenticated project scale execution runner scales=%s flows=%s\n' \
    "$project_scale_scales" "$project_scale_flows"
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
import json
from uuid import uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.observer import RunMonitor
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runs.service import (
    _harness_task_requirements,
    _local_main_agent_auto_mode,
    _main_agent_adjusted_ready_mode,
)
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import _artifact_review_packet_payload
from agent_hub.runtime.defaults import _dispatch_plan
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.project_preflight_context import project_preflight_context_text
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose


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
approved_preflight_routing = {
    "project_preflight_approved": True,
    "project_preflight_proposal": {
        "kind": "project_architecture_preflight",
        "capability": "project.preflight_architecture",
        "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
        "graph_path": "architecture-map.html",
        "requires_constraints_and_skills_reading": True,
    },
}
approved_preflight_context = project_preflight_context_text(approved_preflight_routing)
require(
    "PROJECT_PREFLIGHT_CONTEXT" in approved_preflight_context,
    "approved project preflight context formats",
)
require(
    "constraints and skill rules" in approved_preflight_context,
    "approved project preflight context requires constraints reading",
)
require(
    "stage_status" in approved_preflight_context
    and "verification_evidence" in approved_preflight_context
    and "remaining_risks" in approved_preflight_context
    and "stage_repair_actions" in approved_preflight_context,
    "approved project preflight context exposes implementation stage fields",
)
require(
    "diagnose failed stages" in approved_preflight_context,
    "approved project preflight context requires bounded stage repair",
)
require(
    "acceptance_review" in approved_preflight_context,
    "approved project preflight context exposes review stage fields",
)
staged_packet = _artifact_review_packet_payload(
    Artifact(
        id=uuid4(),
        type="text",
        producer="builder",
        content={
            "text": json.dumps(
                {
                    "stage_status": ["implementation complete"],
                    "verification_evidence": ["harness passed"],
                    "remaining_risks": ["tokened probe pending"],
                    "acceptance_review": ["accepted"],
                    "stage_repair_actions": ["fixed failing build stage"],
                },
                ensure_ascii=False,
            )
        },
    )
)
staged_packet_body = staged_packet.get("artifact_review_packet")
require(isinstance(staged_packet_body, dict), "approved project preflight review packet body")
staged_fields = staged_packet_body.get("staged_preflight_fields")
require(isinstance(staged_fields, dict), "approved project preflight review packet staged fields")
require(
    staged_fields.get("stage_status") == ("implementation complete",),
    "approved project preflight review packet exposes staged status",
)
require(
    staged_fields.get("acceptance_review") == ("accepted",),
    "approved project preflight review packet exposes acceptance review",
)
require(
    staged_fields.get("stage_repair_actions") == ("fixed failing build stage",),
    "approved project preflight review packet exposes stage repair actions",
)
direct_preflight_prompt = DirectRuntime(object(), logical_model="main")._build_prompt(
    TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request=mega_project_message,
        routing_decision=approved_preflight_routing,
    )
)
require(direct_preflight_prompt.messages is not None, "approved project preflight direct prompt")
direct_preflight_serialized = "\n".join(
    str(message.content) for message in direct_preflight_prompt.messages
)
require(
    "PROJECT_PREFLIGHT_CONTEXT" in direct_preflight_serialized,
    "approved project preflight context enters direct prompt",
)
require(
    "project.preflight_architecture" in direct_preflight_serialized,
    "approved project preflight direct prompt capability",
)
builder_role = RoleAssignment(
    id="builder",
    role="Builder",
    purpose=RolePurpose.EXECUTE,
    mission="Build the approved ultra-large project.",
    must_answer=("What was implemented?",),
    allowed_tools=(),
    forbidden_actions=("Do not perform dangerous operations.",),
    skills=(),
    output_schema={"summary": "string"},
    model="main",
)
reviewer_role = RoleAssignment(
    id="quality_reviewer",
    role="Quality Reviewer",
    purpose=RolePurpose.VERIFY,
    mission="Review the staged ultra-large project delivery.",
    must_answer=("Does each stage satisfy the approved acceptance matrix?",),
    allowed_tools=(),
    forbidden_actions=("Do not perform dangerous operations.",),
    skills=(),
    output_schema={"summary": "string"},
    model="main",
)
dispatch_preflight_plan = _dispatch_plan(
    (builder_role, reviewer_role),
    TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISPATCH,
        request=mega_project_message,
        routing_decision=approved_preflight_routing,
    ),
    max_parallelism=1,
)
dispatch_preflight_steps = {step.id: step for step in dispatch_preflight_plan.steps}
dispatch_preflight_agents = {agent.id: agent for agent in dispatch_preflight_plan.agents}
require(
    dispatch_preflight_steps["project_preflight_step"].agent == "project_preflight_architect",
    "approved project preflight explicit dispatch stage",
)
require(
    dispatch_preflight_agents["builder"].output_schema.get("stage_status") == "string[]",
    "approved project preflight role schema exposes staged status",
)
require(
    dispatch_preflight_agents["builder"].output_schema.get("verification_evidence") == "string[]",
    "approved project preflight role schema exposes staged evidence",
)
require(
    dispatch_preflight_agents["builder"].output_schema.get("stage_repair_actions") == "string[]",
    "approved project preflight role schema exposes staged repair actions",
)
require(
    dispatch_preflight_steps["quality_reviewer_step"].depends_on == ("builder_step",),
    "approved project preflight reviewer waits for staged implementation",
)
require(
    dispatch_preflight_agents["quality_reviewer"].output_schema.get("acceptance_review")
    == "string[]",
    "approved project preflight reviewer schema exposes acceptance review",
)
require(
    any("PROJECT_PREFLIGHT_CONTEXT" in step.task for step in dispatch_preflight_plan.steps),
    "approved project preflight context enters dispatch plan",
)
require(
    any("staged implementation" in step.task for step in dispatch_preflight_plan.steps),
    "approved project preflight dispatch plan staged implementation",
)
require(
    dispatch_preflight_steps["builder_step"].depends_on == ("project_preflight_step",),
    "approved project preflight dispatch stage gates implementation",
)
require(
    "Use the project_preflight_step output as the implementation contract"
    in dispatch_preflight_steps["builder_step"].task,
    "approved project preflight implementation consumes preflight contract",
)
require(
    "verification evidence for each stage" in dispatch_preflight_steps["builder_step"].task,
    "approved project preflight implementation requires staged evidence",
)
require(
    "diagnose failed stages before escalating" in dispatch_preflight_steps["builder_step"].task,
    "approved project preflight implementation diagnoses failed stages",
)
require(
    "record stage_repair_actions" in dispatch_preflight_steps["builder_step"].task,
    "approved project preflight implementation records stage repair actions",
)
require(
    "stage-by-stage implementation status"
    in dispatch_preflight_steps["final_response_step"].task,
    "approved project preflight final response reports staged status",
)
require(
    "self-repair actions" in dispatch_preflight_steps["final_response_step"].task,
    "approved project preflight final response reports repair actions",
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
  check_running_recovery_contract || true
  check_self_repair_failure_injection_matrix || true
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
  check_model_capability_recovery_contract || true
  check_model_selection_policy_contract || true
  check_model_fallback_capacity_pressure_contract || true
  check_interaction_prevention_and_recovery || true
  check_multimode_interaction_matrix || true
  check_project_scale_matrix_contract || true
  check_project_scale_runner_contract || true
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

stress_request() {
  local worker="$1"
  local index="$2"
  local path="$3"
  local attempt
  local status=""
  local last_status="curl-error"
  for ((attempt = 1; attempt <= retries; attempt += 1)); do
    if status="$(curl --noproxy '*' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -fsS -o /dev/null \
      -w '%{http_code}' \
      "$base_url$path" 2>/dev/null)" && [[ "$status" == 2* ]]; then
      return 0
    fi
    last_status="${status:-curl-error}"
    if ((attempt < retries)); then
      printf 'stress-retry: worker=%s iteration=%s path=%s attempt=%s status=%s\n' \
        "$worker" "$index" "$path" "$attempt" "$last_status" >&2
      sleep "$retry_delay"
    fi
  done
  printf 'stress-fail: worker=%s iteration=%s path=%s attempts=%s last_status=%s\n' \
    "$worker" "$index" "$path" "$retries" "$last_status" >&2
  return 1
}

stress_worker() {
  local worker="$1"
  local path
  local index
  for ((index = 1; index <= iterations; index += 1)); do
    for path in /health /health/live /health/ready /metrics /openapi.json /login; do
      stress_request "$worker" "$index" "$path" || return 1
    done
  done
}

run_stress_profile() {
  local pids=()
  local worker
  local failed=0
  printf 'profile: bounded stress scale=%s concurrency=%s iterations=%s\n' "$stress_profile" "$concurrency" "$iterations"
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
resolve_acceptance_bearer_token || true
check_acceptance_credential_readiness || true
if [[ "$read_only" -eq 1 ]]; then
  printf 'mode: read-only\n'
else
  printf 'mode: write-probes-enabled\n'
fi
wait_for_readiness || true
run_public_entrypoint_profile || true

case "$profile" in
  codex)
    run_codex_profile
    run_openapi_capability_profile || true
    run_lifecycle_profile || true
    run_create_idempotency_replay_guard_profile || true
    run_control_idempotency_guard_profile || true
    run_schedule_interaction_guard_profile || true
    run_project_preflight_approval_guard_profile || true
    run_authenticated_project_scale_execution_profile || true
    ;;
  deepseek)
    run_deepseek_profile
    run_openapi_capability_profile || true
    run_authenticated_project_scale_execution_profile || true
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
    run_authenticated_project_scale_execution_profile || true
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
