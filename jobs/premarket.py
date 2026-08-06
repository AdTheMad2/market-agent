"""The pre-market digest. Runs at 13:15 UTC (~09:15 ET), before the open.

What it does beyond the shared pipeline in `jobs/digest.py`: it arms the day's
levels from `config/watchlist_core.yml` into the `armed_levels` table, so the
intraday job has something to check against.

**There is no daily counter to reset, and there should not be.**
`store.sent_count_today` derives the count from the UTC date of the rows in
`sent_alerts`, so a new day resets it by construction. The only way to "reset" a
derived count is to delete rows, which would also destroy the `last_sent`
cooldown history the same table serves.

A level disarmed by yesterday's post-close run is re-armed here for as long as
it remains in the YAML. Removal is a YAML edit, never a table edit.

Run:  python -m jobs.premarket [--dry-run] [--db data/market.db] [--day 2026-08-05]
Exit: 0 when the digest was delivered, or when today is not a trading day
      1 when delivery failed

See docs/IMPLEMENTATION_PLAN.md Task 3.4.
"""

from __future__ import annotations

import sys

from jobs import digest
from jobs.cli import parse_args, prepare


def main(argv: list[str] | None = None) -> int:
    args = parse_args(__doc__, argv)
    prepare()
    outcome = digest.run(
        digest.PREMARKET,
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
