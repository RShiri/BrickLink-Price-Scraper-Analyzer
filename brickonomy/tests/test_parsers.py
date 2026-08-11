"""Fixture-based parser tests — run fully offline."""
from pathlib import Path

import pytest

from brickonomy.compat import PriceAnalyzer
from brickonomy.scrapers.bricklink import BrickLinkSource
from brickonomy.scrapers.brickowl import BrickOwlSource
from brickonomy.scrapers.ebay import EbaySource

FIXTURES = Path(__file__).parent / "fixtures"


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# ── BrickLink (real saved page) ──────────────────────────────────────────

class TestBrickLinkParser:
    def test_parses_all_four_price_tables(self):
        res = BrickLinkSource().parse(read("bricklink_76031.html"), "76031")
        assert res.error is None
        assert res.new["sold"], "new-sold table should have rows"
        assert res.used["sold"], "used-sold table should have rows"
        assert res.new["stock"], "new-stock table should have rows"
        assert res.used["stock"], "used-stock table should have rows"

    def test_meta_extracted(self):
        res = BrickLinkSource().parse(read("bricklink_76031.html"), "76031")
        assert "Hulk Buster" in res.meta["item_name"]
        assert res.meta["year_released"] == 2015

    def test_rows_have_expected_shape(self):
        res = BrickLinkSource().parse(read("bricklink_76031.html"), "76031")
        row = res.new["sold"][0]
        assert set(row) >= {"qty", "price", "status"}
        assert row["price"] > 0 and row["qty"] >= 1

    def test_feeds_price_analyzer(self):
        res = BrickLinkSource().parse(read("bricklink_76031.html"), "76031")
        analysis = PriceAnalyzer(res.analyzer_input()).analyze()
        assert analysis["new"]["market_price"] > 0
        assert analysis["new"]["confidence"] in ("HIGH", "MEDIUM", "LOW")


class TestBrickLinkPartsAndPOV:
    def test_parts_inventory(self):
        parts = BrickLinkSource().parse_parts_inventory(read("bricklink_inv_parts_76031.html"))
        assert len(parts) == 3
        by_no = {p["part_no"]: p for p in parts}
        assert by_no["3001"] == {"part_no": "3001", "part_name": "Brick 2 x 4",
                                 "color_id": 5, "color_name": "Reddish Brown", "qty": 4}
        assert by_no["3023"]["color_name"] == "Light Bluish Gray"
        assert by_no["2412b"]["qty"] == 2

    def test_part_out_value(self):
        pov = BrickLinkSource().parse_part_out_value(read("bricklink_pov_76031.html"))
        assert pov == 612.34


# ── eBay ─────────────────────────────────────────────────────────────────

class TestEbayParser:
    @pytest.fixture()
    def result(self):
        htmls = {"sold": read("ebay_75192_sold.html"),
                 "active": read("ebay_75192_active.html")}
        return EbaySource().parse(htmls, "75192")

    def test_sold_new_listings(self, result):
        prices = sorted(x["price"] for x in result.new["sold"])
        assert prices == [689.99, 702.00]

    def test_sold_used_listings(self, result):
        prices = sorted(x["price"] for x in result.used["sold"])
        assert prices == [449.00, 455.50, 470.00]

    def test_active_listings_go_to_stock(self, result):
        assert sorted(x["price"] for x in result.new["stock"]) == [749.95, 779.00]
        assert [x["price"] for x in result.used["stock"]] == [525.00]

    def test_junk_filtered(self, result):
        all_rows = (result.new["sold"] + result.used["sold"]
                    + result.new["stock"] + result.used["stock"])
        texts = " ".join(r["description"].lower() for r in all_rows)
        for junk in ("instructions", "sticker", "compatible", "minifigures only",
                     "incomplete", "x2 sets", "75257"):
            assert junk not in texts

    def test_price_ranges_and_placeholders_dropped(self, result):
        all_prices = [r["price"] for r in
                      result.new["sold"] + result.used["sold"]
                      + result.new["stock"] + result.used["stock"]]
        assert 20.00 not in all_prices   # "Shop on eBay" placeholder
        assert 50.00 not in all_prices   # range listing
        assert 500.00 not in all_prices  # unclassifiable condition

    def test_titles_flow_into_description_for_blacklist(self, result):
        assert all(r["description"] for r in result.new["sold"])


# ── BrickOwl ─────────────────────────────────────────────────────────────

class TestBrickOwlParser:
    @pytest.fixture()
    def result(self):
        return BrickOwlSource().parse(read("brickowl_75192.html"), "75192")

    def test_new_asks(self, result):
        prices = sorted(x["price"] for x in result.new["stock"])
        assert prices == [645.00, 672.50, 699.99]

    def test_used_asks(self, result):
        prices = sorted(x["price"] for x in result.used["stock"])
        assert prices == [450.00, 475.25]

    def test_no_sold_data(self, result):
        assert result.new["sold"] == [] and result.used["sold"] == []

    def test_currency_detected_from_symbol(self, result):
        assert result.currency == "GBP"

    def test_quantities_parsed(self, result):
        by_price = {x["price"]: x["qty"] for x in result.new["stock"] + result.used["stock"]}
        assert by_price[645.00] == 2
        assert by_price[475.25] == 3

    def test_meta(self, result):
        assert result.meta["specs"]["parts"] == 7541
        assert result.meta["specs"]["minifigs"] == 8

    def test_asks_only_yields_low_confidence(self, result):
        analysis = PriceAnalyzer(result.analyzer_input()).analyze()
        assert analysis["new"]["confidence"] == "LOW"
        assert analysis["new"]["market_price"] > 0
