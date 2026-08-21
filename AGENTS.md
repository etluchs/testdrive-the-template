# AGENTS.md — house rules for this app

You are an AI assistant helping build an internal UZH business app from this
template. The person you're working with is **not an engineer**. They rely on
you to keep this app on the golden path so it stays secure, accessible, and
easy for the next person to maintain.

Follow these rules. They are not suggestions.

---

## The three rules that matter most

### 1. Routes stay thin

A route in `app/main.py` may do only three things: read the request, call a
function in `app/logic.py`, and return a template. No data crunching, no loops
building strings, no talking to SharePoint/mail/database inside a route.

If a route is more than a few lines, move the work into `app/logic.py` — that's
where logic is written and where it gets unit-tested without a browser.

```python
# GOOD — app/main.py
@app.post("/mail-summary")
def mail_summary(request: Request, recipient: str = Form(...)):
    summary = logic.send_summary(recipient)      # the work lives in logic.py
    return templates.TemplateResponse(request, "_summary.html",
                                      context(request, summary=summary))
```

```python
# BAD — business logic and integrations crammed into the route
@app.post("/mail-summary")
def mail_summary(request: Request, recipient: str = Form(...)):
    rows = httpx.get("https://graph.microsoft.com/...").json()["value"]   # NO
    text = ""
    for r in rows: text += r["Title"] + "\n"                              # NO
    ...
```

### 2. Never import `httpx` or `psycopg` directly

All Microsoft Graph, email, and database access goes through **appkit**. appkit
owns the network clients, the managed-identity authentication, and the fakes
used in tests. If you import `httpx`, `requests`, `psycopg`, `pyodbc`, or the
Azure SDKs in `app/`, you have left the golden path.

| You need to… | Use this |
| --- | --- |
| Read a SharePoint list | `from appkit import sharepoint` → `sharepoint.list_rows(...)` |
| Send an email | `from appkit import mail` → `mail.send_mail(...)` |
| Query the database | `from appkit import db` → `db.query(...)` / `db.execute(...)` |
| Know who is signed in | `from appkit import auth` → `auth.user(request)` |

If appkit can't do something you need, that's a signal to add it **to appkit**
(with a fake and a test) — not to reach for `httpx` in the app.

> The only place `httpx` legitimately appears in this repo is the **test
> client** (`fastapi.testclient`), and only in `tests/`.

### 3. Never hand-write form or table markup

Use the macros in `app/templates/_macros.html`. They are WCAG 2.1 AA by
construction — labels tied to inputs, table header scopes, focus states,
status roles. Hand-written `<input>` / `<table>` / `<div class="alert">` will
quietly drop those, and the accessibility check (pa11y) in CI will fail.

```jinja
{% from "_macros.html" import field, select, button, table, alert, nav %}

{{ table(columns, rows, caption="Current requests", row_header="Title") }}
{{ field("recipient", "Recipient email", type="email", required=true) }}
{{ button("Send summary", type="submit") }}
{{ alert("Summary sent.", kind="success") }}
```

Don't write raw `<form>` inputs, `<table>` cells, or alert `<div>`s by hand.
Need something the macros don't cover? Add a macro (keep it accessible), don't
inline the HTML.

---

## How the pieces fit

```
app/
  main.py         # routes — thin: input -> logic -> template
  logic.py        # all business logic; only imports appkit
  templates/
    base.html     # page shell: <html lang>, skip link, <main>, nav
    _macros.html  # the ONLY sanctioned form/table/alert/nav markup
    index.html    # the example page
    _summary.html # HTMX partial swapped in after "Send summary"
  static/
    htmx.min.js   # HTMX 2, vendored (no CDN)
    app.css       # UZH corporate design; keep the focus outlines
    uzh_logo.svg  # vendored logo
    fonts/        # Source Sans, vendored (no CDN)
tests/            # pytest; runs on appkit's fake backend, no network
```

- **HTMX is vendored** in `app/static/`. Do not add a `<script src="https://…">`
  to a CDN — everything ships with the app.
- **Backends:** appkit defaults to an in-memory `fake` backend, so the app runs
  and tests pass with no Azure and no network. Production sets
  `APPKIT_BACKEND=azure` and everything authenticates with the app's **managed
  identity**. Never put secrets, connection strings, or tokens in code or
  templates.
- **Auth:** the signed-in user comes from `auth.user(request)`, which reads the
  Container Apps Easy Auth headers. Don't parse those headers yourself.

### Two environment variables the deployment must set

`APPKIT_BACKEND` and `APPKIT_AUTH` are both **required** once the app runs on
Azure Container Apps. appkit raises rather than guessing either one, because a
wrong guess fails silently: the wrong backend discards mail and database writes
while looking like it worked, and the wrong auth mode signs in callers who never
logged in.

| Variable | Local | Container Apps |
| --- | --- | --- |
| `APPKIT_BACKEND` | `fake` | `azure` |
| `APPKIT_AUTH` | `dev` | `easyauth` (or `verify`) |

The `Dockerfile` sets both for production and `tests/conftest.py` sets both for
tests, so you should not need to think about them. If you hit
`ConfigError: APPKIT_AUTH is not set`, the deployment is missing configuration —
do **not** work around it in code.

### Never authorize on something the app cannot verify

`auth.user(request)` returns a `User` whose roles came from headers Easy Auth
injected. Behind Easy Auth that is trustworthy; on any request path that skips
it, a caller can set those headers themselves. So let appkit decide whether the
identity can be believed, and never read the headers yourself:

```python
# GOOD — appkit applies whatever APPKIT_AUTH says, then you check the role
user = auth.user(request)
if user is None or not user.has_role("approver"):
    return templates.TemplateResponse(request, "_denied.html",
                                      context(request), status_code=403)
```

```python
# BAD — reading the headers directly bypasses APPKIT_AUTH entirely
if request.headers.get("x-ms-client-principal-name"):        # NO
    ...
```

If an app does something genuinely sensitive behind a role check, ask for
`APPKIT_AUTH=verify`: it validates the tenant-signed id token instead of
trusting a header. That is a deployment change, not a code change — flag it to
the person you're working with rather than trying to arrange it in `app/`.

## Stay inside the UZH corporate design

`app/static/app.css` implements UZH frontend framework 2.10.0 — its palette, its
type scale, its typeface. The custom properties carry the framework's own names
and values, so **use the tokens** (`rgba(var(--c-blue), 1)`) rather than typing
a hex code, and take any new value from the framework rather than inventing one.

Fonts and the logo are vendored in `app/static/`. Never replace them with a CDN
link: everything ships with the app, and a remote asset would leak every
visitor's IP to whoever hosts it.

The framework turns the focus outline off and replaces it per component. We keep
ours — see the note at the top of `app.css`, and the test that pins it. Do not
"fix" that to match.

If you add a macro, style it in `app.css` and keep the contrast: text at 4.5:1,
borders and focus rings at 3:1.

## Before you say you're done

Run these locally (they are exactly what CI runs):

```sh
uv run ruff check .        # lint & import order
uv run pytest              # unit + integration tests, fake backend
```

CI additionally runs **pa11y** against the live example app to enforce WCAG 2.1
AA. If you changed templates or the macros, expect that check to be the one that
catches a missing label or header scope.

## When in doubt

Prefer the smallest change that keeps the app on the golden path. If a task
seems to require breaking one of the three rules, stop and flag it to the person
you're working with rather than working around it.
