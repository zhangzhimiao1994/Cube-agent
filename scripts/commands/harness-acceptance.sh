#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${AGENT_HUB_ACCEPTANCE_BASE_URL:-http://127.0.0.1:8000}"
profile="all"
stress=0
concurrency="${AGENT_HUB_ACCEPTANCE_CONCURRENCY:-4}"
iterations="${AGENT_HUB_ACCEPTANCE_ITERATIONS:-10}"
connect_timeout="${AGENT_HUB_ACCEPTANCE_CONNECT_TIMEOUT_SECONDS:-5}"
max_time="${AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS:-20}"
failures=0

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub harness-acceptance [options]

Run non-destructive real-machine acceptance checks for Codex-style harness
stability and DeepSeek-style pluggable harness goals.

Options:
  --base-url URL                 Base URL to test.
  --profile codex|deepseek|all   Acceptance profile to run.
  --stress                       Run bounded HTTP stress checks.
  --concurrency N                Stress workers. Defaults to AGENT_HUB_ACCEPTANCE_CONCURRENCY or 4.
  --iterations N                 Requests per worker. Defaults to AGENT_HUB_ACCEPTANCE_ITERATIONS or 10.
  --connect-timeout SECONDS      Curl connect timeout. Defaults to 5.
  --max-time SECONDS             Curl total request timeout. Defaults to 20.
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

if ! positive_int "$concurrency" || ! positive_int "$iterations"; then
  printf 'concurrency and iterations must be positive integers\n' >&2
  exit 2
fi

require_curl() {
  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required for harness acceptance checks\n' >&2
    exit 2
  fi
}

check_url() {
  local name="$1"
  local path="$2"
  local expected="${3:-200}"
  local status
  if status="$(
    curl --noproxy '*' \
      --connect-timeout "$connect_timeout" \
      --max-time "$max_time" \
      -fsS -o /dev/null -w '%{http_code}' \
      "$base_url$path"
  )" && [[ "$status" == "$expected" ]]; then
    printf 'ok: %s %s -> %s\n' "$name" "$path" "$status"
    return 0
  fi
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
        "$base_url$path" || {
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
    ;;
  deepseek)
    run_deepseek_profile
    ;;
  all)
    run_codex_profile
    run_deepseek_profile
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
