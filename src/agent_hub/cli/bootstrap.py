from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def hash_setup_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_bootstrap_record(code: str, *, minutes: int = 15) -> dict[str, str]:
    expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
    return {
        "code_hash": hash_setup_code(code),
        "expires_at": expires_at.isoformat(),
        "status": "open",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a one-time Agent Hub setup code record.")
    parser.add_argument("--code", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args()
    record = create_bootstrap_record(args.code, minutes=args.minutes)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    print("bootstrap record written")


if __name__ == "__main__":
    main()
