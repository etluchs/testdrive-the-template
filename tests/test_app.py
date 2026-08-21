"""Route and rendering tests, running against the synthetic extract."""


def test_index_offers_the_pickers(client):
    body = client.get("/").text

    assert "When should I buy it?" in body
    assert 'name="hero_id"' in body
    assert 'name="item_id"' in body
    assert 'name="enemy"' in body
    assert "Testhero" in body
    # The form opens on the cheapest tier, so that tier's item is the one shown.
    assert "Filler Item" in body


def test_index_uses_accessible_form_markup(client):
    body = client.get("/").text

    assert '<html lang="en">' in body
    assert 'class="skip-link"' in body
    assert '<label class="field__label" for="hero_id">' in body
    assert '<label class="field__label" for="item_id">' in body
    assert "<legend>Enemy team</legend>" in body


def test_index_nav_marks_current_page(client):
    assert 'aria-current="page"' in client.get("/").text


def test_items_partial_offers_the_tier_then_the_items(client):
    body = client.get("/items", params={"hero_id": 1}).text

    assert 'name="item_tier"' in body
    for tier in ("Tier 1", "Tier 2", "Tier 4"):
        assert tier in body
    # Opens on the cheapest tier with data, so only that tier's item shows.
    assert "Filler Item" in body
    assert "Confounded Item" not in body


def test_items_partial_follows_the_chosen_tier(client):
    body = client.get("/items", params={"hero_id": 1, "item_tier": 4}).text

    assert "Confounded Item" in body
    assert "Genuine Item" not in body


def test_items_partial_handles_a_hero_with_no_data(client):
    empty = client.get("/items", params={"hero_id": 2}).text

    assert "no data for this tier" in empty


def test_a_tier_the_hero_lacks_falls_back_instead_of_erroring(client):
    """Changing hero can leave a stale tier selected; that must not 500."""
    body = client.get("/items", params={"hero_id": 1, "item_tier": 3}).text

    assert "Filler Item" in body   # fell back to the cheapest tier


def test_timing_reports_a_window(client):
    resp = client.post("/timing", data={"hero_id": 1, "item_id": 20})

    assert resp.status_code == 200
    assert "minute 20" in resp.text
    assert "Genuine Item" in resp.text


def test_timing_refuses_to_recommend_a_confounded_item(client):
    resp = client.post("/timing", data={"hero_id": 1, "item_id": 10})

    assert resp.status_code == 200
    assert "distinguishable" in resp.text


def test_timing_renders_chart_and_table(client):
    body = client.post("/timing", data={"hero_id": 1, "item_id": 20}).text

    # The chart is never the only carrier of the numbers.
    assert 'role="img"' in body
    assert "<title id=\"chart-title\">" in body
    assert '<th scope="col">' in body
    assert '<th scope="row">' in body
    assert "<caption" in body


def test_timing_accepts_an_enemy_team(client):
    resp = client.post("/timing", data={"hero_id": 1, "item_id": 20, "enemy": ["2", ""]})

    assert resp.status_code == 200
    assert "Enemyhero" in resp.text


def test_timing_rejects_an_unknown_enemy(client):
    resp = client.post("/timing", data={"hero_id": 1, "item_id": 20, "enemy": ["9999"]})

    assert resp.status_code == 400
    assert 'role="alert"' in resp.text


def test_method_page_states_the_caveats(client):
    # The template wraps prose across lines, so compare on flattened whitespace.
    flat = " ".join(client.get("/method").text.split())

    assert "reference population" in flat
    assert "not a randomised trial" in flat
    assert "1.8 trillion" in flat


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_index_offers_the_patch_pickers(client):
    body = client.get("/").text

    assert 'name="patch_before"' in body
    assert 'name="patch_after"' in body
    assert "Newer Update" in body


def test_timing_compares_two_patches(client):
    resp = client.post(
        "/timing",
        data={"hero_id": 1, "item_id": 20, "patch_before": "0", "patch_after": "1"},
    )

    assert resp.status_code == 200
    flat = " ".join(resp.text.split())
    assert "does worse in 2026-06-01 than in 2026-05-01" in flat
    assert "compared between two patches" in flat
    # The different reference is stated wherever the comparison is shown.
    assert "other purchases" in flat


def test_timing_skips_the_comparison_when_no_patches_are_chosen(client):
    body = client.post("/timing", data={"hero_id": 1, "item_id": 20}).text

    assert "compared between two patches" not in body


def test_timing_rejects_an_unknown_patch(client):
    resp = client.post(
        "/timing",
        data={"hero_id": 1, "item_id": 20, "patch_before": "0", "patch_after": "99"},
    )

    assert resp.status_code == 400
    assert 'role="alert"' in resp.text


def test_timing_renders_the_winrate_over_time_chart(client):
    body = client.post("/timing", data={"hero_id": 1, "item_id": 20}).text

    assert 'id="wr-title"' in body          # the win-rate chart
    assert 'id="chart-title"' in body       # and the effect chart alongside it
    assert body.count("<polyline") == 2     # buyers and matched reference
    # Neither series may rely on colour alone.
    assert "chart__key--buy" in body
    assert "chart__key--ref" in body


def test_the_winrate_chart_is_never_shown_without_its_reference(client):
    """A lone win-rate line would read as an endorsement of buying late."""
    flat = " ".join(client.post("/timing", data={"hero_id": 1, "item_id": 10}).text.split())

    assert "comparable players" in flat.lower()
    assert "Comparable players" in flat      # the table column
    assert "gap</em> between the lines" in flat or "gap" in flat
