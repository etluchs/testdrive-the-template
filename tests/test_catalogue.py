"""Catalogue filtering: which items reach the app at all."""

from etl.deadlock import DeadlockClient


def test_brawl_items_are_detected_by_asset_path():
    brawl = {"shop_image": "https://x/images/items/brawl/omnicharge_pendant.png"}
    normal = {"shop_image": "https://x/images/items/tech/rapid_recharge.webp"}

    assert DeadlockClient._is_brawl_only(brawl)
    assert not DeadlockClient._is_brawl_only(normal)


def test_an_item_with_no_artwork_is_not_assumed_to_be_brawl():
    assert not DeadlockClient._is_brawl_only({})


def test_mentioning_brawl_in_text_is_not_enough():
    """Cursed Relic and friends describe their Street Brawl behaviour but are
    ordinary shop items."""
    item = {"description": "In Street Brawl this does something else.",
            "shop_image": "https://x/images/items/spirit/cursed_relic.png"}

    assert not DeadlockClient._is_brawl_only(item)
