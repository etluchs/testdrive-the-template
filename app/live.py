"""The one place in ``app/`` that reaches the network.

AGENTS.md rule 4 forbids app code from opening its own connections. **This
module is an explicit, granted exception**, agreed for one specific job: looking
up which match you are in right now. Everything else in the app still reads
precomputed tables through ``appkit.db``, and the exception does not generalise
— in particular ``/v1/sql`` must never be called from here, because it allows 20
requests an hour and would be rate-limited out of service by the second visitor.

The exception is bounded deliberately:

* **One endpoint only.** ``/v1/matches/active`` — a public, unauthenticated list
  of in-progress matches. No account is logged in, no credential is sent, and
  nothing about the caller is disclosed to the upstream service beyond the
  request itself.
* **A short timeout.** A hung upstream must not tie up a worker; the call gives
  up quickly and the page says so.
* **A shared cache.** The response covers *every* live match, so one fetch
  serves every visitor for a few seconds instead of one fetch per click.
* **No exception escapes.** Every failure becomes a message on the page. A
  third-party service being down is not a 500 in this app.

Privacy note: the account id is supplied by the person using the page and is
only compared against the public list. It is never logged, stored, or sent
anywhere other than as the local filter it is used for.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ACTIVE_URL = "https://api.deadlock-api.com/v1/matches/active"

#: Short enough that a fresh draft shows up quickly, long enough that a page
#: full of people clicking does not turn into a fetch per click.
CACHE_TTL_S = 10.0

#: A request path must not wait on someone else's outage.
TIMEOUT_S = 6.0

USER_AGENT = "uzh-deadlock-item-timing/0.1 (live match lookup)"

_lock = threading.Lock()
_cache: dict[str, object] = {"fetched_at": 0.0, "matches": None}


class LiveLookupError(RuntimeError):
    """The live list could not be read. Always rendered, never raised at a user."""


@dataclass(frozen=True)
class LiveMatch:
    """Who is in the match, from this player's point of view."""

    match_id: int
    hero_id: int
    allies: list[int] = field(default_factory=list)
    enemies: list[int] = field(default_factory=list)
    players_seen: int = 0

    @property
    def partial(self) -> bool:
        """The live list lags the draft, so a match can arrive half-filled."""
        return self.players_seen < 12


def _fetch_active() -> list:
    """The raw live-match list, cached for a few seconds across all callers."""
    now = time.monotonic()
    with _lock:
        cached = _cache["matches"]
        if cached is not None and now - float(_cache["fetched_at"]) < CACHE_TTL_S:
            return cached  # type: ignore[return-value]

    request = urllib.request.Request(ACTIVE_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LiveLookupError(
            f"The live match service answered {exc.code}. Try again shortly."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveLookupError(
            "The live match service could not be reached. Try again shortly."
        ) from exc
    except json.JSONDecodeError as exc:
        raise LiveLookupError("The live match service returned something unreadable.") from exc

    if not isinstance(payload, list):
        raise LiveLookupError("The live match service returned an unexpected shape.")

    with _lock:
        _cache["matches"] = payload
        _cache["fetched_at"] = time.monotonic()
    return payload


def reset_cache() -> None:
    """Forget the cached list (used by tests)."""
    with _lock:
        _cache["matches"] = None
        _cache["fetched_at"] = 0.0


def find_match(account_id: int, matches: list | None = None) -> LiveMatch | None:
    """The match this account is currently in, or ``None`` between games.

    ``matches`` is injectable so the parsing can be tested without a network.
    """
    for match in matches if matches is not None else _fetch_active():
        if not isinstance(match, dict):
            continue
        players = [p for p in (match.get("players") or []) if isinstance(p, dict)]
        me = next((p for p in players if p.get("account_id") == account_id), None)
        if me is None:
            continue

        my_team = me.get("team")
        allies: list[int] = []
        enemies: list[int] = []
        for player in players:
            if player is me or player.get("hero_id") is None:
                continue
            side = allies if player.get("team") == my_team else enemies
            side.append(int(player["hero_id"]))

        return LiveMatch(
            match_id=int(match.get("match_id") or 0),
            hero_id=int(me.get("hero_id") or 0),
            allies=allies,
            enemies=enemies,
            players_seen=len(players),
        )
    return None


def parse_account_id(raw: str | None) -> int:
    """Validate the account-id box.

    Steam's 32-bit account ids are positive and comfortably inside 32 bits;
    anything else is a typo, and is rejected before it reaches the lookup.
    """
    raw = (raw or "").strip().replace(" ", "")
    if not raw.isdigit():
        raise ValueError("Enter your numeric Steam account id.")
    account_id = int(raw)
    if not 0 < account_id < 2**32:
        raise ValueError("That is not a valid Steam account id.")
    return account_id
