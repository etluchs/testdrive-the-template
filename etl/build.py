"""Offline build: turn the Deadlock API into the seed the app reads.

Run it from the repo root::

    python -m etl.build --heroes all            # full build
    python -m etl.build --heroes Billy,Mina     # a slice, for development

``/v1/sql`` allows **20 requests per hour** (and no more than 2 per minute), so
the request count — not the size of any single response — is what decides how
long a build takes. Every extract therefore covers a *batch* of heroes and
groups by ``hero_id``, which puts a full 39-hero build at roughly 34 requests
and a little under two hours, instead of 236 requests and most of a day.

Every response is cached under ``data/raw/`` and re-runs skip what is already
there, so an interrupted build resumes instead of starting over.

The output is ``data/seed.sql``: schema plus multi-row INSERTs. It is loaded
into whatever database appkit is pointed at — the in-memory SQLite in tests, the
real one in production — by ``app.logic.load_seed()``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

from etl import queries
from etl.deadlock import DeadlockAPIError, DeadlockClient

log = logging.getLogger("etl.build")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SEED_PATH = DATA_DIR / "seed.sql"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Heroes per request. The purchase and reference extracts are cheap enough to
# batch generously; the enemy extracts carry a self-join and a second array
# join, so they get smaller batches to stay inside the query timeout.
BUY_BATCH = 10
REF_BATCH = 10
ENEMY_BUY_BATCH = 5
ENEMY_REF_BATCH = 10

# How many recent patches the cross-patch comparison covers. Every era costs
# nothing extra in requests (they all come back from one query), but a longer
# reach means a wider scan window and a slower query.
PATCH_ERAS = 10

# Rows per INSERT statement. Large enough that loading is not statement-bound,
# small enough that a statement stays readable when something goes wrong.
INSERT_CHUNK = 500

# Cells too thin to ever be used are dropped before they reach the seed. These
# thresholds are deliberately at or below the ones the estimator applies in
# `app/logic.py`, so pruning cannot change any number the app reports:
#   * dl_ref / dl_buy_enemy / dl_ref_enemy cells are only read at or above the
#     estimator's minimum, so anything below it is dead weight;
#   * dl_buy is pruned far lower, because its per-minute totals are displayed
#     even where an individual band is too thin to enter the estimate.
PRUNE_BUY_N = 5
PRUNE_REF_N = 30
PRUNE_ENEMY_N = 50


def _batched(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


# ---------------------------------------------------------------------------
# raw-response cache
# ---------------------------------------------------------------------------

def _cached(client: DeadlockClient, name: str, query: str) -> list[dict]:
    """Run ``query`` unless its response is already on disk.

    ``name`` carries the extract's fingerprint (patch date and item-set size),
    because the cache is keyed by filename. Without it, changing the patch
    anchor or the item filter would silently reuse responses computed under the
    old definition — the worst kind of stale, because nothing looks wrong.
    """
    path = RAW_DIR / f"{name}.json"
    if path.exists():
        try:
            rows = json.loads(path.read_text())
            log.info("cached  %s", name)
            return rows
        except json.JSONDecodeError:
            # A build interrupted mid-write used to leave a truncated file that
            # then poisoned every later resume. Re-fetch instead.
            log.warning("cache %s is corrupt, re-fetching", name)
            path.unlink()

    log.info("query   %s", name)
    started = time.monotonic()
    rows = client.sql(query)
    log.info("  -> %s rows in %.1fs", f"{len(rows):,}", time.monotonic() - started)

    # Write to a sibling and rename: os.replace is atomic within a directory, so
    # an interrupt can never leave a half-written cache file behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, path)
    return rows


# ---------------------------------------------------------------------------
# SQL emission
# ---------------------------------------------------------------------------

def _literal(value: object) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "NULL"
    return str(value)


def _insert_statements(table: str, columns: list[str], rows: list[tuple]) -> list[str]:
    """Multi-row INSERTs for ``rows``, chunked so no single statement is huge."""
    if not rows:
        return []
    out = []
    collist = ", ".join(columns)
    for start in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[start : start + INSERT_CHUNK]
        values = ",\n".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in chunk)
        out.append(f"INSERT INTO {table} ({collist}) VALUES\n{values};")
    return out


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _resolve_heroes(spec: str, heroes: dict[int, str]) -> list[int]:
    """Turn ``--heroes`` into hero ids. Accepts ``all``, names, or ids."""
    if spec.strip().lower() == "all":
        return sorted(heroes)
    by_name = {name.casefold(): hid for hid, name in heroes.items()}
    wanted: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and int(token) in heroes:
            wanted.append(int(token))
        elif token.casefold() in by_name:
            wanted.append(by_name[token.casefold()])
        else:
            raise SystemExit(f"unknown hero: {token!r}")
    return sorted(set(wanted))


def _collect(
    client: DeadlockClient,
    tag: str,
    label: str,
    hero_ids: list[int],
    batch_size: int,
    query_for: object,
    failed: list[str],
) -> list[dict]:
    """Run ``query_for`` over batches of heroes, tolerating a failed batch."""
    rows: list[dict] = []
    batches = _batched(hero_ids, batch_size)
    for index, batch in enumerate(batches, start=1):
        name = f"{label}.{tag}.{'-'.join(str(h) for h in batch)}"
        try:
            rows.extend(_cached(client, name, query_for(batch)))
        except DeadlockAPIError as exc:
            # A full build is over an hour of paced requests; losing all of it
            # to one bad batch would be worse than shipping the rest. Cached
            # responses survive, so a re-run picks up only what is missing.
            log.error("  %s batch %s/%s failed: %s", label, index, len(batches), exc)
            failed.append(name)
    return rows


def build(hero_spec: str, comp_limit: int, out_path: Path) -> None:
    client = DeadlockClient()

    log.info("fetching asset catalogues")
    heroes = client.playable_heroes()
    items = client.buyable_items()
    hero_ids = _resolve_heroes(hero_spec, heroes)
    # The buyable catalogue doubles as a filter: `items.*` in the warehouse also
    # records ability points, which otherwise dominate every per-hero ranking.
    item_ids = sorted(items)

    retired = len(client.all_upgrade_ids()) - len(items)
    since = client.current_patch_start()
    era_days = (
        date.today() - date(int(since[:4]), int(since[5:7]), int(since[8:10]))
    ).days
    log.info(
        "current patch began %s (%s days ago); %s shop items, "
        "%s excluded as removed or brawl-only",
        since, era_days, len(items), retired,
    )
    if era_days < queries.MIN_WINDOW_DAYS:
        log.warning(
            "only %s days since the last major patch — estimates will be thin. "
            "Widening the window would mix balance eras, so it is not done "
            "automatically; rebuild in a few weeks.",
            era_days,
        )

    # Anything a cached response depends on goes in its filename.
    tag = f"p{since.replace('-', '')}i{len(item_ids)}"

    requests = (
        1
        + len(_batched(hero_ids, BUY_BATCH))
        + len(_batched(hero_ids, REF_BATCH))
        + len(_batched(hero_ids, ENEMY_BUY_BATCH))
        + len(_batched(hero_ids, ENEMY_REF_BATCH))
        + len(_batched(hero_ids, ENEMY_BUY_BATCH))   # allies
        + len(_batched(hero_ids, ENEMY_REF_BATCH))
        + 1  # the cross-patch extract
    )
    log.info(
        "%s heroes, %s items; %s hero(es) to build in ~%s requests (~%s min)",
        len(heroes), len(items), len(hero_ids), requests, round(requests * 186 / 60),
    )

    statements: list[str] = [SCHEMA_PATH.read_text()]
    failed: list[str] = []

    statements += _insert_statements("dl_hero", ["hero_id", "name"], sorted(heroes.items()))
    statements += _insert_statements(
        "dl_item", ["item_id", "name", "tier", "cost", "slot"],
        [
            (iid, v["name"], v["tier"], v["cost"], v["slot"])
            for iid, v in sorted(items.items())
        ],
    )

    # -- top compositions ------------------------------------------------
    comps = _cached(
        client, f"comps.{tag}.{comp_limit}", queries.top_compositions(since, comp_limit)
    )
    statements += _insert_statements(
        "dl_comp",
        ["comp_key", "heroes", "n", "comp_rank"],
        [
            (row["comp_key"], "-".join(str(h) for h in row["heroes"]), int(row["n"]), rank)
            for rank, row in enumerate(comps, start=1)
        ],
    )
    log.info("compositions: %s rows", f"{len(comps):,}")

    # -- how items have fared across recent patches ------------------------
    # The only extract that looks outside the current patch; see schema.sql.
    patches = client.recent_patches(PATCH_ERAS)
    if patches:
        ascending = sorted(p["date"] for p in patches)
        by_date = {p["date"]: p["title"] for p in patches}
        patch_rows = []
        for idx, start in enumerate(ascending):
            end = ascending[idx + 1] if idx + 1 < len(ascending) else ""
            patch_rows.append((idx, start, end, 1 if idx == len(ascending) - 1 else 0))
        statements += _insert_statements(
            "dl_patch", ["patch_idx", "patch_start", "patch_end", "is_current"], patch_rows
        )
        statements += _insert_statements(
            "dl_patch_title", ["patch_idx", "title"],
            [(idx, by_date[start]) for idx, start in enumerate(ascending)],
        )
        try:
            trend = _cached(
                client,
                f"patchbuy.{tag}.{ascending[0].replace('-', '')}",
                queries.patch_purchase_stats(ascending, item_ids),
            )
            statements += _insert_statements(
                "dl_buy_patch",
                ["item_id", "patch_idx", "nw_band", "n", "wins"],
                [
                    (int(r["item_id"]), int(r["patch_idx"]), int(r["nw_band"]),
                     int(r["n"]), int(r["wins"]))
                    for r in trend
                    if int(r["item_id"]) in items and int(r["n"]) >= PRUNE_BUY_N
                ],
            )
        except DeadlockAPIError as exc:
            log.error("  patch trend failed: %s", exc)
            failed.append("patchbuy")

    # -- purchases -------------------------------------------------------
    buys = _collect(
        client, tag, "buy", hero_ids, BUY_BATCH,
        lambda batch: queries.purchase_stats(since, batch, item_ids), failed,
    )
    statements += _insert_statements(
        "dl_buy",
        ["hero_id", "item_id", "minute", "nw_band", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["item_id"]), int(r["minute"]),
             int(r["nw_band"]), int(r["n"]), int(r["wins"]))
            for r in buys
            if int(r["item_id"]) in items and int(r["hero_id"]) in heroes
            and int(r["n"]) >= PRUNE_BUY_N
        ],
    )

    # -- reference population --------------------------------------------
    refs = _collect(
        client, tag, "ref", hero_ids, REF_BATCH,
        lambda batch: queries.reference_stats(since, batch), failed,
    )
    statements += _insert_statements(
        "dl_ref",
        ["hero_id", "minute", "nw_band", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["minute"]), int(r["nw_band"]),
             int(r["n"]), int(r["wins"]))
            for r in refs
            if int(r["hero_id"]) in heroes and int(r["n"]) >= PRUNE_REF_N
        ],
    )

    # -- purchases split by enemy hero ------------------------------------
    enemy = _collect(
        client, tag, "buyenemy", hero_ids, ENEMY_BUY_BATCH,
        lambda batch: queries.enemy_purchase_stats(since, batch, item_ids), failed,
    )
    statements += _insert_statements(
        "dl_buy_enemy",
        ["hero_id", "item_id", "enemy_hero_id", "minute5", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["item_id"]), int(r["enemy_hero_id"]),
             int(r["minute5"]), int(r["n"]), int(r["wins"]))
            for r in enemy
            if int(r["enemy_hero_id"]) in heroes and int(r["item_id"]) in items
            and int(r["n"]) >= PRUNE_ENEMY_N
        ],
    )

    enemy_ref = _collect(
        client, tag, "refenemy", hero_ids, ENEMY_REF_BATCH,
        lambda batch: queries.enemy_reference_stats(since, batch), failed,
    )
    statements += _insert_statements(
        "dl_ref_enemy",
        ["hero_id", "enemy_hero_id", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["enemy_hero_id"]), int(r["n"]), int(r["wins"]))
            for r in enemy_ref
            if int(r["enemy_hero_id"]) in heroes and int(r["n"]) >= PRUNE_ENEMY_N
        ],
    )

    # -- purchases split by ally hero --------------------------------------
    ally = _collect(
        client, tag, "buyally", hero_ids, ENEMY_BUY_BATCH,
        lambda batch: queries.ally_purchase_stats(since, batch, item_ids), failed,
    )
    statements += _insert_statements(
        "dl_buy_ally",
        ["hero_id", "item_id", "ally_hero_id", "minute5", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["item_id"]), int(r["ally_hero_id"]),
             int(r["minute5"]), int(r["n"]), int(r["wins"]))
            for r in ally
            if int(r["ally_hero_id"]) in heroes and int(r["item_id"]) in items
            and int(r["n"]) >= PRUNE_ENEMY_N
        ],
    )

    ally_ref = _collect(
        client, tag, "refally", hero_ids, ENEMY_REF_BATCH,
        lambda batch: queries.ally_reference_stats(since, batch), failed,
    )
    statements += _insert_statements(
        "dl_ref_ally",
        ["hero_id", "ally_hero_id", "n", "wins"],
        [
            (int(r["hero_id"]), int(r["ally_hero_id"]), int(r["n"]), int(r["wins"]))
            for r in ally_ref
            if int(r["ally_hero_id"]) in heroes and int(r["n"]) >= PRUNE_ENEMY_N
        ],
    )

    if failed:
        log.warning("%s extract(s) incomplete: %s", len(failed), ", ".join(failed))

    # -- provenance ------------------------------------------------------
    statements += _insert_statements(
        "dl_meta",
        ["meta_key", "meta_value"],
        [
            ("patch_start", since),
            ("patch_era_days", str(era_days)),
            ("items_live", str(len(items))),
            ("items_retired_excluded", str(retired)),
            ("modes", queries.MODES),
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())),
            ("heroes_built", str(len(hero_ids))),
            ("extracts_failed", ",".join(failed) or "none"),
            ("comp_limit", str(comp_limit)),
            ("source", "https://api.deadlock-api.com"),
        ],
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n\n".join(statements) + "\n")
    log.info("wrote %s (%.1f MB)", out_path, out_path.stat().st_size / 1e6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heroes", default="all",
                        help="'all', or a comma-separated list of hero names or ids")
    parser.add_argument("--comps", type=int, default=10_000,
                        help="how many of the most-played compositions to store")
    parser.add_argument("--out", type=Path, default=SEED_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    try:
        build(args.heroes, args.comps, args.out)
    except DeadlockAPIError as exc:
        log.error("build failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
