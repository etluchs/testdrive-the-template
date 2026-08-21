"""Generate a **synthetic** seed so the app runs without the rate-limited API.

    python -m etl.devseed

``/v1/sql`` allows 20 requests per hour, so a real build takes about an hour and
cannot be part of an edit-run-look loop, a test run, or CI. This module writes a
``data/seed.sql`` of the same shape, filled with plausible made-up numbers, so
the app, the accessibility check and a browser can all be exercised offline.

Every row it produces is fiction. The extract is stamped
``source = synthetic (development only)`` in ``dl_meta``, and the app surfaces
that on the Method page, so a synthetic build cannot quietly be mistaken for
real advice.

Hero and item names come from the API's *asset* endpoints, which are cached and
not part of the SQL budget; if the network is unavailable, generic names are
used instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
from pathlib import Path

from etl import queries
from etl.build import (
    DATA_DIR,
    PATCH_ERAS,
    RAW_DIR,
    SCHEMA_PATH,
    SEED_PATH,
    _insert_statements,
)
from etl.deadlock import DeadlockAPIError, DeadlockClient

log = logging.getLogger("etl.devseed")

# Fixed so a synthetic build is reproducible: the same seed file every run, and
# therefore stable tests and stable accessibility snapshots.
RNG_SEED = 20260821

MINUTES = list(range(0, queries.MAX_MINUTE + 1, 2))
MINUTES5 = list(range(0, queries.MAX_MINUTE + 1, 5))
BANDS = list(range(0, queries.NW_BAND_MAX + 1))

# The purchase table covers **every** item for every hero, because that is the
# shape of the real extract: a real build keeps any item with enough purchases
# on that hero, which is most of the catalogue. Sampling a subset here used to
# make the item picker look wrong — a hero would show three tier-1 items out of
# the twenty-three that exist, purely because the sample was drawn uniformly.
#
# The enemy table is still sampled. It is the largest table by far (items x 38
# enemies x 9 buckets), it only feeds an optional adjustment, and a missing
# entry exercises the documented fallback rather than breaking anything.
ENEMY_ITEMS_PER_HERO = 30


def _fallback_heroes() -> dict[int, str]:
    return {i: f"Hero {i}" for i in range(1, 40)}


def _fallback_items() -> dict[int, dict]:
    slots = ("weapon", "vitality", "spirit")
    return {
        1000 + i: {
            "name": f"Item {i}",
            "tier": (i % 4) + 1,
            "cost": 800 * (2 ** (i % 4)),
            "slot": slots[i % 3],
        }
        for i in range(1, 60)
    }


def _catalogues() -> tuple[dict[int, str], dict[int, dict]]:
    """Real names if the asset endpoints answer, generic ones if they do not."""
    try:
        client = DeadlockClient()
        return client.playable_heroes(), client.buyable_items()
    except (DeadlockAPIError, OSError) as exc:
        log.warning("asset endpoints unavailable (%s); using generic names", exc)
        return _fallback_heroes(), _fallback_items()


def _expected_networth(minute: int) -> float:
    """Roughly what a player is worth at ``minute``, in souls."""
    return 800.0 * minute + 1_500.0


def _band_winrate(band: int, minute: int) -> float:
    """Being richer than the curve expects is what actually predicts winning.

    This is the confound the estimator has to see through, so the synthetic data
    contains it: win rate depends on net worth *relative to the minute*, and the
    purchase distribution is skewed towards the rich end.
    """
    expected_band = min(
        queries.NW_BAND_MAX,
        _expected_networth(minute) / queries.NW_BAND_SIZE,
    )
    edge = band - expected_band
    return 1.0 / (1.0 + math.exp(-0.45 * edge))


def build_devseed(out_path: Path) -> None:
    rng = random.Random(RNG_SEED)
    heroes, items = _catalogues()
    hero_ids = sorted(heroes)
    item_ids = sorted(items)

    statements: list[str] = [SCHEMA_PATH.read_text()]
    statements += _insert_statements("dl_hero", ["hero_id", "name"], sorted(heroes.items()))
    statements += _insert_statements(
        "dl_item", ["item_id", "name", "tier", "cost", "slot"],
        [
            (iid, v["name"], v["tier"], v["cost"], v["slot"])
            for iid, v in sorted(items.items())
        ],
    )

    # Real compositions if a previous build cached them; otherwise invented ones.
    cached_comps = sorted(RAW_DIR.glob("comps.*.json")) if RAW_DIR.exists() else []
    if cached_comps:
        comps = json.loads(cached_comps[-1].read_text())
        comp_rows = [
            (r["comp_key"], "-".join(str(h) for h in r["heroes"]), int(r["n"]), rank)
            for rank, r in enumerate(comps, start=1)
        ]
        log.info("using %s cached real compositions", f"{len(comp_rows):,}")
    else:
        comp_rows = []
        for rank in range(1, 201):
            picked = sorted(rng.sample(hero_ids, 6))
            key = "-".join(str(h) for h in picked)
            comp_rows.append((key, key, max(5, 1200 - rank * 5), rank))
    statements += _insert_statements(
        "dl_comp", ["comp_key", "heroes", "n", "comp_rank"], comp_rows
    )

    buy_rows: list[tuple] = []
    ref_rows: list[tuple] = []
    enemy_rows: list[tuple] = []
    enemy_ref_rows: list[tuple] = []
    ally_rows: list[tuple] = []
    ally_ref_rows: list[tuple] = []

    for hero_id in hero_ids:
        # Reference population: most players sit near the expected curve.
        for minute in MINUTES:
            centre = _expected_networth(minute) / queries.NW_BAND_SIZE
            for band in BANDS:
                weight = math.exp(-0.5 * ((band - centre) / 1.6) ** 2)
                n = int(40_000 * weight)
                if n < 30:
                    continue
                ref_rows.append(
                    (hero_id, minute, band, n, round(n * _band_winrate(band, minute)))
                )

        enemy_items = set(
            rng.sample(item_ids, min(ENEMY_ITEMS_PER_HERO, len(item_ids)))
        )
        for item_id in item_ids:
            # Each item gets a genuine effect that peaks at some minute and
            # decays away, on top of the wealth confound above.
            peak = rng.choice(MINUTES[2:-3])
            strength = rng.uniform(-0.02, 0.06)
            spread = rng.uniform(6.0, 14.0)

            for minute in MINUTES:
                centre = _expected_networth(minute) / queries.NW_BAND_SIZE
                effect = strength * math.exp(-0.5 * ((minute - peak) / spread) ** 2)
                if minute > peak + spread * 1.5:
                    effect -= 0.02
                for band in BANDS:
                    # Buyers skew rich: this is the confound, not a mistake.
                    weight = math.exp(-0.5 * ((band - centre - 0.8) / 1.4) ** 2)
                    n = int(rng.uniform(0.6, 1.4) * 2_500 * weight)
                    if n < 5:
                        continue
                    p = min(0.97, max(0.03, _band_winrate(band, minute) + effect))
                    buy_rows.append((hero_id, item_id, minute, band, n, round(n * p)))

            if item_id not in enemy_items:
                continue
            for enemy_id in hero_ids:
                if enemy_id == hero_id:
                    continue
                tilt = rng.uniform(-0.05, 0.05)
                ally_tilt = rng.uniform(-0.04, 0.04)
                for minute5 in MINUTES5:
                    n = int(rng.uniform(0.5, 1.5) * 900)
                    if n < 50:
                        continue
                    p = min(0.95, max(0.05, 0.5 + tilt + rng.uniform(-0.02, 0.02)))
                    enemy_rows.append((hero_id, item_id, enemy_id, minute5, n, round(n * p)))
                    q = min(0.95, max(0.05, 0.5 + ally_tilt + rng.uniform(-0.02, 0.02)))
                    ally_rows.append((hero_id, item_id, enemy_id, minute5, n, round(n * q)))

        for enemy_id in hero_ids:
            if enemy_id == hero_id:
                continue
            n = 30_000
            p = 0.5 + rng.uniform(-0.04, 0.04)
            enemy_ref_rows.append((hero_id, enemy_id, n, round(n * p)))
            q = 0.5 + rng.uniform(-0.03, 0.03)
            ally_ref_rows.append((hero_id, enemy_id, n, round(n * q)))

    statements += _insert_statements(
        "dl_buy", ["hero_id", "item_id", "minute", "nw_band", "n", "wins"], buy_rows
    )
    statements += _insert_statements(
        "dl_ref", ["hero_id", "minute", "nw_band", "n", "wins"], ref_rows
    )
    statements += _insert_statements(
        "dl_buy_enemy",
        ["hero_id", "item_id", "enemy_hero_id", "minute5", "n", "wins"],
        enemy_rows,
    )
    statements += _insert_statements(
        "dl_ref_enemy", ["hero_id", "enemy_hero_id", "n", "wins"], enemy_ref_rows
    )
    # Allies mirror enemies exactly, with their own independent tilts.
    statements += _insert_statements(
        "dl_buy_ally",
        ["hero_id", "item_id", "ally_hero_id", "minute5", "n", "wins"],
        ally_rows,
    )
    statements += _insert_statements(
        "dl_ref_ally", ["hero_id", "ally_hero_id", "n", "wins"], ally_ref_rows
    )
    # --- patch eras -----------------------------------------------------
    # Dated like the real ones. A handful of items are given a deliberate buff
    # or nerf part-way through so the comparison view has something to show.
    try:
        eras = [p["date"] for p in DeadlockClient().recent_patches(PATCH_ERAS)]
        titles = {p["date"]: p["title"] for p in DeadlockClient().recent_patches(PATCH_ERAS)}
    except (DeadlockAPIError, OSError):
        eras = [f"2026-0{3 + i // 2}-{1 + (i % 2) * 15:02d}" for i in range(PATCH_ERAS)]
        titles = {d: f"Minor Update - {d}" for d in eras}
    eras = sorted(eras)

    patch_rows = []
    title_rows = []
    for idx, start in enumerate(eras):
        end = eras[idx + 1] if idx + 1 < len(eras) else ""
        patch_rows.append((idx, start, end, 1 if idx == len(eras) - 1 else 0))
        title_rows.append((idx, titles.get(start, f"Minor Update - {start}")))
    statements += _insert_statements(
        "dl_patch", ["patch_idx", "patch_start", "patch_end", "is_current"], patch_rows
    )
    statements += _insert_statements("dl_patch_title", ["patch_idx", "title"], title_rows)

    patch_buy_rows: list[tuple] = []
    for item_id in item_ids:
        # A per-item drift, plus a step change at one era for some items.
        drift = rng.uniform(-0.004, 0.004)
        step_at = rng.choice([None, *range(1, len(eras))])
        step = rng.uniform(-0.06, 0.06)
        for idx in range(len(eras)):
            shift = drift * idx + (step if step_at is not None and idx >= step_at else 0.0)
            for band in BANDS:
                weight = math.exp(-0.5 * ((band - 4.0) / 2.2) ** 2)
                n = int(rng.uniform(0.7, 1.3) * 1_800 * weight)
                if n < 5:
                    continue
                p_win = min(0.95, max(0.05, 0.5 + shift + rng.uniform(-0.01, 0.01)))
                patch_buy_rows.append((item_id, idx, band, n, round(n * p_win)))
    statements += _insert_statements(
        "dl_buy_patch", ["item_id", "patch_idx", "nw_band", "n", "wins"], patch_buy_rows
    )

    statements += _insert_statements(
        "dl_meta",
        ["meta_key", "meta_value"],
        [
            ("source", "synthetic (development only)"),
            ("warning", "Every number in this extract is made up. Do not act on it."),
            ("heroes_built", str(len(hero_ids))),
            ("rng_seed", str(RNG_SEED)),
            ("patch_eras", str(len(eras))),
            ("patch_start", eras[-1] if eras else ""),
        ],
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n\n".join(statements) + "\n")
    log.info(
        "wrote %s (%.1f MB): %s buy, %s ref, %s enemy, %s patch rows across %s eras",
        out_path, out_path.stat().st_size / 1e6,
        f"{len(buy_rows):,}", f"{len(ref_rows):,}", f"{len(enemy_rows):,}",
        f"{len(patch_buy_rows):,}", len(eras),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=SEED_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    build_devseed(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
