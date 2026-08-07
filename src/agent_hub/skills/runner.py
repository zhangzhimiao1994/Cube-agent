from __future__ import annotations

import argparse
import asyncio


async def _run(skill: str | None) -> None:
    del skill
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub isolated skill runner.")
    parser.add_argument("--skill", default=None)
    args = parser.parse_args()
    asyncio.run(_run(args.skill))


if __name__ == "__main__":
    main()
