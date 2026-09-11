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

@test "prune-releases refuses to run when current is outside releases" {
  root="$BATS_TEST_TMPDIR/agent-hub"
  mkdir -p "$root/releases/202601010000-release" "$root/outside"
  ln -s "$root/outside" "$root/current"

  run env AGENT_HUB_INSTALL_ROOT="$root" scripts/agent-hub prune-releases --keep 1 --execute

  [ "$status" -ne 0 ]
  [[ "$output" == *"current must point inside release directory"* ]]
  [ -d "$root/releases/202601010000-release" ]
}
