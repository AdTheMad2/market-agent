"""The post-close digest. Phase 3's observable check runs through this file.

Runs at 21:15 UTC (~17:15 ET), after the 16:00 ET close plus the 15-minute SIP
delay plus slack for GitHub's scheduled-event queue.

What it does beyond the shared pipeline in `jobs/digest.py`: it disarms levels
that fired during the session, and it reports what the intraday job dropped at
the daily ceiling — the only place those ever surface (docs/SPEC.md §6.3).

Run:  python -m jobs.postclose [--dry-run] [--db data/market.db] [--day 2026-08-05]
Exit: 0 when the digest was delivered, or when today is not a trading day
      1 when delivery failed

See docs/IMPLEMENTATION_PLAN.md Task 3.3.
"""

from __future__ import annotations

import sys

from jobs import digest
from jobs.cli import parse_args, prepare


def main(argv: list[str] | None = None) -> int:
    args = parse_args(__doc__, argv)
    prepare()
    outcome = digest.run(
        digest.POSTCLOSE,
        db=args.db,
        day=args.day,
        dry_run=args.dry_run,
        to_test_chat=args.test_chat,
    )
    if not outcome.ran:
        return 0
    print(f"{outcome.triggers} trigger(s); delivered={outcome.delivered}")
    return 0 if outcome.delivered else 1


if __name__ == "__main__":
    sys.exit(main())
