"""Tests for the timing estimator.

The synthetic extract in ``conftest.seeded`` is built so every expected answer
can be worked out on paper. The most important case is "Confounded Item"
(id 10): it is bought almost exclusively by rich players and wins at exactly the
reference rate within each net-worth band, so its raw win rate is ~68% while its
true effect is zero. An estimator that reports anything much above zero for it
is reproducing the bug this whole app exists to avoid.
"""

import pytest

from app import logic
from app.logic import Z95

# ---------------------------------------------------------------------------
# the confounding this app exists to remove
# ---------------------------------------------------------------------------

def test_wealth_confound_is_removed(seeded):
    result = logic.estimate(hero_id=1, item_id=10)

    assert result.points, "the item should still produce a curve"
    for point in result.points:
        assert point.effect_pp == pytest.approx(0.0, abs=0.5), (
            f"minute {point.minute}: effect should be ~0 once net worth is "
            f"controlled for, got {point.effect_pp:+.2f}"
        )


def test_the_raw_winrate_is_reported_and_is_misleading(seeded):
    """The naive number stays visible, so the gap can be seen rather than trusted."""
    result = logic.estimate(hero_id=1, item_id=10)
    point = result.points[0]

    assert point.raw_winrate == pytest.approx(0.68, abs=0.01)
    assert point.effect_pp == pytest.approx(0.0, abs=0.5)


def test_a_confounded_item_gets_no_recommended_window(seeded):
    result = logic.estimate(hero_id=1, item_id=10)

    assert result.best is None
    assert "distinguishable" in result.verdict


# ---------------------------------------------------------------------------
# a genuine effect survives
# ---------------------------------------------------------------------------

def test_a_real_effect_is_recovered(seeded):
    result = logic.estimate(hero_id=1, item_id=20)

    baseline = next(p for p in result.points if p.minute == 10)
    assert baseline.effect_pp == pytest.approx(5.0, abs=0.5)


def test_the_peak_window_is_found(seeded):
    result = logic.estimate(hero_id=1, item_id=20)

    assert result.best is not None
    assert result.best.minute == 20
    assert result.best.effect_pp == pytest.approx(15.0, abs=0.5)
    assert "minute 20" in result.verdict


def test_the_item_stops_paying_off(seeded):
    result = logic.estimate(hero_id=1, item_id=20)

    assert result.dead_from is not None
    assert result.dead_from.minute == 34
    assert "no longer pays for itself" in result.verdict


def test_a_dip_in_the_point_estimate_alone_is_not_a_dead_zone(seeded):
    """The old version asserted the very predicate `dead_from` filters on.

    That could not fail for any data. This constructs the case it was meant to
    exclude: a window whose estimate dips below zero while its interval still
    straddles it. That is noise, and must not be reported as the item having
    stopped paying off.
    """
    def point(minute, effect, half_width):
        return logic.TimingPoint(
            minute=minute, purchases=5_000, raw_winrate=0.5, ref_winrate=0.5,
            effect_pp=effect, ci_low_pp=effect - half_width,
            ci_high_pp=effect + half_width, low_confidence=False,
        )

    result = logic.TimingResult(
        hero_id=1, item_id=20, hero_name="T", item_name="I",
        enemy_ids=[], enemy_names=[], ally_ids=[], ally_names=[],
        points=[point(10, 6.0, 2.0), point(20, -1.0, 4.0), point(30, -8.0, 2.0)],
        level="test", total_purchases=15_000,
    )

    assert result.best.minute == 10
    # Minute 20 dips negative but its interval spans zero, so the dead zone
    # starts at minute 30, where the whole interval is below it.
    assert result.dead_from.minute == 30


# ---------------------------------------------------------------------------
# enemy conditioning
# ---------------------------------------------------------------------------

def test_enemy_presence_shifts_the_estimate(seeded):
    plain = logic.estimate(hero_id=1, item_id=20)
    versus = logic.estimate(hero_id=1, item_id=20, enemy_ids=[2])

    at20_plain = next(p for p in plain.points if p.minute == 20)
    at20_versus = next(p for p in versus.points if p.minute == 20)

    assert at20_versus.effect_pp > at20_plain.effect_pp
    assert "hero(es) in this match" in versus.level


