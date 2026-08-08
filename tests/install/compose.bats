#!/usr/bin/env bats

@test "compose declares every required service" {
  run docker compose -f deploy/compose/docker-compose.yml config --services
  [ "$status" -eq 0 ]
  for service in api worker litellm postgres redis caddy; do
    [[ "$output" == *"$service"* ]]
  done
}

@test "skill runner is isolated" {
  run docker compose -f deploy/compose/docker-compose.yml config
  [ "$status" -eq 0 ]
  [[ "$output" == *"cap_drop:"* ]]
  [[ "$output" == *"no-new-privileges:true"* ]]
  [[ "$output" == *"read_only: true"* ]]
}
