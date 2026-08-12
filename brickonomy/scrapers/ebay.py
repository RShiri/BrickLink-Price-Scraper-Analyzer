"""eBay source — active Buy-It-Now asking prices.

No API key: scrapes the public search results page
    /sch/i.html?_nkw=lego+<set>&LH_BIN=1                  (active asks)
with Selenium (eBay challenges bare HTTP clients). The parser handles both the
classic `li.s-item` markup and the newer `.s-card` variant eBay A/B tests, and
still understands a sold page when one is supplied from a fixture.

Sold/completed listings used to be fetched here too, but eBay now requires a
signed-in session for them — see `_fetch_html`. eBay is therefore an
asks-only source, and PriceAnalyzer rates it LOW confidence accordingly.

Every listing's title is stored as `description`, so the root PriceAnalyzer
blacklist ('no minifig', 'incomplete', 'box only', …) applies to eBay rows
exactly as it does to BrickLink notes.
"""
import re

from bs4 import BeautifulSoup

from ..models import Listing, ScrapeResult
from .base import BaseScraper, polite_sleep, run_cli

SEARCH_URL = "https://www.ebay.com/sch/i.html"

# Listings that are about the set number but are not a saleable set.
JUNK_RE = re.compile(
    r"instructions?\s*only|manual\s*only|box\s*only|sticker|decal|"
    r"minifig(?:ure)?s?\s*only|figures?\s*only|lot\s+of\s+\d|bulk|"
    r"custom|moc\b|compatible|not\s+lego|lepin|repro|poster|"
    r"empty\s+box|parts?\s*only|incomplete",
    re.IGNORECASE,
)
MULTI_QTY_RE = re.compile(r"\b(?:x\s*[2-9]|[2-9]\s*x|two|three)\b.{0,12}sets?\b|\bsets?\b.{0,12}\bx\s*[2-9]\b",
                          re.IGNORECASE)
NEW_RE = re.compile(r"sealed|nisb|nib\b|misb|bnib|new\s+in\s+(?:sealed\s+)?box|"
                    r"brand\s+new|factory\s+sealed|unopened", re.IGNORECASE)
USED_RE = re.compile(r"\bused\b|pre[- ]?owned|built|complete\s+set|100%\s*complete|"
                     r"assembled|displayed|retired\s+built", re.IGNORECASE)
PRICE_RE = re.compile(r"[\$£€₪]\s*([\d,]+(?:\.\d{1,2})?)")


class EbaySource(BaseScraper):
    source = "ebay"
    currency = "USD"

    # ── fetch ────────────────────────────────────────────────────────────

    def fixture_paths(self, item_id: str):
        from pathlib import Path
        from ..config import get_config
        base = Path(get_config().fixtures_dir)
        return {"sold": base / f"ebay_{item_id}_sold.html",
                "active": base / f"ebay_{item_id}_active.html"}

    def _build_query(self, item_id: str) -> str:
        return f"lego {item_id}"

    def _fetch_html(self, item_id: str, item_type: str = "S"):
        """Active Buy-It-Now listings only.

        eBay closed the sold/completed search to anonymous clients: every
        variant of `LH_Sold=1&LH_Complete=1` now answers with "Error Page",
        "Sign in or Register" or "Security Measure" (verified across five URL
        shapes from a clean IP — see `python -m brickonomy.doctor <set>
        --source ebay-sold`). Asking for it anyway cost a page load and a
        polite delay per item and returned nothing, so the request is gone.

        The consequence is honest rather than hidden: with no sold data,
        PriceAnalyzer anchors eBay on its competitive ask and reports LOW
        confidence, exactly as it does for BrickOwl. BrickLink remains the
        only source with real sale history.
        """
        from .base import make_driver

        url = (f"{SEARCH_URL}?_nkw={self._build_query(item_id).replace(' ', '+')}"
               f"&_ipg=120&LH_BIN=1")
        driver = make_driver()
        try:
            driver.get(url)
            polite_sleep()
            return {"sold": "", "active": driver.page_source}
        finally:
            driver.quit()

    # ── parse ────────────────────────────────────────────────────────────

    def parse(self, htmls, item_id: str) -> ScrapeResult:
        if isinstance(htmls, str):
            htmls = {"sold": htmls, "active": ""}
        res = self.empty_result(item_id)
        res.meta = {"item_id": item_id, "query": self._build_query(item_id)}

        for page_kind, bucket in (("sold", "sold"), ("active", "stock")):
            html = htmls.get(page_kind) or ""
            if not html:
                continue
            for listing, condition in self._parse_cards(html, item_id):
                res.__dict__[condition][bucket].append(listing.as_dict())
        return res

    def _parse_cards(self, html: str, item_id: str):
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.s-item") or soup.select(".s-card") or soup.select("li.brwrvr__item-card")
        for card in cards:
            parsed = self._parse_card(card, item_id)
            if parsed:
                yield parsed

    def _parse_card(self, card, item_id: str):
        title_el = (card.select_one(".s-item__title") or card.select_one(".s-card__title")
                    or card.select_one("[role=heading]"))
        price_el = (card.select_one(".s-item__price") or card.select_one(".s-card__price")
                    or card.select_one(".s-item__detail--primary"))
        if not title_el or not price_el:
            return None
        title = title_el.get_text(" ", strip=True)
        price_text = price_el.get_text(" ", strip=True)

        # eBay pads results with a "Shop on eBay" placeholder card.
        if not title or title.lower().startswith("shop on ebay"):
            return None
        # Relevance: the exact set number token must appear.
        if not re.search(rf"\b{re.escape(item_id)}\b", title):
            return None
        if JUNK_RE.search(title) or MULTI_QTY_RE.search(title):
            return None
        # Skip price ranges ("$x to $y") — ambiguous.
        if re.search(r"\bto\b", price_text, re.IGNORECASE) and price_text.count("$") > 1:
            return None
        m = PRICE_RE.search(price_text.replace(",", ""))
        if not m:
            return None
        price = float(m.group(1))
        if price <= 0:
            return None

        condition = self._classify_condition(card, title)
        if condition is None:
            return None
        return Listing(qty=1, price=price, status="complete", description=title), condition

    @staticmethod
    def _classify_condition(card, title: str):
        if NEW_RE.search(title):
            return "new"
        if USED_RE.search(title):
            return "used"
        badge = (card.select_one(".SECONDARY_INFO")
                 or card.select_one(".s-item__subtitle")
                 or card.select_one(".s-card__subtitle"))
        if badge:
            text = badge.get_text(" ", strip=True).lower()
            if "brand new" in text or text == "new":
                return "new"
            if "pre-owned" in text or "used" in text or "open box" in text:
                return "used"
        return None  # unclassifiable → drop rather than pollute a bucket


if __name__ == "__main__":
    run_cli(EbaySource())
