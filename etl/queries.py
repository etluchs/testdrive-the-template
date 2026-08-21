"""The ClickHouse queries the pipeline runs against ``/v1/sql``.

They live in one module so the statistics are reviewable in one place.

The question the app answers is "when should I buy this item?", and the naive
way to answer it is wrong. Grouping purchases by minute and averaging the win
column produces a curve that mostly measures *who was already winning*: a player
who buys a tier-4 item at minute 15 could afford it at minute 15, which usually
means the game was already going well. Late buys are additionally survivor-
biased, because losing games end early.

So every purchase is recorded together with the buyer's net worth at the moment
of purchase (``items.net_worth_at_buy``), and a matching *reference* population
is recorded from the per-minute ``stats.*`` series: players on the same hero, at
the same minute, in the same net-worth band, whatever they bought. Comparing a
purchase against that reference — rather than against everyone — is what turns
"correlates with winning" into something closer to "helped".

Every query takes a *list* of heroes and groups by ``hero_id``. That is not
cosmetic: the API allows 20 requests per hour, so a query per hero would put a
full build at roughly six hours. Batching heroes trades response size, which is
free, for request count, which is the binding constraint.

Bucketing, fixed here and mirrored in ``app/logic.py``:

* ``minute``      – 2-minute buckets, 0..``MAX_MINUTE``
* ``minute5``     – 5-minute buckets, used where the enemy dimension would
                    otherwise make the table too sparse to be worth storing
* ``nw_band``     – net worth in 5,000-soul bands, capped at band 11 (55k+)
"""

from __future__ import annotations

# Bot games and private lobbies are not the game people queue into.
MODES = "('Ranked', 'Unranked')"

# Below this many days of data, a patch-anchored window is too thin to estimate
# from and the caller is warned. It is a warning rather than a fallback on
# purpose: silently widening the window past a major patch would mix balance
# eras, which is the thing the anchoring exists to prevent.
MIN_WINDOW_DAYS = 30

MAX_MINUTE = 40
MAX_SECOND = MAX_MINUTE * 60

NW_BAND_SIZE = 5_000
NW_BAND_MAX = 11

_NW_BAND = f"least(intDiv(nw, {NW_BAND_SIZE}), {NW_BAND_MAX})"


def window(since: str) -> str:
    """Rows from the current balance era only.

    ``since`` is the day the most recent *major* patch landed (see
    ``DeadlockClient.current_patch_start``). Matches before it were played under
    different item and hero numbers, so including them would answer a question
    about a game that no longer exists.
    """
    return f"start_time >= toDateTime('{since} 00:00:00') AND match_mode IN {MODES}"


def _id_list(ids: list[int]) -> str:
    return ", ".join(str(int(i)) for i in ids)


def top_compositions(since: str, limit: int = 10_000) -> str:
    """The ``limit`` most frequently played 6-hero team compositions.

    Deadlock never repeats a hero inside a match, so a composition is an
    unordered set of six distinct hero ids; ``arraySort`` gives it a canonical
    form that can be used as a key.
    """
    return f"""
    WITH comps AS (
        SELECT arraySort(groupArray(hero_id)) AS comp
        FROM match_player
        WHERE {window(since)}
        GROUP BY match_id, team
    ),
    freq AS (
        SELECT comp, count() AS n FROM comps GROUP BY comp
    )
    SELECT arrayStringConcat(arrayMap(x -> toString(x), comp), '-') AS comp_key,
           comp AS heroes,
           n
    FROM freq
    ORDER BY n DESC
    LIMIT {limit}
    """


def purchase_stats(since: str, hero_ids: list[int], item_ids: list[int]) -> str:
    """Purchases: hero x item x minute x net-worth-at-buy band.

    ``items.*`` is not only shop purchases — it also carries an entry every time
    an **ability** point is spent, and those repeat many times per match. Passing
    the catalogue of buyable upgrades in ``item_ids`` is what keeps abilities out
    of the purchase table; without it the most "bought item" on every hero is one
    of that hero's own abilities.

    Within the buyable items there is one row per purchase event, so no
    de-duplication is needed.
    """
    return f"""
    SELECT hero_id,
           iid AS item_id,
           intDiv(t, 120) * 2 AS minute,
           {_NW_BAND} AS nw_band,
           count() AS n,
           countIf(winning_team = team) AS wins
    FROM match_player
    ARRAY JOIN `items.game_time_s` AS t,
               `items.item_id` AS iid,
               `items.net_worth_at_buy` AS nw
    WHERE {window(since)}
      AND hero_id IN ({_id_list(hero_ids)})
      AND t <= {MAX_SECOND}
      AND team IN ('Team0', 'Team1')
      AND iid IN ({_id_list(item_ids)})
    GROUP BY hero_id, item_id, minute, nw_band
    """