def test_an_enemy_without_data_does_not_invent_a_number(seeded):
    """Hero 3 has a baseline but no purchase rows, so it must not adjust anything."""
    plain = logic.estimate(hero_id=1, item_id=20)
    versus = logic.estimate(hero_id=1, item_id=20, enemy_ids=[3])

    at20_plain = next(p for p in plain.points if p.minute == 20)
    at20_versus = next(p for p in versus.points if p.minute == 20)

    assert at20_versus.effect_pp == pytest.approx(at20_plain.effect_pp)
    assert any("Not enough games" in note for note in versus.notes)


def test_the_estimate_level_is_always_reported(seeded):
    assert "net-worth adjusted" in logic.estimate(1, 20).level


# ---------------------------------------------------------------------------
# compositions
# ---------------------------------------------------------------------------

def test_a_known_composition_is_recognised(seeded):
    result = logic.estimate(1, 20, enemy_ids=[7, 6, 5, 4, 3, 2])

    assert result.comp is not None or any("most played" in n for n in result.notes)


def test_an_unknown_full_composition_says_so(seeded):
    result = logic.estimate(1, 20, enemy_ids=[2, 3, 4, 5, 6, 8])

    assert any("not in the 10,000" in note for note in result.notes)


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

def test_blank_enemy_slots_are_ignored(seeded):
    assert logic.parse_enemy_ids(["", "2", "", "3"]) == [2, 3]


def test_duplicate_enemies_are_collapsed(seeded):
    assert logic.parse_enemy_ids(["2", "2"]) == [2]


def test_unknown_enemy_is_rejected(seeded):
    with pytest.raises(ValueError):
        logic.parse_enemy_ids(["9999"])


def test_more_than_six_enemies_is_rejected(seeded):
    with pytest.raises(ValueError):
        logic.parse_enemy_ids(["2", "3", "4", "5", "6", "7", "8"])


def test_missing_item_returns_an_explanation_not_a_crash(seeded):
    result = logic.estimate(hero_id=1, item_id=999)

    assert result.points == []
    assert result.level == "no data"
    assert result.notes


# ---------------------------------------------------------------------------
# patches
# ---------------------------------------------------------------------------

def test_patches_are_listed_newest_first(seeded):
    eras = logic.patches()

    assert [e["patch_idx"] for e in eras] == [1, 0]
    assert eras[0]["is_current"] is True
    assert "Newer Update" in eras[0]["title"]


def test_a_nerf_between_patches_is_detected(seeded):
    comparison = logic.compare_patches(item_id=20, before_idx=0, after_idx=1)

    assert comparison is not None
    # Item 20 goes 58% -> 46% while its comparison group holds at 50%,
    # so the effect moves from +8 pp to -4 pp: a 12 point drop.
    assert comparison.before.effect_pp == pytest.approx(8.0, abs=0.5)
    assert comparison.after.effect_pp == pytest.approx(-4.0, abs=0.5)
    assert comparison.delta_pp == pytest.approx(-12.0, abs=0.5)
    assert comparison.significant
    assert "does worse" in comparison.verdict


def test_an_unchanged_item_is_not_called_a_change(seeded):
    comparison = logic.compare_patches(item_id=10, before_idx=0, after_idx=1)

    assert comparison is not None
    assert not comparison.significant
    assert "about the same" in comparison.verdict


def test_the_trend_covers_every_era(seeded):
    trend = logic.patch_trend(item_id=20)

    assert [e.patch_idx for e in trend] == [0, 1]
    assert trend[0].effect_pp > trend[1].effect_pp


def test_an_unknown_patch_is_rejected(seeded):
    with pytest.raises(ValueError):
        logic.parse_patch_idx("99")


def test_a_blank_patch_choice_means_no_comparison(seeded):
    assert logic.parse_patch_idx("") is None
    assert logic.parse_patch_idx(None) is None


