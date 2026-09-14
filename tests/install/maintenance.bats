#!/usr/bin/env bats

@test "doctor prints suggested fixes instead of secrets" {
  run scripts/agent-hub doctor
  [[ "$output" != *"POSTGRES_PASSWORD"* ]]
}

@test "backup manifest verifies payload" {
  export AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state"
  mkdir -p "$AGENT_HUB_STATE_DIR"
  echo ok > "$AGENT_HUB_STATE_DIR/file"
  run scripts/agent-hub backup "$BATS_TEST_TMPDIR/backup.tar.gz"
  [ "$status" -eq 0 ]
  run scripts/agent-hub backup verify "$BATS_TEST_TMPDIR/backup.tar.gz"
  [ "$status" -eq 0 ]
}

@test "failed readiness rolls back application version" {
  export AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state"
  mkdir -p "$AGENT_HUB_STATE_DIR"
  echo 0.1.0 > "$AGENT_HUB_STATE_DIR/version"
  run env AGENT_HUB_FAKE_NEW_VERSION_UNHEALTHY=1 scripts/agent-hub upgrade --version 0.2.0
  [ "$status" -ne 0 ]
  [ "$(scripts/agent-hub version)" = "0.1.0" ]
}

@test "prune-releases previews old releases without deleting by default" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  mkdir -p "$root/releases/202601010000-old" "$root/releases/202601020000-current" "$root/releases/202601030000-new"
  ln -s "$root/releases/202601020000-current" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub prune-releases --keep 1

  [ "$status" -eq 0 ]
  [[ "$output" == *"mode=dry-run"* ]]
  [[ "$output" == *"keep $root/releases/202601020000-current reason=current"* ]]
  [[ "$output" == *"remove $root/releases/202601010000-old reason=older"* ]]
  [ -d "$root/releases/202601010000-old" ]
}

@test "prune-releases execution preserves current even when current is old" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  mkdir -p "$root/releases/202601010000-current" "$root/releases/202601020000-old" "$root/releases/202601030000-new"
  ln -s "$root/releases/202601010000-current" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub prune-releases --keep 1 --execute

  [ "$status" -eq 0 ]
  [ -d "$root/releases/202601010000-current" ]
  [ ! -e "$root/releases/202601020000-old" ]
  [ -d "$root/releases/202601030000-new" ]
}

@test "prune-releases preserves runtime releases referenced by current venv links" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  runtime="$root/releases/202601010000-runtime"
  old="$root/releases/202601020000-old"
  current="$root/releases/202601030000-current"
  newest="$root/releases/202601040000-newest"
  mkdir -p "$runtime/.venv/bin" "$runtime/.litellm-venv/bin" "$old" "$current" "$newest"
  touch "$runtime/.venv/bin/python" "$runtime/.litellm-venv/bin/python"
  ln -s "$runtime/.venv" "$current/.venv"
  ln -s "$runtime/.litellm-venv" "$current/.litellm-venv"
  ln -s "$current" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub prune-releases --keep 1 --execute

  [ "$status" -eq 0 ]
  [[ "$output" == *"keep $runtime reason=current-runtime:.venv"* ]]
  [ -d "$runtime" ]
  [ ! -e "$old" ]
  [ -d "$current" ]
  [ -d "$newest" ]
}

@test "prune-releases refuses to run when current is outside releases" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  mkdir -p "$root/releases/202601010000-release" "$root/outside"
  ln -s "$root/outside" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub prune-releases --keep 1 --execute

  [ "$status" -ne 0 ]
  [[ "$output" == *"current must point inside release directory"* ]]
  [ -d "$root/releases/202601010000-release" ]
}

@test "verify-release accepts current release with revision" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  release="$root/releases/202601010000-release"
  mkdir -p "$release/.venv/bin" "$release/.litellm-venv/bin" "$release/web/dist" "$release/scripts"
  echo abc123 > "$release/REVISION"
  touch "$release/.venv/bin/python" "$release/.litellm-venv/bin/python" "$release/web/dist/index.html" "$release/scripts/agent-hub"
  chmod +x "$release/.venv/bin/python" "$release/.litellm-venv/bin/python" "$release/scripts/agent-hub"
  ln -s "$release" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub verify-release --expect-revision abc123 --skip-services

  [ "$status" -eq 0 ]
  [[ "$output" == *"ok: current release $release revision=abc123"* ]]
  [[ "$output" == *"ok: current release runtime python entrypoints are executable"* ]]
  [[ "$output" == *"ok: current release Web UI and launcher entrypoints are present"* ]]
}

@test "verify-release refuses missing current revision" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  release="$root/releases/202601010000-release"
  mkdir -p "$release"
  ln -s "$release" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub verify-release --skip-services

  [ "$status" -ne 0 ]
  [[ "$output" == *"current release REVISION file is missing"* ]]
}

@test "verify-release refuses missing runtime entrypoints" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  release="$root/releases/202601010000-release"
  mkdir -p "$release"
  echo abc123 > "$release/REVISION"
  ln -s "$release" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub verify-release --skip-services

  [ "$status" -ne 0 ]
  [[ "$output" == *"current release API python is missing"* ]]
}

@test "verify-release refuses missing Web UI index" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  release="$root/releases/202601010000-release"
  mkdir -p "$release/.venv/bin" "$release/.litellm-venv/bin" "$release/scripts"
  echo abc123 > "$release/REVISION"
  touch "$release/.venv/bin/python" "$release/.litellm-venv/bin/python" "$release/scripts/agent-hub"
  chmod +x "$release/.venv/bin/python" "$release/.litellm-venv/bin/python" "$release/scripts/agent-hub"
  ln -s "$release" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub verify-release --skip-services

  [ "$status" -ne 0 ]
  [[ "$output" == *"current release Web UI index is missing"* ]]
}
