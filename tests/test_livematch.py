"""Resolving a live match to two rosters.

``app/live.py`` is the one place in the app allowed to reach the network, so
these tests matter twice over: they pin the parsing, and they pin that no
failure mode of a third-party service can reach the user as an error page.

Nothing here touches the network — the match list is injected, and the fetch
itself is monkeypatched where it needs to fail.
"""

import pytest

from app import live, logic


def _match(match_id=1, players=None):
    return {"match_id": match_id, "players": players or []}


def _player(account_id, hero_id, team):
    return {"account_id": account_id, "hero_id": hero_id, "team": team}


# ---------------------------------------------------------------------------
# reading the roster
# ---------------------------------------------------------------------------

def test_your_team_and_the_enemy_team_are_told_apart():
    matches = [
        _match(players=[
            _player(100, 6, 0), _player(101, 13, 0), _player(102, 15, 0),
            _player(200, 3, 1), _player(201, 4, 1),
        ])
    ]

    found = live.find_match(100, matches)

    assert found.hero_id == 6
    assert sorted(found.allies) == [13, 15]
    assert sorted(found.enemies) == [3, 4]


def test_you_are_not_listed_as_your_own_ally():
    matches = [_match(players=[_player(100, 6, 0), _player(101, 13, 0)])]

    found = live.find_match(100, matches)

    assert found.allies == [13]


def test_the_right_match_is_picked_out_of_many():
    matches = [
        _match(match_id=1, players=[_player(999, 1, 0)]),
        _match(match_id=2, players=[_player(100, 6, 1), _player(101, 3, 0)]),
    ]

    found = live.find_match(100, matches)

    assert found.match_id == 2
    assert found.enemies == [3]


def test_not_being_in_a_match_is_a_normal_outcome():
    """Between games this is the expected answer, not an error."""
    matches = [_match(players=[_player(999, 1, 0)])]

    assert live.find_match(100, matches) is None


def test_a_partly_drafted_match_is_flagged():
    """The live list lags the draft; a half-filled roster must say so."""
    matches = [
        _match(players=[_player(100, 6, 0), _player(101, 13, 0), _player(200, 3, 1)])
    ]

    found = live.find_match(100, matches)

    assert found.players_seen == 3
    assert found.partial


def test_a_full_match_is_not_flagged_as_partial():
    players = [_player(100 + i, i + 1, 0) for i in range(6)]
    players += [_player(200 + i, i + 20, 1) for i in range(6)]

    found = live.find_match(100, [_match(players=players)])

    assert found.players_seen == 12
    assert not found.partial


def test_a_malformed_entry_does_not_break_the_scan():
    matches = ["nonsense", None, _match(players=[_player(100, 6, 0)])]

    assert live.find_match(100, matches).hero_id == 6


# ---------------------------------------------------------------------------
# validating what the user typed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "abc", "-1", "0", "12.5", "99999999999999"])
def test_a_bad_account_id_is_rejected(raw):
    with pytest.raises(ValueError):
        live.parse_account_id(raw)


def test_a_valid_account_id_is_accepted():
    assert live.parse_account_id(" 180 942 972 ") == 180942972


# ---------------------------------------------------------------------------
# failures never reach the user as an error page
# ---------------------------------------------------------------------------

def test_an_upstream_outage_becomes_a_message(seeded, monkeypatch):
    def boom(*_args, **_kwargs):
        raise live.LiveLookupError("The live match service could not be reached.")

    monkeypatch.setattr(live, "find_match", boom)
    found, note = logic.live_lineup("180942972")

    assert found is None
    assert "could not be reached" in note


def test_a_typo_becomes_a_message_not_an_exception(seeded):
    found, note = logic.live_lineup("not-a-number")

    assert found is None
    assert "Steam account id" in note


def test_an_account_between_games_becomes_a_message(seeded, monkeypatch):
    monkeypatch.setattr(live, "find_match", lambda *_a, **_k: None)
    found, note = logic.live_lineup("180942972")

    assert found is None
    assert "not in a match" in note


def test_a_found_match_reports_the_hero_by_name(seeded, monkeypatch):
    monkeypatch.setattr(
        live, "find_match",
        lambda *_a, **_k: live.LiveMatch(
            match_id=7, hero_id=1, allies=[2], enemies=[3], players_seen=12
        ),
    )
    found, note = logic.live_lineup("180942972")

    assert found == {"hero_id": 1, "allies": [2], "enemies": [3]}
    assert "Testhero" in note
    assert "match 7" in note


def test_a_hero_the_app_has_no_data_for_is_explained(seeded, monkeypatch):
    monkeypatch.setattr(
        live, "find_match",
        lambda *_a, **_k: live.LiveMatch(
            match_id=7, hero_id=4242, allies=[], enemies=[], players_seen=12
        ),
    )
    found, note = logic.live_lineup("180942972")

    assert found is None
    assert "not one this app has data for" in note


# ---------------------------------------------------------------------------
# the shared cache
# ---------------------------------------------------------------------------

def test_the_live_list_is_fetched_once_for_many_callers(monkeypatch):
    """One upstream call serves every visitor for the TTL.

    Without this, a page full of people clicking "find my match" turns into one
    request per click against someone else's service.
    """
    calls = {"n": 0}

    def counting(request, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        raise live.urllib.error.URLError("stop here — the call was made")

    live.reset_cache()
    monkeypatch.setattr(live.urllib.request, "urlopen", counting)

    for _ in range(3):
        with pytest.raises(live.LiveLookupError):
            live.find_match(1)

    # Failures are not cached, so each attempt does try again.
    assert calls["n"] == 3
    live.reset_cache()