def test_the_patch_measure_is_relative_to_other_purchases(seeded):
    """A limitation worth pinning, because it is easy to misread as a bug.

    An item's patch figure is its win rate against *other purchases at the same
    wealth in the same era*. An item's own rows are excluded from its reference,
    so when the rest of the pool shifts, an item whose own win rate never moved
    still shows a change.

    Item 30's win rate is identical in both eras. Its comparison pool is items 10
    and 20, and item 20 was nerfed by 12 points — so item 30 reads as +6 pp
    purely from the pool moving under it. That is the measure behaving as
    defined, not a defect, and it is why the app tells the reader on screen that
    a change in what others buy can move this line.
    """
    filler = logic.compare_patches(item_id=30, before_idx=0, after_idx=1)

    assert filler is not None
    assert filler.before.effect_pp == pytest.approx(-4.0, abs=0.5)
    assert filler.after.effect_pp == pytest.approx(2.0, abs=0.5)
    assert filler.delta_pp == pytest.approx(6.0, abs=0.5)


def test_an_unchanged_item_is_stable_when_the_pool_is_dominated(seeded):
    """The flip side: item 10's reference is dominated by the constant item 30.

    That is what makes its "no change" verdict meaningful rather than an
    artefact of a two-item pool.
    """
    unchanged = logic.compare_patches(item_id=10, before_idx=0, after_idx=1)

    assert abs(unchanged.delta_pp) < 1.0


# ---------------------------------------------------------------------------
# win rate against purchase time
# ---------------------------------------------------------------------------

def test_the_matched_reference_rate_is_reported(seeded):
    """The confounded item's two lines must sit on top of each other.

    Item 10 wins at exactly the reference rate within every net-worth band, so
    the win rate of its buyers and of comparable non-buyers should coincide —
    which is what makes the flat gap on the chart mean "no effect".
    """
    result = logic.estimate(hero_id=1, item_id=10)

    for point in result.points:
        assert point.raw_winrate == pytest.approx(point.ref_winrate, abs=0.005)


def test_the_gap_between_the_lines_matches_the_fixtures_known_effect(seeded):
    """Pinned against the fixture's designed values, not against the code.

    Asserting `raw - ref == effect` only restates how `_base_curve` computes
    them and holds for any data. Item 20 is built to beat its reference by a
    flat 5 points, spiking to 15 at minute 20 — so the gap on the chart has
    known values, and both lines are checked against them.
    """
    result = logic.estimate(hero_id=1, item_id=20)
    by_minute = {p.minute: p for p in result.points}

    early = by_minute[10]
    assert (early.raw_winrate - early.ref_winrate) * 100.0 == pytest.approx(5.0, abs=0.3)

    spike = by_minute[20]
    assert (spike.raw_winrate - spike.ref_winrate) * 100.0 == pytest.approx(15.0, abs=0.3)

    late = by_minute[36]
    assert (late.raw_winrate - late.ref_winrate) * 100.0 == pytest.approx(-10.0, abs=0.3)


def test_the_confounded_item_still_shows_a_high_win_rate(seeded):
    """Both facts are true at once, and the app has to show both.

    Item 10's buyers win 68% of the time and the item does nothing. A win-rate
    chart without the reference line would be read as an endorsement.
    """
    point = logic.estimate(hero_id=1, item_id=10).points[0]

    assert point.raw_winrate == pytest.approx(0.68, abs=0.01)
    assert point.effect_pp == pytest.approx(0.0, abs=0.5)


# ---------------------------------------------------------------------------
# which items are offered at all
# ---------------------------------------------------------------------------

def test_the_item_picker_is_not_truncated(seeded):
    """Every item with enough data must be offered, not the first N by name.

    A name-ordered limit used to cut the list off mid-alphabet, which dropped
    cheap components while keeping the expensive items they build into.
    """
    offered = logic.items_for_hero(hero_id=1)

    assert {o["value"] for o in offered} == {"10", "20", "30"}


def test_the_tier_is_chosen_before_the_item(seeded):
    """156 items is too many for one list, and a player is choosing what to
    spend the souls they have — so the tier comes first."""
    tiers = logic.tier_options(hero_id=1)

    assert [t["value"] for t in tiers] == ["1", "2", "4"]
    assert "1 item" in tiers[0]["label"]


def test_the_item_list_is_filtered_to_the_chosen_tier(seeded):
    assert [o["value"] for o in logic.items_for_hero(1, tier=2)] == ["20"]
    assert [o["value"] for o in logic.items_for_hero(1, tier=4)] == ["10"]


def test_the_form_opens_on_the_cheapest_tier_with_data(seeded):
    assert logic.default_tier(hero_id=1) == 1


