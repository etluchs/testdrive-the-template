# Deadlock item timing

**When is buying this item actually worth it — and when does it stop being
worth it?** Pick a hero, an item, and the enemy team; the app returns the
purchase window with the largest effect on win rate, and the point at which the
item stops paying for itself.

Built on the UZH golden-path template (adate): FastAPI · Jinja2 · HTMX 2
(vendored) · [appkit](https://github.com/uzh-zi/appkit) · uv · ruff · pytest ·
pa11y.

## The one thing to understand about the numbers

Grouping purchases by minute and averaging the win column gives an answer that
is confidently wrong. A player who buys a tier-4 item at minute 15 could
*afford* it at minute 15, which usually means the game was already going well.
Billy's Phantom Strike looks like it climbs from a 46% to a 63% win rate the
later you buy it — which would read as "buy it as late as possible". Most of
that curve is the lead paying for the item, not the item earning the lead. Late
buys are survivor-biased on top: losing games end before minute 30 at all.

So every purchase is compared against a **reference population** — players on
the same hero, at the same minute, holding a similar net worth, whatever they
bought — rather than against the global average. See `/method` in the running
app, or the module docstring in `app/logic.py`.

This removes confounding by wealth at the moment of purchase. It is not a
randomised trial, and the app says so on screen.

## Why not every team composition

Deadlock never repeats a hero in a match, so with 39 playable heroes there are
`C(39,6) × C(33,6) / 2` ≈ **1.8 trillion** possible 6-v-6 matchups against
roughly 10.7 million matches on record. Even the 10,000 most-played
compositions cover only **11.5%** of games, and the single most common one
occurs ~1,131 times — which is nothing once split across 251 items and 21
purchase windows.

The enemy team is therefore handled by the **presence of individual enemy
heroes**, which is both estimable and closer to the mechanism: Phantom Strike is
good into Vindicta because she is squishy, mobile and hard to reach, not because
of her identity. Estimates built that way generalise to compositions nobody has
ever played. Every result states which level produced it and how many purchases
sit underneath.

## Current patch only

Two filters, both applied in `etl/`:

**Items.** Of the 251 upgrades in the catalogue:

| | Count | Why |
| --- | --- | --- |
| Removed from the game | 78 | `disabled: true` + `shopable: false`; several never had a display name (`upgrade_clip_size_fixed`). They exist so old matches can still be decoded. |
| Street Brawl only | 17 | A different mode from the one these statistics describe. |
| **Kept** | **156** | Tiers 1–4, everything buyable in the standard shop. |

Brawl items are detected by asset path (`.../images/items/brawl/...`). Two other
signals agree exactly — they are the only `item_tier` 5 items and the only ones
priced 9999, a sentinel rather than a real cost — and the build *warns* if those
sets ever diverge, rather than silently dropping items. Four ordinary shop items
(Cursed Relic, Alchemical Fire, Greater/Mystic Expansion) merely mention Street
Brawl in their tooltips; they are kept.

**Components are kept.** Healing Booster builds into Healing Tempo, but it is an
ordinary tier-2 item bought and held on its own — often the real early decision
is between components, not between the tier-4 items they become. The picker
orders by tier so they are not buried. (An earlier name-ordered `LIMIT 60` cut
the list off mid-alphabet and dropped exactly these; `tests/test_logic.py` now
pins that the list is complete.)

**Matches.** The extract window is anchored to the most recent *major* patch,
which `/v1/patches/big-days` identifies separately from the frequent minor
updates that tweak one hero. At the time of writing that is **2026-03-11**, so
the window is 163 days — statistics from before it describe a different game.
The anchor is resolved at build time, not hardcoded; if a major patch lands and
leaves less than 30 days of data, the build warns rather than silently widening
the window back across the boundary.

## Comparing patches

Deadlock publishes no version numbers — updates are identified by date and
headline ("Minor Update - 08-12-2026"), and the only numeric ids are opaque
client build numbers. So the app names each era by its start date and title, and
lets you pick any two to compare:

```
Earlier patch:  2026-07-30 — Matchmaking Update
Later patch:    2026-08-12 — Minor Update - 08-12-2026
→ "Magic Carpet does worse in 2026-08-12 than in 2026-07-30: -6.1 pp
   (95% CI -7.5 to -4.7)."
```

**A patch figure is not the timing curve's number.** The curve compares an item
against all players on one hero. A patch figure compares it against all *other
purchases* at the same net worth in the same era, pooled across heroes — a
coarser measure that survives being cut ten ways by era. An item's own purchases
are excluded from its own reference, so when the rest of the shop shifts, an item
whose own win rate never moved can still show a change. The app says this on
screen, and `tests/test_logic.py` pins it with a constant item that reads +6 pp
purely because another item was nerfed around it.

All ten eras come back from a **single** request: the era index is computed
inline with `arrayCount` over the boundary dates. One query per era would have
cost half the hourly budget.

## Finding your current match

```sh
uv run python -m etl.livematch --account-id 180942972
```
```
match 100782725  (8/12 players visible)
  you      : Paige (67)
  your team: Lash (31), Shiv (19), Venator (65)
  enemies  : Haze (13), Graves (76), Lady Geist (4), Mo & Krill (18)
  note     : the live list lags the draft, so some slots may still be missing.
```

The page can also do this itself: enter your account id in **Find my match** and
the hero, your team and the enemy team are filled in for you.

```
Found match 100783220: you are on Ivy. Only 8 of 12 players are visible so far —
run it again in a few seconds for the full line-up.
```

It reads the **public** active-match list and filters it by the id you give it —
it does not read your game, your files or your Steam session, and nothing is
installed beside the client. The live list is populated from the draft onward,
so it can arrive partly filled; the app says so rather than pretending the
roster is complete.

This is the **only** place `app/` reaches the network, as a deliberate exception
to the no-network rule (see `app/live.py` and AGENTS.md). It is bounded: one
public endpoint, a short timeout, a cache shared across visitors so one fetch
serves everyone, and no failure path that can produce an error page. `/v1/sql`
is still never called from the app — 20 requests an hour would be gone by the
second visitor.

## Why this template exists

Internal apps here are increasingly written with an AI assistant driving. This
template plus **[AGENTS.md](AGENTS.md)** keeps that work on a golden path:

- **Routes stay thin** — request in, `logic.py` does the work, template out.
- **No raw integrations** — SharePoint, mail, DB, and auth go through appkit;
  app code never imports `httpx` or `psycopg`.
- **No hand-written form/table markup** — an accessible Jinja **macro library**
  (`field`, `select`, `button`, `table`, `alert`, `nav`) is the only sanctioned
  way to emit UI, and CI runs **pa11y** to enforce WCAG 2.1 AA.

Read [AGENTS.md](AGENTS.md) first — it's the rulebook the assistant must follow.

## Layout

```
app/
  main.py            # FastAPI app — thin routes
  logic.py           # the estimator; imports appkit only, never the network
  templates/
    base.html        # page shell: <html lang>, skip link, <main>, nav
    _macros.html     # accessible macro library (+ effect_chart)
    index.html       # hero / item / enemy-team picker
    _items.html      # HTMX partial: item list follows the chosen hero
    _timing.html     # HTMX partial: verdict, chart and table
    method.html      # how the numbers are made, and what they do not prove
  static/
    htmx.min.js       # HTMX 2.0.4, vendored — no CDN
    app.css           # UZH corporate design (frontend framework 2.10.0)
    uzh_logo.svg      # vendored from the 2.10.0 release
    fonts/            # Source Sans, vendored — no CDN
etl/                 # OFFLINE pipeline — the only code that touches the network
  deadlock.py        # paced client for the public Deadlock API
  queries.py         # the ClickHouse queries, and why they look like that
  schema.sql         # the tables the app reads
  build.py           # python -m etl.build --heroes all   (real, slow)
  devseed.py         # python -m etl.devseed                (synthetic, instant)
  livematch.py       # python -m etl.livematch --account-id N (who is in my game)
data/
  raw/               # cached API responses (resumable builds)
  seed.sql           # the extract the app loads at startup
tests/               # pytest, runs on a synthetic extract — no network
Dockerfile           # multi-stage, non-root, managed-identity runtime
AGENTS.md            # house rules for the AI assistant
.pa11yci.json        # accessibility config (WCAG2AA)
.github/workflows/   # ruff + pytest + pa11y
```

`etl/` sits outside `app/` deliberately. AGENTS.md forbids app code from opening
its own network connections, and the app never calls the API: `/v1/sql` allows
**20 requests per hour**, which is fine for a nightly build and unusable on a
request path. The pipeline writes `data/seed.sql`; the app reads the seeded
tables through `appkit.db`.

## Getting started

```sh
uv sync --extra dev              # installs appkit (from git) + app deps
uv run python -m etl.devseed     # synthetic extract, offline, ~3 seconds
uv run uvicorn app.main:app --reload --port 8080
# open http://localhost:8080  — you're the local "Dev User"
```

That gets you a running app immediately. There are **two ways to produce the
extract the app serves**, and the difference matters:

| | `etl.devseed` | `etl.build` |
| --- | --- | --- |
| Data | invented | real |
| Time | ~5 s | ~105 min |
| Network | none | 34 requests, rate limited |
| Use for | development, tests, CI, pa11y | anything anyone will act on |

```sh
uv run python -m etl.build --heroes all          # the real one
uv run python -m etl.build --heroes Billy,Mina   # a slice, while developing
```

A synthetic extract is stamped `source = synthetic (development only)` in
`dl_meta` and shown as such on the Method page, so it cannot quietly be mistaken
for real advice.

### Why the real build takes ~2 hours

`/v1/sql` allows **20 requests per hour per IP**, on a rolling window, plus a
2-per-minute burst cap. The pipeline paces itself at one request per 200 s and
batches heroes into each query, which puts a full 39-hero build at **34
requests, a little under two hours**. Querying per hero instead would need 236
requests — roughly twelve hours.

The build is **resumable**: every response is cached under `data/raw/`, and
re-running skips what is already there. Re-running after a code change rebuilds
`seed.sql` from cache without spending any request budget.

Run the checks CI runs:

```sh
uv run ruff check .
uv run pytest
```

Run the accessibility check locally (needs Node):

```sh
uv run uvicorn app.main:app --port 8080 &
npx pa11y-ci --config .pa11yci.json
```

## Corporate design

Styling follows the **UZH frontend framework 2.10.0**
(https://www.frontend.uzh.ch/prod/index.html): its palette, its type scale
(42/26/18px headings at weight 600, 18px body copy), Source Sans as the
corporate typeface, pill buttons, and the UZH wordmark in the header. The
custom properties in `app.css` use the framework's own names and values, so
`--c-blue: 0, 40, 165` means the same here as it does there — **use the tokens**
rather than typing a hex code.

The framework's own 220 KB stylesheet is deliberately *not* used: it targets the
university web platform's markup, while an app built from this template renders
the macros below. Taking the tokens gets the look without the coupling. Fonts
and the logo are vendored into `app/static/`; nothing is fetched from a CDN,
which also keeps visitors' IP addresses off third-party servers.

`tests/test_corporate_design.py` pins the palette, the type scale, the vendored
assets, the no-CDN rule and — see below — the focus ring, so an app that
inherits this template inherits the checks too.

**One deliberate deviation.** The framework sets
`:focus-visible { outline: none !important }` and supplies its own per-component
focus indicators. This template does not ship those components, so dropping the
outline would leave keyboard users with no visible focus at all — a WCAG 2.4.7
failure that no automated checker flags, because it cannot distinguish a styled
focus state from a missing one. The template keeps a visible focus ring in UZH
blue. Don't "fix" it to match.

## The macro library

```jinja
{% from "_macros.html" import select, button, table, alert, effect_chart %}

{{ select("hero_id", "Your hero", heroes, required=true) }}
{{ button("Show timing", type="submit") }}
{{ alert(result.verdict, kind="success") }}
{{ effect_chart(result.points, caption="Change in win rate by purchase minute") }}
{{ table(columns, result.rows_for_table, caption="…", row_header="window") }}
```

Each macro bakes in the accessibility details that are easy to forget: `<label
for>` tied to every input, `aria-describedby` for help/error text, `<th
scope>` on tables, `role="status"`/`role="alert"` on banners (meaning carried by
text, not colour), and `aria-current="page"` in the nav.

`effect_chart` is this app's addition to the library. A chart is not an
accessible medium on its own, so it emits an `<svg role="img">` with a real text
alternative, and the same numbers always appear in a table beneath it — the
chart is never the only carrier of a value. Direction is shown by position
relative to the zero line, not by colour alone, and a window with too few
purchases is drawn hollow *and* labelled as such in the table.

## Configuration

Local dev needs nothing. In production the app reads its configuration from the
environment and authenticates with its managed identity; see the
[appkit README](https://github.com/uzh-zi/appkit) for the full list
(`APPKIT_SHAREPOINT_SITE`, `APPKIT_MAIL_SENDER`, `APPKIT_DB_DSN`, …).

Two variables are **required** on Container Apps — appkit raises rather than
guessing them, because a wrong guess fails silently:

| Variable | Local | Container Apps | What a wrong value does |
| --- | --- | --- | --- |
| `APPKIT_BACKEND` | `fake` | `azure` | On `fake`, mail is discarded and database writes vanish on restart, while every call still looks like it worked. |
| `APPKIT_AUTH` | `dev` | `easyauth` | On `dev`, every caller is signed in as the dev user. appkit refuses this one outright on Container Apps. |

The `Dockerfile` sets both.

## Deploying

```sh
docker build -t uzh-app .
```

The image runs as a non-root user, serves on port 8080, exposes `/health`, and
sets `APPKIT_BACKEND=azure` and `APPKIT_AUTH=easyauth`. Deploy to Azure Container
Apps with a managed identity granted the Graph and Postgres permissions appkit
needs, and with authentication configured as below.

### Easy Auth is load-bearing, not decoration

`APPKIT_AUTH=easyauth` tells appkit to believe the `X-MS-CLIENT-PRINCIPAL`
headers on incoming requests. Easy Auth strips client-supplied copies of those
headers and injects its own, so **behind it** they are trustworthy. Any request
path that skips it lets the caller write them by hand — and with them their own
roles. So when you deploy:

- Enable authentication on the Container App, and set unauthenticated requests
  to be **rejected** (HTTP 302 to the login, or 401), not allowed through.
- Keep ingress external-only. If other apps in the same environment can reach
  this one directly, they bypass the auth proxy along with everything else.

If an app makes a genuinely sensitive decision on `has_role()`, use
`APPKIT_AUTH=verify` instead. It ignores those headers and cryptographically
verifies the tenant-signed id token, which a forged header cannot survive. It
needs the Easy Auth **token store** enabled, the `appkit[verify]` extra, and
`APPKIT_AUTH_TENANT_ID` / `APPKIT_AUTH_CLIENT_ID`.

## appkit dependency

This template depends on [appkit](https://github.com/uzh-zi/appkit) via a git
source in `pyproject.toml`, tracking appkit's `main` branch. Pin it to a tag
once appkit publishes releases.
