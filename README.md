# adate — UZH Python ADmin App TEmplate (golden-path)

A ready-to-clone starting point for internal business apps at UZH.

**Stack:** FastAPI · Jinja2 · HTMX 2 (vendored) · [appkit](https://github.com/uzh-zi/appkit)
· uv · ruff · pytest · pa11y

The template ships one **working example app** that lists rows from a SharePoint
list and emails a summary — the shape of a typical internal tool. Everything
runs locally with no Azure and no network, because [appkit](https://github.com/uzh-zi/appkit)
provides in-memory fakes; in production the same code authenticates with the
app's **managed identity**.

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
  logic.py           # business logic (imports appkit only)
  templates/
    base.html        # page shell: <html lang>, skip link, <main>, nav
    _macros.html     # accessible macro library
    index.html       # the example page
    _summary.html    # HTMX partial (mail confirmation)
  static/
    htmx.min.js       # HTMX 2.0.4, vendored — no CDN
    app.css           # UZH corporate design (frontend framework 2.10.0)
    uzh_logo.svg      # vendored from the 2.10.0 release
    fonts/            # Source Sans, vendored — no CDN
tests/               # pytest, runs on appkit's fake backend
Dockerfile           # multi-stage, non-root, managed-identity runtime
AGENTS.md            # house rules for the AI assistant
.pa11yci.json        # accessibility config (WCAG2AA)
.github/workflows/   # ruff + pytest + pa11y
```

## Getting started

```sh
uv sync --extra dev              # installs appkit (from git) + app deps
uv run uvicorn app.main:app --reload --port 8080
# open http://localhost:8080  — you're the local "Dev User"
```

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
{% from "_macros.html" import field, select, button, table, alert, nav %}

{{ table(columns, rows, caption="Current requests", row_header="Title") }}
{{ field("recipient", "Recipient email", type="email", required=true,
         help="We’ll email the summary here.") }}
{{ button("Send summary", type="submit") }}
{{ alert("Summary sent.", kind="success") }}
```

Each macro bakes in the accessibility details that are easy to forget: `<label
for>` tied to every input, `aria-describedby` for help/error text, `<th
scope>` on tables, `role="status"`/`role="alert"` on banners (meaning carried by
text, not colour), and `aria-current="page"` in the nav.

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
