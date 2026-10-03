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

@test "skill broker has only probe directory and ownership capabilities" {
  grep -q '^SupplementaryGroups=agent-hub' deploy/native/systemd/agent-hub-skill-broker.service
  grep -q '^CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_CHOWN$' deploy/native/systemd/agent-hub-skill-broker.service
  ! grep -Eq '^AmbientCapabilities=.+$' deploy/native/systemd/agent-hub-skill-broker.service
}

@test "python systemd services load the active release source tree" {
  for unit in \
    deploy/native/systemd/agent-hub-api.service \
    deploy/native/systemd/agent-hub-worker.service
  do
    grep -q '^Environment=PYTHONPATH=/opt/agent-hub/current/src' "$unit"
  done
  grep -q '^Environment=PYTHONPATH=/opt/agent-hub/current/src' \
    deploy/native/systemd/agent-hub-skill-broker.service
}
@test "native installer deploys a release before starting systemd services" {
  grep -q 'deploy_native_release' scripts/lib/install_native.sh
  grep -q 'ln -sfn' scripts/lib/install_native.sh
  grep -q '"$INSTALL_ROOT/current"' scripts/lib/install_native.sh
  grep -q 'uv sync --frozen --no-dev' scripts/lib/install_native.sh
}

@test "native upgrades restart every process bound to the active release" {
  grep -q 'systemctl stop agent-hub-skill-broker.service' scripts/lib/install_native.sh
  grep -q 'systemctl restart agent-hub-litellm.service' scripts/lib/install_native.sh
  grep -q 'systemctl restart agent-hub-api.service' scripts/lib/install_native.sh
  grep -q 'systemctl restart agent-hub-worker.service' scripts/lib/install_native.sh
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

@test "preview broker socket only permits the console service group" {
  grep -qx 'ListenStream=/run/agent-hub/preview-broker.sock' deploy/native/systemd/agent-hub-preview-broker.socket
  grep -qx 'SocketUser=root' deploy/native/systemd/agent-hub-preview-broker.socket
  grep -qx 'SocketGroup=agent-hub' deploy/native/systemd/agent-hub-preview-broker.socket
  grep -qx 'SocketMode=0660' deploy/native/systemd/agent-hub-preview-broker.socket
}

@test "preview broker never inherits the production secret group or environment" {
  unit=deploy/native/systemd/agent-hub-preview-broker.service
  grep -qx 'CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_CHOWN' "$unit"
  grep -qx 'ReadWritePaths=/run/agent-hub-preview' "$unit"
  ! grep -Eq '^(SupplementaryGroups=.+|EnvironmentFile=|AmbientCapabilities=.+)' "$unit"
  grep -qx 'd /run/agent-hub-preview 0700 root root -' deploy/native/agent-hub.tmpfiles
}

@test "preview config uses account uid and publishes root-only policy atomically" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    CONFIG_DIR="$1/config"
    INSTALL_ROOT="$1/opt"
    STATE_DIR=/var/lib/agent-hub
    mkdir -p "$INSTALL_ROOT/current/.venv/bin"
    printf "#!/usr/bin/env bash\nexport MSYS2_ARG_CONV_EXCL=\"*\"\nexec %q \"\$@\"\n" "$(command -v python)" > "$INSTALL_ROOT/current/.venv/bin/python"
    chmod +x "$INSTALL_ROOT/current/.venv/bin/python"
    id() { [[ "$*" == "-u agent-hub" ]] && printf "23456\n"; }
    chown() { [[ "$1" == "root:root" ]]; }
    die() { printf "%s\n" "$*" >&2; exit 1; }
    write_native_preview_broker_config
    python - "$CONFIG_DIR/preview-broker.json" <<"PY"
import json, os, stat, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
assert data["allowed_uid"] == 23456
assert data["workspace_root"] == "/var/lib/agent-hub/workspaces"
assert data["runtime_root"] == "/run/agent-hub-preview"
assert set(data) == {"workspace_root", "allowed_uid", "runtime_root", "trusted_source_root", "node_root"}
if os.name == "posix":
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
assert not list(path.parent.glob(".preview-broker.json.*"))
PY
    export AGENT_HUB_PROJECT_WORKSPACE_DIR=/srv/exported-workspaces
    write_native_preview_broker_config
    python - "$CONFIG_DIR/preview-broker.json" <<"PY"
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text())["workspace_root"] == "/srv/exported-workspaces"
PY
    SECRETS_FILE="$CONFIG_DIR/secrets.env"
    printf "%s\n" "AGENT_HUB_PROJECT_WORKSPACE_DIR=\"/srv/configured-workspaces\"" \
      "UNRELATED_PROVIDER_KEY=do-not-copy" "exit 99" > "$SECRETS_FILE"
    write_native_preview_broker_config
    python - "$CONFIG_DIR/preview-broker.json" <<"PY"
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
assert data["workspace_root"] == "/srv/configured-workspaces"
assert "do-not-copy" not in str(data)
PY
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "preview config refuses missing account without replacing existing policy" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    CONFIG_DIR="$1"
    printf "unchanged\n" > "$CONFIG_DIR/preview-broker.json"
    id() { return 1; }
    die() { exit 1; }
    (write_native_preview_broker_config) && exit 2
    [[ "$(cat "$CONFIG_DIR/preview-broker.json")" == unchanged ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "preview probe fails install when broker isolation probe fails" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT=/opt/agent-hub
    runuser() { [[ "$*" == "-u agent-hub -- env -i "* ]] || exit 2; return 1; }
    die() { printf "%s\n" "$*" >&2; exit 1; }
    require_native_preview_broker
  '
  [ "$status" -eq 1 ]
  [[ "$output" == *"native preview broker isolation probe failed"* ]]
}