def reference_stats(since: str, hero_ids: list[int]) -> str:
    """The comparison population: everyone on those heroes, bought or not.

    **Read this before changing the bucketing.** The obvious version — bucket
    each ``stats.*`` sample by its own timestamp — is wrong, and wrong in a way
    that is invisible until you look at real data. The series is not sampled
    every two minutes, so a player alive throughout appears in some buckets and
    not others. Measured on Billy over the current patch, minutes 2, 6, 8 and 12
    each had ~3.2M reference players while minutes 4, 10, 16, 18, 22 and 26 had
    30k-100k: a 1-10% survivor of the real population, and not a random one. The
    estimator then compared buyers against that skewed remnant and produced a
    +46 percentage-point effect for Phantom Strike, with a "comparable players"
    win rate of 16%.

    So the net worth used at minute *m* is the most recent sample **at or
    before** m — a step function, which is what "net worth at minute m" means —
    and a player is in the population for minute *m* only if their match
    actually reached it (``duration_s >= m * 60``). Without that second
    condition the forward fill carries players past the end of their own game
    and every minute reports an identical population, which compares a minute-40
    buyer against people whose match ended at minute 20.
    """
    return f"""
    SELECT hero_id,
           minute,
           {_NW_BAND} AS nw_band,
           count() AS n,
           countIf(won) AS wins
    FROM (
        SELECT hero_id,
               m AS minute,
               winning_team = team AS won,
               arrayLastIndex(x -> x <= m * 60, `stats.time_stamp_s`) AS idx,
               `stats.net_worth`[idx] AS nw
        FROM match_player
        ARRAY JOIN range(0, {MAX_MINUTE + 2}, 2) AS m
        WHERE {window(since)}
          AND hero_id IN ({_id_list(hero_ids)})
          AND team IN ('Team0', 'Team1')
          AND duration_s >= m * 60
    )
    WHERE idx > 0
    GROUP BY hero_id, minute, nw_band
    """


def enemy_purchase_stats(since: str, hero_ids: list[int], item_ids: list[int]) -> str:
    """Purchases split by which enemy heroes were on the other team.

    Crossing item x enemy x minute is what makes "Phantom Strike into Vindicta"
    answerable, and also what makes the table large — so the minute axis is
    coarsened to 5-minute buckets.

    The enemy roster comes from a self-aggregation of the same table: one row per
    match holding both teams' hero lists, joined back on ``match_id``.
    """
    return f"""
    WITH rosters AS (
        SELECT match_id,
               groupArrayIf(hero_id, team = 'Team0') AS t0,
               groupArrayIf(hero_id, team = 'Team1') AS t1
        FROM match_player
        WHERE {window(since)}
        GROUP BY match_id
    )
    SELECT hero_id, item_id, enemy_hero_id, minute5,
           count() AS n,
           countIf(won) AS wins
    FROM (
        SELECT hero_id,
               iid AS item_id,
               intDiv(t, 300) * 5 AS minute5,
               winning_team = team AS won,
               if(team = 'Team0', rosters.t1, rosters.t0) AS enemies
        FROM match_player
        INNER JOIN rosters USING (match_id)
        ARRAY JOIN `items.game_time_s` AS t,
                   `items.item_id` AS iid
        WHERE {window(since)}
          AND hero_id IN ({_id_list(hero_ids)})
          AND t <= {MAX_SECOND}
          AND team IN ('Team0', 'Team1')
          AND iid IN ({_id_list(item_ids)})
    )
    ARRAY JOIN enemies AS enemy_hero_id
    GROUP BY hero_id, item_id, enemy_hero_id, minute5
    """


def enemy_reference_stats(since: str, hero_ids: list[int]) -> str:
    """Baseline win rate for each hero against each individual enemy hero.

    Without this, a strong hero would look like a strong *item* every time it
    met a weak enemy. The enemy-conditioned purchase numbers are read relative
    to these.
    """
    return f"""
    SELECT hero_id, enemy_hero_id,
           count() AS n,
           countIf(won) AS wins
    FROM (
        SELECT hero_id,
               winning_team = team AS won,
               if(team = 'Team0', rosters.t1, rosters.t0) AS enemies
        FROM match_player
        INNER JOIN (
            SELECT match_id,
                   groupArrayIf(hero_id, team = 'Team0') AS t0,
                   groupArrayIf(hero_id, team = 'Team1') AS t1
            FROM match_player
            WHERE {window(since)}
            GROUP BY match_id
        ) AS rosters USING (match_id)
        WHERE {window(since)}
          AND hero_id IN ({_id_list(hero_ids)})
          AND team IN ('Team0', 'Team1')
    )
    ARRAY JOIN enemies AS enemy_hero_id
    GROUP BY hero_id, enemy_hero_id
    """


