"""
Autoresearch worker entry point.

Usage:
    python -m usr.plugins.autoresearch.worker \
        --run-id <id> \
        --program-md <path> \
        [--max-experiments N] \
        [--cost-cap-usd X]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import string
import sys
import time
from decimal import Decimal
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("autoresearch.worker")


def _generate_run_id() -> str:
    try:
        import uuid_utils
        return str(uuid_utils.uuid7())
    except ImportError:
        suffix = "".join(random.choices(string.hexdigits[:16], k=4))
        return f"{time.time_ns()}-{suffix}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autoresearch worker")
    parser.add_argument("--run-id", required=False, default=None)
    parser.add_argument("--program-md", required=True)
    parser.add_argument("--max-experiments", type=int, default=None)
    parser.add_argument("--cost-cap-usd", type=str, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    run_id = args.run_id or _generate_run_id()
    program_md_path = Path(args.program_md).resolve()

    if not program_md_path.exists():
        log.error("program-md not found: %s", program_md_path)
        sys.exit(1)

    cost_cap = Decimal(args.cost_cap_usd) if args.cost_cap_usd else None

    log.info("starting run %s program-md=%s", run_id, program_md_path)

    from usr.plugins.autoresearch.worker.loop import run_loop

    final_state = asyncio.run(
        run_loop(
            run_id=run_id,
            program_md_path=program_md_path,
            max_experiments_override=args.max_experiments,
            cost_cap_override=cost_cap,
        )
    )
    log.info(
        "run %s finished: status=%s experiments=%d",
        run_id,
        final_state.status,
        len(final_state.experiments),
    )


if __name__ == "__main__":
    main()
