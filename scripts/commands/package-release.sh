#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
output=""

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub package-release --output file.tar

Builds the Web UI and creates a deployable source archive that includes web/dist.
EOF
}

while (($#)); do
  case "$1" in
    --output)
      output="${2:?missing output}"
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

if [[ -z "$output" ]]; then
  usage >&2
  exit 2
fi

npm --prefix "$SOURCE_DIR/web" run build

if ! [[ -f "$SOURCE_DIR/web/dist/index.html" ]]; then
  printf 'web build did not produce web/dist/index.html\n' >&2
  exit 1
fi

revision="$(git -C "$SOURCE_DIR" rev-parse HEAD)"

mkdir -p "$(dirname -- "$output")"
tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$tmp_dir"
}
trap cleanup EXIT

staging_dir="$tmp_dir/source"
mkdir -p "$staging_dir/web/dist"
git -C "$SOURCE_DIR" archive --format=tar HEAD | tar -xf - -C "$staging_dir"
cp -a "$SOURCE_DIR/web/dist/." "$staging_dir/web/dist/"
printf '%s\n' "$revision" > "$staging_dir/REVISION"

tar -cf "$output" -C "$staging_dir" .
