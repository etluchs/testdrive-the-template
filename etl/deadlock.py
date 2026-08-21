"""Rate-limited read-only client for the public Deadlock API.

This module is part of the **offline** data pipeline, not the app. It is the one
place in this repository that talks to the internet directly, and it lives
outside ``app/`` on purpose: AGENTS.md forbids app code from opening its own
network connections, and the app never calls this module. The pipeline writes a
seed file; the app reads the seeded tables through ``appkit.db``.

Why the app cannot call the API live: ``/v1/sql`` is limited to **20 requests
per hour per IP** (on a rolling window), on top of a 2-per-minute burst cap.
That is fine for a nightly build and unusable for a request path, so every
number the app shows is precomputed here.

Only ``urllib`` from the standard library is used — no new dependency, and
nothing that could be mistaken for a sanctioned app-side HTTP client.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BASE_URL = "https://api.deadlock-api.com"

# /v1/sql enforces two limits at once, and the second is the one that bites:
#   * 2 requests per 60 seconds
#   * 20 requests per 3600 seconds, as a *rolling* window
# Pacing to the burst limit alone gets you ~24 minutes into a build and then
# stalls for the rest of the hour. 3600/20 = 180 s is the real budget; the
# margin on top absorbs clock skew between us and the limiter.
SQL_MIN_INTERVAL_S = 200.0

# Asset endpoints are served from cache and are not part of the SQL budget.
ASSET_MIN_INTERVAL_S = 1.0

USER_AGENT = "uzh-deadlock-item-timing/0.1 (offline research build)"


class DeadlockAPIError(RuntimeError):
    """The API returned something the pipeline cannot use."""


@dataclass
class DeadlockClient:
    """Paced client for the endpoints the pipeline needs.

    The client is deliberately dumb: it fetches, it waits, it retries on 429.
    Query construction lives in :mod:`etl.queries` so the SQL is reviewable in
    one place.
    """

    base_url: str = BASE_URL
    timeout_s: float = 300.0
    # Waits here are legitimate, not failures: a single hourly-quota wait can
    # exceed 20 minutes, and giving up would discard an otherwise fine build.
    max_retries: int = 20
    _last_sql_at: float = field(default=0.0, repr=False)
    _last_asset_at: float = field(default=0.0, repr=False)

    # -- plumbing ---------------------------------------------------------

    def _wait(self, last_at: float, min_interval: float) -> None:
        elapsed = time.monotonic() - last_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
            payload = resp.read()
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DeadlockAPIError(f"{path}: response was not JSON") from exc

    @staticmethod
    def _rate_limited(payload: object) -> int | None:
        """Return the suggested wait if ``payload`` is the limiter's 429 body."""
        if isinstance(payload, dict) and payload.get("status") == 429:
            error = payload.get("error")
            if isinstance(error, dict):
                return int(error.get("next_request_in", 30)) + 2
            return 32
        return None

    # -- endpoints --------------------------------------------------------

    def asset(self, path: str) -> object:
        """Fetch a static asset endpoint (heroes, items, ...)."""
        self._wait(self._last_asset_at, ASSET_MIN_INTERVAL_S)
        try:
            payload = self._get(path)
        finally:
            self._last_asset_at = time.monotonic()
        if isinstance(payload, dict) and "error" in payload:
            raise DeadlockAPIError(f"{path}: {payload['error']}")
        return payload

    def sql(self, query: str) -> list[dict]:
        """Run one ClickHouse query against ``/v1/sql`` and return its rows."""
        collapsed = " ".join(query.split())
        for attempt in range(1, self.max_retries + 1):
            self._wait(self._last_sql_at, SQL_MIN_INTERVAL_S)
            try:
                payload = self._get("/v1/sql", {"query": collapsed})
            except urllib.error.HTTPError as exc:
                self._last_sql_at = time.monotonic()
                if exc.code == 429 and attempt < self.max_retries:
                    log.warning("429 from /v1/sql, backing off (attempt %s)", attempt)
                    time.sleep(SQL_MIN_INTERVAL_S)
                    continue
                raise DeadlockAPIError(f"/v1/sql failed with HTTP {exc.code}") from exc
            except (urllib.error.URLError, OSError) as exc:
                # A socket timeout or reset is not a failed build. Over ~34
                # paced requests spanning two hours, one dropped connection is
                # likely, and it used to abort the whole run.
                self._last_sql_at = time.monotonic()
                if attempt < self.max_retries:
                    log.warning(
                        "network error on /v1/sql (%s), retrying (attempt %s)", exc, attempt
                    )
                    continue
                raise DeadlockAPIError(f"/v1/sql: network error: {exc}") from exc
            self._last_sql_at = time.monotonic()

            wait_s = self._rate_limited(payload)
            if wait_s is not None:
                if attempt == self.max_retries:
                    raise DeadlockAPIError("/v1/sql: still rate limited after retries")
                log.warning("rate limited, waiting %ss (attempt %s)", wait_s, attempt)
                time.sleep(wait_s)
                continue

            if isinstance(payload, dict) and "error" in payload:
                raise DeadlockAPIError(f"/v1/sql: {payload['error']}")
            if not isinstance(payload, list):
                raise DeadlockAPIError("/v1/sql: expected a list of rows")
            return payload

        raise DeadlockAPIError("/v1/sql: exhausted retries")

    # -- convenience ------------------------------------------------------

    def playable_heroes(self) -> dict[int, str]:
        """Hero id -> display name, excluding heroes still in development."""
        rows = self.asset("/v1/assets/heroes")
        if not isinstance(rows, list):
            raise DeadlockAPIError("/v1/assets/heroes: expected a list")
        return {
            int(h["id"]): str(h["name"])
            for h in rows
            if isinstance(h, dict) and not h.get("in_development")
        }

    @staticmethod
    def _is_brawl_only(item: dict) -> bool:
        """True for items that only exist in Street Brawl.

        Detected by the asset path: brawl items serve their artwork from
        ``.../images/items/brawl/...``. Two other signals agree exactly — they
        are the only ``item_tier`` 5 items, and the only ones priced 9999, which
        is a sentinel rather than a real shop cost. The path is used as the rule
        because it says what the item *is*; tier and cost are checked against it
        in :meth:`buyable_items` so that if the game ever ships a real tier 5,
        the divergence is reported instead of silently dropping those items.

        Note that four ordinary shop items (Cursed Relic, Alchemical Fire,
        Greater/Mystic Expansion) merely mention Street Brawl in their tooltip
        text. They are kept.
        """
        return any(
            "/brawl/" in str(item.get(key) or "")
            for key in ("shop_image", "shop_image_webp", "image", "image_webp")
        )

    def buyable_items(self) -> dict[int, dict]:
        """Item id -> ``{name, tier, cost}`` for items in the current shop.

        Three filters, all of which change what the app may recommend:

        * **Removed items.** The catalogue is historical: of 251 upgrades, 78
          have been taken out of the game and survive only so old matches can be
          decoded. They carry ``disabled: true`` and ``shopable: false``, and
          several never had a display name (``upgrade_clip_size_fixed``).
        * **Brawl-only items.** 17 more exist only in Street Brawl, a different
          mode from the one these statistics describe.
        * Everything else is kept — including **components**. A component such
          as Healing Booster is an ordinary shopable tier-2 item that also
          happens to build into Healing Tempo, and it is bought and held on its
          own, so it belongs in the list on its own.

        That leaves 156 items across tiers 1-4.
        """
        rows = self.asset("/v1/assets/items")
        if not isinstance(rows, list):
            raise DeadlockAPIError("/v1/assets/items: expected a list")

        upgrades = [
            i for i in rows
            if isinstance(i, dict)
            and i.get("type") == "upgrade"
            and i.get("name")
            and i.get("shopable")
            and not i.get("disabled")
        ]

        brawl = {int(i["id"]) for i in upgrades if self._is_brawl_only(i)}
        tier5 = {int(i["id"]) for i in upgrades if i.get("item_tier") == 5}
        if brawl != tier5:
            log.warning(
                "brawl detection disagrees with tier 5 (%s by asset path, %s by tier); "
                "the shop may have changed — check etl/deadlock.py::_is_brawl_only",
                len(brawl), len(tier5),
            )

        return {
            int(i["id"]): {
                "name": str(i["name"]),
                "tier": int(i.get("item_tier") or 0),
                "cost": int(i.get("cost") or 0),
                # weapon / vitality / spirit. Deadlock gives four slots of each,
                # which is the hard constraint on any build.
                "slot": str(i.get("item_slot_type") or ""),
            }
            for i in upgrades
            if int(i["id"]) not in brawl
        }

    def all_upgrade_ids(self) -> set[int]:
        """Every upgrade the catalogue knows, live or removed.

        Used only to report how many items the live filter dropped.
        """
        rows = self.asset("/v1/assets/items")
        if not isinstance(rows, list):
            raise DeadlockAPIError("/v1/assets/items: expected a list")
        return {int(i["id"]) for i in rows if isinstance(i, dict) and i.get("type") == "upgrade"}

    def recent_patches(self, limit: int = 10) -> list[dict]:
        """The most recent patches, newest first, as ``{date, title}``.

        Deadlock does not publish semantic version numbers — updates are
        identified by date and headline ("Minor Update - 08-12-2026"), and the
        only numeric identifiers are opaque client build ids. So an era is named
        by the day it started and the title of the patch that started it.

        These are *all* patches, not only the major ones, because consecutive
        releases are what a player wants to compare.
        """
        rows = self.asset("/v2/patches")
        if not isinstance(rows, list):
            raise DeadlockAPIError("/v2/patches: expected a list")
        seen: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            published = str(row.get("pub_date") or "")[:10]
            title = " ".join(str(row.get("title") or "").split())
            if len(published) != 10 or not title:
                continue
            # Steam and the forum both carry the same update; keep one per day.
            seen.setdefault(published, title)
        newest = sorted(seen.items(), reverse=True)[:limit]
        return [{"date": d, "title": t} for d, t in newest]

    def current_patch_start(self) -> str:
        """When the balance era the game is currently in began, as ``YYYY-MM-DD``.

        ``/v1/patches/big-days`` lists the major patch days — the ones that
        reworked items and heroes — as opposed to the frequent minor updates
        that tweak a single hero. Anchoring the extract here is what makes
        "the current patch" mean something: statistics from before the last
        major patch describe a different game.
        """
        days = self.asset("/v1/patches/big-days")
        if not isinstance(days, list) or not days:
            raise DeadlockAPIError("/v1/patches/big-days: expected a non-empty list")
        newest = max(str(d) for d in days)
        return newest[:10]