def test_a_tier_the_hero_has_no_items_for_is_rejected(seeded):
    with pytest.raises(ValueError):
        logic.parse_tier("3", hero_id=1)


def test_a_blank_tier_falls_back_rather_than_failing(seeded):
    assert logic.parse_tier("", hero_id=1) == 1


# ---------------------------------------------------------------------------
# build path
# ---------------------------------------------------------------------------

def test_the_plan_respects_deadlocks_slot_limits(seeded):
    plan = logic.plan_build(hero_id=1)

    for slot, used in plan.slot_usage.items():
        assert used <= logic.SLOT_CAPACITY, f"{slot} over its four slots"
    assert len(plan.steps) <= logic.BUILD_SIZE


def test_purchases_are_ordered_in_time(seeded):
    """The order column is a purchase sequence, so targets cannot go backwards."""
    plan = logic.plan_build(hero_id=1)
    targets = [step.target_minute for step in plan.steps]

    assert targets == sorted(targets)
    assert [s.order for s in plan.steps] == list(range(1, len(plan.steps) + 1))


def test_an_item_affordable_from_the_start_is_not_pushed_to_the_end(seeded):
    """Regression: `affordable or MAX_MINUTE` treated minute 0 as "no answer".

    A cheap item affordable at minute 0 was given a target of minute 40, which
    put the cheapest early purchases last in the build.
    """
    plan = logic.plan_build(hero_id=1)
    cheapest = min(plan.steps, key=lambda s: s.cost)

    assert cheapest.target_minute < logic.MAX_MINUTE


def test_the_target_is_never_earlier_than_the_item_can_be_afforded(seeded):
    plan = logic.plan_build(hero_id=1)
    curve = logic.souls_curve(hero_id=1)

    for step in plan.steps:
        if step.limited_by == "unreachable":
            continue
        assert curve.get(step.target_minute, 0) >= step.cumulative_cost


def test_the_target_is_never_earlier_than_the_items_own_peak(seeded):
    for step in logic.plan_build(hero_id=1).steps:
        assert step.target_minute >= min(step.peak_minute, logic.MAX_MINUTE)


def test_the_souls_curve_uses_the_median_not_the_mean(seeded):
    """The top net-worth band is open-ended, so a mean over-states what a
    typical player can afford."""
    curve = logic.souls_curve(hero_id=1)

    # The fixture splits every minute evenly between band 0 and band 3, so the
    # median band is 0 and a mean would sit between the two.
    assert curve[20] == logic._band_midpoint(0)


def test_the_plan_reports_a_range_not_a_single_number(seeded):
    """A composed total is an assumption, so it is never quoted on its own.

    Multiplying twelve marginal effects gave a 97% win rate, which is not
    credible: each effect was measured against players who already had builds,
    so the effects overlap by construction. The plan therefore reports a floor
    (only the best item counts) and a ceiling (all items independent).
    """
    plan = logic.plan_build(hero_id=1)

    assert plan.total_floor <= plan.total_winrate
    assert plan.baseline <= plan.total_floor
    assert "somewhere between" in plan.summary
    assert "Neither is true" in plan.summary
    assert any("floor" in note and "ceiling" in note for note in plan.notes)


def test_the_floor_is_the_best_single_item_alone(seeded):
    plan = logic.plan_build(hero_id=1)
    best = max(step.effect_pp for step in plan.steps)

    assert plan.total_floor == pytest.approx(
        logic.combine_effects(plan.baseline, [best])
    )


def test_the_running_range_never_inverts(seeded):
    for step in logic.plan_build(hero_id=1).steps:
        assert step.cumulative_floor <= step.cumulative_winrate


def test_a_hero_without_data_gets_an_explanation_not_a_crash(seeded):
    plan = logic.plan_build(hero_id=2)

    assert plan.steps == []
    assert plan.notes


def test_effects_compose_as_odds_ratios_not_by_addition(seeded):
    """Twelve items at six points each must not read as +72 points."""
    combined = logic.combine_effects(0.50, [6.0] * 12)

    assert combined < 1.0
    assert combined > 0.50
    # Far below the 1.22 that naive addition would produce.
    assert combined < 0.95


def test_combining_nothing_leaves_the_baseline(seeded):
    assert logic.combine_effects(0.47, []) == pytest.approx(0.47)