def patch_purchase_stats(boundaries: list[str], item_ids: list[int]) -> str:
    """An item's win rate per patch era, for comparing releases against each other.

    ``boundaries`` are the era start dates in **ascending** order; a match falls
    in era ``i`` if it was played on or after ``boundaries[i]`` and before
    ``boundaries[i+1]``. ``arrayCount`` does that lookup inline, which is what
    lets every era come back from a single request — one query per era would
    cost ten of the twenty requests available in an hour.

    This is the only extract that reaches back beyond the current patch, and it
    is deliberately coarse: no hero and no minute dimension. Crossing eras with
    either would divide the data past the point of meaning, and the question
    here is only whether an item got better or worse, not when to buy it.

    Note the win column is *not* comparable to ``dl_buy`` — see the reference
    note on ``dl_buy_patch`` in ``schema.sql``.
    """
    edges = ", ".join(f"toDateTime('{b} 00:00:00')" for b in boundaries)
    return f"""
    SELECT iid AS item_id,
           arrayCount(x -> x <= start_time, [{edges}]) - 1 AS patch_idx,
           {_NW_BAND} AS nw_band,
           count() AS n,
           countIf(winning_team = team) AS wins
    FROM match_player
    ARRAY JOIN `items.game_time_s` AS t,
               `items.item_id` AS iid,
               `items.net_worth_at_buy` AS nw
    WHERE start_time >= toDateTime('{boundaries[0]} 00:00:00')
      AND match_mode IN {MODES}
      AND t <= {MAX_SECOND}
      AND team IN ('Team0', 'Team1')
      AND iid IN ({_id_list(item_ids)})
    GROUP BY item_id, patch_idx, nw_band
    HAVING patch_idx >= 0
    """


def ally_purchase_stats(since: str, hero_ids: list[int], item_ids: list[int]) -> str:
    """Purchases split by which heroes were on the buyer's **own** team.

    Identical in shape to :func:`enemy_purchase_stats`, but selecting the
    buyer's own roster rather than the opposition's, and excluding the buyer
    so a hero is never counted as its own ally.
    """
    return f"""
    WITH rosters AS (
        SELECT match_id,
               groupArrayIf(hero_id, team = 'Team0') AS t0,
               groupArrayIf(hero_id, team = 'Team1') AS t1
        FROM match_player
        WHERE {window(since)}
        GROUP BY match_id
    )
    SELECT hero_id, item_id, ally_hero_id, minute5,
           count() AS n,
           countIf(won) AS wins
    FROM (
        SELECT hero_id,
               iid AS item_id,
               intDiv(t, 300) * 5 AS minute5,
               winning_team = team AS won,
               arrayFilter(x -> x != hero_id,
                           if(team = 'Team0', rosters.t0, rosters.t1)) AS allies
        FROM match_player
        INNER JOIN rosters USING (match_id)
        ARRAY JOIN `items.game_time_s` AS t,
                   `items.item_id` AS iid
        WHERE {window(since)}
          AND hero_id IN ({_id_list(hero_ids)})
          AND t <= {MAX_SECOND}
          AND team IN ('Team0', 'Team1')
          AND iid IN ({_id_list(item_ids)})
    )
    ARRAY JOIN allies AS ally_hero_id
    GROUP BY hero_id, item_id, ally_hero_id, minute5
    """


def ally_reference_stats(since: str, hero_ids: list[int]) -> str:
    """Baseline win rate for each hero alongside each individual ally hero."""
    return f"""
    SELECT hero_id, ally_hero_id,
           count() AS n,
           countIf(won) AS wins
    FROM (
        SELECT hero_id,
               winning_team = team AS won,
               arrayFilter(x -> x != hero_id,
                           if(team = 'Team0', rosters.t0, rosters.t1)) AS allies
        FROM match_player
        INNER JOIN (
            SELECT match_id,
                   groupArrayIf(hero_id, team = 'Team0') AS t0,
                   groupArrayIf(hero_id, team = 'Team1') AS t1
            FROM match_player
            WHERE {window(since)}
            GROUP BY match_id
        ) AS rosters USING (match_id)
        WHERE {window(since)}
          AND hero_id IN ({_id_list(hero_ids)})
          AND team IN ('Team0', 'Team1')
    )
    ARRAY JOIN allies AS ally_hero_id
    GROUP BY hero_id, ally_hero_id
    """
