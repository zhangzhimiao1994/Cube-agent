#!/usr/bin/env bats

@test "ubuntu debian rocky and alma map to supported package managers" {
  for fixture in ubuntu debian rocky almalinux; do
    run bash deploy/native/install-packages.sh --detect tests/install/fixtures/os-release-$fixture
    [ "$status" -eq 0 ]
  done
}

@test "unknown distro fails clearly and points to docker" {
  run bash deploy/native/install-packages.sh --detect tests/install/fixtures/os-release-unknown
  [ "$status" -ne 0 ]
  [[ "$error" == *"Docker mode"* || "$output" == *"Docker mode"* ]]
}

@test "api service has mandatory hardening" {
  grep -q '^NoNewPrivileges=yes' deploy/native/systemd/agent-hub-api.service
  grep -q '^ProtectSystem=strict' deploy/native/systemd/agent-hub-api.service
  grep -q '^ReadWritePaths=/var/lib/agent-hub /run/agent-hub' deploy/native/systemd/agent-hub-api.service
}

@test "native installer deploys a release before starting systemd services" {
  grep -q 'deploy_native_release' scripts/lib/install_native.sh
  grep -q 'ln -sfn' scripts/lib/install_native.sh
  grep -q '"$INSTALL_ROOT/current"' scripts/lib/install_native.sh
  grep -q 'uv sync --frozen --no-dev' scripts/lib/install_native.sh
}

@test "native installer creates runtime directories and runs migrations before services" {
  grep -q 'systemd-tmpfiles --create' scripts/lib/install_native.sh
  grep -q 'alembic upgrade head' scripts/lib/install_native.sh
  python - <<'PY'
from pathlib import Path

script = Path("scripts/lib/install_native.sh").read_text()
tmpfiles = script.index("systemd-tmpfiles --create")
migrations = script.index("alembic upgrade head")
start = script.index("systemctl enable --now agent-hub.target")
assert tmpfiles < start
assert migrations < start
PY
}

@test "native services keep api private behind caddy" {
  grep -q -- '--host ${AGENT_HUB_API_BIND_HOST:-127.0.0.1}' deploy/native/systemd/agent-hub-api.service
  grep -q 'reverse_proxy 127.0.0.1:8000' deploy/native/Caddyfile
}
