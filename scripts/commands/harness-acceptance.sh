#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${AGENT_HUB_ACCEPTANCE_BASE_URL:-http://127.0.0.1:8000}"
profile="all"
stress=0
concurrency="${AGENT_HUB_ACCEPTANCE_CONCURRENCY:-4}"
iterations="${AGENT_HUB_ACCEPTANCE_ITERATIONS:-10}"
connect_timeout="${AGENT_HUB_ACCEPTANCE_CONNECT_TIMEOUT_SECONDS:-5}"
max_time="${AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS:-20}"
retries="${AGENT_HUB_ACCEPTANCE_RETRIES:-3}"
retry_delay="${AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS:-2}"
bearer_token="${AGENT_HUB_ACCEPTANCE_BEARER_TOKEN:-}"
run_message="${AGENT_HUB_ACCEPTANCE_RUN_MESSAGE:-Agent Hub harness acceptance run lifecycle probe}"
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
  --profile codex|deepseek|all   Acceptance profile to run.
  --stress                       Run bounded HTTP stress checks.
  --concurrency N                Stress workers. Defaults to AGENT_HUB_ACCEPTANCE_CONCURRENCY or 4.
  --iterations N                 Requests per worker. Defaults to AGENT_HUB_ACCEPTANCE_ITERATIONS or 10.
  --connect-timeout SECONDS      Curl connect timeout. Defaults to 5.
  --max-time SECONDS             Curl total request timeout. Defaults to 20.
  --retries N                    Attempts per smoke URL. Defaults to AGENT_HUB_ACCEPTANCE_RETRIES or 3.
  --retry-delay SECONDS          Delay between URL attempts. Defaults to AGENT_HUB_ACCEPTANCE_RETRY_DELAY_SECONDS or 2.
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
  codex|deepseek|all) ;;
  *)
    printf 'invalid --profile: %s\n' "$profile" >&2
    exit 2
    ;;
esac

positive_int() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

if ! positive_int "$concurrency" || ! positive_int "$iterations" || ! positive_int "$retries"; then
  printf 'concurrency, iterations, and retries must be positive integers\n' >&2
  exit 2
fi

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

run_codex_profile() {
  printf 'profile: codex harness stability\n'
  check_url "api live health" "/health/live" || true
  check_url "api readiness" "/health/ready" || true
  check_url "openapi contract" "/openapi.json" || true
  check_url "management ui entry" "/login" || true
}

run_deepseek_profile() {
  printf 'profile: deepseek pluggable harness goals\n'
  check_url "plugin-safe openapi projection" "/openapi.json" || true
  check_url "operator ui for plugin orchestration" "/login" || true
  check_url "runtime readiness boundary" "/health/ready" || true
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
  check_openapi_path "run pause control" "/api/v1/runs/{run_id}/pause" "post" || true
  check_openapi_path "run resume control" "/api/v1/runs/{run_id}/resume" "post" || true
  check_openapi_path "run capability approval" "/api/v1/runs/{run_id}/approve-capability" "post" || true
  check_openapi_path "plugin adapters" "/api/v1/admin/plugins/adapters" "get" || true
  check_openapi_path "plugin package install" "/api/v1/admin/plugins/install" "post" || true
  check_openapi_path "plugin package approval" "/api/v1/admin/plugins/{plugin_id}/package/approve" "post" || true
  check_openapi_path "plugin capability manifest" "/api/v1/admin/capabilities/manifest" "get" || true
  check_openapi_path "mcp server registry" "/api/v1/admin/mcp" "get" || true
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

  printf 'ok: run lifecycle create/read/events run_id=%s\n' "$run_id"
}

stress_worker() {
  local worker="$1"
  local path
  local index
  for ((index = 1; index <= iterations; index += 1)); do
    for path in /health/live /health/ready /openapi.json /login; do
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

require_curl

case "$profile" in
  codex)
    run_codex_profile
    run_openapi_capability_profile || true
    run_lifecycle_profile || true
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
    ;;
esac

if [[ "$stress" -eq 1 ]]; then
  run_stress_profile || true
fi

if [[ "$failures" -gt 0 ]]; then
  printf 'harness acceptance failed: %s check(s) failed\n' "$failures" >&2
  exit 1
fi

printf 'harness acceptance passed profile=%s stress=%s base_url=%s\n' "$profile" "$stress" "$base_url"
