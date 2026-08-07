#!/usr/bin/env sh
set -eu

python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5) as response:
    body = json.loads(response.read().decode("utf-8"))
    if body.get("status") != "ok":
        raise SystemExit(1)
PY
