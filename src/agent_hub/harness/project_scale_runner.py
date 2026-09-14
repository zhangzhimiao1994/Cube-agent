from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from agent_hub.harness.project_scale import build_project_scale_run_plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_hub.harness.project_scale_runner",
        description="Build a safe project-scale acceptance fixture run plan.",
    )
    parser.add_argument("--scale", action="append", dest="scales", default=None)
    parser.add_argument("--flow", action="append", dest="flows", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    if args.execute and not os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN"):
        parser.error("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN is required for --execute")

    try:
        plan = build_project_scale_run_plan(
            scales=tuple(args.scales) if args.scales is not None else None,
            flows=tuple(args.flows) if args.flows is not None else None,
            execute=args.execute,
        )
    except ValueError as error:
        parser.error(str(error))

    payload = plan.to_payload()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"project-scale plan cases={plan.case_count} "
            f"dry_run={str(plan.dry_run).lower()} execute={str(plan.execute).lower()}"
        )
        for request in plan.requests:
            print(request.case_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
