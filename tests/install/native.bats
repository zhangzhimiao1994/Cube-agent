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
    AGENT_HUB_NODE_HOME=/srv/agent-preview-node
    write_native_preview_broker_config
    python - "$CONFIG_DIR/preview-broker.json" <<"PY"
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
assert data["workspace_root"] == "/srv/exported-workspaces"
assert data["node_root"] == "/srv/agent-preview-node"
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

@test "dedicated Node reuse repairs only resolved custom home before execution" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    AGENT_HUB_NODE_HOME="$1/custom-node"
    mkdir -p "$AGENT_HUB_NODE_HOME/bin" "$AGENT_HUB_NODE_HOME/lib/node_modules/npm/bin"
    printf "#!/bin/sh\necho v22.12.0\n" > "$AGENT_HUB_NODE_HOME/bin/node"
    cp "$AGENT_HUB_NODE_HOME/bin/node" "$AGENT_HUB_NODE_HOME/bin/npm"
    touch "$AGENT_HUB_NODE_HOME/lib/node_modules/npm/bin/npm-cli.js"
    chmod +x "$AGENT_HUB_NODE_HOME/bin/"*
    expected="$(realpath -e "$AGENT_HUB_NODE_HOME")"
    node() { exit 93; }
    npm() { exit 94; }
    chown() { [[ "$*" == "-hR -P root:root -- $expected" ]] || exit 91; printf repaired; }
    download_native_node() { exit 92; }
    die() { printf "%s\n" "$*" >&2; exit 1; }
    ensure_native_nodejs
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [[ "$output" == repaired ]]
}

@test "dedicated Node normalization refuses shared system roots and root symlinks" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    mkdir -p "$INSTALL_ROOT"
    chown() { exit 91; }
    die() { exit 1; }
    for target in / /usr /usr/local /bin /opt "$INSTALL_ROOT"; do
      AGENT_HUB_NODE_HOME="$target"
      if (normalize_native_node_ownership); then exit 92; else [[ "$?" -eq 1 ]]; fi
    done
    AGENT_HUB_NODE_HOME="$1/link"
    ln -s "$INSTALL_ROOT" "$AGENT_HUB_NODE_HOME"
    if [[ -L "$AGENT_HUB_NODE_HOME" ]]; then
      if (normalize_native_node_ownership); then exit 93; else [[ "$?" -eq 1 ]]; fi
    fi
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "system Node 22 still prepares missing dedicated preview runtime at custom home" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    AGENT_HUB_NODE_HOME="$1/custom-node"
    node() { printf "v22.12.0\n"; }
    npm() { :; }
    chown() { exit 91; }
    download_native_node() {
      mkdir -p "$AGENT_HUB_NODE_HOME/bin" "$AGENT_HUB_NODE_HOME/lib/node_modules/npm/bin"
      printf "#!/bin/sh\necho v22.12.0\n" > "$AGENT_HUB_NODE_HOME/bin/node"
      cp "$AGENT_HUB_NODE_HOME/bin/node" "$AGENT_HUB_NODE_HOME/bin/npm"
      touch "$AGENT_HUB_NODE_HOME/lib/node_modules/npm/bin/npm-cli.js"
      chmod +x "$AGENT_HUB_NODE_HOME/bin/"*
      printf prepared
    }
    warn() { :; }
    die() { exit 1; }
    ensure_native_nodejs
    [[ -x "$AGENT_HUB_NODE_HOME/bin/node" && ! -e "$INSTALL_ROOT/node" ]]
    [[ "$PATH" == "$(realpath -e "$AGENT_HUB_NODE_HOME")/bin:"* ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [[ "$output" == prepared ]]
}

@test "dedicated Node download discards tar publisher UID and normalizes custom home" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    AGENT_HUB_NODE_HOME="$1/custom-node"
    fixture="$1/fixture"
    archive_fixture="$1/publisher.tar.xz"
    mkdir -p "$fixture/node-runtime/bin"
    printf "node fixture\n" > "$fixture/node-runtime/bin/node"
    command tar --owner=23456 --group=23456 -cJf "$archive_fixture" -C "$fixture" node-runtime
    curl() { cp "$archive_fixture" "${@: -1}"; }
    tar() { [[ "$*" == *"--no-same-owner"* ]] || exit 91; command tar "$@"; }
    chown() { [[ "$*" == "-hR -P root:root -- $(realpath -e "$AGENT_HUB_NODE_HOME")" ]] || exit 92; printf repaired; }
    die() { printf "%s\n" "$*" >&2; exit 1; }
    download_native_node
    [[ "$(cat "$AGENT_HUB_NODE_HOME/bin/node")" == "node fixture" ]]
    [[ ! -e "$INSTALL_ROOT/node" ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [[ "$output" == repaired ]]
}

@test "system Node 22 cannot mask failed or incomplete dedicated preparation" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    node() { printf "v22.12.0\n"; }
    npm() { :; }
    chown() { exit 91; }
    warn() { :; }
    die() { exit 1; }
    for result in 1 0; do
      download_native_node() { return "$result"; }
      if (ensure_native_nodejs); then exit 92; else [[ "$?" -eq 1 ]]; fi
    done
    [[ ! -e "$INSTALL_ROOT/node" ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "dedicated Node normalization rejects hardlinks before changing ownership" {
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    AGENT_HUB_NODE_HOME="$1/custom-node"
    mkdir -p "$AGENT_HUB_NODE_HOME"
    printf sentinel > "$1/outside"
    ln "$1/outside" "$AGENT_HUB_NODE_HOME/linked"
    chown() { exit 91; }
    die() { exit 1; }
    if (normalize_native_node_ownership); then exit 92; else [[ "$?" -eq 1 ]]; fi
    [[ "$(cat "$1/outside")" == sentinel ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "dedicated Node ownership repair leaves external symlink targets untouched on Linux" {
  [[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || skip "requires Linux root for real ownership checks"
  run bash -c '
    set -euo pipefail
    source scripts/lib/install_native.sh
    INSTALL_ROOT="$1/install"
    AGENT_HUB_NODE_HOME="$1/custom-node"
    mkdir -p "$AGENT_HUB_NODE_HOME/bin" "$1/outside"
    printf sentinel > "$1/outside/file"
    printf node > "$AGENT_HUB_NODE_HOME/bin/node"
    ln -s "$1/outside" "$AGENT_HUB_NODE_HOME/external"
    ln -s node "$AGENT_HUB_NODE_HOME/bin/npm"
    command chown -R 23456:23456 "$1/outside" "$AGENT_HUB_NODE_HOME"
    die() { exit 1; }
    normalize_native_node_ownership
    [[ "$(stat -c %u:%g "$AGENT_HUB_NODE_HOME/bin/node")" == 0:0 ]]
    [[ "$(stat -c %u:%g "$AGENT_HUB_NODE_HOME/bin/npm")" == 0:0 ]]
    [[ "$(stat -c %u:%g "$1/outside/file")" == 23456:23456 ]]
  ' _ "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}
