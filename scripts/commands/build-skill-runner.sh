#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
image="agent-hub-skill-runner:latest"
image="${AGENT_HUB_SKILL_RUNNER_IMAGE:-$image}"
docker_bin="${DOCKER_BIN:-docker}"

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub build-skill-runner [--source DIR] [--image NAME]

Builds Dockerfile target "skill-runner". Defaults:
  --source  repository root
  --image   agent-hub-skill-runner:latest
EOF
}

while (($#)); do
  case "$1" in
    --source)
      [[ $# -ge 2 && -n "$2" ]] || { printf 'missing value for --source\n' >&2; exit 2; }
      source="$2"
      shift 2
      ;;
    --image)
      [[ $# -ge 2 && -n "$2" ]] || { printf 'missing value for --image\n' >&2; exit 2; }
      image="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ -d "$source" ]]; then
  printf 'skill runner source directory does not exist: %s\n' "$source" >&2
  exit 2
fi
source="$(cd -- "$source" && pwd)"
if ! [[ -f "$source/Dockerfile" ]]; then
  printf 'skill runner source has no Dockerfile: %s\n' "$source" >&2
  exit 2
fi
if ! command -v "$docker_bin" >/dev/null 2>&1; then
  printf 'Docker CLI is unavailable: %s\n' "$docker_bin" >&2
  exit 127
fi

build_args=(build --target skill-runner --tag "$image" "$source")
printf 'Building %s from %s\n' "$image" "$source"
exec "$docker_bin" "${build_args[@]}"
