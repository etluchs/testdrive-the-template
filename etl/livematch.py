"""Find the match you are in right now, from the command line.

    python -m etl.livematch --account-id 180942972

The app can do this too (there is a "Find my match" box on the page). The
parsing and the network call live in :mod:`app.live`, which documents the terms
of the exception that lets app code reach the network at all; this module is
just a terminal front end onto the same function, useful when the app is not
running.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app import live, logic

log = logging.getLogger("etl.livematch")


def describe(match: live.LiveMatch, heroes: dict[int, str]) -> str:
    """A human-readable summary, with the ids the form wants."""
    def named(ids: list[int]) -> str:
        return ", ".join(f"{heroes.get(i, '?')} ({i})" for i in ids) or "\u2014 none seen \u2014"

    lines = [
        f"match {match.match_id}  ({match.players_seen}/12 players visible)",
        f"  you      : {heroes.get(match.hero_id, '?')} ({match.hero_id})",
        f"  your team: {named(match.allies)}",
        f"  enemies  : {named(match.enemies)}",
    ]
    if match.partial:
        lines.append(
            "  note     : the live list lags the draft, so some slots may still "
            "be missing. Re-run in a few seconds for the full line-up."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, required=True,
                        help="your Steam account id (the 32-bit one the API uses)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    try:
        match = live.find_match(args.account_id)
    except live.LiveLookupError as exc:
        log.error("%s", exc)
        return 2
    if match is None:
        log.info("account %s is not in any match in the live list", args.account_id)
        return 1

    logic.load_seed()
    print(describe(match, logic.heroes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
