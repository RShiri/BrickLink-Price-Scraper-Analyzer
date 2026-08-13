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

    def test_parts_inventory_color_from_part_link(self):
        """color_id must come from the part's own link, not the first
        idColor anywhere in the row (counterpart links can differ)."""
        html = """<table><tr>
          <td><img></td><td>7</td>
          <td><a href="/catalogItem.asp?P=3005&idColor=11">3005</a></td>
          <td><a href="catalogList.asp?colorID=99">other</a>
              <a href="/catalogItem.asp?P=3005&idColor=11">Black Brick 1 x 1</a></td>
        </tr></table>"""
        parts = BrickLinkSource().parse_parts_inventory(html)
        assert parts == [{"part_no": "3005", "part_name": "Brick 1 x 1",
                          "color_id": 11, "color_name": "Black", "qty": 7}]

    def test_parts_inventory_color_map_resolves_unknown_colors(self):
        """Colors outside the builtin list resolve through the DB-synced
        color table, un-mangling the part name."""
        html = """<table><tr>
          <td><img></td><td>2</td>
          <td><a href="/catalogItem.asp?P=98138&idColor=227">98138</a></td>
          <td><a href="/catalogItem.asp?P=98138&idColor=227">Glitter Trans-Purple Tile, Round 1 x 1</a></td>
        </tr></table>"""
        src = BrickLinkSource()
        bare = src.parse_parts_inventory(html)[0]
        assert bare["color_name"] is None            # not in the builtin list
        mapped = src.parse_parts_inventory(
            html, color_map={227: "Glitter Trans-Purple"})[0]
        assert mapped["color_name"] == "Glitter Trans-Purple"
        assert mapped["part_name"] == "Tile, Round 1 x 1"

    def test_parts_inventory_qty_from_cell_before_item_no(self):
        """A leading numeric cell (e.g. a match-count column) must not be
        mistaken for the quantity."""
        html = """<table><tr>
          <td>2016</td><td>3</td>
          <td><a href="/catalogItem.asp?P=3001&idColor=5">3001</a></td>
          <td><a href="/catalogItem.asp?P=3001&idColor=5">Reddish Brown Brick 2 x 4</a></td>
        </tr></table>"""
        parts = BrickLinkSource().parse_parts_inventory(html)
        assert parts[0]["qty"] == 3

    def test_minifig_inventory_url_shape(self):
        """Minifig ids carry no -1 sequence suffix; set ids get one."""
        from brickonomy.scrapers.bricklink import inv_ref
        assert inv_ref("76031", "S") == "76031-1"
        assert inv_ref("76031-2", "S") == "76031-2"
        assert inv_ref("sh0167", "M") == "sh0167"

    def test_color_table_parser(self):
        html = """<table>
          <tr><td>Name</td></tr>
          <tr><td><img></td><td>11</td><td>Black</td><td>21,000</td><td>2,417</td><td>1949 - 2026</td></tr>
          <tr><td><img></td><td>227</td><td>Glitter Trans-Purple</td><td>93</td><td>50</td><td>2013 - 2024</td></tr>
        </table>"""
        colors = BrickLinkSource().parse_color_table(html)
        assert colors == {11: "Black", 227: "Glitter Trans-Purple"}

    def test_part_out_value(self):
        pov, ccy = BrickLinkSource().parse_part_out_value(read("bricklink_pov_76031.html"))
        assert pov == 612.34
        assert ccy == "ILS"

    def test_part_out_value_current_layout(self):
        """The live page labels the total 'Average of last 6 months Sales'
        and quotes it in US dollars — the old 'Average Value' regex silently
        returned None for every set."""
        pov, ccy = BrickLinkSource().parse_part_out_value(
            read("bricklink_pov_75192_live.html"))
        assert pov == 1234.66          # sold average, not the 1620.09 asking one
        assert ccy == "USD"


