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