def test_a_single_effect_reproduces_itself(seeded):
    """One item composed against its own baseline must return that win rate."""
    assert logic.combine_effects(0.50, [4.0]) == pytest.approx(0.54, abs=1e-6)


def test_the_running_total_only_rises_with_positive_effects(seeded):
    plan = logic.plan_build(hero_id=1)
    running = [step.cumulative_winrate for step in plan.steps]

    assert running == sorted(running)
    assert plan.total_winrate == running[-1]
    assert plan.baseline <= running[0]


def test_every_strategy_produces_a_legal_build(seeded):
    for key in logic.STRATEGIES:
        plan = logic.plan_build(hero_id=1, strategy=key)

        assert plan.strategy == key
        for slot, used in plan.slot_usage.items():
            assert used <= logic.SLOT_CAPACITY, f"{key}/{slot} over its slots"
        targets = [s.target_minute for s in plan.steps]
        assert targets == sorted(targets), f"{key} is out of time order"


def _candidate(item_id, cost, low, effect, roster=0.0, slot="weapon"):
    """An ItemCandidate with the two windows set to known values."""
    point = logic.TimingPoint(
        minute=10, purchases=5_000, raw_winrate=0.55, ref_winrate=0.50,
        effect_pp=effect, ci_low_pp=low, ci_high_pp=effect + (effect - low),
        low_confidence=False,
    )
    return logic.ItemCandidate(
        item_id=item_id, name=f"Item {item_id}", tier=1, cost=cost, slot=slot,
        safe=point, upside=point, roster_gain_pp=roster, points=[point],
    )


def test_the_strategies_rank_differently(seeded):
    """Each build path must be a different question, not a re-sort of one answer.

    The fixture cannot show this — it has too few items for two strategies to
    disagree — so the ranking keys are exercised directly.
    """
    cheap = _candidate(1, cost=800, low=3.0, effect=4.0, roster=0.5)
    dear = _candidate(2, cost=6400, low=5.0, effect=6.0, roster=0.2)
    risky = _candidate(3, cost=1600, low=0.5, effect=9.0, roster=4.0)

    def top(strategy):
        key = logic.STRATEGIES[strategy]["key"]
        return max([cheap, dear, risky], key=key).item_id

    assert top("safe") == 2, "Safest should take the best worst-case"
    assert top("upside") == 3, "Upside should take the biggest point estimate"
    assert top("economy") == 1, "Value should take the most effect per soul"
    assert top("counter") == 3, "Counter-pick should take the most matchup-specific"


def test_an_unknown_strategy_is_rejected(seeded):
    with pytest.raises(ValueError):
        logic.parse_strategy("wishful")


def test_the_default_strategy_is_the_conservative_one(seeded):
    assert logic.parse_strategy("") == "safe"
    assert logic.parse_strategy(None) == "safe"


# ---------------------------------------------------------------------------
# things an audit found the suite was not actually checking
# ---------------------------------------------------------------------------

def test_band_midpoints_have_the_values_the_scheduler_assumes():
    """Literal values, not a self-reference.

    This is the only place net-worth bands become souls, and it decides every
    target minute in every build. The previous test asserted
    `curve[20] == _band_midpoint(0)` — the function on both sides — so shifting
    every player's wealth by 5,000 souls left the whole suite green.
    """
    assert logic._band_midpoint(0) == 2_500
    assert logic._band_midpoint(1) == 7_500
    assert logic._band_midpoint(3) == 17_500


def test_the_open_ended_top_band_uses_its_floor_not_a_midpoint():
    """Band 11 is "55,000 or more" — there is no midpoint to take."""
    top = logic._band_midpoint(logic.NW_BAND_MAX)

    assert top == 55_000
    assert top < logic.NW_BAND_MAX * logic.NW_BAND_SIZE + logic.NW_BAND_SIZE / 2


def test_the_slot_cap_actually_binds(seeded):
    """The old assertion held with the cap removed entirely.

    The seeded plan only ever had two items in two different slots, so
    `used <= SLOT_CAPACITY` could not fail. Here twenty candidates all compete
    for the same four weapon slots.
    """
    crowd = [
        _candidate(i, cost=800, low=10.0 - i * 0.1, effect=12.0, slot="weapon")
        for i in range(1, 21)
    ]
    plan = logic.plan_build(hero_id=1, candidates=crowd)

    assert len(plan.steps) == logic.SLOT_CAPACITY
    assert plan.slot_usage["weapon"] == logic.SLOT_CAPACITY
    assert {s.slot for s in plan.steps} == {"weapon"}


