-- Tables the app reads through appkit.db.
--
-- Written to be portable between the in-memory SQLite that appkit's `fake`
-- backend uses in tests and the Postgres it uses in production: no dialect
-- specific types, no reserved words as column names ("rank" is spelled
-- comp_rank for exactly that reason).
--
-- Every table is a precomputed aggregate. Nothing here is per-player or
-- per-match, so there is no personal data in the app's database.

CREATE TABLE IF NOT EXISTS dl_hero (
    hero_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);

-- Only items currently purchasable in the standard shop: removed items and
-- Street Brawl exclusives are filtered out in etl/deadlock.py. Components are
-- kept — a component is an ordinary shop item that also builds into something.
CREATE TABLE IF NOT EXISTS dl_item (
    item_id BIGINT PRIMARY KEY,
    name    TEXT NOT NULL,
    tier    INTEGER NOT NULL,
    cost    INTEGER NOT NULL,
    slot    TEXT NOT NULL      -- weapon | vitality | spirit; four slots of each
);

-- The most frequently played 6-hero team compositions, most common first.
CREATE TABLE IF NOT EXISTS dl_comp (
    comp_key  TEXT PRIMARY KEY,   -- sorted hero ids joined by "-"
    heroes    TEXT NOT NULL,      -- same ids, kept for display
    n         INTEGER NOT NULL,   -- times this composition was played
    comp_rank INTEGER NOT NULL    -- 1 = most common
);

-- Purchases: hero x item x 2-minute bucket x net-worth-at-buy band.
CREATE TABLE IF NOT EXISTS dl_buy (
    hero_id INTEGER NOT NULL,
    item_id BIGINT  NOT NULL,
    minute  INTEGER NOT NULL,
    nw_band INTEGER NOT NULL,
    n       INTEGER NOT NULL,
    wins    INTEGER NOT NULL
);

-- The comparison population for dl_buy: every player on that hero who reached
-- that minute in that net-worth band, whatever they bought.
CREATE TABLE IF NOT EXISTS dl_ref (
    hero_id INTEGER NOT NULL,
    minute  INTEGER NOT NULL,
    nw_band INTEGER NOT NULL,
    n       INTEGER NOT NULL,
    wins    INTEGER NOT NULL
);

-- Purchases split by a single enemy hero present on the other team,
-- on a coarser 5-minute axis because the enemy dimension is 38x sparser.
CREATE TABLE IF NOT EXISTS dl_buy_enemy (
    hero_id       INTEGER NOT NULL,
    item_id       BIGINT  NOT NULL,
    enemy_hero_id INTEGER NOT NULL,
    minute5       INTEGER NOT NULL,
    n             INTEGER NOT NULL,
    wins          INTEGER NOT NULL
);

-- Baseline for dl_buy_enemy: how this hero does against that enemy at all,
-- so a favourable matchup is not misread as a good item.
CREATE TABLE IF NOT EXISTS dl_ref_enemy (
    hero_id       INTEGER NOT NULL,
    enemy_hero_id INTEGER NOT NULL,
    n             INTEGER NOT NULL,
    wins          INTEGER NOT NULL
);

-- The patch eras the extract covers, ascending by date.
CREATE TABLE IF NOT EXISTS dl_patch (
    patch_idx   INTEGER PRIMARY KEY,  -- 0 = OLDEST era; the highest index is current
    patch_start TEXT NOT NULL,        -- YYYY-MM-DD the era began
    patch_end   TEXT NOT NULL,        -- YYYY-MM-DD it ended ('' for the current one)
    is_current  INTEGER NOT NULL
);

-- Human-readable name for each era, from the patch notes feed. Kept separate
-- from dl_patch so a title can be re-fetched without touching the numbers.
CREATE TABLE IF NOT EXISTS dl_patch_title (
    patch_idx INTEGER PRIMARY KEY,
    title     TEXT NOT NULL
);

-- How an item has fared across balance eras: item x era x net-worth band.
--
-- This is the one table that deliberately looks *outside* the current patch.
-- Everything else in this database is current-patch-only, because that is what
-- a player should act on; this exists to answer the different question of
-- whether an item is being buffed or nerfed over time.
--
-- Note the reference is different from dl_ref: here an item is compared against
-- *other purchases at the same wealth in the same era*, which is derivable by
-- summing this table across items and subtracting the item itself. That keeps
-- the trend to one query per era instead of two.
CREATE TABLE IF NOT EXISTS dl_buy_patch (
    item_id   BIGINT  NOT NULL,
    patch_idx INTEGER NOT NULL,
    nw_band   INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    wins      INTEGER NOT NULL
);

-- Purchases split by which heroes were on the buyer's own team, and the
-- matching baseline. Mirrors dl_buy_enemy / dl_ref_enemy exactly: an item that
-- looks strong alongside a particular ally may only be riding that ally's own
-- strength, so the purchase numbers are read relative to dl_ref_ally.
CREATE TABLE IF NOT EXISTS dl_buy_ally (
    hero_id      INTEGER NOT NULL,
    item_id      BIGINT  NOT NULL,
    ally_hero_id INTEGER NOT NULL,
    minute5      INTEGER NOT NULL,
    n            INTEGER NOT NULL,
    wins         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dl_ref_ally (
    hero_id      INTEGER NOT NULL,
    ally_hero_id INTEGER NOT NULL,
    n            INTEGER NOT NULL,
    wins         INTEGER NOT NULL
);

-- Provenance: window, patch range, build time. Shown in the app footer so a
-- number on screen can always be traced to the extract that produced it.
CREATE TABLE IF NOT EXISTS dl_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_dl_buy_lookup       ON dl_buy (hero_id, item_id);
CREATE INDEX IF NOT EXISTS ix_dl_ref_lookup       ON dl_ref (hero_id);
CREATE INDEX IF NOT EXISTS ix_dl_buy_enemy_lookup ON dl_buy_enemy (hero_id, item_id);
CREATE INDEX IF NOT EXISTS ix_dl_ref_enemy_lookup ON dl_ref_enemy (hero_id);
CREATE INDEX IF NOT EXISTS ix_dl_comp_rank        ON dl_comp (comp_rank);
CREATE INDEX IF NOT EXISTS ix_dl_buy_patch_item    ON dl_buy_patch (item_id);
CREATE INDEX IF NOT EXISTS ix_dl_buy_patch_era     ON dl_buy_patch (patch_idx);
CREATE INDEX IF NOT EXISTS ix_dl_buy_ally_lookup   ON dl_buy_ally (hero_id, item_id);
CREATE INDEX IF NOT EXISTS ix_dl_ref_ally_lookup   ON dl_ref_ally (hero_id);
