"""The synthetic extract is what CI and local development actually serve.

It is not just filler: it deliberately contains the confound the estimator has
to remove, so that running the app on it exercises the same code path as running
it on real data. These tests pin that, and they never touch the network.
"""

from etl import devseed, queries


def test_richer_players_win_more_at_a_given_minute():
    """The confound is present by construction, not by accident."""
    minute = 20
    rates = [devseed._band_winrate(band, minute) for band in range(0, queries.NW_BAND_MAX + 1)]

    assert rates == sorted(rates), "win rate must increase with net worth"
    assert rates[-1] > rates[0] + 0.3, "the confound must be large enough to matter"


def test_being_rich_matters_relative_to_the_minute():
    """20k souls is a lead at minute 10 and a deficit at minute 35."""
    band = 4  # 20,000-25,000 souls
    assert devseed._band_winrate(band, 10) > 0.5
    assert devseed._band_winrate(band, 35) < 0.5


def test_the_generated_extract_is_marked_synthetic(tmp_path):
    """A synthetic build must never be mistakable for real advice."""
    out = tmp_path / "seed.sql"
    devseed.build_devseed(out)
    text = out.read_text()

    assert "synthetic (development only)" in text
    assert "Do not act on it." in text


def test_the_generated_extract_loads_and_answers(tmp_path, seeded):
    """It produces a seed the app's own loader and estimator can consume."""
    from appkit import db

    from app import logic

    out = tmp_path / "seed.sql"
    devseed.build_devseed(out)

    # Fresh tables, then load the generated file the way the app does.
    for table in ("dl_buy", "dl_ref", "dl_buy_enemy", "dl_ref_enemy",
                  "dl_hero", "dl_item", "dl_comp", "dl_meta",
                  "dl_patch", "dl_patch_title", "dl_buy_patch",
                  "dl_buy_ally", "dl_ref_ally"):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    logic.clear_caches()
    for statement in logic._split_sql(out.read_text()):
        db.execute(statement)

    heroes = logic.heroes_with_data()
    assert heroes, "the synthetic extract should carry heroes with purchase data"

    hero_id = int(heroes[0]["value"])
    items = logic.items_for_hero(hero_id)
    assert items, "and items for them"

    result = logic.estimate(hero_id, int(items[0]["value"]))
    assert result.points, "and enough data to produce a curve"
    assert result.verdict


def test_every_hero_gets_the_whole_catalogue(tmp_path, seeded):
    """The synthetic extract must have the same *shape* as a real one.

    An earlier version sampled 30 of the 156 items per hero, drawn uniformly.
    The picker then showed a hero three tier-1 items out of the twenty-three
    that exist — which looks like a broken filter and is really just the sample.
    Local data that misrepresents the real data is worse than no local data.
    """
    from appkit import db

    from app import logic

    out = tmp_path / "seed.sql"
    devseed.build_devseed(out)

    for table in ("dl_buy", "dl_ref", "dl_buy_enemy", "dl_ref_enemy",
                  "dl_hero", "dl_item", "dl_comp", "dl_meta",
                  "dl_patch", "dl_patch_title", "dl_buy_patch",
                  "dl_buy_ally", "dl_ref_ally"):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    logic.clear_caches()
    for statement in logic._split_sql(out.read_text()):
        db.execute(statement)

    catalogue = db.query("SELECT tier, COUNT(*) AS n FROM dl_item GROUP BY tier ORDER BY tier")
    catalogue = {int(r["tier"]): int(r["n"]) for r in catalogue}

    hero_id = int(logic.heroes_with_data()[0]["value"])

    assert logic.item_counts_by_tier(hero_id) == catalogue, (
        "every hero should offer the full catalogue"
    )


def test_brawl_and_removed_items_never_reach_the_seed(tmp_path):
    """Only the standard shop is represented, at every tier."""
    out = tmp_path / "seed.sql"
    devseed.build_devseed(out)
    text = out.read_text()

    # Tier 5 is the Street Brawl tier; nothing in the seed may carry it.
    assert ", 5, 9999)" not in text
    assert "Mystical Piano" not in text
    assert "upgrade_clip_size_fixed" not in text