def test_a_build_fills_every_slot_type_before_doubling_up(seeded):
    mixed = [
        _candidate(i, cost=800, low=5.0, effect=6.0, slot=slot)
        for i, slot in enumerate(logic.SLOTS * 5, start=1)
    ]
    plan = logic.plan_build(hero_id=1, candidates=mixed)

    assert len(plan.steps) == logic.BUILD_SIZE
    for slot in logic.SLOTS:
        assert plan.slot_usage[slot] == logic.SLOT_CAPACITY


def test_an_unknown_slot_is_not_silently_dropped(seeded):
    """A catalogue slot the planner does not know must not vanish unexplained."""
    odd = [_candidate(1, cost=800, low=9.0, effect=10.0, slot="mystery")]
    plan = logic.plan_build(hero_id=1, candidates=odd)

    assert plan.steps == []
    assert plan.notes


def test_a_semicolon_inside_an_item_name_does_not_split_the_statement():
    """Patch titles and item names are data; a `;` in one used to truncate it."""
    sql = (
        "INSERT INTO dl_item (item_id, name) VALUES (1, 'Update; hotfix');\n"
        "INSERT INTO dl_item (item_id, name) VALUES (2, 'Plain');"
    )
    statements = logic._split_sql(sql)

    assert len(statements) == 2
    assert "Update; hotfix" in statements[0]


def test_an_apostrophe_in_a_name_survives_splitting():
    sql = "INSERT INTO dl_item (name) VALUES ('Hunter''s Aura'); SELECT 1;"
    statements = logic._split_sql(sql)

    assert len(statements) == 2
    assert "Hunter''s Aura" in statements[0]


def test_a_semicolon_in_a_comment_does_not_split_the_statement():
    sql = "CREATE TABLE t (\n  a INTEGER  -- weapon; vitality; spirit\n);\nSELECT 1;"
    statements = logic._split_sql(sql)

    assert len(statements) == 2
    assert "CREATE TABLE" in statements[0]


# ---------------------------------------------------------------------------
# how a line-up adjustment is combined
# ---------------------------------------------------------------------------

def test_the_lineup_shift_is_averaged_once_over_both_teams(seeded):
    """Not summed, and not two per-roster averages added together.

    The fixture gives one enemy worth +10 pp and one ally worth +7.5 pp in the
    20-25 window. Pooled and averaged over the two contributors that is +8.75 —
    summing would give +17.5, and averaging each side then adding would give the
    same +17.5 while looking like an average.
    """
    shift, se, contributors = logic.lineup_shift(
        hero_id=1, item_id=20, enemy_ids=[2], ally_ids=[2]
    )

    assert contributors == 2
    assert shift[20] == pytest.approx(8.75, abs=0.2)
    assert se[20] > 0.0


def test_a_hero_without_data_does_not_shrink_the_denominator(seeded):
    """Hero 3 has a baseline but no purchase rows, so it cannot contribute."""
    with_three, _, contributors = logic.lineup_shift(
        hero_id=1, item_id=20, enemy_ids=[2, 3], ally_ids=[]
    )
    alone, _, alone_contributors = logic.lineup_shift(
        hero_id=1, item_id=20, enemy_ids=[2], ally_ids=[]
    )

    assert contributors == alone_contributors == 1
    assert with_three[20] == pytest.approx(alone[20])


def test_the_ally_adjustment_works_on_its_own(seeded):
    """Previously only the enemy path was exercised."""
    shift, _, contributors = logic.lineup_shift(
        hero_id=1, item_id=20, enemy_ids=[], ally_ids=[2]
    )

    assert contributors == 1
    assert shift[20] == pytest.approx(7.5, abs=0.2)


