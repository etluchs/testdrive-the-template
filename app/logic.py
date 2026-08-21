"""Business logic: when is buying an item actually worth it?

Routes stay thin; everything below is unit-testable without a web server. This
module only ever calls into ``appkit`` — it never opens a network connection of
its own (see AGENTS.md). The numbers it reads were precomputed offline by
``etl/`` against the public Deadlock API.

The estimate
------------

The obvious way to answer "when should I buy this?" is to group purchases by
minute and average the win column. That answer is wrong, and confidently so: a
player who buys a tier-4 item at minute 15 could *afford* it at minute 15, which
usually means the game was already going well. The raw curve for Billy's Phantom
Strike climbs from 46% to 63% purely on that effect.

So each purchase is compared against a reference population rather than against
the global average: players on the same hero, at the same minute, holding a
similar net worth, whatever they bought. The difference between those two win
rates is what this module reports.

Formally this is direct standardisation over net-worth bands, weighted by the
distribution of the people who actually buy the item (an ATT-style estimand):

    effect(m) = sum_b w_b * ( winrate_buyers(m, b) - winrate_reference(m, b) )
    w_b       = purchases(m, b) / purchases(m)

It removes confounding by *wealth at the moment of purchase*. It does not make
this a randomised trial: anything that makes a player both buy early and win,
beyond their net worth, still leaks in. The app says so on screen.

Composition backoff
-------------------

Exact team compositions are far too sparse to carry their own timing curves.
Across 180 days there are ~1.9M distinct compositions; the single most common one
occurs ~1,100 times, and the 10,000th ~163 times. Split either across 251 items
and 21 minute buckets and nothing survives. So the enemy team is handled by
*presence* of individual enemy heroes, which is both estimable and closer to the
real mechanism — Phantom Strike is good into Vindicta because she is squishy and
mobile, not because of her identity. Estimates fall back a level whenever the
sample beneath them is too thin, and the level used is always reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from appkit import db

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed.sql"

# Bucketing — must match etl/queries.py.
MINUTE_STEP = 2
MAX_MINUTE = 40
MINUTE5_STEP = 5

# A band/minute cell thinner than this is noise; it is dropped rather than shown.
MIN_CELL_N = 30
# Below this many purchases in a minute bucket we mark the point low-confidence.
MIN_POINT_N = 100
# An enemy-conditioned cell needs at least this much before it may adjust anything.
MIN_ENEMY_N = 50

# Net-worth banding, mirrored from etl/queries.py.
NW_BAND_SIZE = 5_000
NW_BAND_MAX = 11

Z95 = 1.959964

#: Bonferroni-adjusted z for picking the best of the ~21 two-minute windows.
#: Two-sided alpha 0.05 spread over 21 comparisons.
N_WINDOWS = MAX_MINUTE // MINUTE_STEP + 1
Z_SELECT = 3.043


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

def clear_caches() -> None:
    """Drop every memo that depends on the loaded extract.

    The lru_caches below are keyed on hero or composition, not on which database
    is underneath them — so anything that swaps the data (a fresh seed, a test
    resetting appkit's in-memory backend) has to clear them or the next read
    silently answers from the previous dataset.
    """
    heroes.cache_clear()
    items.cache_clear()
    _reference_by_minute.cache_clear()
    composition_rank.cache_clear()
    _roster_baseline.cache_clear()


def seed_is_loaded() -> bool:
    """True if the precomputed tables are present and populated."""
    try:
        rows = db.query("SELECT count(*) AS n FROM dl_hero")
    except Exception:
        return False
    return bool(rows) and int(next(iter(rows[0].values()))) > 0


def load_seed(path: Path | None = None) -> None:
    """Load ``data/seed.sql`` into the appkit database if it is not there yet.

    The seed is a plain SQL file of ``CREATE TABLE`` plus multi-row ``INSERT``s,
    so the same file works against the in-memory SQLite appkit uses for tests and
    the real database it uses in production.
    """
    if seed_is_loaded():
        return
    seed = path or SEED_PATH
    if not seed.exists():
        raise FileNotFoundError(
            f"{seed} is missing — run `python -m etl.build --heroes all` to create it."
        )
    # One transaction for the whole file. Statement-at-a-time autocommit is
    # harmless against the in-memory fake — it is thrown away between runs — but
    # against a real database a failure part-way through leaves the tables
    # permanently half-filled, and `seed_is_loaded` would then see dl_hero
    # populated and never retry.
    statements = _split_sql(seed.read_text())
    with db.transaction() as tx:
        for statement in statements:
            tx.execute(statement)
    clear_caches()


def _split_sql(text: str) -> list[str]:
    """Split a SQL file into statements, respecting string literals and comments.

    Splitting naively on ``;`` breaks two ways, and the seed contains data that
    triggers both: a semicolon inside a ``--`` comment truncates the statement it
    annotates, and a semicolon inside a quoted item name or patch title
    (``Minor Update - 08-12-2026; hotfix``) cuts the INSERT in half. So this
    walks the text tracking whether it is inside a literal, and treats ``''`` as
    an escaped quote rather than the end of one.
    """
    out: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            # '' is an escaped quote inside a SQL string literal.
            if ch == "'" and text[i + 1 : i + 2] == "'":
                current.append("''")
                i += 2
                continue
            if ch == "'":
                in_string = False
            current.append(ch)
        elif ch == "'":
            in_string = True
            current.append(ch)
        elif ch == "-" and text[i + 1 : i + 2] == "-":
            # Comment runs to end of line, and may itself contain a semicolon.
            j = text.find("\n", i)
            i = len(text) if j == -1 else j
            continue
        elif ch == ";":
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    out.append("".join(current))
    return [statement.strip() for statement in out if statement.strip()]


# ---------------------------------------------------------------------------
# catalogue
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def heroes() -> dict[int, str]:
    return {int(r["hero_id"]): r["name"] for r in db.query("SELECT hero_id, name FROM dl_hero")}


@lru_cache(maxsize=1)
def items() -> dict[int, str]:
    return {int(r["item_id"]): r["name"] for r in db.query("SELECT item_id, name FROM dl_item")}


def heroes_with_data() -> list[dict]:
    """Heroes the seed actually carries purchase data for, alphabetically."""
    rows = db.query(
        "SELECT DISTINCT b.hero_id AS hero_id, h.name AS name "
        "FROM dl_buy b JOIN dl_hero h ON h.hero_id = b.hero_id ORDER BY h.name"
    )
    return [{"value": str(r["hero_id"]), "label": r["name"]} for r in rows]


def enemy_options() -> list[dict]:
    """Every hero, plus an empty choice, for the six enemy-team pickers."""
    rows = db.query("SELECT hero_id, name FROM dl_hero ORDER BY name")
    return [{"value": "", "label": "— none —"}] + [
        {"value": str(r["hero_id"]), "label": r["name"]} for r in rows
    ]


#: An item is offerable when at least one net-worth band in one minute has
#: enough buyers *and* enough reference players — exactly the condition
#: `_base_curve` applies. Selecting on total purchases instead let the picker
#: offer items that then answered "no data for that combination", because a
#: large total spread thinly across bands clears no cell.
_USABLE_ITEMS = (
    "SELECT DISTINCT b.item_id AS item_id "
    "FROM dl_buy b "
    "JOIN dl_ref r ON r.hero_id = b.hero_id "
    "                AND r.minute = b.minute AND r.nw_band = b.nw_band "
    "WHERE b.hero_id = %s AND b.n >= %s AND r.n >= %s"
)


def item_counts_by_tier(hero_id: int) -> dict[int, int]:
    """How many items this hero has usable data for, per shop tier."""
    rows = db.query(
        f"SELECT i.tier AS tier, COUNT(*) AS n FROM ({_USABLE_ITEMS}) u "
        "JOIN dl_item i ON i.item_id = u.item_id GROUP BY i.tier ORDER BY i.tier",
        (hero_id, MIN_CELL_N, MIN_CELL_N),
    )
    return {int(r["tier"]): int(r["n"]) for r in rows}


def tier_options(hero_id: int) -> list[dict]:
    """The shop tiers to choose between before picking an item.

    The catalogue is 156 items deep, which is too many to scan in one list —
    and a player is not choosing between a tier-1 component and a tier-4 item
    anyway. They are choosing what to spend the souls they have. So the tier is
    picked first and the item list is filtered to it.
    """
    return [
        {"value": str(tier), "label": f"Tier {tier} — {count} item{'s' if count != 1 else ''}"}
        for tier, count in sorted(item_counts_by_tier(hero_id).items())
    ]


def default_tier(hero_id: int) -> int:
    """The tier the form opens on: the cheapest one that has any data."""
    counts = item_counts_by_tier(hero_id)
    return min(counts) if counts else 1


def items_for_hero(hero_id: int, tier: int | None = None) -> list[dict]:
    """Items bought often enough on this hero to say anything about.

    Deliberately unlimited within a tier. An earlier version took the first 60
    by name across the whole catalogue, which silently cut the list off around
    the letter H — so cheap components, exactly the items a player is choosing
    between early, could vanish from the picker while the tier-4 item they build
    into stayed.
    """
    if tier is None:
        rows = db.query(
            f"SELECT u.item_id AS item_id, i.name AS name FROM ({_USABLE_ITEMS}) u "
            "JOIN dl_item i ON i.item_id = u.item_id ORDER BY i.tier, i.name",
            (hero_id, MIN_CELL_N, MIN_CELL_N),
        )
    else:
        rows = db.query(
            f"SELECT u.item_id AS item_id, i.name AS name FROM ({_USABLE_ITEMS}) u "
            "JOIN dl_item i ON i.item_id = u.item_id WHERE i.tier = %s ORDER BY i.name",
            (hero_id, MIN_CELL_N, MIN_CELL_N, tier),
        )
    return [{"value": str(r["item_id"]), "label": r["name"]} for r in rows]


def parse_tier(raw: str | None, hero_id: int) -> int:
    """Validate the tier picker, falling back to the hero's cheapest tier."""
    raw = (raw or "").strip()
    if not raw:
        return default_tier(hero_id)
    if not raw.isdigit() or int(raw) not in item_counts_by_tier(hero_id):
        raise ValueError("Pick an item tier from the list.")
    return int(raw)


def common_compositions(limit: int = 20) -> list[dict]:
    """The most-played enemy teams, offered as one-click presets."""
    rows = db.query(
        "SELECT comp_key, heroes, n, comp_rank FROM dl_comp ORDER BY comp_rank LIMIT %s",
        (limit,),
    )
    names = heroes()
    out = []
    for r in rows:
        ids = [int(x) for x in str(r["heroes"]).split("-") if x]
        out.append(
            {
                "comp_key": r["comp_key"],
                "hero_ids": ids,
                "label": ", ".join(names.get(i, str(i)) for i in ids),
                "n": int(r["n"]),
                "rank": int(r["comp_rank"]),
            }
        )
    return out


@lru_cache(maxsize=256)
def composition_rank(enemy_ids: tuple[int, ...]) -> dict | None:
    """Where this exact enemy team sits in the top-10,000 list, if at all.

    Cached and keyed on a tuple: it does not depend on the item, but `estimate`
    is called once per candidate item while planning.
    """
    if len(enemy_ids) != 6:
        return None
    key = "-".join(str(i) for i in sorted(enemy_ids))
    rows = db.query("SELECT n, comp_rank FROM dl_comp WHERE comp_key = %s", (key,))
    if not rows:
        return None
    return {"rank": int(rows[0]["comp_rank"]), "n": int(rows[0]["n"])}


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def _wilson_se(wins: int, n: int) -> float:
    """Standard error of a proportion, floored so a degenerate cell is not 0."""
    if n <= 0:
        return float("inf")
    p = wins / n
    return math.sqrt(max(p * (1.0 - p), 1e-6) / n)


@dataclass(frozen=True)
class TimingPoint:
    """One minute bucket of the curve."""

    minute: int
    purchases: int
    raw_winrate: float          # win rate of players who bought it in this window
    ref_winrate: float          # win rate of comparable players who did not
    effect_pp: float            # adjusted effect, in percentage points
    ci_low_pp: float
    ci_high_pp: float
    low_confidence: bool

    @property
    def se_pp(self) -> float:
        """The standard error the interval was built from."""
        return (self.ci_high_pp - self.ci_low_pp) / (2.0 * Z95)

    @property
    def selection_bound_pp(self) -> float:
        """A lower bound safe to pick the *best of 21 windows* on.

        Choosing an item's best window means taking a maximum over every
        two-minute bucket, so the winning bucket is partly whichever one got
        lucky. Ranking items on an unadjusted 95% bound reintroduces exactly the
        optimism the bound exists to remove. This widens for that search using a
        Bonferroni correction across the buckets actually examined.
        """
        return self.effect_pp - Z_SELECT * self.se_pp

    @property
    def significant(self) -> bool:
        """The interval excludes zero, so the sign is trustworthy."""
        return self.ci_low_pp > 0.0 or self.ci_high_pp < 0.0

    @property
    def label(self) -> str:
        return f"{self.minute}–{self.minute + MINUTE_STEP}"


@dataclass(frozen=True)
class TimingResult:
    hero_id: int
    item_id: int
    hero_name: str
    item_name: str
    enemy_ids: list[int]
    enemy_names: list[str]
    ally_ids: list[int]
    ally_names: list[str]
    points: list[TimingPoint]
    level: str                  # which backoff level produced the numbers
    total_purchases: int
    comp: dict | None = None
    notes: list[str] = field(default_factory=list)

    # -- the two answers the tool exists to give ---------------------------

    @property
    def best(self) -> TimingPoint | None:
        """The buy window with the highest effect that is distinguishable from zero."""
        usable = [p for p in self.points if p.ci_low_pp > 0.0 and not p.low_confidence]
        return max(usable, key=lambda p: p.effect_pp) if usable else None

    @property
    def dead_from(self) -> TimingPoint | None:
        """The first minute after the peak where the item stops paying for itself.

        "Stops paying" means the whole confidence interval has fallen to or below
        zero — not merely that the point estimate dipped, which happens by noise
        alone somewhere in almost every curve.
        """
        peak = self.best
        if peak is None:
            return None
        for p in self.points:
            # `ci_high_pp <= 0`, not `effect_pp <= 0`. The point estimate dips
            # below zero somewhere in almost every curve by noise alone; saying
            # an item has stopped paying off on that basis is precisely the
            # misreading this property exists to avoid.
            if p.minute > peak.minute and p.ci_high_pp <= 0.0 and not p.low_confidence:
                return p
        return None

    @property
    def rows_for_table(self) -> list[dict]:
        """The same curve as table rows, so the chart is never the only source."""
        peak = self.best
        dead = self.dead_from
        rows = []
        for p in self.points:
            if p.low_confidence:
                reading = "too few purchases"
            elif peak is not None and p.minute == peak.minute:
                reading = "best window"
            elif dead is not None and p.minute >= dead.minute:
                reading = "no longer pays off"
            elif p.ci_low_pp > 0:
                reading = "helps"
            elif p.ci_high_pp < 0:
                reading = "hurts"
            else:
                reading = "no clear effect"
            rows.append(
                {
                    "window": p.label,
                    "effect": f"{p.effect_pp:+.1f}",
                    "ci": f"{p.ci_low_pp:+.1f} to {p.ci_high_pp:+.1f}",
                    "raw": f"{p.raw_winrate * 100:.1f}%",
                    "ref": f"{p.ref_winrate * 100:.1f}%",
                    "n": f"{p.purchases:,}",
                    "verdict": reading,
                }
            )
        return rows

    @property
    def verdict(self) -> str:
        if self.best is None:
            return (
                f"No purchase window for {self.item_name} on {self.hero_name} is "
                "distinguishable from simply having the souls to buy it."
            )
        peak, dead = self.best, self.dead_from
        text = (
            f"{self.item_name} pays off most when bought around minute "
            f"{peak.label} ({peak.effect_pp:+.1f} pp win rate, "
            f"95% CI {peak.ci_low_pp:+.1f} to {peak.ci_high_pp:+.1f})."
        )
        if dead is not None:
            text += f" From minute {dead.label} on it no longer pays for itself."
        else:
            text += " It does not clearly stop paying off within the first 40 minutes."
        return text


# ---------------------------------------------------------------------------
# the estimator
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _reference_by_minute(hero_id: int) -> dict[tuple[int, int], tuple[int, int]]:
    """The hero's reference population, cached.

    This depends only on the hero, but `_base_curve` needs it for every item —
    and planning scores the whole 156-item catalogue in one request. Uncached it
    was issuing the same query a few hundred times per page load. The cache is
    cleared alongside the other catalogue caches whenever the seed is reloaded.
    """
    rows = db.query(
        "SELECT minute, nw_band, n, wins FROM dl_ref WHERE hero_id = %s", (hero_id,)
    )
    return {(int(r["minute"]), int(r["nw_band"])): (int(r["n"]), int(r["wins"])) for r in rows}


def _purchases_by_minute(hero_id: int, item_id: int) -> dict[tuple[int, int], tuple[int, int]]:
    rows = db.query(
        "SELECT minute, nw_band, n, wins FROM dl_buy WHERE hero_id = %s AND item_id = %s",
        (hero_id, item_id),
    )
    return {(int(r["minute"]), int(r["nw_band"])): (int(r["n"]), int(r["wins"])) for r in rows}


def _base_curve(hero_id: int, item_id: int) -> list[TimingPoint]:
    """Net-worth-standardised effect of buying ``item_id`` in each minute bucket."""
    buys = _purchases_by_minute(hero_id, item_id)
    refs = _reference_by_minute(hero_id)

    points: list[TimingPoint] = []
    for minute in range(0, MAX_MINUTE + 1, MINUTE_STEP):
        bands = [(b, v) for (m, b), v in buys.items() if m == minute]
        usable = [
            (band, bn, bw)
            for band, (bn, bw) in bands
            if bn >= MIN_CELL_N and refs.get((minute, band), (0, 0))[0] >= MIN_CELL_N
        ]
        if not usable:
            continue

        weight_total = sum(bn for _, bn, _ in usable)
        effect = 0.0
        variance = 0.0
        raw_wins = sum(bw for _, _, bw in usable)
        raw_n = weight_total

        reference = 0.0
        for band, bn, bw in usable:
            rn, rw = refs[(minute, band)]
            w = bn / weight_total
            effect += w * (bw / bn - rw / rn)
            # The same weights, so `reference` is the win rate the buyers would
            # have had at their own net worth without the item. Plotted against
            # the buyers' own rate, the gap between the two lines *is* the effect.
            reference += w * (rw / rn)
            variance += (w ** 2) * (_wilson_se(bw, bn) ** 2 + _wilson_se(rw, rn) ** 2)

        se = math.sqrt(variance)
        points.append(
            TimingPoint(
                minute=minute,
                # The purchases that actually informed the estimate. Counting
                # bands that MIN_CELL_N discarded would report a sample size the
                # number was never computed from, and would mark a point
                # confident on the strength of data it ignored.
                purchases=weight_total,
                raw_winrate=raw_wins / raw_n if raw_n else 0.0,
                ref_winrate=reference,
                effect_pp=effect * 100.0,
                ci_low_pp=(effect - Z95 * se) * 100.0,
                ci_high_pp=(effect + Z95 * se) * 100.0,
                low_confidence=weight_total < MIN_POINT_N,
            )
        )
    return points


@lru_cache(maxsize=256)
def _roster_baseline(
    ref_table: str, key: str, hero_id: int, hero_ids: tuple[int, ...]
) -> dict[int, tuple[int, int]]:
    """How this hero does alongside/against each listed hero, item-independent.

    Cached because it does not vary with the item, while planning asks for it
    once per candidate — 156 identical queries per page load before this.
    """
    placeholders = ", ".join(["%s"] * len(hero_ids))
    rows = db.query(
        f"SELECT {key}, n, wins FROM {ref_table} "
        f"WHERE hero_id = %s AND {key} IN ({placeholders})",
        (hero_id, *hero_ids),
    )
    return {
        int(r[key]): (int(r["n"]), int(r["wins"]))
        for r in rows
        if int(r["n"]) >= MIN_ENEMY_N
    }


def _hero_contributions(
    table: str, ref_table: str, key: str,
    hero_id: int, item_id: int, hero_ids: list[int],
) -> dict[int, dict[int, tuple[float, float]]]:
    """Per hero, per 5-minute bucket: the delta they account for and its variance.

    Kept un-aggregated on purpose. Averaging inside this function is what made
    the old version's scale wander — a bucket where three heroes had data was
    divided by three while the next bucket was divided by one, so the same
    line-up produced shifts on different scales minute to minute.
    """
    if not hero_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(hero_ids))
    buy_rows = db.query(
        f"SELECT {key}, minute5, n, wins FROM {table} "
        f"WHERE hero_id = %s AND item_id = %s AND {key} IN ({placeholders})",
        (hero_id, item_id, *hero_ids),
    )
    baseline = _roster_baseline(ref_table, key, hero_id, tuple(hero_ids))

    out: dict[int, dict[int, tuple[float, float]]] = {}
    for r in buy_rows:
        other = int(r[key])
        n, wins = int(r["n"]), int(r["wins"])
        if n < MIN_ENEMY_N or other not in baseline:
            continue
        rn, rw = baseline[other]
        delta = (wins / n - rw / rn) * 100.0
        variance = (_wilson_se(wins, n) ** 2 + _wilson_se(rw, rn) ** 2) * 100.0 ** 2
        out.setdefault(other, {})[int(r["minute5"])] = (delta, variance)
    return out


