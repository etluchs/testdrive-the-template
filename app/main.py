"""FastAPI entrypoint.

Routes are deliberately thin: read input, call ``app.logic``, render a
template. No integration code, no data shaping, no hand-written HTML.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from appkit import auth
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import logic

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Load the precomputed extract into the database once, at startup."""
    logic.load_seed()
    yield


app = FastAPI(title="Deadlock Item Timing", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

APP_NAME = "Item Timing"

# The enemy team is optional, and six empty selects post six empty strings.
# Held as a module-level singleton because a call in an argument default is
# evaluated once at import anyway, and writing it inline trips B008.
ENEMY_FIELD = Form(None)
ALLY_FIELD = Form(None)
STRATEGY_FIELD = Form(None)
PATCH_BEFORE_FIELD = Form(None)
PATCH_AFTER_FIELD = Form(None)
NAV_ITEMS = [{"href": "/", "label": "Item timing"}, {"href": "/method", "label": "Method"}]


def context(request: Request, **extra) -> dict:
    """Common template context: signed-in user and page chrome."""
    return {
        "user": auth.user(request),
        "app_name": APP_NAME,
        "nav_items": NAV_ITEMS,
        "current_path": request.url.path,
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    heroes = logic.heroes_with_data()
    first = int(heroes[0]["value"]) if heroes else 0
    tier = logic.default_tier(first) if first else 1
    return templates.TemplateResponse(
        request,
        "index.html",
        context(
            request,
            title="When should I buy it?",
            heroes=heroes,
            **logic.lineup_values(first or None),
            tier=str(tier),
            tiers=logic.tier_options(first) if first else [],
            items=logic.items_for_hero(first, tier) if first else [],
            enemy_options=logic.enemy_options(),
            ally_options=logic.enemy_options(),
            strategy_options=logic.strategy_options(),
            patch_options=logic.patch_options(),
            presets=logic.common_compositions(10),
            extract=logic.extract_info(),
        ),
    )


@app.get("/items", response_class=HTMLResponse)
def items_for_hero(request: Request, hero_id: int = 0, item_tier: str = "") -> HTMLResponse:
    """HTMX partial: the tier and item pickers, refreshed together.

    Both live in one swapped region so that changing the hero cannot leave a
    tier selected that the new hero has no items for.
    """
    try:
        tier = logic.parse_tier(item_tier, hero_id)
    except ValueError:
        tier = logic.default_tier(hero_id)
    return templates.TemplateResponse(
        request,
        "_items.html",
        context(
            request,
            tier=str(tier),
            tiers=logic.tier_options(hero_id),
            items=logic.items_for_hero(hero_id, tier),
        ),
    )


@app.get("/live", response_class=HTMLResponse)
def live_lineup(request: Request, account_id: str = "") -> HTMLResponse:
    """Fill the pickers from the match this account is currently in.

    The one route that touches the network — see ``app/live.py`` for the terms
    of that exception. It cannot fail the page: every problem, including the
    upstream service being unreachable, comes back as a rendered message with
    the pickers left as they were.
    """
    found, note = logic.live_lineup(account_id)
    return templates.TemplateResponse(
        request,
        "_lineup.html",
        context(
            request,
            heroes=logic.heroes_with_data(),
            enemy_options=logic.enemy_options(),
            ally_options=logic.enemy_options(),
            tiers=logic.tier_options(found["hero_id"]) if found else [],
            tier=str(logic.default_tier(found["hero_id"])) if found else "1",
            items=(
                logic.items_for_hero(found["hero_id"], logic.default_tier(found["hero_id"]))
                if found else []
            ),
            live_note=note,
            **logic.lineup_values(
                found["hero_id"] if found else None,
                found["allies"] if found else None,
                found["enemies"] if found else None,
            ),
        ),
    )


@app.post("/timing", response_class=HTMLResponse)
def timing(
    request: Request,
    hero_id: int = Form(...),
    item_id: int = Form(...),
    enemy: list[str] | None = ENEMY_FIELD,
    ally: list[str] | None = ALLY_FIELD,
    build_strategy: str | None = STRATEGY_FIELD,
    patch_before: str | None = PATCH_BEFORE_FIELD,
    patch_after: str | None = PATCH_AFTER_FIELD,
) -> HTMLResponse:
    try:
        enemy_ids = logic.parse_enemy_ids(enemy)
        ally_ids = logic.parse_ally_ids(ally)
        strategy = logic.parse_strategy(build_strategy)
        before_idx = logic.parse_patch_idx(patch_before)
        after_idx = logic.parse_patch_idx(patch_after)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_timing.html", context(request, error=str(exc)), status_code=400
        )
    result = logic.estimate(hero_id, item_id, enemy_ids, ally_ids)
    # Scored once and shared: this is the expensive half of planning.
    candidates = logic.score_candidates(hero_id, ally_ids, enemy_ids)
    comparison = None
    if before_idx is not None and after_idx is not None and before_idx != after_idx:
        comparison = logic.compare_patches(item_id, before_idx, after_idx)
    return templates.TemplateResponse(
        request,
        "_timing.html",
        context(
            request,
            result=result,
            plan=logic.plan_build(hero_id, ally_ids, enemy_ids, strategy, candidates),
            key_buys=logic.key_buy_rows(
                logic.key_buys(hero_id, ally_ids, enemy_ids, candidates=candidates)
            ),
            comparison=comparison,
            comparison_rows=logic.comparison_rows(comparison) if comparison else [],
            trend_rows=logic.patch_trend_rows(item_id),
        ),
    )


@app.get("/method", response_class=HTMLResponse)
def method(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "method.html",
        context(request, title="How the numbers are made", extract_rows=logic.extract_rows()),
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
