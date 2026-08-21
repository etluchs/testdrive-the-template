import base64
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """In-memory data and the local dev user, reset before every test."""
    monkeypatch.setenv("APPKIT_BACKEND", "fake")
    monkeypatch.setenv("APPKIT_AUTH", "dev")
    # appkit refuses to guess a backend or an auth mode when these are set, so
    # make sure a CI runner that happens to define them cannot change what the
    # tests exercise.
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    import appkit

    appkit.reset_fakes()
    yield
    appkit.reset_fakes()


@pytest.fixture
def seeded():
    """A small synthetic extract, built so the expected answers are known.

    The app's real data comes from an offline extract of millions of matches;
    tests must not depend on it, or on the network that produced it. So the
    tables are filled here with a hand-built case whose correct answer can be
    reasoned about on paper — see ``tests/test_logic.py`` for what each part is
    designed to prove.
    """
    from pathlib import Path

    from appkit import db

    from app import logic

    logic.clear_caches()

    schema = Path(__file__).resolve().parent.parent / "etl" / "schema.sql"
    for statement in logic._split_sql(schema.read_text()):
        db.execute(statement)

    db.execute("INSERT INTO dl_hero (hero_id, name) VALUES (1, 'Testhero')")
    db.execute("INSERT INTO dl_hero (hero_id, name) VALUES (2, 'Enemyhero')")
    db.execute("INSERT INTO dl_hero (hero_id, name) VALUES (3, 'Otherenemy')")
    # Tiers differ so the picker's tier-then-name ordering is exercised: the
    # cheap component must come first, not be sorted away alphabetically.
    for item_id, name, tier, cost, slot in (
        (10, "Confounded Item", 4, 6400, "weapon"),
        (20, "Genuine Item", 2, 1600, "vitality"),
        (30, "Filler Item", 1, 800, "spirit"),
    ):
        db.execute(
            "INSERT INTO dl_item (item_id, name, tier, cost, slot) "
            "VALUES (%s, %s, %s, %s, %s)",
            (item_id, name, tier, cost, slot),
        )
    db.execute("INSERT INTO dl_meta (meta_key, meta_value) VALUES ('window_days', '120')")

    def ref(minute, band, n, wins):
        db.execute(
            "INSERT INTO dl_ref (hero_id, minute, nw_band, n, wins) VALUES (1, %s, %s, %s, %s)",
            (minute, band, n, wins),
        )

    def buy(item, minute, band, n, wins):
        db.execute(
            "INSERT INTO dl_buy (hero_id, item_id, minute, nw_band, n, wins) "
            "VALUES (1, %s, %s, %s, %s, %s)",
            (item, minute, band, n, wins),
        )

    # Reference population: in every minute bucket, poor players (band 0) win
    # 30% and rich players (band 3) win 70%. This is the confound the estimator
    # has to see through.
    for minute in range(0, 42, 2):
        ref(minute, 0, 10_000, 3_000)
        ref(minute, 3, 10_000, 7_000)

    for minute in range(0, 42, 2):
        # Item 10 is bought overwhelmingly by rich players and wins at exactly
        # the reference rate *within* each band. Its true effect is zero, even
        # though its raw win rate is ~68%.
        buy(10, minute, 0, 500, 150)      # 30% — same as reference
        buy(10, minute, 3, 9_500, 6_650)  # 70% — same as reference

        # Item 20 is bought evenly across bands and beats the reference by a
        # flat 5 points everywhere, except for a 15-point spike at minute 20 and
        # a 10-point deficit from minute 34 on.
        if minute == 20:
            lift = 0.15
        elif minute >= 34:
            lift = -0.10
        else:
            lift = 0.05
        buy(20, minute, 0, 5_000, round(5_000 * (0.30 + lift)))
        buy(20, minute, 3, 5_000, round(5_000 * (0.70 + lift)))

    # Item 30 is cheap (800), in a third slot, and peaks early — so a plan can
    # use all three slots, and the cheapest item must not be pushed to the end.
    for minute in range(0, 42, 2):
        lift = 0.08 if minute <= 6 else 0.01
        buy(30, minute, 0, 5_000, round(5_000 * (0.30 + lift)))
        buy(30, minute, 3, 5_000, round(5_000 * (0.70 + lift)))

    # Enemy tables: hero 1 wins 50% against hero 2 overall, but buying item 20
    # against hero 2 in the 20-25 minute window does markedly better.
    for enemy in (2, 3):
        db.execute(
            "INSERT INTO dl_ref_enemy (hero_id, enemy_hero_id, n, wins) "
            "VALUES (1, %s, 20000, 10000)",
            (enemy,),
        )
    for minute5 in range(0, 45, 5):
        wins = 3_000 if minute5 != 20 else 3_600   # 50% normally, 60% at 20-25
        db.execute(
            "INSERT INTO dl_buy_enemy (hero_id, item_id, enemy_hero_id, minute5, n, wins) "
            "VALUES (1, 20, 2, %s, 6000, %s)",
            (minute5, wins),
        )

    # Ally tables mirror the enemy ones: hero 1 wins 50% alongside hero 2, but
    # buying item 20 with hero 2 on the team does better in the 20-25 window.
    for ally in (2, 3):
        db.execute(
            "INSERT INTO dl_ref_ally (hero_id, ally_hero_id, n, wins) "
            "VALUES (1, %s, 20000, 10000)",
            (ally,),
        )
    for minute5 in range(0, 45, 5):
        wins = 3_000 if minute5 != 20 else 3_450   # 50% normally, 57.5% at 20-25
        db.execute(
            "INSERT INTO dl_buy_ally (hero_id, item_id, ally_hero_id, minute5, n, wins) "
            "VALUES (1, 20, 2, %s, 6000, %s)",
            (minute5, wins),
        )

    db.execute(
        "INSERT INTO dl_comp (comp_key, heroes, n, comp_rank) "
        "VALUES ('2-3-4-5-6-7', '2-3-4-5-6-7', 900, 1)"
    )

    # Two patch eras. Item 20 is clearly nerfed between them; item 10 is
    # unchanged.
    #
    # Item 30 exists to make that statement meaningful. The patch measure is
    # *relative* — an item is compared against other purchases in the same era —
    # so with only two items, nerfing one would mechanically "buff" the other.
    # Item 30 is large and constant, so it dominates the comparison pool and an
    # unchanged item stays unchanged.
    for idx, (start, end, current, title) in enumerate([
        ("2026-05-01", "2026-06-01", 0, "Older Update - 05-01-2026"),
        ("2026-06-01", "", 1, "Newer Update - 06-01-2026"),
    ]):
        db.execute(
            "INSERT INTO dl_patch (patch_idx, patch_start, patch_end, is_current) "
            "VALUES (%s, %s, %s, %s)",
            (idx, start, end, current),
        )
        db.execute(
            "INSERT INTO dl_patch_title (patch_idx, title) VALUES (%s, %s)", (idx, title)
        )

    def buy_patch(item, idx, band, n, rate):
        db.execute(
            "INSERT INTO dl_buy_patch (item_id, patch_idx, nw_band, n, wins) "
            "VALUES (%s, %s, %s, %s, %s)",
            (item, idx, band, n, round(n * rate)),
        )

    for band in (0, 3):
        for idx, item20_rate in enumerate((0.58, 0.46)):   # 12 pp nerf
            buy_patch(20, idx, band, 8_000, item20_rate)
            buy_patch(10, idx, band, 8_000, 0.50)
            buy_patch(30, idx, band, 200_000, 0.50)

    yield
    logic.clear_caches()


@pytest.fixture
def client(seeded):
    from app.main import app

    return TestClient(app)


@pytest.fixture
def signed_in_client(seeded):
    """A client that presents Easy Auth headers, the way the platform would.

    Use this to check what a particular user or role sees. It works because the
    tests run with ``APPKIT_AUTH=dev``, where header simulation is allowed;
    in a real deployment appkit trusts these headers only when the operator has
    declared that a trusted proxy sits in front of the app.
    """
    from app.main import app

    def make(*, name="Amelia Stucki", email="amelia.stucki@uzh.ch", roles=("approver",)):
        principal = {
            "auth_typ": "aad",
            "claims": [
                {"typ": "name", "val": name},
                {"typ": "preferred_username", "val": email},
                *[{"typ": "roles", "val": role} for role in roles],
            ],
        }
        encoded = base64.b64encode(json.dumps(principal).encode()).decode()
        return TestClient(
            app,
            headers={
                "x-ms-client-principal": encoded,
                "x-ms-client-principal-name": email,
                "x-ms-client-principal-idp": "aad",
            },
        )

    return make