def apply_lineup(
    points: list[TimingPoint],
    shift_by_bucket: dict[int, float],
    se_by_bucket: dict[int, float],
) -> list[TimingPoint]:
    """Move a curve by the line-up adjustment, carrying its error into the CI.

    The adjustment has its own sampling error, so the interval widens by
    combining variances rather than by a made-up fraction of the shift. The
    earlier `abs(shift) * 0.5` had no distributional meaning: it made a large
    adjustment and a large *but well-measured* one look equally uncertain.
    """
    out: list[TimingPoint] = []
    for p in points:
        bucket = (p.minute // MINUTE5_STEP) * MINUTE5_STEP
        centre = p.effect_pp + shift_by_bucket.get(bucket, 0.0)
        se = math.sqrt(p.se_pp ** 2 + se_by_bucket.get(bucket, 0.0) ** 2)
        out.append(
            TimingPoint(
                minute=p.minute, purchases=p.purchases,
                raw_winrate=p.raw_winrate, ref_winrate=p.ref_winrate,
                effect_pp=centre,
                ci_low_pp=centre - Z95 * se,
                ci_high_pp=centre + Z95 * se,
                low_confidence=p.low_confidence,
            )
        )
    return out


def lineup_shift(
    hero_id: int, item_id: int, enemy_ids: list[int], ally_ids: list[int],
) -> tuple[dict[int, float], dict[int, float], int]:
    """How this specific line-up moves an item's value, with real uncertainty.

    Every hero in the match — both sides — contributes one marginal effect,
    measured against a baseline that already averages over all the *other*
    line-ups they appear in. Those contributions are pooled and **averaged
    once**, over every hero that had usable data anywhere.

    Two earlier versions of this were wrong in opposite directions. Summing the
    contributions treated eleven overlapping measurements as independent and
    produced shifts of twenty to forty points for a single item. Averaging each
    roster separately and then adding the two averages was neither one thing nor
    the other, and still double-counted. A hero with no usable data contributes
    nothing but is not removed from the denominator either, so a bucket where
    fewer heroes have evidence yields a smaller shift rather than a rescaled one.

    Returns the shift per bucket, its standard error per bucket, and how many
    heroes contributed.
    """
    # Keyed by (side, hero): the same hero cannot really be on both teams, but
    # keying on the hero alone would let one side silently overwrite the other
    # rather than fail loudly, and the two are different measurements.
    contributions: dict[tuple[str, int], dict[int, tuple[float, float]]] = {}
    for side, table, ref_table, key, ids in (
        ("enemy", "dl_buy_enemy", "dl_ref_enemy", "enemy_hero_id", enemy_ids),
        ("ally", "dl_buy_ally", "dl_ref_ally", "ally_hero_id", ally_ids),
    ):
        for other, buckets in _hero_contributions(
            table, ref_table, key, hero_id, item_id, ids
        ).items():
            contributions[(side, other)] = buckets
    if not contributions:
        return {}, {}, 0

    k = len(contributions)
    shift: dict[int, float] = {}
    variance: dict[int, float] = {}
    for buckets in contributions.values():
        for bucket, (delta, var) in buckets.items():
            shift[bucket] = shift.get(bucket, 0.0) + delta
            variance[bucket] = variance.get(bucket, 0.0) + var
    return (
        {b: v / k for b, v in shift.items()},
        {b: math.sqrt(v) / k for b, v in variance.items()},
        k,
    )


def estimate(
    hero_id: int,
    item_id: int,
    enemy_ids: list[int] | None = None,
    ally_ids: list[int] | None = None,
) -> TimingResult:
    """Full answer for one hero / item / enemy team / own team."""
    enemy_ids = sorted(set(enemy_ids or []))
    ally_ids = sorted(set(ally_ids or []) - {hero_id})
    names = heroes()
    hero_name = names.get(hero_id, str(hero_id))
    item_name = items().get(item_id, str(item_id))

    points = _base_curve(hero_id, item_id)
    notes: list[str] = []
    level = "hero+item, net-worth adjusted"

    if not points:
        return TimingResult(
            hero_id=hero_id, item_id=item_id, hero_name=hero_name, item_name=item_name,
            enemy_ids=enemy_ids, enemy_names=[names.get(e, str(e)) for e in enemy_ids],
            ally_ids=ally_ids, ally_names=[names.get(a, str(a)) for a in ally_ids],
            points=[], level="no data", total_purchases=0,
            notes=[f"No purchases of {item_name} on {hero_name} in the extract."],
        )

    shift_by_bucket, se_by_bucket, contributors = lineup_shift(
        hero_id, item_id, enemy_ids, ally_ids
    )
    if contributors:
        level = (
            f"hero+item vs {contributors} hero(es) in this match, net-worth adjusted"
        )
        points = apply_lineup(points, shift_by_bucket, se_by_bucket)
        notes.append(
            f"Adjusted for the {contributors} hero(es) in this match with enough "
            "games to measure. Each contributes one marginal effect against a "
            "baseline that already averages over the line-ups they usually see; "
            "those are pooled and averaged once, and their sampling error is "
            "carried into the interval."
        )
    elif enemy_ids or ally_ids:
        notes.append(
            "Not enough games with this line-up to adjust for it; showing the "
            "hero-and-item estimate instead."
        )

    comp = composition_rank(tuple(enemy_ids))
    if comp:
        notes.append(
            f"This exact enemy team is #{comp['rank']:,} most played "
            f"({comp['n']:,} games in the extract)."
        )
    elif len(enemy_ids) == 6:
        notes.append(
            "This exact enemy team is not in the 10,000 most-played compositions, "
            "so the estimate comes from the individual heroes in it."
        )

    return TimingResult(
        hero_id=hero_id, item_id=item_id, hero_name=hero_name, item_name=item_name,
        enemy_ids=enemy_ids, enemy_names=[names.get(e, str(e)) for e in enemy_ids],
        ally_ids=ally_ids, ally_names=[names.get(a, str(a)) for a in ally_ids],
        points=points, level=level, comp=comp,
        total_purchases=sum(p.purchases for p in points), notes=notes,
    )


# ---------------------------------------------------------------------------
# build path
# ---------------------------------------------------------------------------

# Deadlock gives four slots each of weapon, vitality and spirit.
SLOTS = ("weapon", "vitality", "spirit")
SLOT_CAPACITY = 4
BUILD_SIZE = SLOT_CAPACITY * len(SLOTS)

# Midpoint of a net-worth band, used to turn the reference distribution into a
# souls-over-time curve. The top band is open-ended, so it gets its floor.
def _band_midpoint(band: int) -> float:
    if band >= NW_BAND_MAX:
        return float(NW_BAND_MAX * NW_BAND_SIZE)
    return band * NW_BAND_SIZE + NW_BAND_SIZE / 2


def souls_curve(hero_id: int) -> dict[int, float]:
    """Typical net worth for this hero at each minute, from the reference table.

    This is what makes a build *order* rather than a wish list: an item cannot
    be bought before the player can afford it, and the reference population
    already describes how much a player on this hero is worth minute by minute.
    """
    rows = db.query(
        "SELECT minute, nw_band, n FROM dl_ref WHERE hero_id = %s", (hero_id,)
    )
    # The weighted *median* band, not the mean. The top band is open-ended, so a
    # mean of band midpoints is dragged upward by the handful of very rich
    # players and would claim a typical player can afford more than they can.
    by_minute: dict[int, list[tuple[int, int]]] = {}
    for r in rows:
        by_minute.setdefault(int(r["minute"]), []).append(
            (int(r["nw_band"]), int(r["n"]))
        )

    curve: dict[int, float] = {}
    for minute, bands in by_minute.items():
        bands.sort()
        total = sum(n for _, n in bands)
        if not total:
            continue
        seen = 0
        for band, n in bands:
            seen += n
            if seen * 2 >= total:
                curve[minute] = _band_midpoint(band)
                break
    return curve


def _odds(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return p / (1.0 - p)


def baseline_winrate(hero_id: int) -> float:
    """The hero's overall win rate in the extract — the floor a build builds on."""
    rows = db.query(
        "SELECT SUM(n) AS n, SUM(wins) AS wins FROM dl_ref WHERE hero_id = %s", (hero_id,)
    )
    if not rows or not rows[0]["n"]:
        return 0.5
    return float(rows[0]["wins"]) / float(rows[0]["n"])


def combine_effects(baseline: float, effects_pp: list[float]) -> float:
    """The **ceiling**: what the build is worth if the items never overlap.

    Adding percentage points would be wrong twice over — twelve items at six
    points each would read as +72 points, and any baseline above 28% would take
    the "probability" past 1. So effects are composed as odds ratios, which is
    how independent effects combine for a binary outcome and which saturates
    instead of running off the scale.

    It is still only a ceiling, and a generous one. Each item's effect was
    measured against a reference population that already contains players with
    good builds, so the effects overlap by construction; multiplying twelve of
    them double-counts. Use :func:`plan_build`, which reports this alongside a
    floor, rather than quoting this number on its own.
    """
    odds = _odds(baseline)
    for effect in effects_pp:
        odds *= _odds(baseline + effect / 100.0) / _odds(baseline)
    return odds / (1.0 + odds)


@dataclass(frozen=True)
class ItemCandidate:
    """One item scored for this hero and this game, before any build is chosen.

    Scoring is the expensive half of planning — an estimate per item across the
    whole catalogue — so it is done once and every strategy selects from the
    same list rather than recomputing.
    """

    item_id: int
    name: str
    tier: int
    cost: int
    slot: str
    safe: TimingPoint       # best window judged on the adjusted lower bound
    upside: TimingPoint     # best window judged on the point estimate
    roster_gain_pp: float   # how much of the effect comes from this specific game
    points: list[TimingPoint]   # the whole curve, so a moved target can be re-read

    def at(self, minute: int) -> TimingPoint:
        """The item's numbers in a specific window, falling back to its best."""
        for point in self.points:
            if point.minute == minute:
                return point
        return self.safe


@dataclass(frozen=True)
class BuildStep:
    """One purchase in the recommended order."""

    order: int
    item_id: int
    item_name: str
    tier: int
    cost: int
    slot: str
    target_minute: int          # when to aim to have it
    peak_minute: int            # when the item's own effect is largest
    effect_pp: float
    ci_low_pp: float
    ci_high_pp: float
    cumulative_cost: int
    limited_by: str             # "effect", "souls" or "unreachable"
    # Two bounds, because the truth between them cannot be estimated from this
    # data: the floor assumes the items overlap completely (only the best one
    # really counts), the ceiling assumes they are independent.
    cumulative_floor: float
    cumulative_winrate: float

    @property
    def window(self) -> str:
        """The target as a two-minute window, or "40+" at the edge of the data."""
        if self.target_minute >= MAX_MINUTE:
            return f"{MAX_MINUTE}+"
        return f"{self.target_minute}–{self.target_minute + MINUTE_STEP}"


@dataclass(frozen=True)
class BuildPlan:
    hero_id: int
    hero_name: str
    ally_names: list[str]
    enemy_names: list[str]
    steps: list[BuildStep]
    considered: int
    strategy: str = "safe"
    strategy_label: str = ""
    baseline: float = 0.5
    notes: list[str] = field(default_factory=list)

    @property
    def total_winrate(self) -> float:
        """Ceiling: the items are assumed not to overlap."""
        return self.steps[-1].cumulative_winrate if self.steps else self.baseline

    @property
    def total_floor(self) -> float:
        """Floor: only the single best item is assumed to count at all."""
        return self.steps[-1].cumulative_floor if self.steps else self.baseline

    @property
    def total_gain_pp(self) -> float:
        return (self.total_winrate - self.baseline) * 100.0

    @property
    def summary(self) -> str:
        if not self.steps:
            return "No build could be assembled from the available data."
        return (
            f"{len(self.steps)} items should take {self.hero_name} from a "
            f"{self.baseline * 100:.1f}% baseline to somewhere between "
            f"{self.total_floor * 100:.1f}% and {self.total_winrate * 100:.1f}%. "
            f"The floor assumes only the single best item really counts; the "
            f"ceiling assumes all {len(self.steps)} are independent. Neither is "
            f"true, and this data cannot say where in between the answer lies."
        )

    @property
    def slot_usage(self) -> dict[str, int]:
        used = dict.fromkeys(SLOTS, 0)
        for step in self.steps:
            if step.slot in used:
                used[step.slot] += 1
        return used

    @property
    def rows_for_table(self) -> list[dict]:
        return [
            {
                "order": str(step.order),
                "item": step.item_name,
                "slot": f"T{step.tier} {step.slot}",
                "cost": f"{step.cost:,}",
                "target": step.window,
                "effect": f"{step.effect_pp:+.1f}",
                "ci": f"{step.ci_low_pp:+.1f} to {step.ci_high_pp:+.1f}",
                "running": (
                    f"{step.cumulative_floor * 100:.1f}\u2013"
                    f"{step.cumulative_winrate * 100:.1f}%"
                ),
                "why": {
                    "souls": "souls",
                    "unreachable": "not affordable by 40",
                }.get(step.limited_by, "peak"),
            }
            for step in self.steps
        ]


def _catalogue_for_hero(hero_id: int) -> dict[int, dict]:
    rows = db.query(
        "SELECT i.item_id AS item_id, i.name AS name, i.tier AS tier, "
        f"i.cost AS cost, i.slot AS slot FROM ({_USABLE_ITEMS}) u "
        "JOIN dl_item i ON i.item_id = u.item_id",
        (hero_id, MIN_CELL_N, MIN_CELL_N),
    )
    return {
        int(r["item_id"]): {
            "name": str(r["name"]), "tier": int(r["tier"]),
            "cost": int(r["cost"]), "slot": str(r["slot"]),
        }
        for r in rows
    }


#: Selectable build paths. Each is a different, defensible answer to "which
#: items", not a cosmetic re-sort: they optimise for different things, and a
#: player picks the one that matches how the game is going.
STRATEGIES: dict[str, dict] = {
    "safe": {
        "label": "Safest",
        "blurb": "Ranks by the low end of each item's interval, so a thinly "
                 "sampled item cannot reach the build on luck.",
        "key": lambda c: c.safe.selection_bound_pp,
        "point": lambda c: c.safe,
    },
    "upside": {
        "label": "Highest upside",
        "blurb": "Ranks by the point estimate. Picks bolder items with wider "
                 "intervals — more to gain, less certainty.",
        "key": lambda c: c.upside.effect_pp,
        "point": lambda c: c.upside,
    },
    "economy": {
        "label": "Best value",
        "blurb": "Ranks by effect per 1,000 souls, so cheap components that "
                 "pay off early come first.",
        "key": lambda c: c.safe.selection_bound_pp / max(c.cost / 1000.0, 0.1),
        "point": lambda c: c.safe,
    },
    "counter": {
        "label": "Counter-pick",
        "blurb": "Ranks by how much of an item's value comes from this "
                 "particular enemy and ally line-up rather than in general.",
        "key": lambda c: c.roster_gain_pp,
        "point": lambda c: c.safe,
    },
}


def strategy_options() -> list[dict]:
    return [
        {"value": key, "label": f"{spec['label']} — {spec['blurb']}"}
        for key, spec in STRATEGIES.items()
    ]


def parse_strategy(raw: str | None) -> str:
    raw = (raw or "").strip() or "safe"
    if raw not in STRATEGIES:
        raise ValueError("Pick a build path from the list.")
    return raw


def score_candidates(
    hero_id: int, ally_ids: list[int], enemy_ids: list[int]
) -> list[ItemCandidate]:
    """Score every item this hero has data for, once, for all strategies.

    Deliberately does not go through :func:`estimate`. Scoring the catalogue
    means doing this 156 times, and ``estimate`` would repeat the composition
    lookup and — worse — a second full pass per item just to recover how much of
    the effect came from the line-up. Here the base curve is computed once and
    the line-up shift is read directly, so the roster gain falls out for free.
    """
    catalogue = _catalogue_for_hero(hero_id)
    out: list[ItemCandidate] = []
    for item_id, meta in catalogue.items():
        base = _base_curve(hero_id, item_id)
        if not base:
            continue

        shift, se, contributors = lineup_shift(hero_id, item_id, enemy_ids, ally_ids)
        points = apply_lineup(base, shift, se) if contributors else base

        usable = [p for p in points if not p.low_confidence and p.ci_low_pp > 0.0]
        if not usable:
            continue
        safe = max(usable, key=lambda p: p.selection_bound_pp)
        upside = max(usable, key=lambda p: p.effect_pp)

        # How much of the chosen window's value is specific to this line-up: it
        # is exactly the shift applied to that window, no second estimate needed.
        bucket = (safe.minute // MINUTE5_STEP) * MINUTE5_STEP
        roster_gain = shift.get(bucket, 0.0) if contributors else 0.0

        out.append(
            ItemCandidate(
                item_id=item_id, name=meta["name"], tier=meta["tier"],
                cost=meta["cost"], slot=meta["slot"],
                safe=safe, upside=upside, roster_gain_pp=roster_gain,
                points=points,
            )
        )
    return out


def plan_build(
    hero_id: int,
    ally_ids: list[int] | None = None,
    enemy_ids: list[int] | None = None,
    strategy: str = "safe",
    candidates: list[ItemCandidate] | None = None,
) -> BuildPlan:
    """A suggested purchase order with a target minute for each item.

    Items are ranked by the **lower bound** of their confidence interval rather
    than the point estimate. Ranking on the point estimate would put whichever
    thinly-sampled item got lucky at the top of every build; the lower bound
    asks "how good is this item at worst", which is the right question when
    picking a handful out of 156.

    Selection is greedy under Deadlock's slot limits — four each of weapon,
    vitality and spirit — and then each pick is scheduled at the later of its
    own best window and the first minute the player can typically afford it,
    given everything bought before it.

    The effects are estimated one item at a time against a common reference, so
    they are **not additive**: this is an ordering, not a predicted win rate for
    the finished build. Nothing here models one item making another better.
    """
    ally_ids = sorted(set(ally_ids or []) - {hero_id})
    enemy_ids = sorted(set(enemy_ids or []))
    names = heroes()
    spec = STRATEGIES[strategy]
    baseline = baseline_winrate(hero_id)
    if candidates is None:
        candidates = score_candidates(hero_id, ally_ids, enemy_ids)

    def empty(note: str) -> BuildPlan:
        return BuildPlan(
            hero_id=hero_id, hero_name=names.get(hero_id, str(hero_id)),
            ally_names=[names.get(a, str(a)) for a in ally_ids],
            enemy_names=[names.get(e, str(e)) for e in enemy_ids],
            steps=[], considered=len(candidates), strategy=strategy,
            strategy_label=spec["label"], baseline=baseline, notes=[note],
        )

    if not candidates:
        return empty("No item on this hero has a clear enough effect to build around.")

    # Greedy pick under Deadlock's four-per-slot limit, in the strategy's order.
    ranked = sorted(candidates, key=spec["key"], reverse=True)
    used = dict.fromkeys(SLOTS, 0)
    picked: list[tuple[ItemCandidate, TimingPoint]] = []
    for candidate in ranked:
        if candidate.slot not in used or used[candidate.slot] >= SLOT_CAPACITY:
            continue
        if spec["key"](candidate) <= 0:
            continue
        used[candidate.slot] += 1
        picked.append((candidate, spec["point"](candidate)))
        if len(picked) >= BUILD_SIZE:
            break

    if not picked:
        return empty("No item scored positively under this build path.")

    # Schedule in *rank* order, not by peak minute. The souls budget is spent in
    # the order items are bought, so scheduling the cheapest-peaking item first
    # would make lower-ranked picks push the best item later purely by getting
    # in ahead of it. Highest-ranked gets first call on the budget; the display
    # order is recovered afterwards by sorting on the resulting target.
    curve = souls_curve(hero_id)
    minutes = sorted(curve)

    scheduled: list[tuple[ItemCandidate, TimingPoint, int, int, bool]] = []
    running = 0
    for candidate, point in picked:
        running += candidate.cost
        affordable = next((m for m in minutes if curve[m] >= running), None)
        # `is None` rather than a truthiness test: minute 0 is a real answer
        # (affordable from the start) and `0 or MAX_MINUTE` would silently push
        # the cheapest early items to the end of the build.
        unreachable = affordable is None
        target = MAX_MINUTE if unreachable else min(MAX_MINUTE, max(point.minute, affordable))
        scheduled.append((candidate, point, target, running, unreachable))

    scheduled.sort(key=lambda row: (row[2], row[3]))

    steps: list[BuildStep] = []
    so_far: list[float] = []
    for order, (candidate, point, target, cumulative, unreachable) in enumerate(
        scheduled, start=1
    ):
        # Report the item's numbers in the window it is actually being bought in.
        # Showing the peak window's effect beside a target the souls budget moved
        # elsewhere would attach a number to a purchase that never happens.
        shown = candidate.at(target)
        so_far.append(shown.effect_pp)
        floor = combine_effects(baseline, [max(so_far)])
        steps.append(
            BuildStep(
                order=order, item_id=candidate.item_id, item_name=candidate.name,
                tier=candidate.tier, cost=candidate.cost, slot=candidate.slot,
                target_minute=target, peak_minute=point.minute,
                effect_pp=shown.effect_pp, ci_low_pp=shown.ci_low_pp,
                ci_high_pp=shown.ci_high_pp, cumulative_cost=cumulative,
                limited_by=(
                    "unreachable" if unreachable
                    else "souls" if target > point.minute
                    else "effect"
                ),
                cumulative_floor=floor,
                cumulative_winrate=combine_effects(baseline, so_far),
            )
        )

    notes = [
        f"{spec['label']}: {spec['blurb']}",
        "The running win rate is a range, not a number. Its ceiling composes "
        "the effects as odds ratios assuming the items are independent; its "
        "floor assumes they overlap so completely that only the best one "
        "counts. Each item's effect was measured against players who already "
        "had builds of their own, so the truth is somewhere inside — and "
        "nothing in this data says where.",
    ]
    if any(step.limited_by == "souls" for step in steps):
        notes.append(
            "Where the target is later than the item's own best window, it is "
            "because a player on this hero cannot typically afford it sooner."
        )
    if any(step.limited_by == "unreachable" for step in steps):
        notes.append(
            "Items marked \u201cnot affordable by 40\u201d complete a full "
            "twelve-slot build that a typical game on this hero does not last "
            "long enough to finish."
        )

    return BuildPlan(
        hero_id=hero_id, hero_name=names.get(hero_id, str(hero_id)),
        ally_names=[names.get(a, str(a)) for a in ally_ids],
        enemy_names=[names.get(e, str(e)) for e in enemy_ids],
        steps=steps, considered=len(candidates), strategy=strategy,
        strategy_label=spec["label"], baseline=baseline, notes=notes,
    )


@dataclass(frozen=True)
class KeyBuy:
    """An item whose value is unusually specific to this game."""

    item_id: int
    item_name: str
    tier: int
    cost: int
    slot: str
    target_minute: int
    effect_pp: float
    ci_low_pp: float
    matchup_gain_pp: float
    drivers: list[str]          # the enemies/allies that account for it

    @property
    def window(self) -> str:
        if self.target_minute >= MAX_MINUTE:
            return f"{MAX_MINUTE}+"
        return f"{self.target_minute}\u2013{self.target_minute + MINUTE_STEP}"

    @property
    def because(self) -> str:
        if not self.drivers:
            return "Strong on this hero generally, rather than for this line-up."
        return "Mostly because of " + ", ".join(self.drivers) + "."


def key_buys(
    hero_id: int,
    ally_ids: list[int] | None = None,
    enemy_ids: list[int] | None = None,
    limit: int = 5,
    candidates: list[ItemCandidate] | None = None,
) -> list[KeyBuy]:
    """The handful of items that matter most *because of this particular game*.

    Ranked by how much of an item's value comes from the heroes actually in the
    match rather than from being good in general — so an item that is merely
    strong on this hero does not crowd out the one that answers the enemy team.
    Each pick names the heroes that account for it.
    """
    ally_ids = sorted(set(ally_ids or []) - {hero_id})
    enemy_ids = sorted(set(enemy_ids or []))
    if candidates is None:
        candidates = score_candidates(hero_id, ally_ids, enemy_ids)
    names = heroes()

    ranked = sorted(candidates, key=lambda c: c.roster_gain_pp, reverse=True)
    out: list[KeyBuy] = []
    for candidate in ranked[:limit]:
        if candidate.roster_gain_pp <= 0:
            break
        # Averaged over the buckets each hero actually has data in, so the
        # ranking of drivers does not favour whoever happens to appear in more
        # of the match's five-minute windows.
        per_hero: dict[int, float] = {}
        for table, ref_table, key, ids in (
            ("dl_buy_enemy", "dl_ref_enemy", "enemy_hero_id", enemy_ids),
            ("dl_buy_ally", "dl_ref_ally", "ally_hero_id", ally_ids),
        ):
            for other, buckets in _hero_contributions(
                table, ref_table, key, hero_id, candidate.item_id, ids
            ).items():
                if buckets:
                    per_hero[other] = sum(d for d, _ in buckets.values()) / len(buckets)
        contributions = per_hero

        drivers = [
            names.get(h, str(h))
            for h, value in sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
            if value > 0
        ][:3]

        out.append(
            KeyBuy(
                item_id=candidate.item_id, item_name=candidate.name,
                tier=candidate.tier, cost=candidate.cost, slot=candidate.slot,
                target_minute=candidate.safe.minute,
                effect_pp=candidate.safe.effect_pp,
                ci_low_pp=candidate.safe.ci_low_pp,
                matchup_gain_pp=candidate.roster_gain_pp, drivers=drivers,
            )
        )
    return out


def key_buy_rows(buys: list[KeyBuy]) -> list[dict]:
    """Key buys shaped for the table macro."""
    return [
        {
            "item": b.item_name,
            "slot": f"T{b.tier} {b.slot}",
            "target": b.window,
            "effect": f"{b.effect_pp:+.1f}",
            "matchup": f"{b.matchup_gain_pp:+.1f}",
            "because": b.because,
        }
        for b in buys
    ]


def compare_strategies(
    hero_id: int,
    ally_ids: list[int] | None = None,
    enemy_ids: list[int] | None = None,
) -> list[BuildPlan]:
    """Every build path, so the player can see what each one costs them."""
    return [plan_build(hero_id, ally_ids, enemy_ids, key) for key in STRATEGIES]


# ---------------------------------------------------------------------------
# patches: how an item has fared across releases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatchEffect:
    """An item's showing in one patch era."""

    patch_idx: int
    start: str
    end: str
    title: str
    is_current: bool
    purchases: int
    effect_pp: float
    se_pp: float

    @property
    def label(self) -> str:
        return f"{self.start} — {self.title}"

    @property
    def ci_low_pp(self) -> float:
        return self.effect_pp - Z95 * self.se_pp

    @property
    def ci_high_pp(self) -> float:
        return self.effect_pp + Z95 * self.se_pp


@dataclass(frozen=True)
class PatchComparison:
    """Two eras of the same item, and whether the change is real."""

    item_id: int
    item_name: str
    before: PatchEffect
    after: PatchEffect

    @property
    def delta_pp(self) -> float:
        return self.after.effect_pp - self.before.effect_pp

    @property
    def se_pp(self) -> float:
        return math.sqrt(self.before.se_pp ** 2 + self.after.se_pp ** 2)

    @property
    def ci_low_pp(self) -> float:
        return self.delta_pp - Z95 * self.se_pp

    @property
    def ci_high_pp(self) -> float:
        return self.delta_pp + Z95 * self.se_pp

    @property
    def significant(self) -> bool:
        return self.ci_low_pp > 0.0 or self.ci_high_pp < 0.0

    @property
    def verdict(self) -> str:
        if not self.significant:
            return (
                f"{self.item_name} performs about the same in both patches "
                f"({self.delta_pp:+.1f} pp, 95% CI {self.ci_low_pp:+.1f} to "
                f"{self.ci_high_pp:+.1f} — the interval includes zero)."
            )
        direction = "better" if self.delta_pp > 0 else "worse"
        return (
            f"{self.item_name} does {direction} in {self.after.start} than in "
            f"{self.before.start}: {self.delta_pp:+.1f} pp "
            f"(95% CI {self.ci_low_pp:+.1f} to {self.ci_high_pp:+.1f})."
        )


def patches() -> list[dict]:
    """The patch eras the extract covers, newest first."""
    rows = db.query(
        "SELECT p.patch_idx AS patch_idx, p.patch_start AS patch_start, "
        "p.patch_end AS patch_end, p.is_current AS is_current, "
        "COALESCE(t.title, '') AS title "
        "FROM dl_patch p LEFT JOIN dl_patch_title t ON t.patch_idx = p.patch_idx "
        "ORDER BY p.patch_idx DESC"
    )
    return [
        {
            "patch_idx": int(r["patch_idx"]),
            "start": str(r["patch_start"]),
            "end": str(r["patch_end"]),
            "title": str(r["title"]),
            "is_current": bool(int(r["is_current"])),
        }
        for r in rows
    ]


def patch_options() -> list[dict]:
    """Patch eras as picker options, newest first."""
    return [
        {"value": str(p["patch_idx"]), "label": f"{p['start']} — {p['title']}"}
        for p in patches()
    ]


def _patch_band_totals() -> dict[tuple[int, int], tuple[int, int]]:
    rows = db.query(
        "SELECT patch_idx, nw_band, SUM(n) AS n, SUM(wins) AS wins "
        "FROM dl_buy_patch GROUP BY patch_idx, nw_band"
    )
    return {
        (int(r["patch_idx"]), int(r["nw_band"])): (int(r["n"]), int(r["wins"]))
        for r in rows
    }


def _patch_band_weights(item_id: int, eras: list[dict]) -> dict[int, float]:
    """One shared set of band weights for every era being compared.

    Standardising each era to its own purchase mix means the difference between
    two eras is partly just a change in *who* bought the item — if buyers were
    richer in the later patch, the item looks better even at unchanged win rates
    in every band. Both eras are therefore weighted by their combined
    distribution, so only the rates can move the number.
    """
    combined: dict[int, int] = {}
    for era in eras:
        rows = db.query(
            "SELECT nw_band, n FROM dl_buy_patch WHERE item_id = %s AND patch_idx = %s",
            (item_id, era["patch_idx"]),
        )
        for r in rows:
            combined[int(r["nw_band"])] = combined.get(int(r["nw_band"]), 0) + int(r["n"])
    total = sum(combined.values())
    return {b: n / total for b, n in combined.items()} if total else {}


def _patch_effect(
    item_id: int, era: dict, totals, weights: dict[int, float] | None = None
) -> PatchEffect | None:
    """The item's effect in one era, versus *other* purchases at the same wealth.

    The reference here is every other purchase in the same era and net-worth
    band — the item's own rows are subtracted out, so it is not compared against
    itself. That is a different reference from the timing curve, which uses all
    players rather than all purchases, so the two numbers are not interchangeable.
    """
    rows = db.query(
        "SELECT nw_band, n, wins FROM dl_buy_patch WHERE item_id = %s AND patch_idx = %s",
        (item_id, era["patch_idx"]),
    )
    usable: list[tuple[int, int, int, int, int]] = []
    for r in rows:
        band, n, wins = int(r["nw_band"]), int(r["n"]), int(r["wins"])
        total_n, total_w = totals.get((era["patch_idx"], band), (0, 0))
        other_n, other_w = total_n - n, total_w - wins
        if n >= MIN_CELL_N and other_n >= MIN_CELL_N:
            usable.append((band, n, wins, other_n, other_w))
    if not usable:
        return None

    if weights:
        # Shared weights, renormalised over the bands this era can actually
        # support. Falls back to the era's own mix only if none of the shared
        # bands survive here.
        chosen = {band: weights.get(band, 0.0) for band, _, _, _, _ in usable}
        share = sum(chosen.values())
        if share <= 0:
            chosen = {band: n for band, n, _, _, _ in usable}
            share = sum(chosen.values())
    else:
        chosen = {band: n for band, n, _, _, _ in usable}
        share = sum(chosen.values())

    effect = 0.0
    variance = 0.0
    for band, n, wins, other_n, other_w in usable:
        w = chosen[band] / share
        effect += w * (wins / n - other_w / other_n)
        variance += (w ** 2) * (_wilson_se(wins, n) ** 2 + _wilson_se(other_w, other_n) ** 2)

    weight_total = sum(n for _, n, _, _, _ in usable)

    return PatchEffect(
        patch_idx=era["patch_idx"], start=era["start"], end=era["end"],
        title=era["title"], is_current=era["is_current"],
        purchases=weight_total, effect_pp=effect * 100.0,
        se_pp=math.sqrt(variance) * 100.0,
    )


def patch_trend(item_id: int) -> list[PatchEffect]:
    """The item's effect in every era the extract covers, oldest first."""
    totals = _patch_band_totals()
    out = [_patch_effect(item_id, era, totals) for era in reversed(patches())]
    return [e for e in out if e is not None]


def comparison_rows(comparison: PatchComparison) -> list[dict]:
    """The two compared eras, shaped for the table macro."""
    return [
        {
            "patch": era.label,
            "effect": f"{era.effect_pp:+.1f}",
            "ci": f"{era.ci_low_pp:+.1f} to {era.ci_high_pp:+.1f}",
            "n": f"{era.purchases:,}",
        }
        for era in (comparison.before, comparison.after)
    ]


def patch_trend_rows(item_id: int) -> list[dict]:
    """The trend shaped for the table macro (templates do no data shaping)."""
    return [
        {
            "patch": e.label,
            "effect": f"{e.effect_pp:+.1f}",
            "ci": f"{e.ci_low_pp:+.1f} to {e.ci_high_pp:+.1f}",
            "n": f"{e.purchases:,}",
        }
        for e in patch_trend(item_id)
    ]


def compare_patches(item_id: int, before_idx: int, after_idx: int) -> PatchComparison | None:
    """Compare one item between two patch eras."""
    by_idx = {p["patch_idx"]: p for p in patches()}
    if before_idx not in by_idx or after_idx not in by_idx:
        return None
    totals = _patch_band_totals()
    eras = [by_idx[before_idx], by_idx[after_idx]]
    weights = _patch_band_weights(item_id, eras)
    before = _patch_effect(item_id, eras[0], totals, weights)
    after = _patch_effect(item_id, eras[1], totals, weights)
    if before is None or after is None:
        return None
    return PatchComparison(
        item_id=item_id, item_name=items().get(item_id, str(item_id)),
        before=before, after=after,
    )


def parse_patch_idx(raw: str | None) -> int | None:
    """Validate a patch picker value."""
    raw = (raw or "").strip()
    if not raw:
        return None
    known = {p["patch_idx"] for p in patches()}
    if not raw.lstrip("-").isdigit() or int(raw) not in known:
        raise ValueError("Pick a patch from the list.")
    return int(raw)


def extract_info() -> dict[str, str]:
    """Provenance for the footer, so a number on screen can be traced."""
    return {r["meta_key"]: r["meta_value"] for r in db.query("SELECT * FROM dl_meta")}


def extract_rows() -> list[dict]:
    """Provenance shaped for the table macro (templates do no data shaping)."""
    return [{"k": key, "v": value} for key, value in sorted(extract_info().items())]


def live_lineup(raw_account_id: str) -> tuple[dict | None, str]:
    """Resolve an account id to a line-up, plus a message for the page.

    Never raises. A bad id, an account that is not playing, and the upstream
    service being down are all ordinary outcomes here and each gets its own
    sentence — a third-party outage is not an error page in this app.
    """
    from . import live

    try:
        account_id = live.parse_account_id(raw_account_id)
    except ValueError as exc:
        return None, str(exc)

    try:
        match = live.find_match(account_id)
    except live.LiveLookupError as exc:
        return None, str(exc)

    if match is None:
        return None, (
            "That account is not in a match right now. The live list also lags "
            "the draft by a few seconds, so try again shortly after picking."
        )

    known = heroes()
    if match.hero_id not in known:
        return None, "Found the match, but your hero is not one this app has data for."

    note = f"Found match {match.match_id}: you are on {known[match.hero_id]}."
    if match.partial:
        note += (
            f" Only {match.players_seen} of 12 players are visible so far — "
            "run it again in a few seconds for the full line-up."
        )
    return (
        {"hero_id": match.hero_id, "allies": match.allies, "enemies": match.enemies},
        note,
    )


def lineup_values(
    hero_id: int | None = None,
    ally_ids: list[int] | None = None,
    enemy_ids: list[int] | None = None,
) -> dict:
    """Form values for the hero and both team pickers, padded to their slots.

    Returned as strings because that is what the select macro compares against,
    and padded to five and six so the template only has to index — no shaping in
    the template (AGENTS.md rule 1). Unknown hero ids are dropped rather than
    rendered as a blank-looking selection.
    """
    known = heroes()
    allies = [h for h in (ally_ids or []) if h in known and h != hero_id][:5]
    enemies = [h for h in (enemy_ids or []) if h in known][:6]
    return {
        "hero": str(hero_id) if hero_id in known else "",
        "ally_values": [str(h) for h in allies] + [""] * (5 - len(allies)),
        "enemy_values": [str(h) for h in enemies] + [""] * (6 - len(enemies)),
    }


def parse_hero_ids(raw: list[str] | None, limit: int, what: str) -> list[int]:
    """Validate a roster form input: up to ``limit`` known, distinct heroes."""
    known = heroes()
    out: list[int] = []
    for token in raw or []:
        token = (token or "").strip()
        if not token:
            continue
        if not token.isdigit() or int(token) not in known:
            raise ValueError(f"Pick {what} from the list.")
        if int(token) not in out:
            out.append(int(token))
    if len(out) > limit:
        raise ValueError(f"A team has at most {limit} other heroes.")
    return out


def parse_ally_ids(raw: list[str] | None) -> list[int]:
    """Your five team-mates, not counting you."""
    return parse_hero_ids(raw, 5, "team-mates")


def parse_enemy_ids(raw: list[str] | None) -> list[int]:
    """Validate the enemy-team form input: up to six known, distinct heroes."""
    known = heroes()
    out: list[int] = []
    for token in raw or []:
        token = (token or "").strip()
        if not token:
            continue
        if not token.isdigit() or int(token) not in known:
            raise ValueError("Pick enemy heroes from the list.")
        if int(token) not in out:
            out.append(int(token))
    if len(out) > 6:
        raise ValueError("A team has at most six heroes.")
    return out