class TestBrickLinkCatalog:
    def test_category_tree_depths(self):
        cats = BrickLinkSource().parse_category_tree(read("bricklink_tree_S.html"))
        by_name = {c["name"]: c for c in cats}
        assert by_name["Star Wars"]["depth"] == 0
        assert by_name["Ultimate Collector Series"]["depth"] == 1
        assert by_name["Death Star"]["depth"] == 2
        assert by_name["Technic"]["depth"] == 0
        assert by_name["Star Wars"]["cat_id"] == 65

    def test_catalog_list_sets_and_pages(self):
        sets, pages = BrickLinkSource().parse_catalog_list(read("bricklink_list_66.html"))
        assert pages == 2
        by_id = {s["item_id"]: s for s in sets}
        assert by_id["75192"]["name"].startswith("Millennium Falcon")
        assert by_id["75192"]["year"] == 2017
        assert by_id["10188"]["year"] == 2008
        assert len(sets) == 3  # -1 suffix stripped, no duplicates

    def test_minifig_inventory_with_quantities(self):
        figs = BrickLinkSource().parse_minifig_inventory(read("bricklink_inv_figs_76031.html"))
        by_id = {f["id"]: f for f in figs}
        assert by_id["sh0173"] == {"id": "sh0173", "name": "Iron Man Mark 43", "qty": 2}
        assert by_id["sh0167"]["qty"] == 1
        assert len(figs) == 3


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

    def test_us_locale_pages_stay_usd(self, result):
        assert result.currency == "USD"


class TestEbayLocaleCurrency:
    """eBay formats prices for the visitor's locale — an Israeli scraper sees
    "ILS 1,357.00", not "$379.99". The page's own symbols must win over the
    USD default, or every local price is recorded ~3.5x too high."""

    ILS_ACTIVE = """<ul>
      <li class="s-item"><div class="s-item__title">LEGO 76178 Daily Bugle NEW SEALED</div>
        <span class="s-item__price">ILS 1,357.00</span></li>
      <li class="s-item"><div class="s-item__title">LEGO 76178 Daily Bugle factory sealed</div>
        <span class="s-item__price">1,420.50 ₪</span></li>
      <li class="s-item"><div class="s-item__title">LEGO 76178 Daily Bugle brand new import</div>
        <span class="s-item__price">$379.99</span></li>
    </ul>"""

    def test_majority_currency_wins_and_stray_rows_drop(self):
        res = EbaySource().parse({"sold": "", "active": self.ILS_ACTIVE}, "76178")
        assert res.currency == "ILS"
        prices = sorted(x["price"] for x in res.new["stock"])
        assert prices == [1357.00, 1420.50]     # the lone $ row is dropped


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


class TestPriceParsing:
    """Prices are formatted for the visitor's locale.

    Requiring a leading currency symbol made both eBay and BrickOwl parse
    nothing on pages full of offers, and the doctor agreed with the broken
    parser because it kept its own copy of the same regex. This is the
    regression that cost the most time in this project; keep it loud.
    """

    CASES = [
        ("$1,234.56", 1234.56, "USD"),
        ("US $12.34", 12.34, "USD"),
        ("ILS 45.60", 45.60, "ILS"),     # ISO code counts as well as a symbol
        ("45.60 ₪", 45.60, "ILS"),
        ("612.00", 612.00, None),        # bare — currency comes from the page
        ("€ 99.90", 99.90, "EUR"),
        ("£8.75", 8.75, "GBP"),
    ]

    def test_formats(self):
        from brickonomy.scrapers.base import find_prices
        for text, amount, ccy in self.CASES:
            found = find_prices(text)
            assert found, f"no price parsed from {text!r}"
            assert found[0][0] == amount, text
            assert found[0][1] == ccy, text

    def test_default_currency_fills_bare_numbers(self):
        from brickonomy.scrapers.base import find_prices
        assert find_prices("612.00", default_ccy="ILS") == [(612.00, "ILS")]

    def test_rejects_non_prices(self):
        from brickonomy.scrapers.base import find_prices
        # Integers without decimals are years, set numbers and piece counts.
        for text in ("2017", "Set 75192", "7541 pieces", "no digits here"):
            assert find_prices(text) == [], text

    def test_doctor_uses_the_same_detector(self):
        """The diagnostic must never be able to contradict the parser."""
        from brickonomy import doctor
        from brickonomy.scrapers.base import PRICE_RE
        import inspect
        assert "PRICE_RE" in inspect.getsource(doctor._prices_seen)
        assert PRICE_RE.search("612.00")