def test_the_adjustment_widens_the_interval_by_its_own_error(seeded):
    """The old code widened by `abs(shift) * 0.5`, which meant nothing."""
    plain = logic.estimate(hero_id=1, item_id=20)
    versus = logic.estimate(hero_id=1, item_id=20, enemy_ids=[2])

    at20_plain = next(p for p in plain.points if p.minute == 20)
    at20_versus = next(p for p in versus.points if p.minute == 20)

    assert at20_versus.se_pp > at20_plain.se_pp
    # and the widening is the two errors combined, not a fraction of the shift
    _, se, _ = logic.lineup_shift(1, 20, [2], [])
    expected = (at20_plain.se_pp ** 2 + se[20] ** 2) ** 0.5
    assert at20_versus.se_pp == pytest.approx(expected, abs=1e-9)


def test_a_shift_in_who_buys_an_item_is_not_reported_as_a_change(seeded):
    """Two eras must be weighted by the same net-worth mix.

    Item 40 wins at exactly 55% in every band in both eras — it did not change.
    What changed is who bought it: mostly poor players in the first era, mostly
    rich ones in the second. Because the comparison pool wins at 40% among poor
    players and 60% among rich ones, weighting each era by its *own* buyers
    turns that composition shift into a large fake nerf. The test asserts both
    halves: the artifact is real if you weight per era, and the shared weighting
    removes it.
    """
    from appkit import db

    db.execute(
        "INSERT INTO dl_item (item_id, name, tier, cost, slot) "
        "VALUES (40, 'Unchanged Item', 2, 1600, 'weapon')"
    )
    db.execute(
        "INSERT INTO dl_item (item_id, name, tier, cost, slot) "
        "VALUES (50, 'Pool Item', 2, 1600, 'weapon')"
    )
    for idx in (0, 1):
        # The pool's win rate depends on wealth, which is what makes the mix
        # matter at all.
        for band, rate in ((0, 0.40), (3, 0.60)):
            db.execute(
                "INSERT INTO dl_buy_patch (item_id, patch_idx, nw_band, n, wins) "
                "VALUES (50, %s, %s, 200000, %s)",
                (idx, band, round(200_000 * rate)),
            )
    # Item 40: same 55% everywhere, but its buyers move from poor to rich.
    for idx, (n_poor, n_rich) in enumerate([(20_000, 2_000), (2_000, 20_000)]):
        for band, n in ((0, n_poor), (3, n_rich)):
            db.execute(
                "INSERT INTO dl_buy_patch (item_id, patch_idx, nw_band, n, wins) "
                "VALUES (40, %s, %s, %s, %s)",
                (idx, band, n, round(n * 0.55)),
            )

    totals = logic._patch_band_totals()
    eras = {p["patch_idx"]: p for p in logic.patches()}

    # Weighted by each era's own buyers, the untouched item swings by ~7.6
    # points — comfortably "significant" at these sample sizes, and entirely an
    # artifact of who was buying.
    naive_before = logic._patch_effect(40, eras[0], totals)
    naive_after = logic._patch_effect(40, eras[1], totals)
    naive_delta = abs(naive_after.effect_pp - naive_before.effect_pp)
    assert naive_delta > 5.0
    assert naive_delta > Z95 * (naive_before.se_pp ** 2 + naive_after.se_pp ** 2) ** 0.5

    # Weighted by the two eras combined, it is correctly reported as unchanged.
    comparison = logic.compare_patches(item_id=40, before_idx=0, after_idx=1)
    assert comparison is not None
    assert comparison.delta_pp == pytest.approx(0.0, abs=0.5)
    assert not comparison.significant


def test_the_picker_only_offers_items_that_can_actually_be_estimated(seeded):
    """Offering an item that then reports "no data" is a broken promise.

    Item 60 has a large total purchase count spread so thinly across bands that
    no single cell clears the threshold the estimator uses, so it has no curve.
    The old picker selected on the total and offered it anyway.
    """
    from appkit import db

    db.execute(
        "INSERT INTO dl_item (item_id, name, tier, cost, slot) "
        "VALUES (60, 'Thinly Spread', 2, 1600, 'weapon')"
    )
    for minute in range(0, 42, 2):
        for band in (0, 3):
            db.execute(
                "INSERT INTO dl_buy (hero_id, item_id, minute, nw_band, n, wins) "
                "VALUES (1, 60, %s, %s, 10, 6)",
                (minute, band),
            )

    offered = {o["value"] for o in logic.items_for_hero(hero_id=1)}
    assert "60" not in offered

    # And the estimator agrees there is nothing there.
    assert logic.estimate(hero_id=1, item_id=60).points == []
